"""Persistent SQLite backend that implements all Genie storage capabilities."""

from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict, deque
from collections.abc import Generator, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from threading import RLock
from time import monotonic, sleep
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self

from genie.memory.block import MemoryBlock
from genie.memory.errors import (
    BackendBusy,
    BackendCapacityExceeded,
    BackendCorruption,
    BackendError,
    BackendReadOnly,
    BackendUnavailable,
    MemoryConflict,
    UnsupportedOperation,
)
from genie.memory.links import MemoryLink
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.query.filters import matches_filters
from genie.query.paging import page_results
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult
from genie.query.scope import Scope

if TYPE_CHECKING:
    from genie.backend.composite.routed import RouteInfo
    from genie.structures.records import (
        KeyBinding,
        LogEntry,
        MemoryAddress,
        TemporalEntry,
        ValidityInterval,
    )


_SCHEMA_VERSION = 1
_WAL_RETRY_SECONDS = 0.005
_APPLICATION_ID = 0x47454E49
_BLOCK_COLUMNS = """
    id, parent_id, type, value_json, content_type, metadata_json, policy_json,
    created_at, updated_at, created_by, version
"""
_LINK_COLUMNS = """
    id, source_id, target_id, relation, metadata_json, created_at
"""
_TEMPORAL_COLUMNS = """
    collection_id, block_id, occurred_at, valid_at, invalid_at
"""
_EXPECTED_TABLE_COLUMNS = {
    "acyclic_relations": ("relation",),
    "blocks": (
        "storage_order",
        "id",
        "parent_id",
        "type",
        "value_json",
        "content_type",
        "metadata_json",
        "policy_json",
        "created_at",
        "updated_at",
        "created_by",
        "version",
    ),
    "key_bindings": ("collection_id", "namespace_json", "key", "block_id"),
    "links": (
        "storage_order",
        "id",
        "source_id",
        "target_id",
        "relation",
        "metadata_json",
        "created_at",
    ),
    "log_counters": ("log_id", "next_sequence"),
    "log_entries": ("log_id", "sequence", "block_id"),
    "route_entries": ("block_id", "route"),
    "temporal_entries": (
        "collection_id",
        "block_id",
        "occurred_at",
        "valid_at",
        "invalid_at",
    ),
}
_EXPECTED_INDEXES = {
    "blocks_parent_idx",
    "blocks_type_idx",
    "key_bindings_block_idx",
    "links_relation_idx",
    "links_source_idx",
    "links_target_idx",
    "log_entries_block_idx",
    "temporal_occurred_idx",
    "temporal_validity_idx",
}
_EXPECTED_FOREIGN_KEYS = {
    "blocks": {("parent_id", "blocks", "id", "CASCADE")},
    "key_bindings": {
        ("block_id", "blocks", "id", "CASCADE"),
        ("collection_id", "blocks", "id", "CASCADE"),
    },
    "links": {
        ("source_id", "blocks", "id", "CASCADE"),
        ("target_id", "blocks", "id", "CASCADE"),
    },
    "log_counters": {("log_id", "blocks", "id", "CASCADE")},
    "log_entries": {
        ("block_id", "blocks", "id", "CASCADE"),
        ("log_id", "blocks", "id", "CASCADE"),
    },
    "temporal_entries": {
        ("block_id", "blocks", "id", "CASCADE"),
        ("collection_id", "blocks", "id", "CASCADE"),
    },
}


class SQLiteBackend:
    """Persistent backend on the SQLite driver of the Python standard library.

    - **Capabilities:** the backend implements the required `MemoryBackend`
      behavior and all optional capabilities.
    - **One connection:** each instance owns one connection. An internal
      reentrant lock protects it.
    - **Several instances:** SQLite write transactions coordinate separate
      backend instances that use the same file.

    Attributes:
        path: SQLite database path that this backend opened.
        timeout: Seconds to wait when another connection holds a database
            lock.
        durability: SQLite synchronous mode in use, `"full"` or `"normal"`.
    """

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        timeout: float = 5.0,
        durability: Literal["full", "normal"] = "full",
    ) -> None:
        """Open the database, and create the current schema if necessary.

        Args:
            path: SQLite database path. `":memory:"` creates a separate
                in-memory database that this backend instance owns. It does not
                use WAL.
            timeout: Seconds to wait when another connection holds a database
                lock. `0` does not wait.
            durability: SQLite synchronous mode. `"full"` is the safe default
                for production. `"normal"` gives better write throughput, but
                the most recent commits can be lost after a power or
                operating-system crash.

        Raises:
            TypeError: If `timeout` or `durability` has the wrong type.
            ValueError: If `timeout` is negative or not finite, or
                `durability` is not `"full"` or `"normal"`.
            UnsupportedOperation: If the database schema is newer than this
                Genie version supports, the database belongs to another
                application, or its schema is incompatible, unowned, or a
                partial version-0 schema.
            BackendBusy: If another connection holds the write lock for longer
                than `timeout`. Construction also opens a write transaction.
            BackendError: If SQLite cannot open or configure the storage.
        """

        if isinstance(timeout, bool) or not isinstance(timeout, int | float):
            raise TypeError("timeout must be a number.")
        if not math.isfinite(float(timeout)) or timeout < 0:
            raise ValueError("timeout must be a finite number greater than or equal to zero.")
        if not isinstance(durability, str):
            raise TypeError("durability must be a string.")
        if durability not in {"full", "normal"}:
            raise ValueError("durability must be either 'full' or 'normal'.")
        self.path = str(path)
        self.timeout = float(timeout)
        self.durability = durability
        self._lock = RLock()
        self._connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                timeout=self.timeout,
                isolation_level=None,
                check_same_thread=False,
            )
            connection.row_factory = sqlite3.Row
            self._connection = connection
            self._configure_connection()
            self._initialize_schema()
        except sqlite3.Error as error:
            self._discard_connection()
            raise _translate_sqlite_error(error) from error
        except BaseException:
            self._discard_connection()
            raise

    def __enter__(self) -> Self:
        """Return this open backend for use as a context manager.

        Returns:
            This backend instance.

        Raises:
            RuntimeError: If the backend is already closed.
        """

        self._require_connection()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the owned connection when the `with` block ends.

        Args:
            exc_type: Type of the exception that leaves the block, if any.
            exc: Exception instance that leaves the block, if any.
            traceback: Traceback of the exception that leaves the block, if any.
        """

        self.close()

    def close(self) -> None:
        """Close the owned SQLite connection.

        A second `close` call is safe. Operations after `close` raise
        `RuntimeError`.

        Raises:
            BackendError: If SQLite fails during the rollback or the close.
        """

        with self._lock:
            connection = self._connection
            if connection is None:
                return
            try:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()
            except sqlite3.Error as error:
                raise _translate_sqlite_error(error) from error
            finally:
                self._connection = None

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store a new encoded memory block.

        Args:
            block: Block to create.

        Returns:
            Stored block, read back from SQLite.

        Raises:
            MemoryConflict: If the id already exists, or the parent does not
                exist or is the block itself.
            TypeError: If the encoded value or metadata is not strict JSON.
        """

        with self._write_transaction() as connection:
            if self._block_row(connection, block.id) is not None:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            self._validate_parent(connection, block.id, block.parent_id)
            try:
                connection.execute(
                    """
                    INSERT INTO blocks (
                        id, parent_id, type, value_json, content_type,
                        metadata_json, policy_json, created_at, updated_at,
                        created_by, version
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    _block_values(block),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryConflict(f"Memory block {block.id!r} could not be created.") from error
            row = self._block_row(connection, block.id)
            assert row is not None
            return _block_from_row(row)

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Get a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            A new block object, or `None` when the block does not exist.
        """

        with self._read_connection() as connection:
            row = self._block_row(connection, block_id)
            return _block_from_row(row) if row is not None else None

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace an existing encoded block, optionally by version.

        Args:
            block: Complete replacement block.
            expected_version: Current version that the compare-and-swap
                requires.

        Returns:
            Stored replacement, read back from SQLite.

        Raises:
            MemoryConflict: If the block does not exist, its version changed,
                the replacement changes its `type`, `created_at`,
                `created_by`, or `policy`, or the new parent does not exist,
                is the block itself, or is one of its descendants. With
                `expected_version`, also if the replacement does not increase
                the version by exactly one.
            TypeError: If the encoded value or metadata is not strict JSON.
        """

        with self._write_transaction() as connection:
            previous_row = self._block_row(connection, block.id)
            if previous_row is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            previous = _block_from_row(previous_row)
            _validate_block_identity(previous, block)
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._validate_parent(connection, block.id, block.parent_id)
                if block.parent_id is not None and self._contains(
                    connection,
                    block.id,
                    block.parent_id,
                ):
                    raise MemoryConflict("A memory block cannot be moved under one of its descendants.")
            try:
                parameters: tuple[Any, ...] = (
                    block.parent_id,
                    block.type,
                    _dump_json(block.value, field_name="block value"),
                    block.content_type,
                    _dump_json(block.metadata, field_name="block metadata"),
                    _encode_policy(block.policy),
                    _encode_datetime(block.created_at, field_name="created_at"),
                    _encode_datetime(block.updated_at, field_name="updated_at"),
                    block.created_by,
                    block.version,
                    block.id,
                )
                version_clause = ""
                if expected_version is not None:
                    version_clause = " AND version = ?"
                    parameters = (*parameters, expected_version)
                cursor = connection.execute(
                    """
                    UPDATE blocks
                    SET parent_id = ?, type = ?, value_json = ?, content_type = ?,
                        metadata_json = ?, policy_json = ?, created_at = ?,
                        updated_at = ?, created_by = ?, version = ?
                    WHERE id = ?
                    """ + version_clause,
                    parameters,
                )
                if cursor.rowcount != 1:
                    raise MemoryConflict(f"Memory block {block.id!r} changed before it could be updated.")
            except sqlite3.IntegrityError as error:
                raise MemoryConflict(f"Memory block {block.id!r} could not be updated.") from error
            row = self._block_row(connection, block.id)
            assert row is not None
            return _block_from_row(row)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_blocks: Sequence[MemoryBlock] | None = None,
    ) -> None:
        """Delete a subtree, optionally from an exact snapshot.

        Args:
            block_id: Root block id to delete. If the id does not exist and
                `expected_blocks` is empty or `None`, nothing happens.
            expected_blocks: Exact snapshot of the subtree, as the blocks that
                the caller read, that the delete requires.

        Raises:
            MemoryConflict: If the given subtree snapshot is stale.
        """

        with self._write_transaction() as connection:
            rows = self._descendant_rows(connection, block_id, include_self=True)
            if expected_blocks is not None and not _matches_snapshot([_block_from_row(row) for row in rows], expected_blocks):
                raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            self._delete_rows_leaf_first(connection, rows)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children in creation order.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks in creation order.
        """

        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_BLOCK_COLUMNS}
                FROM blocks
                WHERE parent_id = ?
                ORDER BY storage_order
                """,
                (block_id,),
            ).fetchall()
            return [_block_from_row(row) for row in rows]

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from the nearest parent upward.

        Args:
            block_id: Block id to examine.

        Returns:
            Ancestor blocks, nearest parent first.
        """

        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                WITH RECURSIVE chain(id, depth) AS (
                    SELECT parent_id, 1
                    FROM blocks
                    WHERE id = ? AND parent_id IS NOT NULL
                    UNION ALL
                    SELECT blocks.parent_id, chain.depth + 1
                    FROM blocks
                    JOIN chain ON blocks.id = chain.id
                    WHERE blocks.parent_id IS NOT NULL
                )
                SELECT {_qualified_columns("blocks", _BLOCK_COLUMNS)}
                FROM chain
                JOIN blocks ON blocks.id = chain.id
                ORDER BY chain.depth
                """,
                (block_id,),
            ).fetchall()
            return [_block_from_row(row) for row in rows]

    def descendants(
        self,
        block_id: str,
        *,
        include_self: bool = False,
    ) -> list[MemoryBlock]:
        """Return descendants in deterministic depth-first order.

        Args:
            block_id: Root block id.
            include_self: If `True`, the result includes `block_id`.

        Returns:
            Descendant blocks in depth-first creation order.
        """

        with self._read_connection() as connection:
            return [
                _block_from_row(row)
                for row in self._descendant_rows(
                    connection,
                    block_id,
                    include_self=include_self,
                )
            ]

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is `root_id` or is below it.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is the root or one of its descendants.
        """

        with self._read_connection() as connection:
            return self._contains(connection, root_id, block_id)

    def bind_key(self, binding: KeyBinding) -> KeyBinding:
        """Bind an unused namespaced address to a block, in one atomic step.

        Args:
            binding: Key binding to create.

        Returns:
            Stored binding.

        Raises:
            MemoryConflict: If the collection or the block does not exist, or
                the address is already bound.
        """

        with self._write_transaction() as connection:
            self._require_structure_blocks(
                connection,
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            try:
                connection.execute(
                    """
                    INSERT INTO key_bindings (
                        collection_id, namespace_json, key, block_id
                    ) VALUES (?, ?, ?, ?)
                    """,
                    _binding_values(binding),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in " f"collection {binding.collection_id!r}.") from error
            return binding

    def rebind_key(
        self,
        binding: KeyBinding,
        *,
        expected_block_id: str,
    ) -> KeyBinding:
        """Replace a binding in one atomic step, if its block id still matches.

        Args:
            binding: Replacement binding. Its collection id and address select
                the current binding to replace.
            expected_block_id: Block id that the binding points to now, as the
                caller expects.

        Returns:
            Stored replacement binding.

        Raises:
            MemoryConflict: If the collection or the block does not exist, the
                address is not bound, or it no longer points to
                `expected_block_id`.
        """

        with self._write_transaction() as connection:
            self._require_structure_blocks(
                connection,
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            cursor = connection.execute(
                """
                UPDATE key_bindings
                SET block_id = ?
                WHERE collection_id = ? AND namespace_json = ? AND key = ?
                    AND block_id = ?
                """,
                (
                    binding.block_id,
                    binding.collection_id,
                    _encode_namespace(binding.address.namespace),
                    binding.address.key,
                    expected_block_id,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Key address {binding.address!r} in collection " f"{binding.collection_id!r} no longer points to expected " f"block {expected_block_id!r}.")
            return binding

    def get_key(
        self,
        collection_id: str,
        address: MemoryAddress,
    ) -> KeyBinding | None:
        """Return one exact key binding.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to look up.

        Returns:
            Stored binding, or `None` when the address is not bound.
        """

        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT collection_id, namespace_json, key, block_id
                FROM key_bindings
                WHERE collection_id = ? AND namespace_json = ? AND key = ?
                """,
                (collection_id, _encode_namespace(address.namespace), address.key),
            ).fetchone()
            return _binding_from_row(row) if row is not None else None

    def list_keys(
        self,
        collection_id: str,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        after: MemoryAddress | None = None,
        limit: int = 100,
    ) -> list[KeyBinding]:
        """Return one bounded page in stable namespace-and-key order.

        Each call reads all bindings of the collection, then filters and sorts
        them in Python.

        Args:
            collection_id: Id of the collection to list.
            namespace: Optional exact namespace filter. With `recursive=True`,
                it is a namespace prefix.
            recursive: If `True`, the result also includes the nested
                namespaces below `namespace`.
            after: Optional exclusive start address, for pages in address
                order.
            limit: Maximum number of bindings to return.

        Returns:
            Matching bindings in ascending namespace-and-key order.

        Raises:
            TypeError: If `recursive` is not a boolean or `limit` is not an
                integer.
            ValueError: If `limit` is negative.
        """

        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a boolean.")
        _validate_limit(limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                """
                SELECT collection_id, namespace_json, key, block_id
                FROM key_bindings
                WHERE collection_id = ?
                """,
                (collection_id,),
            ).fetchall()
        bindings = [
            _binding_from_row(row)
            for row in rows
            if _namespace_matches(
                _decode_namespace(row["namespace_json"]),
                namespace,
                recursive=recursive,
            )
        ]
        bindings.sort(key=lambda binding: (binding.address.namespace, binding.address.key))
        if after is not None:
            after_key = (after.namespace, after.key)
            bindings = [binding for binding in bindings if (binding.address.namespace, binding.address.key) > after_key]
        return bindings[:limit]

    def unbind_key(
        self,
        collection_id: str,
        address: MemoryAddress,
        *,
        expected_block_id: str,
    ) -> None:
        """Remove a binding in one atomic step, if its block id still matches.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to remove.
            expected_block_id: Block id that the binding points to now, as the
                caller expects.

        Raises:
            MemoryConflict: If the address is not bound, or it no longer points
                to `expected_block_id`.
        """

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM key_bindings
                WHERE collection_id = ? AND namespace_json = ? AND key = ?
                    AND block_id = ?
                """,
                (
                    collection_id,
                    _encode_namespace(address.namespace),
                    address.key,
                    expected_block_id,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} " f"no longer points to expected block {expected_block_id!r}.")

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove the bindings that one deleted block owns, and the bindings that point to it.

        Args:
            block_id: Id of the deleted block. The call removes the bindings
                whose collection id or target block id matches.
        """

        with self._write_transaction() as connection:
            connection.execute(
                """
                DELETE FROM key_bindings
                WHERE collection_id = ? OR block_id = ?
                """,
                (block_id, block_id),
            )

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Allocate a sequence and append a block reference, in one atomic step.

        Args:
            log_id: Id of the ordered log to append to.
            block_id: Id of the memory block to place at the new position.

        Returns:
            Stored entry with the allocated sequence number.

        Raises:
            MemoryConflict: If the log or referenced block does not exist.
        """

        from genie.structures.records import LogEntry

        with self._write_transaction() as connection:
            self._require_structure_blocks(
                connection,
                container_id=log_id,
                block_id=block_id,
                container_name="Event log",
            )
            row = connection.execute(
                """
                SELECT next_sequence
                FROM log_counters
                WHERE log_id = ?
                """,
                (log_id,),
            ).fetchone()
            if row is None:
                sequence = 0
                connection.execute(
                    """
                    INSERT INTO log_counters (log_id, next_sequence)
                    VALUES (?, 1)
                    """,
                    (log_id,),
                )
            else:
                sequence = int(row["next_sequence"])
                connection.execute(
                    """
                    UPDATE log_counters
                    SET next_sequence = ?
                    WHERE log_id = ?
                    """,
                    (sequence + 1, log_id),
                )
            try:
                connection.execute(
                    """
                    INSERT INTO log_entries (log_id, sequence, block_id)
                    VALUES (?, ?, ?)
                    """,
                    (log_id, sequence, block_id),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryConflict(f"Event log {log_id!r} could not allocate sequence {sequence}.") from error
            return LogEntry(log_id=log_id, sequence=sequence, block_id=block_id)

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry.

        Args:
            log_id: Id of the ordered log.
            sequence: Exact position in the log.

        Returns:
            Stored entry, or `None` when the position has no entry.

        Raises:
            TypeError: If `sequence` is not an integer.
            ValueError: If `sequence` is negative.
        """

        _validate_sequence(sequence, name="sequence")
        with self._read_connection() as connection:
            row = connection.execute(
                """
                SELECT log_id, sequence, block_id
                FROM log_entries
                WHERE log_id = ? AND sequence = ?
                """,
                (log_id, sequence),
            ).fetchone()
            return _log_entry_from_row(row) if row is not None else None

    def list_log_entries(
        self,
        log_id: str,
        *,
        start: int | None = None,
        stop: int | None = None,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Return one bounded page in a half-open sequence range.

        Args:
            log_id: Id of the ordered log.
            start: Optional inclusive lower sequence bound.
            stop: Optional exclusive upper sequence bound.
            reverse: If `True`, entries come in descending sequence order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in sequence order, descending when `reverse` is
            `True`.

        Raises:
            TypeError: If `reverse` is not a boolean, or `start`, `stop`, or
                `limit` is not an integer.
            ValueError: If a bound or `limit` is negative, or `stop` is less
                than `start`.
        """

        if not isinstance(reverse, bool):
            raise TypeError("reverse must be a boolean.")
        if start is not None:
            _validate_sequence(start, name="start")
        if stop is not None:
            _validate_sequence(stop, name="stop")
        if start is not None and stop is not None and stop < start:
            raise ValueError("stop must be greater than or equal to start.")
        _validate_limit(limit)
        clauses = ["log_id = ?"]
        parameters: list[Any] = [log_id]
        if start is not None:
            clauses.append("sequence >= ?")
            parameters.append(start)
        if stop is not None:
            clauses.append("sequence < ?")
            parameters.append(stop)
        parameters.append(limit)
        direction = "DESC" if reverse else "ASC"
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT log_id, sequence, block_id
                FROM log_entries
                WHERE {' AND '.join(clauses)}
                ORDER BY sequence {direction}
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [_log_entry_from_row(row) for row in rows]

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove the log that one deleted block owns, and the entries that point to the block.

        Args:
            block_id: Id of the deleted block. The call drops the log of this
                id with its counter, and the entries that point to the block in
                any log.
        """

        with self._write_transaction() as connection:
            connection.execute(
                """
                DELETE FROM log_entries
                WHERE log_id = ? OR block_id = ?
                """,
                (block_id, block_id),
            )
            connection.execute(
                "DELETE FROM log_counters WHERE log_id = ?",
                (block_id,),
            )

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Add one unique temporal collection entry.

        Args:
            entry: Collection id, block id, and event time or validity period
                to store.

        Returns:
            Stored entry.

        Raises:
            MemoryConflict: If the collection or the block does not exist, or
                the collection already contains the block.
        """

        with self._write_transaction() as connection:
            self._require_structure_blocks(
                connection,
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            try:
                connection.execute(
                    """
                    INSERT INTO temporal_entries (
                        collection_id, block_id, occurred_at, valid_at, invalid_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    _temporal_values(entry),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal " f"collection {entry.collection_id!r}.") from error
            return entry

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Replace an existing temporal collection entry.

        Args:
            entry: Replacement entry. Its collection id and block id select
                the current entry to replace.

        Returns:
            Stored replacement entry.

        Raises:
            MemoryConflict: If the collection or the block does not exist, or
                the collection does not contain the block.
        """

        with self._write_transaction() as connection:
            self._require_structure_blocks(
                connection,
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            values = _temporal_values(entry)
            cursor = connection.execute(
                """
                UPDATE temporal_entries
                SET occurred_at = ?, valid_at = ?, invalid_at = ?
                WHERE collection_id = ? AND block_id = ?
                """,
                (values[2], values[3], values[4], values[0], values[1]),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal " f"collection {entry.collection_id!r}.")
            return entry

    def get_temporal_entry(
        self,
        collection_id: str,
        block_id: str,
    ) -> TemporalEntry | None:
        """Return one exact temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block.

        Returns:
            Stored entry, or `None` when the collection does not contain the
            block.
        """

        with self._read_connection() as connection:
            row = connection.execute(
                f"""
                SELECT {_TEMPORAL_COLUMNS}
                FROM temporal_entries
                WHERE collection_id = ? AND block_id = ?
                """,
                (collection_id, block_id),
            ).fetchone()
            return _temporal_from_row(row) if row is not None else None

    def list_temporal_entries(
        self,
        collection_id: str,
        *,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """Return one page in stable block-id order.

        Args:
            collection_id: Id of the temporal collection.
            after_block_id: Optional exclusive start block id, for pages in
                block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Entries in ascending block-id order.

        Raises:
            TypeError: If `limit` is not an integer.
            ValueError: If `limit` is negative.
        """

        _validate_limit(limit)
        clauses = ["collection_id = ?"]
        parameters: list[Any] = [collection_id]
        if after_block_id is not None:
            clauses.append("block_id > ?")
            parameters.append(after_block_id)
        parameters.append(limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_TEMPORAL_COLUMNS}
                FROM temporal_entries
                WHERE {' AND '.join(clauses)}
                ORDER BY block_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [_temporal_from_row(row) for row in rows]

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block to remove.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        with self._write_transaction() as connection:
            cursor = connection.execute(
                """
                DELETE FROM temporal_entries
                WHERE collection_id = ? AND block_id = ?
                """,
                (collection_id, block_id),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection " f"{collection_id!r}.")

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove the temporal entries that one deleted block owns, and the entries that point to it.

        Args:
            block_id: Id of the deleted block. The call removes the entries
                whose collection id or target block id matches.
        """

        with self._write_transaction() as connection:
            connection.execute(
                """
                DELETE FROM temporal_entries
                WHERE collection_id = ? OR block_id = ?
                """,
                (block_id, block_id),
            )

    def query_temporal_entries(
        self,
        collection_id: str,
        *,
        occurred_only: bool = False,
        occurred_start: datetime | None = None,
        occurred_stop: datetime | None = None,
        valid_at: datetime | None = None,
        overlaps: ValidityInterval | None = None,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """Return one temporal page that matches all given time filters.

        Args:
            collection_id: Id of the temporal collection.
            occurred_only: If `True`, only entries with an event time match.
            occurred_start: Optional inclusive lower bound on event time.
            occurred_stop: Optional exclusive upper bound on event time.
            valid_at: Optional instant that the validity period of an entry
                contains.
            overlaps: Optional validity period that the period of an entry
                overlaps.
            after_block_id: Optional exclusive start block id, for pages in
                block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Matching entries in ascending block-id order.

        Raises:
            TypeError: If `occurred_only` is not a boolean, a time bound is
                not a datetime, `overlaps` is not a `ValidityInterval`, or
                `limit` is not an integer.
            ValueError: If a time bound is naive, `occurred_stop` is earlier
                than `occurred_start`, or `limit` is negative.
        """

        from genie.structures.records import ValidityInterval

        if not isinstance(occurred_only, bool):
            raise TypeError("occurred_only must be a boolean.")
        occurred_start = _normalize_optional_time(
            occurred_start,
            name="occurred_start",
        )
        occurred_stop = _normalize_optional_time(
            occurred_stop,
            name="occurred_stop",
        )
        valid_at = _normalize_optional_time(valid_at, name="valid_at")
        if (occurred_start is not None and occurred_stop is not None) and occurred_stop < occurred_start:
            raise ValueError("occurred_stop must be greater than or equal to occurred_start.")
        if overlaps is not None and not isinstance(overlaps, ValidityInterval):
            raise TypeError("overlaps must be a ValidityInterval.")
        _validate_limit(limit)

        clauses = ["collection_id = ?"]
        parameters: list[Any] = [collection_id]
        if after_block_id is not None:
            clauses.append("block_id > ?")
            parameters.append(after_block_id)
        if occurred_only:
            clauses.append("occurred_at IS NOT NULL")
        if occurred_start is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(_encode_datetime(occurred_start, field_name="occurred_start"))
        if occurred_stop is not None:
            clauses.append("occurred_at < ?")
            parameters.append(_encode_datetime(occurred_stop, field_name="occurred_stop"))
        if valid_at is not None:
            encoded_valid_at = _encode_datetime(valid_at, field_name="valid_at")
            clauses.extend(
                [
                    "valid_at IS NOT NULL",
                    "valid_at <= ?",
                    "(invalid_at IS NULL OR invalid_at > ?)",
                ]
            )
            parameters.extend([encoded_valid_at, encoded_valid_at])
        if overlaps is not None:
            clauses.extend(
                [
                    "valid_at IS NOT NULL",
                    "(invalid_at IS NULL OR invalid_at > ?)",
                ]
            )
            parameters.append(_encode_datetime(overlaps.valid_at, field_name="overlaps.valid_at"))
            if overlaps.invalid_at is not None:
                clauses.append("valid_at < ?")
                parameters.append(
                    _encode_datetime(
                        overlaps.invalid_at,
                        field_name="overlaps.invalid_at",
                    )
                )
        parameters.append(limit)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_TEMPORAL_COLUMNS}
                FROM temporal_entries
                WHERE {' AND '.join(clauses)}
                ORDER BY block_id
                LIMIT ?
                """,
                parameters,
            ).fetchall()
            return [_temporal_from_row(row) for row in rows]

    def claim_route(self, entry: RouteInfo) -> RouteInfo:
        """Store a route catalog entry only when its block id has no entry.

        Args:
            entry: Block id and owning route to record.

        Returns:
            Stored entry.

        Raises:
            MemoryConflict: If the block id already has an entry.
        """

        with self._write_transaction() as connection:
            try:
                connection.execute(
                    "INSERT INTO route_entries (block_id, route) VALUES (?, ?)",
                    (entry.block_id, entry.route),
                )
            except sqlite3.IntegrityError as error:
                raise MemoryConflict(f"Block id {entry.block_id!r} already has a route catalog entry.") from error
            return entry

    def get_route(self, block_id: str) -> RouteInfo | None:
        """Return the route catalog entry of a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Stored entry, or `None` when the block id has no entry.
        """

        with self._read_connection() as connection:
            row = connection.execute(
                "SELECT block_id, route FROM route_entries WHERE block_id = ?",
                (block_id,),
            ).fetchone()
            return _route_entry_from_row(row) if row is not None else None

    def release_routes(self, block_ids: Sequence[str]) -> None:
        """Remove the route catalog entries of block ids, in one transaction.

        Args:
            block_ids: Block ids whose entries to remove. An id with no entry
                is ignored.
        """

        with self._write_transaction() as connection:
            connection.executemany(
                "DELETE FROM route_entries WHERE block_id = ?",
                [(block_id,) for block_id in block_ids],
            )

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a typed link between existing blocks.

        Args:
            link: Link to store.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, or the relation is protected as an acyclic graph.
        """

        with self._write_transaction() as connection:
            if self._relation_is_protected(connection, link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic " "graph; use atomic acyclic insertion.")
            self._validate_new_link(connection, link)
            return self._insert_link(connection, link)

    def protect_relation(self, relation: str) -> None:
        """Protect a relation as an acyclic graph, in one atomic step.

        - **Current links:** they stay. The call accepts them only if they
          already form a DAG.
        - **Protection:** after a successful call, a plain `add_link` with the
          relation raises `MemoryConflict`. The protection is stored in the
          database.
        - **Repeated call:** a call for a protected relation changes nothing.

        Args:
            relation: Relation that identifies the protected graph.

        Raises:
            MemoryConflict: If the current links of the relation contain a
                cycle.
        """

        with self._write_transaction() as connection:
            if self._relation_is_protected(connection, relation):
                return
            if self._relation_has_cycle(connection, relation):
                raise MemoryConflict(f"Link relation {relation!r} already contains a cycle.")
            connection.execute(
                "INSERT INTO acyclic_relations (relation) VALUES (?)",
                (relation,),
            )

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Add a link to a protected relation in one atomic step, unless the link makes a directed cycle.

        Args:
            link: Link to store. Its relation identifies the protected graph of
                the cycle check.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, the relation is not protected, or the new link makes a
                cycle. A self-link is a cycle.
        """

        with self._write_transaction() as connection:
            self._validate_new_link(connection, link)
            if not self._relation_is_protected(connection, link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} is not protected as an acyclic graph; call protect_relation first.")
            if link.source_id == link.target_id or self._link_path_exists(
                connection,
                link.target_id,
                link.source_id,
                relation=link.relation,
            ):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create " f"a cycle in relation {link.relation!r}.")
            return self._insert_link(connection, link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace only the metadata of an existing link.

        Args:
            link: Link with the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link does not exist, or a field other than
                metadata changed.
        """

        with self._write_transaction() as connection:
            row = self._link_row(connection, link.id)
            if row is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            previous = _link_from_row(row)
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            connection.execute(
                """
                UPDATE links
                SET metadata_json = ?
                WHERE id = ?
                """,
                (
                    _dump_json(link.metadata, field_name="link metadata"),
                    link.id,
                ),
            )
            updated_row = self._link_row(connection, link.id)
            assert updated_row is not None
            return _link_from_row(updated_row)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to one block.

        Args:
            block_id: Block id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.

        Raises:
            ValueError: If `direction` is not `"out"`, `"in"`, or `"both"`.
        """

        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")
        if direction == "out":
            endpoint_clause = "source_id = ?"
            parameters: list[Any] = [block_id]
        elif direction == "in":
            endpoint_clause = "target_id = ?"
            parameters = [block_id]
        else:
            endpoint_clause = "(source_id = ? OR target_id = ?)"
            parameters = [block_id, block_id]
        relation_clause = ""
        if relation is not None:
            relation_clause = " AND relation = ?"
            parameters.append(relation)
        with self._read_connection() as connection:
            rows = connection.execute(
                f"""
                SELECT {_LINK_COLUMNS}
                FROM links
                WHERE {endpoint_clause}{relation_clause}
                ORDER BY storage_order
                """,
                parameters,
            ).fetchall()
            return [_link_from_row(row) for row in rows]

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete incoming and outgoing links for one block.

        Args:
            block_id: Block id whose connected links to remove.
        """

        with self._write_transaction() as connection:
            connection.execute(
                """
                DELETE FROM links
                WHERE source_id = ? OR target_id = ?
                """,
                (block_id, block_id),
            )

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks with the portable matching behavior of the in-memory backend.

        - **SQL step:** SQL selects the candidate rows by scope only.
        - **Python step:** Python then matches the types, filters, and text. So
          the results stay the same as in the reference backend.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results in the search order of `page_results`: at most
            `query.limit` results after `query.after`.
        """

        with self._read_connection() as connection:
            candidates = [_block_from_row(row) for row in self._scope_candidate_rows(connection, query)]
        results: list[SearchResult] = []
        text = query.text.casefold() if query.text else None
        for block in candidates:
            if query.types and block.type not in query.types:
                continue
            if query.filters and not matches_filters(block, query.filters):
                continue
            score = 1.0
            reason = "matched filters"
            if text:
                haystack = f"{block.type} {block.value!r} {block.metadata!r}".casefold()
                if text not in haystack:
                    continue
                score = float(haystack.count(text) or 1)
                reason = "matched text"
            results.append(SearchResult(block=block, score=score, reason=reason))
        return page_results(results, query)

    def _configure_connection(self) -> None:
        """Turn on the SQLite settings that the backend needs, and check them, or raise `BackendUnavailable`."""

        connection = self._require_connection()
        connection.execute("PRAGMA foreign_keys = ON")
        foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()
        if foreign_keys is None or int(foreign_keys[0]) != 1:
            raise BackendUnavailable("SQLite foreign-key support could not be enabled.")
        connection.execute(f"PRAGMA busy_timeout = {int(self.timeout * 1000)}")
        connection.execute("PRAGMA trusted_schema = OFF")
        if self.path != ":memory:":
            journal_mode = self._enable_wal(connection)
            if journal_mode != "wal":
                raise BackendUnavailable(f"SQLite WAL mode could not be enabled; active mode is {journal_mode!r}.")
        synchronous = "FULL" if self.durability == "full" else "NORMAL"
        expected_synchronous = 2 if self.durability == "full" else 1
        connection.execute(f"PRAGMA synchronous = {synchronous}")
        synchronous_row = connection.execute("PRAGMA synchronous").fetchone()
        if synchronous_row is None or int(synchronous_row[0]) != expected_synchronous:
            raise BackendUnavailable(f"SQLite synchronous={synchronous} could not be enabled.")

    def _enable_wal(self, connection: sqlite3.Connection) -> str:
        """Switch the database file to WAL mode, and return the active mode.

        - **No busy handler:** when another connection holds a lock, SQLite
          answers this pragma with "database is locked" at once. It does not
          call the busy handler that the other statements wait on.
        - **When:** this happens when several connections open one new file at
          the same time.
        - **Retry:** so the backend runs the pragma again for up to `timeout`
          seconds, as for any other busy statement.
        """

        deadline = monotonic() + self.timeout
        while True:
            try:
                journal_row = connection.execute("PRAGMA journal_mode = WAL").fetchone()
            except sqlite3.OperationalError as error:
                if not _is_busy_error(error) or monotonic() >= deadline:
                    raise
                sleep(_WAL_RETRY_SECONDS)
                continue
            return str(journal_row[0]).casefold() if journal_row else ""

    def _initialize_schema(self) -> None:
        """Identify, migrate, and validate the Genie SQLite schema.

        - **One snapshot:** one read transaction reads the version, the
          application id, and the object list, so they describe one state.
          Another constructor commits its tables and both pragmas together.
          Separate reads could see the version from before that commit and the
          tables from after it.
        - **Adoption:** a database with application id 0 and the exact current
          schema gets the Genie application id.
        - **Refusals:** a newer version, another application id, an unowned or
          incompatible schema, or a partial version-0 schema raises
          `UnsupportedOperation`.
        """

        connection = self._require_connection()
        with self._read_connection() as snapshot:
            version_row = snapshot.execute("PRAGMA user_version").fetchone()
            version = int(version_row[0]) if version_row is not None else 0
            application_row = snapshot.execute("PRAGMA application_id").fetchone()
            application_id = int(application_row[0]) if application_row is not None else 0
            user_objects = _user_schema_objects(snapshot)
        if version > _SCHEMA_VERSION:
            raise UnsupportedOperation(f"SQLite schema version {version} is newer than supported " f"version {_SCHEMA_VERSION}.")
        if application_id not in {0, _APPLICATION_ID}:
            raise UnsupportedOperation("The SQLite database belongs to another application " f"(application_id={application_id}).")
        legacy_schema = False
        if application_id == 0 and user_objects:
            if version == _SCHEMA_VERSION:
                legacy_schema = True
            else:
                raise UnsupportedOperation("The SQLite database contains an incompatible or unowned schema; " "use an empty database or a validated Genie database.")
        if application_id == _APPLICATION_ID and version == 0 and user_objects:
            raise UnsupportedOperation("The Genie SQLite database has a partial version-0 schema.")
        statements = (
            """
            CREATE TABLE IF NOT EXISTS blocks (
                storage_order INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                parent_id TEXT,
                type TEXT NOT NULL,
                value_json TEXT NOT NULL,
                content_type TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                policy_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by TEXT,
                version INTEGER NOT NULL CHECK (version >= 1),
                CHECK (parent_id IS NULL OR parent_id <> id),
                FOREIGN KEY (parent_id) REFERENCES blocks(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS blocks_parent_idx ON blocks(parent_id, storage_order)",
            "CREATE INDEX IF NOT EXISTS blocks_type_idx ON blocks(type)",
            """
            CREATE TABLE IF NOT EXISTS links (
                storage_order INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (source_id) REFERENCES blocks(id) ON DELETE CASCADE,
                FOREIGN KEY (target_id) REFERENCES blocks(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS links_source_idx ON links(source_id, relation, storage_order)",
            "CREATE INDEX IF NOT EXISTS links_target_idx ON links(target_id, relation, storage_order)",
            "CREATE INDEX IF NOT EXISTS links_relation_idx ON links(relation, source_id, target_id)",
            """
            CREATE TABLE IF NOT EXISTS acyclic_relations (
                relation TEXT PRIMARY KEY
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS key_bindings (
                collection_id TEXT NOT NULL,
                namespace_json TEXT NOT NULL,
                key TEXT NOT NULL,
                block_id TEXT NOT NULL,
                PRIMARY KEY (collection_id, namespace_json, key),
                FOREIGN KEY (collection_id) REFERENCES blocks(id) ON DELETE CASCADE,
                FOREIGN KEY (block_id) REFERENCES blocks(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS key_bindings_block_idx ON key_bindings(block_id)",
            """
            CREATE TABLE IF NOT EXISTS log_counters (
                log_id TEXT PRIMARY KEY,
                next_sequence INTEGER NOT NULL CHECK (next_sequence >= 0),
                FOREIGN KEY (log_id) REFERENCES blocks(id) ON DELETE CASCADE
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS log_entries (
                log_id TEXT NOT NULL,
                sequence INTEGER NOT NULL CHECK (sequence >= 0),
                block_id TEXT NOT NULL,
                PRIMARY KEY (log_id, sequence),
                FOREIGN KEY (log_id) REFERENCES blocks(id) ON DELETE CASCADE,
                FOREIGN KEY (block_id) REFERENCES blocks(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS log_entries_block_idx ON log_entries(block_id)",
            """
            CREATE TABLE IF NOT EXISTS temporal_entries (
                collection_id TEXT NOT NULL,
                block_id TEXT NOT NULL,
                occurred_at TEXT,
                valid_at TEXT,
                invalid_at TEXT,
                PRIMARY KEY (collection_id, block_id),
                CHECK (occurred_at IS NOT NULL OR valid_at IS NOT NULL),
                CHECK (invalid_at IS NULL OR valid_at IS NOT NULL),
                CHECK (invalid_at IS NULL OR invalid_at > valid_at),
                FOREIGN KEY (collection_id) REFERENCES blocks(id) ON DELETE CASCADE,
                FOREIGN KEY (block_id) REFERENCES blocks(id) ON DELETE CASCADE
            )
            """,
            "CREATE INDEX IF NOT EXISTS temporal_occurred_idx ON temporal_entries(collection_id, occurred_at, block_id)",
            "CREATE INDEX IF NOT EXISTS temporal_validity_idx ON temporal_entries(collection_id, valid_at, invalid_at, block_id)",
            """
            CREATE TABLE IF NOT EXISTS route_entries (
                block_id TEXT PRIMARY KEY,
                route TEXT NOT NULL
            )
            """,
        )
        migrations = {1: statements}
        if legacy_schema:
            _validate_schema(connection, expected_statements=statements)
            with self._write_transaction() as transaction:
                transaction.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
            return
        with self._write_transaction() as transaction:
            for target_version in range(version + 1, _SCHEMA_VERSION + 1):
                migration = migrations.get(target_version)
                if migration is None:
                    raise UnsupportedOperation(f"No SQLite migration is available for version {target_version}.")
                for statement in migration:
                    transaction.execute(statement)
                transaction.execute(f"PRAGMA user_version = {target_version}")
            transaction.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
        _validate_schema(connection, expected_statements=statements)

    @contextmanager
    def _read_connection(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield one consistent read transaction under the instance lock."""

        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN")
                yield connection
                connection.commit()
            except sqlite3.Error as error:
                _rollback_quietly(connection)
                raise _translate_sqlite_error(error) from error
            except BaseException:
                _rollback_quietly(connection)
                raise

    @contextmanager
    def _write_transaction(self) -> Generator[sqlite3.Connection, None, None]:
        """Yield one serialized immediate transaction and commit it atomically."""

        with self._lock:
            connection = self._require_connection()
            try:
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.commit()
            except sqlite3.Error as error:
                _rollback_quietly(connection)
                raise _translate_sqlite_error(error) from error
            except BaseException:
                _rollback_quietly(connection)
                raise

    def _discard_connection(self) -> None:
        """Clean up after a constructor failure, as far as possible."""

        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            if connection.in_transaction:
                connection.rollback()
            connection.close()
        except sqlite3.Error:
            pass

    def _require_connection(self) -> sqlite3.Connection:
        """Return the open connection, or raise `RuntimeError` after `close`."""

        if self._connection is None:
            raise RuntimeError("SQLiteBackend is closed.")
        return self._connection

    def _block_row(
        self,
        connection: sqlite3.Connection,
        block_id: str,
    ) -> sqlite3.Row | None:
        """Return one raw block row by id."""

        return connection.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM blocks WHERE id = ?",
            (block_id,),
        ).fetchone()

    def _link_row(
        self,
        connection: sqlite3.Connection,
        link_id: str,
    ) -> sqlite3.Row | None:
        """Return one raw link row by id."""

        return connection.execute(
            f"SELECT {_LINK_COLUMNS} FROM links WHERE id = ?",
            (link_id,),
        ).fetchone()

    def _validate_parent(
        self,
        connection: sqlite3.Connection,
        block_id: str,
        parent_id: str | None,
    ) -> None:
        """Raise `MemoryConflict` if the parent id is not valid for a create or a move."""

        if parent_id == block_id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if parent_id is not None and self._block_row(connection, parent_id) is None:
            raise MemoryConflict(f"Parent memory block {parent_id!r} does not exist.")

    def _descendant_rows(
        self,
        connection: sqlite3.Connection,
        block_id: str,
        *,
        include_self: bool,
    ) -> list[sqlite3.Row]:
        """Return descendant rows in depth-first creation order."""

        return connection.execute(
            f"""
            WITH RECURSIVE tree(id, path, depth) AS (
                SELECT id, printf('%020d', storage_order), 0
                FROM blocks
                WHERE id = ?
                UNION ALL
                SELECT blocks.id,
                    tree.path || '/' || printf('%020d', blocks.storage_order),
                    tree.depth + 1
                FROM blocks
                JOIN tree ON blocks.parent_id = tree.id
            )
            SELECT {_qualified_columns("blocks", _BLOCK_COLUMNS)}
            FROM tree
            JOIN blocks ON blocks.id = tree.id
            WHERE (? = 1 OR tree.depth > 0)
            ORDER BY tree.path
            """,
            (block_id, int(include_self)),
        ).fetchall()

    def _delete_rows_leaf_first(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> None:
        """Delete a subtree from the leaves up, so no foreign-key cascade recurses."""

        connection.executemany(
            "DELETE FROM blocks WHERE id = ?",
            ((str(row["id"]),) for row in reversed(rows)),
        )

    def _contains(
        self,
        connection: sqlite3.Connection,
        root_id: str,
        block_id: str,
    ) -> bool:
        """Check ownership containment with one recursive query."""

        row = connection.execute(
            """
            WITH RECURSIVE tree(id) AS (
                SELECT id
                FROM blocks
                WHERE id = ?
                UNION ALL
                SELECT blocks.id
                FROM blocks
                JOIN tree ON blocks.parent_id = tree.id
            )
            SELECT 1
            FROM tree
            WHERE id = ?
            LIMIT 1
            """,
            (root_id, block_id),
        ).fetchone()
        return row is not None

    def _require_structure_blocks(
        self,
        connection: sqlite3.Connection,
        *,
        container_id: str,
        block_id: str,
        container_name: str,
    ) -> None:
        """Raise `MemoryConflict` if the structure container or the referenced block does not exist."""

        if self._block_row(connection, container_id) is None:
            raise MemoryConflict(f"{container_name} block {container_id!r} does not exist.")
        if self._block_row(connection, block_id) is None:
            raise MemoryConflict(f"Memory block {block_id!r} does not exist.")

    def _validate_new_link(
        self,
        connection: sqlite3.Connection,
        link: MemoryLink,
    ) -> None:
        """Raise `MemoryConflict` if the link id already exists or an endpoint is not stored."""

        if self._link_row(connection, link.id) is not None:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if self._block_row(connection, link.source_id) is None:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if self._block_row(connection, link.target_id) is None:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _insert_link(
        self,
        connection: sqlite3.Connection,
        link: MemoryLink,
    ) -> MemoryLink:
        """Insert a checked link inside the transaction of the caller."""

        try:
            connection.execute(
                """
                INSERT INTO links (
                    id, source_id, target_id, relation, metadata_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    link.id,
                    link.source_id,
                    link.target_id,
                    link.relation,
                    _dump_json(link.metadata, field_name="link metadata"),
                    _encode_datetime(link.created_at, field_name="created_at"),
                ),
            )
        except sqlite3.IntegrityError as error:
            raise MemoryConflict(f"Memory link {link.id!r} could not be created.") from error
        row = self._link_row(connection, link.id)
        assert row is not None
        return _link_from_row(row)

    def _relation_is_protected(
        self,
        connection: sqlite3.Connection,
        relation: str,
    ) -> bool:
        """Return whether a relation is protected as a DAG."""

        return (
            connection.execute(
                "SELECT 1 FROM acyclic_relations WHERE relation = ?",
                (relation,),
            ).fetchone()
            is not None
        )

    def _link_path_exists(
        self,
        connection: sqlite3.Connection,
        start_id: str,
        target_id: str,
        *,
        relation: str,
    ) -> bool:
        """Return whether one relation has a directed path between 2 blocks."""

        row = connection.execute(
            """
            WITH RECURSIVE reachable(id) AS (
                VALUES (?)
                UNION
                SELECT links.target_id
                FROM links
                JOIN reachable ON links.source_id = reachable.id
                WHERE links.relation = ?
            )
            SELECT 1
            FROM reachable
            WHERE id = ?
            LIMIT 1
            """,
            (start_id, relation, target_id),
        ).fetchone()
        return row is not None

    def _relation_has_cycle(
        self,
        connection: sqlite3.Connection,
        relation: str,
    ) -> bool:
        """Return whether the stored relation already contains a cycle."""

        rows = connection.execute(
            """
            SELECT source_id, target_id
            FROM links
            WHERE relation = ?
            """,
            (relation,),
        ).fetchall()
        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = defaultdict(int)
        nodes: set[str] = set()
        for row in rows:
            source_id = str(row["source_id"])
            target_id = str(row["target_id"])
            adjacency[source_id].append(target_id)
            indegree[target_id] += 1
            nodes.add(source_id)
            nodes.add(target_id)
        pending = deque(node_id for node_id in nodes if indegree[node_id] == 0)
        visited = 0
        while pending:
            node_id = pending.popleft()
            visited += 1
            for target_id in adjacency[node_id]:
                indegree[target_id] -= 1
                if indegree[target_id] == 0:
                    pending.append(target_id)
        return visited != len(nodes)

    def _scope_candidate_rows(
        self,
        connection: sqlite3.Connection,
        query: MemoryQuery,
    ) -> list[sqlite3.Row]:
        """Return raw candidate rows for one normalized search scope."""

        if query.scope == Scope.GLOBAL or query.root_id is None:
            return connection.execute(f"SELECT {_BLOCK_COLUMNS} FROM blocks ORDER BY storage_order").fetchall()
        if self._block_row(connection, query.root_id) is None:
            return []
        if query.scope == Scope.SELF:
            row = self._block_row(connection, query.root_id)
            return [row] if row is not None else []
        if query.scope == Scope.SUBTREE:
            return self._descendant_rows(
                connection,
                query.root_id,
                include_self=True,
            )
        if query.scope == Scope.ANCESTORS:
            root = self._block_row(connection, query.root_id)
            rows = connection.execute(
                f"""
                WITH RECURSIVE chain(id, depth) AS (
                    SELECT parent_id, 1
                    FROM blocks
                    WHERE id = ? AND parent_id IS NOT NULL
                    UNION ALL
                    SELECT blocks.parent_id, chain.depth + 1
                    FROM blocks
                    JOIN chain ON blocks.id = chain.id
                    WHERE blocks.parent_id IS NOT NULL
                )
                SELECT {_qualified_columns("blocks", _BLOCK_COLUMNS)}
                FROM chain
                JOIN blocks ON blocks.id = chain.id
                ORDER BY chain.depth
                """,
                (query.root_id,),
            ).fetchall()
            return ([root] if root is not None else []) + rows
        if query.scope == Scope.LINKS:
            return connection.execute(
                f"""
                SELECT {_BLOCK_COLUMNS}
                FROM blocks
                WHERE id = ? OR id IN (
                    SELECT source_id
                    FROM links
                    WHERE source_id = ? OR target_id = ?
                    UNION
                    SELECT target_id
                    FROM links
                    WHERE source_id = ? OR target_id = ?
                )
                ORDER BY storage_order
                """,
                (query.root_id, query.root_id, query.root_id, query.root_id, query.root_id),
            ).fetchall()
        return connection.execute(f"SELECT {_BLOCK_COLUMNS} FROM blocks ORDER BY storage_order").fetchall()


def _qualified_columns(table: str, columns: str) -> str:
    """Add the table name to each column of a constant comma-separated column list, for joined queries."""

    return ", ".join(f"{table}.{column.strip()}" for column in columns.replace("\n", " ").split(",") if column.strip())


def _block_values(block: MemoryBlock) -> tuple[Any, ...]:
    """Serialize a block into its SQLite column values."""

    return (
        block.id,
        block.parent_id,
        block.type,
        _dump_json(block.value, field_name="block value"),
        block.content_type,
        _dump_json(block.metadata, field_name="block metadata"),
        _encode_policy(block.policy),
        _encode_datetime(block.created_at, field_name="created_at"),
        _encode_datetime(block.updated_at, field_name="updated_at"),
        block.created_by,
        block.version,
    )


def _block_from_row(row: sqlite3.Row) -> MemoryBlock:
    """Reconstruct a memory block from one SQLite row."""

    try:
        return MemoryBlock(
            id=str(row["id"]),
            parent_id=str(row["parent_id"]) if row["parent_id"] is not None else None,
            type=str(row["type"]),
            value=_load_json(row["value_json"]),
            content_type=str(row["content_type"]),
            metadata=_load_json(row["metadata_json"]),
            policy=_decode_policy(row["policy_json"]),
            created_at=_decode_datetime(row["created_at"]),
            updated_at=_decode_datetime(row["updated_at"]),
            created_by=str(row["created_by"]) if row["created_by"] is not None else None,
            version=int(row["version"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored SQLite memory block is invalid.") from error


def _link_from_row(row: sqlite3.Row) -> MemoryLink:
    """Reconstruct a typed link from one SQLite row."""

    try:
        return MemoryLink(
            id=str(row["id"]),
            source_id=str(row["source_id"]),
            target_id=str(row["target_id"]),
            relation=str(row["relation"]),
            metadata=_load_json(row["metadata_json"]),
            created_at=_decode_datetime(row["created_at"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored SQLite memory link is invalid.") from error


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same stored relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _same_block_identity(left: MemoryBlock, right: MemoryBlock) -> bool:
    """Return whether two blocks are the same stored block, apart from the fields that an update changes."""

    return left.id == right.id and left.type == right.type and left.created_at == right.created_at and left.created_by == right.created_by and left.policy == right.policy


def _validate_block_identity(previous: MemoryBlock, replacement: MemoryBlock) -> None:
    """Raise `MemoryConflict` if a replacement is not made from the stored block."""

    if not _same_block_identity(previous, replacement):
        raise MemoryConflict(f"Memory block {previous.id!r} is not the block that the replacement was made from. A replacement cannot change the type, created_at, created_by, or policy.")


def _matches_snapshot(current: Sequence[MemoryBlock], expected: Sequence[MemoryBlock]) -> bool:
    """Return whether a subtree has exactly the expected blocks, with the same versions and identities."""

    current_by_id = {block.id: block for block in current}
    expected_by_id = {block.id: block for block in expected}
    if len(expected_by_id) != len(expected) or current_by_id.keys() != expected_by_id.keys():
        return False
    return all(block.version == expected_by_id[block.id].version and _same_block_identity(block, expected_by_id[block.id]) for block in current_by_id.values())


def _validate_expected_version(
    previous: MemoryBlock,
    replacement: MemoryBlock,
    expected_version: int | None,
) -> None:
    """Raise `MemoryConflict` if an optional compare-and-swap replacement does not match `expected_version` or does not increase the version by exactly one."""

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version " f"{expected_version}.")
    if replacement.version != expected_version + 1:
        raise MemoryConflict("A conditional block replacement must increment its version by one.")


def _binding_values(binding: KeyBinding) -> tuple[str, str, str, str]:
    """Serialize a keyed binding for insertion."""

    return (
        binding.collection_id,
        _encode_namespace(binding.address.namespace),
        binding.address.key,
        binding.block_id,
    )


def _binding_from_row(row: sqlite3.Row) -> KeyBinding:
    """Reconstruct a keyed binding from one SQLite row."""

    from genie.structures.records import KeyBinding, MemoryAddress

    try:
        return KeyBinding(
            collection_id=str(row["collection_id"]),
            address=MemoryAddress(
                namespace=_decode_namespace(row["namespace_json"]),
                key=str(row["key"]),
            ),
            block_id=str(row["block_id"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored SQLite key binding is invalid.") from error


def _route_entry_from_row(row: sqlite3.Row) -> RouteInfo:
    """Reconstruct a route catalog entry from one SQLite row."""

    from genie.backend.composite.routed import RouteInfo

    try:
        return RouteInfo(block_id=row["block_id"], route=row["route"])
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored SQLite route catalog entry is invalid.") from error


def _log_entry_from_row(row: sqlite3.Row) -> LogEntry:
    """Reconstruct a log entry from one SQLite row."""

    from genie.structures.records import LogEntry

    try:
        return LogEntry(
            log_id=str(row["log_id"]),
            sequence=int(row["sequence"]),
            block_id=str(row["block_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored SQLite log entry is invalid.") from error


def _temporal_values(entry: TemporalEntry) -> tuple[str, str, str | None, str | None, str | None]:
    """Serialize one temporal entry into SQLite values."""

    occurred_at = _encode_datetime(entry.occurred_at, field_name="occurred_at") if entry.occurred_at is not None else None
    valid_at = _encode_datetime(entry.validity.valid_at, field_name="valid_at") if entry.validity is not None else None
    invalid_at = _encode_datetime(entry.validity.invalid_at, field_name="invalid_at") if entry.validity is not None and entry.validity.invalid_at is not None else None
    return entry.collection_id, entry.block_id, occurred_at, valid_at, invalid_at


def _temporal_from_row(row: sqlite3.Row) -> TemporalEntry:
    """Reconstruct one temporal entry from SQLite values."""

    from genie.structures.records import TemporalEntry, ValidityInterval

    try:
        validity = None
        if row["valid_at"] is not None:
            validity = ValidityInterval(
                valid_at=_decode_datetime(row["valid_at"]),
                invalid_at=(_decode_datetime(row["invalid_at"]) if row["invalid_at"] is not None else None),
            )
        return TemporalEntry(
            collection_id=str(row["collection_id"]),
            block_id=str(row["block_id"]),
            occurred_at=(_decode_datetime(row["occurred_at"]) if row["occurred_at"] is not None else None),
            validity=validity,
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored SQLite temporal entry is invalid.") from error


def _encode_policy(policy: MemoryPolicy | None) -> str | None:
    """Serialize a memory policy as strict JSON."""

    if policy is None:
        return None
    payload = {
        "read": _encode_rule(policy.read),
        "write": _encode_rule(policy.write),
        "search": _encode_rule(policy.search),
        "traverse_up": _encode_rule(policy.traverse_up),
        "traverse_down": _encode_rule(policy.traverse_down),
        "operations": {name: _encode_rule(rule) for name, rule in sorted(policy.operations.items())},
    }
    return _dump_json(payload, field_name="memory policy")


def _decode_policy(value: object) -> MemoryPolicy | None:
    """Reconstruct a memory policy from its stored JSON."""

    if value is None:
        return None
    payload = _load_json(value)
    return MemoryPolicy(
        read=_decode_rule(payload["read"]),
        write=_decode_rule(payload["write"]),
        search=_decode_rule(payload["search"]),
        traverse_up=_decode_rule(payload["traverse_up"]),
        traverse_down=_decode_rule(payload["traverse_down"]),
        operations={name: _decode_rule(rule) for name, rule in payload["operations"].items()},
    )


def _encode_rule(rule: AccessRule) -> dict[str, Any]:
    """Return the JSON form of one access rule."""

    return {
        "allow_all": rule.allow_all,
        "principals": sorted(rule.principals),
        "roles": sorted(rule.roles),
        "allow_owner": rule.allow_owner,
    }


def _decode_rule(payload: dict[str, Any]) -> AccessRule:
    """Reconstruct one access rule from stored JSON data."""

    return AccessRule(
        allow_all=payload["allow_all"],
        principals=frozenset(payload["principals"]),
        roles=frozenset(payload["roles"]),
        allow_owner=payload["allow_owner"],
    )


def _dump_json(value: Any, *, field_name: str) -> str:
    """Encode a strict JSON value, with no silent type change, or raise `TypeError`."""

    if not _is_strict_json(value):
        raise TypeError(f"SQLiteBackend {field_name} must be strict JSON data.")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _load_json(value: object) -> Any:
    """Decode one JSON text value into a new Python object, or raise `BackendCorruption`."""

    if not isinstance(value, str):
        raise BackendCorruption("Stored SQLite JSON values must be strings.")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise BackendCorruption("Stored SQLite JSON is invalid.") from error
    if not _is_strict_json(decoded):
        raise BackendCorruption("Stored SQLite JSON is not strict JSON data.")
    return decoded


def _is_strict_json(value: Any) -> bool:
    """Return whether a value round-trips through JSON without type changes."""

    if value is None or isinstance(value, str | bool | int):
        return True
    if isinstance(value, float):
        return math.isfinite(value)
    if isinstance(value, list):
        return all(_is_strict_json(item) for item in value)
    if isinstance(value, dict):
        return all(isinstance(key, str) and _is_strict_json(item) for key, item in value.items())
    return False


def _encode_datetime(value: datetime, *, field_name: str) -> str:
    """Normalize an aware datetime to an ISO 8601 UTC string."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _decode_datetime(value: object) -> datetime:
    """Decode one stored ISO 8601 datetime and normalize it to UTC."""

    if not isinstance(value, str):
        raise BackendCorruption("Stored SQLite timestamps must be strings.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise BackendCorruption("Stored SQLite timestamp is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackendCorruption("Stored SQLite timestamps must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _encode_namespace(namespace: tuple[str, ...]) -> str:
    """Encode a namespace tuple, and keep the segment boundaries."""

    return _dump_json(list(namespace), field_name="key namespace")


def _decode_namespace(value: object) -> tuple[str, ...]:
    """Decode one stored namespace tuple."""

    decoded = _load_json(value)
    if not isinstance(decoded, list) or not all(isinstance(segment, str) for segment in decoded):
        raise BackendCorruption("Stored SQLite key namespaces must be string lists.")
    return tuple(decoded)


def _namespace_matches(
    candidate: tuple[str, ...],
    selected: tuple[str, ...] | None,
    *,
    recursive: bool,
) -> bool:
    """Return whether a namespace matches an exact or prefix selection."""

    if selected is None:
        return True
    if recursive:
        return candidate[: len(selected)] == selected
    return candidate == selected


def _validate_sequence(value: object, *, name: str) -> None:
    """Raise an error if a sequence value is not a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero.")


def _validate_limit(value: object) -> None:
    """Raise an error if a page limit is not a non-negative integer."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer.")
    if value < 0:
        raise ValueError("limit must be greater than or equal to zero.")


def _user_schema_objects(connection: sqlite3.Connection) -> set[str]:
    """Return application-owned SQLite schema object names."""

    rows = connection.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type IN ('table', 'index', 'view', 'trigger')
            AND name NOT LIKE 'sqlite_%'
        """).fetchall()
    return {str(row["name"]) for row in rows}


def _validate_schema(
    connection: sqlite3.Connection,
    *,
    expected_statements: tuple[str, ...],
) -> None:
    """Raise `UnsupportedOperation` if the tables, columns, indexes, foreign keys, or schema SQL do not match the Genie schema exactly."""

    table_rows = connection.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'table' AND name NOT LIKE 'sqlite_%'
        """).fetchall()
    actual_tables = {str(row["name"]) for row in table_rows}
    expected_tables = set(_EXPECTED_TABLE_COLUMNS)
    if actual_tables != expected_tables:
        raise UnsupportedOperation("The Genie SQLite schema has unexpected tables: " f"expected {sorted(expected_tables)!r}, found {sorted(actual_tables)!r}.")

    for table, expected_columns in _EXPECTED_TABLE_COLUMNS.items():
        rows = connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        actual_columns = tuple(str(row["name"]) for row in rows)
        if actual_columns != expected_columns:
            raise UnsupportedOperation(f"The Genie SQLite table {table!r} has incompatible columns: " f"expected {expected_columns!r}, found {actual_columns!r}.")

    index_rows = connection.execute("""
        SELECT name
        FROM sqlite_master
        WHERE type = 'index' AND name NOT LIKE 'sqlite_%'
        """).fetchall()
    actual_indexes = {str(row["name"]) for row in index_rows}
    if actual_indexes != _EXPECTED_INDEXES:
        raise UnsupportedOperation("The Genie SQLite schema has incompatible indexes: " f"expected {sorted(_EXPECTED_INDEXES)!r}, found {sorted(actual_indexes)!r}.")

    for table in _EXPECTED_TABLE_COLUMNS:
        rows = connection.execute(f'PRAGMA foreign_key_list("{table}")').fetchall()
        actual_foreign_keys = {
            (
                str(row["from"]),
                str(row["table"]),
                str(row["to"]),
                str(row["on_delete"]),
            )
            for row in rows
        }
        expected_foreign_keys = _EXPECTED_FOREIGN_KEYS.get(table, set())
        if actual_foreign_keys != expected_foreign_keys:
            raise UnsupportedOperation(f"The Genie SQLite table {table!r} has incompatible foreign keys.")

    sql_rows = connection.execute("""
        SELECT sql
        FROM sqlite_master
        WHERE type IN ('table', 'index')
            AND name NOT LIKE 'sqlite_%'
            AND sql IS NOT NULL
        """).fetchall()
    actual_sql = {_normalize_schema_sql(str(row["sql"])) for row in sql_rows}
    expected_sql = {_normalize_schema_sql(statement) for statement in expected_statements}
    if actual_sql != expected_sql:
        raise UnsupportedOperation("The Genie SQLite schema definitions do not match the supported version.")


def _normalize_schema_sql(statement: str) -> str:
    """Normalize schema SQL for deterministic compatibility validation."""

    without_optional_clause = statement.replace("IF NOT EXISTS", "")
    return " ".join(without_optional_clause.casefold().split())


def _rollback_quietly(connection: sqlite3.Connection) -> None:
    """Roll back, and do not hide the original failure of the operation."""

    if not connection.in_transaction:
        return
    try:
        connection.rollback()
    except sqlite3.Error:
        pass


def _is_busy_error(error: sqlite3.Error) -> bool:
    """Return whether a driver error reports a lock that another connection holds."""

    code = getattr(error, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    normalized = (str(error) or "").casefold()
    return primary_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED} or any(text in normalized for text in ("database is locked", "database table is locked"))


def _translate_sqlite_error(error: sqlite3.Error) -> BackendError:
    """Translate driver-specific failures into stable Genie backend errors."""

    code = getattr(error, "sqlite_errorcode", None)
    primary_code = code & 0xFF if isinstance(code, int) else None
    message = str(error) or error.__class__.__name__
    normalized = message.casefold()

    if _is_busy_error(error):
        return BackendBusy(f"SQLite remained busy past the configured timeout: {message}")
    if primary_code in {sqlite3.SQLITE_READONLY, sqlite3.SQLITE_PERM} or "readonly" in normalized:
        return BackendReadOnly(f"SQLite storage is read-only: {message}")
    if primary_code in {sqlite3.SQLITE_FULL, sqlite3.SQLITE_NOMEM, sqlite3.SQLITE_TOOBIG} or any(text in normalized for text in ("database or disk is full", "out of memory", "too big")):
        return BackendCapacityExceeded(f"SQLite storage capacity was exceeded: {message}")
    if primary_code in {sqlite3.SQLITE_CORRUPT, sqlite3.SQLITE_NOTADB} or any(text in normalized for text in ("database disk image is malformed", "not a database")):
        return BackendCorruption(f"SQLite storage is corrupt or invalid: {message}")
    unavailable_codes = {
        sqlite3.SQLITE_CANTOPEN,
        sqlite3.SQLITE_IOERR,
        sqlite3.SQLITE_PROTOCOL,
    }
    if primary_code in unavailable_codes or any(text in normalized for text in ("unable to open database", "disk i/o error")):
        return BackendUnavailable(f"SQLite storage is unavailable: {message}")
    return BackendError(f"SQLite backend operation failed: {message}")


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Check an optional timezone-aware datetime, and return it in UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)
