import collections
import pathlib
import pickle
import sqlite3
from dataclasses import dataclass, field
from typing import (
    Any,
    Callable,
    Dict,
    Generic,
    Iterator,
    List,
    MutableMapping,
    Optional,
    OrderedDict,
    Sequence,
    Tuple,
    TypeVar,
    Union,
)

_DEFAULT_TABLE_NAME = "data"
_DEFAULT_MEMORY_CACHE_MAX_SIZE = 2000
_DEFAULT_MEMORY_CACHE_EVICTION_BATCH_SIZE = 200

# https://docs.python.org/3/library/sqlite3.html#sqlite-and-python-types
SqliteValue = Union[int, float, str, bytes, None]

_VT = TypeVar("_VT")


@dataclass
class _SqliteConnectionCache:
    """
    If you pass the same filename to multiple FileBacked* objects, they will
    share the same underlying database connection. This also does ref counting
    to drop the connection when appropriate.

    It's necessary to use the same underlying connection because we're using
    exclusive locking mode. It's useful to keep data from multiple FileBacked*
    objects in the same SQLite database because it allows us to perform queries
    across multiple tables.

    This is used as a singleton class.
    """

    _ref_count: Dict[pathlib.Path, int] = field(default_factory=dict)
    _sqlite_connection_cache: Dict[pathlib.Path, sqlite3.Connection] = field(
        default_factory=dict
    )

    def get_connection(self, filename: pathlib.Path) -> sqlite3.Connection:
        if filename not in self._ref_count:
            conn = sqlite3.connect(filename, isolation_level=None)

            # These settings are optimized for performance.
            # See https://www.sqlite.org/pragma.html for more information.
            # Because we're only using these dbs to offload data from memory, we don't need
            # to worry about data integrity too much.
            conn.execute('PRAGMA locking_mode = "EXCLUSIVE"')
            conn.execute('PRAGMA synchronous = "OFF"')
            conn.execute('PRAGMA journal_mode = "MEMORY"')
            conn.execute(f"PRAGMA journal_size_limit = {100 * 1024 * 1024}")  # 100MB

            self._ref_count[filename] = 0
            self._sqlite_connection_cache[filename] = conn

        self._ref_count[filename] += 1
        return self._sqlite_connection_cache[filename]

    def drop_connection(self, filename: pathlib.Path) -> None:
        self._ref_count[filename] -= 1

        if self._ref_count[filename] == 0:
            # Cleanup the connection object.
            self._sqlite_connection_cache[filename].close()
            del self._sqlite_connection_cache[filename]
            del self._ref_count[filename]


_sqlite_connection_cache = _SqliteConnectionCache()


# DESIGN: Why is pickle the default serializer/deserializer?
#
# Benefits:
# (1) In my comparisons of pickle vs manually generating a Python object
#     and then calling json.dumps on it, pickle was consistently slightly faster
#     for both reads and writes.
# (2) The interface is simpler - you don't have to write a custom serializer.
#     This is especially useful when dealing with non-standard types like
#     collections.Counter or datetime.
# (3) Pickle is built-in to Python and requires no additional dependencies.
#     It's true that we might be able to eek out a bit more performance by
#     using a faster serializer like msgpack or cbor.
#
# Downsides:
# (1) The serialized data is not human-readable.
# (2) For simple types like ints, it has slightly worse performance.
#
# Overall, pickle seems like the right default choice.


def _default_serializer(value: Any) -> SqliteValue:
    return pickle.dumps(value)


def _default_deserializer(value: Any) -> Any:
    return pickle.loads(value)


@dataclass(eq=False)
class FileBackedDict(MutableMapping[str, _VT], Generic[_VT]):
    """
    A dict-like object that stores its data in a temporary SQLite database.
    This is useful for storing large amounts of data that don't fit in memory.

    This class is not thread-safe.
    """

    filename: pathlib.Path
    tablename: str = field(default=_DEFAULT_TABLE_NAME)

    serializer: Callable[[_VT], SqliteValue] = field(default=_default_serializer)
    deserializer: Callable[[Any], _VT] = field(default=_default_deserializer)
    extra_columns: Dict[str, Callable[[_VT], SqliteValue]] = field(default_factory=dict)

    cache_max_size: int = field(default=_DEFAULT_MEMORY_CACHE_MAX_SIZE)
    cache_eviction_batch_size: int = field(
        default=_DEFAULT_MEMORY_CACHE_EVICTION_BATCH_SIZE
    )

    _conn: sqlite3.Connection = field(init=False, repr=False)

    # To improve performance, we maintain an in-memory LRU cache using an OrderedDict.
    _active_object_cache: OrderedDict[str, _VT] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        assert (
            self.cache_eviction_batch_size > 0
        ), "cache_eviction_batch_size must be positive"

        assert "key" not in self.extra_columns, '"key" is a reserved column name'
        assert "value" not in self.extra_columns, '"value" is a reserved column name'

        self._conn = _sqlite_connection_cache.get_connection(self.filename)

        # We keep a small cache in memory to avoid having to serialize/deserialize
        # data from the database too often. We use an OrderedDict to build
        # a poor-man's LRU cache.
        self._active_object_cache = collections.OrderedDict()

        # Create the table. We're not using "IF NOT EXISTS" because creating
        # the same table twice indicates a client usage error.
        self._conn.execute(
            f"""CREATE TABLE {self.tablename} (
                key TEXT PRIMARY KEY,
                value BLOB
                {''.join(f', {column_name} BLOB' for column_name in self.extra_columns.keys())}
            )"""
        )

        # The key column will automatically be indexed, but we need indexes
        # for the extra columns.
        for column_name in self.extra_columns.keys():
            self._conn.execute(
                f"CREATE INDEX {self.tablename}_{column_name} ON {self.tablename} ({column_name})"
            )

    def _add_to_cache(self, key: str, value: _VT) -> None:
        self._active_object_cache[key] = value

        if len(self._active_object_cache) > self.cache_max_size:
            # Try to prune in batches rather than one at a time.
            num_items_to_prune = min(
                len(self._active_object_cache), self.cache_eviction_batch_size
            )
            self._prune_cache(num_items_to_prune)

    def _prune_cache(self, num_items_to_prune: int) -> None:
        items_to_write: List[Tuple[SqliteValue, ...]] = []
        for _ in range(num_items_to_prune):
            key, value = self._active_object_cache.popitem(last=False)

            values = [key, self.serializer(value)]
            for column_serializer in self.extra_columns.values():
                values.append(column_serializer(value))
            items_to_write.append(tuple(values))

        self._conn.executemany(
            f"""INSERT OR REPLACE INTO {self.tablename} (
                key,
                value
                {''.join(f', {column_name}' for column_name in self.extra_columns.keys())}
            )
            VALUES ({', '.join(['?'] *(2 + len(self.extra_columns)))})""",
            items_to_write,
        )

    def flush(self) -> None:
        self._prune_cache(len(self._active_object_cache))

    def __getitem__(self, key: str) -> _VT:
        if key in self._active_object_cache:
            self._active_object_cache.move_to_end(key)
            return self._active_object_cache[key]

        cursor = self._conn.execute(
            f"SELECT value FROM {self.tablename} WHERE key = ?", (key,)
        )
        result: Sequence[SqliteValue] = cursor.fetchone()
        if result is None:
            raise KeyError(key)

        deserialized_result = self.deserializer(result[0])
        self._add_to_cache(key, deserialized_result)
        return deserialized_result

    def __setitem__(self, key: str, value: _VT) -> None:
        self._add_to_cache(key, value)

    def __delitem__(self, key: str) -> None:
        in_cache = False
        if key in self._active_object_cache:
            del self._active_object_cache[key]
            in_cache = True

        n_deleted = self._conn.execute(
            f"DELETE FROM {self.tablename} WHERE key = ?", (key,)
        ).rowcount
        if not in_cache and not n_deleted:
            raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        cursor = self._conn.execute(f"SELECT key FROM {self.tablename}")
        for row in cursor:
            if row[0] in self._active_object_cache:
                # If the key is in the active object cache, then SQL isn't the source of truth.
                continue

            yield row[0]

        for key in self._active_object_cache:
            yield key

    def __len__(self) -> int:
        cursor = self._conn.execute(
            # Binding a list of values in SQLite: https://stackoverflow.com/a/1310001/5004662.
            f"SELECT COUNT(*) FROM {self.tablename} WHERE key NOT IN ({','.join('?' * len(self._active_object_cache))})",
            (*self._active_object_cache.keys(),),
        )
        row = cursor.fetchone()

        return row[0] + len(self._active_object_cache)

    def sql_query(
        self,
        query: str,
        params: Tuple[Any, ...] = (),
        refs: Optional[List[Union["FileBackedList", "FileBackedDict"]]] = None,
    ) -> List[Tuple[Any, ...]]:
        # We need to flush object and any objects the query references to ensure
        # that we don't miss objects that have been modified but not yet flushed.
        self.flush()
        if refs is not None:
            for referenced_table in refs:
                referenced_table.flush()

        cursor = self._conn.execute(query, params)
        return cursor.fetchall()

    def close(self) -> None:
        if self._conn:
            # Ensure everything is written out.
            self.flush()

            # Make sure that we don't try to use the connection anymore
            # and that we don't drop the connection twice.
            _sqlite_connection_cache.drop_connection(self.filename)
            self._conn = None  # type: ignore

            # This forces all writes to go directly to the DB so they fail immediately.
            self.cache_max_size = 0

    def __del__(self) -> None:
        self.close()


class FileBackedList(Generic[_VT]):
    """
    An append-only, list-like object that stores its contents in a SQLite database.

    This class is not thread-safe.
    """

    _len: int = field(default=0)
    _dict: FileBackedDict[_VT] = field(init=False)

    def __init__(
        self,
        filename: pathlib.Path,
        tablename: str = _DEFAULT_TABLE_NAME,
        serializer: Callable[[_VT], SqliteValue] = _default_serializer,
        deserializer: Callable[[Any], _VT] = _default_deserializer,
        extra_columns: Optional[Dict[str, Callable[[_VT], SqliteValue]]] = None,
        cache_max_size: Optional[int] = None,
        cache_eviction_batch_size: Optional[int] = None,
    ) -> None:
        self._len = 0
        self._dict = FileBackedDict(
            filename=filename,
            serializer=serializer,
            deserializer=deserializer,
            tablename=tablename,
            extra_columns=extra_columns or {},
            cache_max_size=cache_max_size or _DEFAULT_MEMORY_CACHE_MAX_SIZE,
            cache_eviction_batch_size=cache_eviction_batch_size
            or _DEFAULT_MEMORY_CACHE_EVICTION_BATCH_SIZE,
        )

    @property
    def tablename(self) -> str:
        return self._dict.tablename

    def __getitem__(self, index: int) -> _VT:
        if index < 0 or index >= self._len:
            raise IndexError(f"list index {index} out of range")

        return self._dict[str(index)]

    def __setitem__(self, index: int, value: _VT) -> None:
        if index < 0 or index >= self._len:
            raise IndexError(f"list index {index} out of range")

        self._dict[str(index)] = value

    def append(self, value: _VT) -> None:
        self._dict[str(self._len)] = value
        self._len += 1

    def __len__(self) -> int:
        return self._len

    def __iter__(self) -> Iterator[_VT]:
        for index in range(self._len):
            yield self[index]

    def flush(self) -> None:
        self._dict.flush()

    def sql_query(
        self,
        query: str,
        params: Tuple[Any, ...] = (),
        refs: Optional[List[Union["FileBackedList", "FileBackedDict"]]] = None,
    ) -> List[Tuple[Any, ...]]:
        return self._dict.sql_query(query, params, refs=refs)

    def close(self) -> None:
        self._dict.close()

    def __del__(self) -> None:
        self.close()
