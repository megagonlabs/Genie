"""Persistent PostgreSQL backend that implements all Genie storage capabilities.

- **Same layout as `SQLiteBackend`:** tables for blocks and links, and tables
  for the optional structure capabilities. Values, metadata, policies, key
  namespaces, and timestamps use the same portable encodings. So the 2
  relational backends store the same data in the same shape.

3 PostgreSQL details are on purpose:

- **Advisory lock for each write.** Each write takes one transaction-scoped
  advisory lock before it reads or changes Genie state. PostgreSQL allows
  concurrent writers. Without the lock, a compound "read, check, then write"
  step could interleave with another writer. The lock gives the
  serialized-writer model that `SQLiteBackend` gets from `BEGIN IMMEDIATE` and
  `Neo4jBackend` gets from its guard node.
- **`COLLATE "C"` on each text column.** Otherwise, PostgreSQL compares text
  with the database collation. That collation depends on the server locale,
  can ignore punctuation, and can change when the operating system upgrades its
  locale data. Genie needs plain byte order, because it compares stored
  timestamps as strings, and paging uses `block_id > ...`.
- **JSON stored as `text`, not `jsonb`.** `jsonb` normalizes what it stores,
  and it rejects the NUL character inside strings. So it would not round-trip
  Genie values byte for byte.
"""

from __future__ import annotations

import json
import math
import re
import zlib
from collections import defaultdict, deque
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import RLock
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
    GenieError,
    MemoryConflict,
    UnsupportedOperation,
)
from genie.memory.links import MemoryLink
from genie.policy.policy import AccessRule, MemoryPolicy
from genie.query.filters import matches_filters
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
_APPLICATION_ID = "genie"
_ADVISORY_LOCK_CLASS = 0x47454E49
_SCHEMA_NAME_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_MAX_IDENTIFIER_BYTES = 63

_BLOCK_COLUMNS = """
    id, parent_id, "type", value_json, content_type, metadata_json, policy_json,
    created_at, updated_at, created_by, "version"
"""
_LINK_COLUMNS = """
    id, source_id, target_id, relation, metadata_json, created_at
"""
_TEMPORAL_COLUMNS = """
    collection_id, block_id, occurred_at, valid_at, invalid_at
"""

_EXPECTED_TABLE_COLUMNS: dict[str, tuple[tuple[str, str, bool], ...]] = {
    "acyclic_relations": (("relation", "text", False),),
    "blocks": (
        ("storage_order", "bigint", False),
        ("id", "text", False),
        ("parent_id", "text", True),
        ("type", "text", False),
        ("value_json", "text", False),
        ("content_type", "text", False),
        ("metadata_json", "text", False),
        ("policy_json", "text", True),
        ("created_at", "text", False),
        ("updated_at", "text", False),
        ("created_by", "text", True),
        ("version", "bigint", False),
    ),
    "key_bindings": (
        ("collection_id", "text", False),
        ("namespace_json", "text", False),
        ("key", "text", False),
        ("block_id", "text", False),
    ),
    "links": (
        ("storage_order", "bigint", False),
        ("id", "text", False),
        ("source_id", "text", False),
        ("target_id", "text", False),
        ("relation", "text", False),
        ("metadata_json", "text", False),
        ("created_at", "text", False),
    ),
    "log_counters": (
        ("log_id", "text", False),
        ("next_sequence", "bigint", False),
    ),
    "log_entries": (
        ("log_id", "text", False),
        ("sequence", "bigint", False),
        ("block_id", "text", False),
    ),
    "route_entries": (
        ("block_id", "text", False),
        ("route", "text", False),
    ),
    "schema_info": (
        ("name", "text", False),
        ("application_id", "text", False),
        ("version", "bigint", False),
    ),
    "temporal_entries": (
        ("collection_id", "text", False),
        ("block_id", "text", False),
        ("occurred_at", "text", True),
        ("valid_at", "text", True),
        ("invalid_at", "text", True),
    ),
}

# PostgreSQL creates one index for each primary key and unique constraint. The
# schema gives those constraints explicit names, so the index names below stay
# the same on all server versions.
_EXPECTED_INDEXES = {
    "acyclic_relations_pkey",
    "blocks_id_key",
    "blocks_parent_idx",
    "blocks_pkey",
    "blocks_type_idx",
    "key_bindings_block_idx",
    "key_bindings_pkey",
    "links_id_key",
    "links_pkey",
    "links_relation_idx",
    "links_source_idx",
    "links_target_idx",
    "log_counters_pkey",
    "log_entries_block_idx",
    "log_entries_pkey",
    "route_entries_pkey",
    "schema_info_pkey",
    "temporal_entries_pkey",
    "temporal_occurred_idx",
    "temporal_validity_idx",
}

_EXPECTED_FOREIGN_KEYS: dict[str, set[tuple[str, str, str, str]]] = {
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

_FOREIGN_KEY_ACTIONS = {"a": "NO ACTION", "r": "RESTRICT", "c": "CASCADE", "n": "SET NULL", "d": "SET DEFAULT"}

_FOREIGN_KEY_QUERY = """
    SELECT
        source_table.relname AS table_name,
        source_column.attname AS column_name,
        target_table.relname AS foreign_table_name,
        target_column.attname AS foreign_column_name,
        constraint_record.confdeltype AS delete_action
    FROM pg_constraint AS constraint_record
    JOIN pg_class AS source_table ON source_table.oid = constraint_record.conrelid
    JOIN pg_namespace AS source_namespace ON source_namespace.oid = source_table.relnamespace
    JOIN pg_class AS target_table ON target_table.oid = constraint_record.confrelid
    JOIN LATERAL unnest(constraint_record.conkey) WITH ORDINALITY AS source_key(attnum, key_position) ON TRUE
    JOIN LATERAL unnest(constraint_record.confkey) WITH ORDINALITY AS target_key(attnum, key_position)
        ON source_key.key_position = target_key.key_position
    JOIN pg_attribute AS source_column
        ON source_column.attrelid = source_table.oid AND source_column.attnum = source_key.attnum
    JOIN pg_attribute AS target_column
        ON target_column.attrelid = target_table.oid AND target_column.attnum = target_key.attnum
    WHERE constraint_record.contype = 'f' AND source_namespace.nspname = %s
"""


class PostgresBackend:
    """Persistent backend on the official `psycopg` PostgreSQL driver.

    - **Optional driver:** the `psycopg` package is an optional dependency.
      `pip install 'genie[postgres]'` installs it. The constructor raises
      `UnsupportedOperation` without it.
    - **Capabilities:** the backend implements the required `MemoryBackend`
      behavior and all optional capabilities.
    - **One connection:** each instance owns one connection. An internal
      reentrant lock protects it.
    - **Several instances:** a transaction-scoped advisory lock serializes the
      writes of separate backend instances and separate processes on the same
      schema.
    - **One schema:** all Genie tables are in one PostgreSQL schema, with the
      default name `"genie"`. Several Genie datasets can share one database
      with different schema names.

    Attributes:
        conninfo: PostgreSQL connection string that this backend opened.
        schema: PostgreSQL schema that holds all Genie tables.
        timeout: Seconds that a statement waits for a lock that another writer
            holds.
        connect_timeout: Seconds for the connection setup.
        durability: Durability mode in use, `"full"` or `"normal"`. It sets
            `synchronous_commit` to `on` or `off`.
    """

    def __init__(
        self,
        conninfo: str,
        *,
        schema: str = "genie",
        timeout: float = 5.0,
        connect_timeout: float = 30.0,
        durability: Literal["full", "normal"] = "full",
        verify_server_role: bool = True,
        initialize_schema: bool = True,
    ) -> None:
        """Connect, and create, migrate, or validate the Genie schema.

        `psycopg` connects during this constructor. So a successful
        construction already proves that the server is reachable, and there is
        no separate connectivity flag.

        Args:
            conninfo: PostgreSQL connection string, such as
                `"postgresql://user:password@localhost:5432/app"`.
            schema: PostgreSQL schema that holds all Genie tables. It is a
                plain unquoted identifier: ASCII letters, digits, and
                underscores, with no digit first and at most 63 bytes.
            timeout: Seconds that a statement waits for a lock that another
                writer holds. `0` waits with no limit.
            connect_timeout: Seconds for the connection setup. The driver gets
                only the whole seconds, `int(connect_timeout)`.
            durability: PostgreSQL `synchronous_commit` mode. `"full"` is the
                safe default for production. `"normal"` gives better write
                throughput, but the most recent commits can be lost after a
                server crash.
            verify_server_role: Whether to refuse a connection to a read-only
                replica. The check refuses only when the server reports that it
                is in recovery. So a primary that has replicas stays supported.
            initialize_schema: Whether to create the schema, and the tables
                and indexes that it does not have yet. `False` suits runtime identities that use
                existing data but cannot run DDL.

        Raises:
            TypeError: If an argument has the wrong type.
            ValueError: If `conninfo` is empty, `schema` is not a plain
                identifier, a timeout is negative or not finite, or
                `durability` is not `"full"` or `"normal"`.
            UnsupportedOperation: If the driver is not available, the
                connection goes to a replica, or the stored schema is
                incompatible. With `initialize_schema=False`, also if the
                schema or its record does not exist, or its version is older.
            BackendError: If PostgreSQL cannot be reached or configured.
        """

        _validate_nonempty_string(conninfo, name="conninfo")
        _validate_schema_name(schema)
        _validate_timeout(timeout, name="timeout")
        _validate_timeout(connect_timeout, name="connect_timeout")
        if not isinstance(durability, str):
            raise TypeError("durability must be a string.")
        if durability not in {"full", "normal"}:
            raise ValueError("durability must be either 'full' or 'normal'.")
        if not isinstance(verify_server_role, bool):
            raise TypeError("verify_server_role must be a boolean.")
        if not isinstance(initialize_schema, bool):
            raise TypeError("initialize_schema must be a boolean.")

        driver, dict_row, exception_types, integrity_errors = _load_psycopg()
        self.conninfo = conninfo
        self.schema = schema
        self.timeout = float(timeout)
        self.connect_timeout = float(connect_timeout)
        self.durability = durability
        self._driver = driver
        self._exception_types = exception_types
        self._integrity_errors = integrity_errors
        self._lock = RLock()
        self._connection: Any | None = None

        # The code above checks that `schema` is a plain identifier, so the
        # quotes here cannot inject SQL. Table names are constant.
        quoted_schema = f'"{schema}"'
        self._blocks = f"{quoted_schema}.blocks"
        self._links = f"{quoted_schema}.links"
        self._acyclic_relations = f"{quoted_schema}.acyclic_relations"
        self._key_bindings = f"{quoted_schema}.key_bindings"
        self._log_counters = f"{quoted_schema}.log_counters"
        self._log_entries = f"{quoted_schema}.log_entries"
        self._temporal_entries = f"{quoted_schema}.temporal_entries"
        self._route_entries = f"{quoted_schema}.route_entries"
        self._schema_info = f"{quoted_schema}.schema_info"
        self._advisory_lock_key = _advisory_lock_key(schema)

        try:
            connection = driver.connect(
                conninfo,
                autocommit=True,
                connect_timeout=int(self.connect_timeout),
                row_factory=dict_row,
            )
            self._connection = connection
            self._configure_connection()
            if verify_server_role:
                self._verify_server_role()
            if initialize_schema:
                self._initialize_schema()
            else:
                self._validate_schema()
        except GenieError:
            self._discard_connection()
            raise
        except exception_types as error:
            self._discard_connection()
            raise _translate_postgres_error(error) from error
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
        """Close the owned PostgreSQL connection.

        A second `close` call is safe. Operations after `close` raise
        `RuntimeError`.

        Raises:
            BackendError: If PostgreSQL fails during the close.
        """

        with self._lock:
            connection = self._connection
            if connection is None:
                return
            try:
                connection.close()
            except self._exception_types as error:
                raise _translate_postgres_error(error) from error
            finally:
                self._connection = None

    # ------------------------------------------------------------------
    # Core block storage and ownership traversal

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store a new encoded memory block.

        Args:
            block: Block to create.

        Returns:
            Stored block, read back from PostgreSQL.

        Raises:
            MemoryConflict: If the id already exists, or the parent does not
                exist or is the block itself.
            TypeError: If the encoded value or metadata is not strict JSON.
        """

        values = _block_values(block)
        with self._write_transaction() as connection:
            if self._block_row(connection, block.id) is not None:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            self._validate_parent(connection, block.id, block.parent_id)
            try:
                connection.execute(
                    f"""
                    INSERT INTO {self._blocks} (
                        id, parent_id, "type", value_json, content_type,
                        metadata_json, policy_json, created_at, updated_at,
                        created_by, "version"
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    values,
                )
            except self._integrity_errors as error:
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

        with self._read_transaction() as connection:
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
            Stored replacement, read back from PostgreSQL.

        Raises:
            MemoryConflict: If the block does not exist, its version changed,
                or the new parent does not exist, is the block itself, or is one
                of its descendants. With `expected_version`, also if the
                replacement does not increase the version by exactly one.
            TypeError: If the encoded value or metadata is not strict JSON.
        """

        with self._write_transaction() as connection:
            previous_row = self._block_row(connection, block.id)
            if previous_row is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            previous = _block_from_row(previous_row)
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._validate_parent(connection, block.id, block.parent_id)
                if block.parent_id is not None and self._contains(connection, block.id, block.parent_id):
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
                    version_clause = ' AND "version" = %s'
                    parameters = (*parameters, expected_version)
                cursor = connection.execute(
                    f"""
                    UPDATE {self._blocks}
                    SET parent_id = %s, "type" = %s, value_json = %s, content_type = %s,
                        metadata_json = %s, policy_json = %s, created_at = %s,
                        updated_at = %s, created_by = %s, "version" = %s
                    WHERE id = %s
                    """
                    + version_clause,
                    parameters,
                )
                if cursor.rowcount != 1:
                    raise MemoryConflict(f"Memory block {block.id!r} changed before it could be updated.")
            except self._integrity_errors as error:
                raise MemoryConflict(f"Memory block {block.id!r} could not be updated.") from error
            row = self._block_row(connection, block.id)
            assert row is not None
            return _block_from_row(row)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a subtree, optionally from an exact version snapshot.

        In the same transaction, cascading foreign keys remove the links, key
        bindings, log entries, and temporal entries of the deleted blocks.

        Args:
            block_id: Root block id to delete. If the id does not exist and
                `expected_versions` is empty or `None`, nothing happens.
            expected_versions: Exact snapshot of the subtree, as block id to
                version, that the delete requires.

        Raises:
            MemoryConflict: If the given subtree snapshot is stale.
        """

        with self._write_transaction() as connection:
            rows = self._descendant_rows(connection, block_id, include_self=True)
            current = {str(row["id"]): int(row["version"]) for row in rows}
            if expected_versions is not None and current != dict(expected_versions):
                raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            if not current:
                return
            connection.execute(
                f"DELETE FROM {self._blocks} WHERE id = ANY(%s)",
                (list(current),),
            )

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children in creation order.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks in creation order.
        """

        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT {_BLOCK_COLUMNS}
                FROM {self._blocks}
                WHERE parent_id = %s
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

        with self._read_transaction() as connection:
            return [_block_from_row(row) for row in self._ancestor_rows(connection, block_id)]

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

        with self._read_transaction() as connection:
            return [_block_from_row(row) for row in self._descendant_rows(connection, block_id, include_self=include_self)]

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is `root_id` or is below it.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is the root or one of its descendants.
        """

        with self._read_transaction() as connection:
            return self._contains(connection, root_id, block_id)

    # ------------------------------------------------------------------
    # Keyed collection

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
                    f"""
                    INSERT INTO {self._key_bindings} (
                        collection_id, namespace_json, "key", block_id
                    ) VALUES (%s, %s, %s, %s)
                    """,
                    _binding_values(binding),
                )
            except self._integrity_errors as error:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in collection {binding.collection_id!r}.") from error
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
                f"""
                UPDATE {self._key_bindings}
                SET block_id = %s
                WHERE collection_id = %s AND namespace_json = %s AND "key" = %s
                    AND block_id = %s
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
                raise MemoryConflict(f"Key address {binding.address!r} in collection {binding.collection_id!r} no longer points to expected block {expected_block_id!r}.")
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

        with self._read_transaction() as connection:
            row = connection.execute(
                f"""
                SELECT collection_id, namespace_json, "key", block_id
                FROM {self._key_bindings}
                WHERE collection_id = %s AND namespace_json = %s AND "key" = %s
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

        - **Python filter:** namespaces are stored as JSON text. So each call
          reads all bindings of the collection, and the namespace filter and
          the address order run in Python.
        - **Same result:** `SQLiteBackend` also filters and sorts in Python, so
          both relational backends give the same result.

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
        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT collection_id, namespace_json, "key", block_id
                FROM {self._key_bindings}
                WHERE collection_id = %s
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
                f"""
                DELETE FROM {self._key_bindings}
                WHERE collection_id = %s AND namespace_json = %s AND "key" = %s
                    AND block_id = %s
                """,
                (
                    collection_id,
                    _encode_namespace(address.namespace),
                    address.key,
                    expected_block_id,
                ),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} no longer points to expected block {expected_block_id!r}.")

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove the bindings that one deleted block owns, and the bindings that point to it.

        Args:
            block_id: Id of the deleted block. The call removes the bindings
                whose collection id or target block id matches.
        """

        with self._write_transaction() as connection:
            connection.execute(
                f"""
                DELETE FROM {self._key_bindings}
                WHERE collection_id = %s OR block_id = %s
                """,
                (block_id, block_id),
            )

    # ------------------------------------------------------------------
    # Ordered event log

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Allocate a sequence and append a block reference, in one atomic step.

        - **One upsert:** one upsert creates or advances the counter row, so
          the allocation needs no separate read.
        - **Counter row, not a sequence:** a PostgreSQL sequence is not
          transactional, so it skips a number when an append rolls back. The
          counter row does not skip numbers.

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
                f"""
                INSERT INTO {self._log_counters} (log_id, next_sequence)
                VALUES (%s, 1)
                ON CONFLICT (log_id) DO UPDATE
                    SET next_sequence = log_counters.next_sequence + 1
                RETURNING next_sequence - 1 AS sequence
                """,
                (log_id,),
            ).fetchone()
            assert row is not None
            sequence = int(row["sequence"])
            try:
                connection.execute(
                    f"""
                    INSERT INTO {self._log_entries} (log_id, "sequence", block_id)
                    VALUES (%s, %s, %s)
                    """,
                    (log_id, sequence, block_id),
                )
            except self._integrity_errors as error:
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
        with self._read_transaction() as connection:
            row = connection.execute(
                f"""
                SELECT log_id, "sequence", block_id
                FROM {self._log_entries}
                WHERE log_id = %s AND "sequence" = %s
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
        clauses = ["log_id = %s"]
        parameters: list[Any] = [log_id]
        if start is not None:
            clauses.append('"sequence" >= %s')
            parameters.append(start)
        if stop is not None:
            clauses.append('"sequence" < %s')
            parameters.append(stop)
        parameters.append(limit)
        direction = "DESC" if reverse else "ASC"
        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT log_id, "sequence", block_id
                FROM {self._log_entries}
                WHERE {" AND ".join(clauses)}
                ORDER BY "sequence" {direction}
                LIMIT %s
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
                f"""
                DELETE FROM {self._log_entries}
                WHERE log_id = %s OR block_id = %s
                """,
                (block_id, block_id),
            )
            connection.execute(
                f"DELETE FROM {self._log_counters} WHERE log_id = %s",
                (block_id,),
            )

    # ------------------------------------------------------------------
    # Temporal collection and index

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
                    f"""
                    INSERT INTO {self._temporal_entries} (
                        collection_id, block_id, occurred_at, valid_at, invalid_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    """,
                    _temporal_values(entry),
                )
            except self._integrity_errors as error:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal collection {entry.collection_id!r}.") from error
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
                f"""
                UPDATE {self._temporal_entries}
                SET occurred_at = %s, valid_at = %s, invalid_at = %s
                WHERE collection_id = %s AND block_id = %s
                """,
                (values[2], values[3], values[4], values[0], values[1]),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal collection {entry.collection_id!r}.")
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

        with self._read_transaction() as connection:
            row = connection.execute(
                f"""
                SELECT {_TEMPORAL_COLUMNS}
                FROM {self._temporal_entries}
                WHERE collection_id = %s AND block_id = %s
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
        clauses = ["collection_id = %s"]
        parameters: list[Any] = [collection_id]
        if after_block_id is not None:
            clauses.append("block_id > %s")
            parameters.append(after_block_id)
        parameters.append(limit)
        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT {_TEMPORAL_COLUMNS}
                FROM {self._temporal_entries}
                WHERE {" AND ".join(clauses)}
                ORDER BY block_id
                LIMIT %s
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
                f"""
                DELETE FROM {self._temporal_entries}
                WHERE collection_id = %s AND block_id = %s
                """,
                (collection_id, block_id),
            )
            if cursor.rowcount != 1:
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection {collection_id!r}.")

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove the temporal entries that one deleted block owns, and the entries that point to it.

        Args:
            block_id: Id of the deleted block. The call removes the entries
                whose collection id or target block id matches.
        """

        with self._write_transaction() as connection:
            connection.execute(
                f"""
                DELETE FROM {self._temporal_entries}
                WHERE collection_id = %s OR block_id = %s
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
        occurred_start = _normalize_optional_time(occurred_start, name="occurred_start")
        occurred_stop = _normalize_optional_time(occurred_stop, name="occurred_stop")
        valid_at = _normalize_optional_time(valid_at, name="valid_at")
        if (occurred_start is not None and occurred_stop is not None) and occurred_stop < occurred_start:
            raise ValueError("occurred_stop must be greater than or equal to occurred_start.")
        if overlaps is not None and not isinstance(overlaps, ValidityInterval):
            raise TypeError("overlaps must be a ValidityInterval.")
        _validate_limit(limit)

        clauses = ["collection_id = %s"]
        parameters: list[Any] = [collection_id]
        if after_block_id is not None:
            clauses.append("block_id > %s")
            parameters.append(after_block_id)
        if occurred_only:
            clauses.append("occurred_at IS NOT NULL")
        if occurred_start is not None:
            clauses.append("occurred_at >= %s")
            parameters.append(_encode_datetime(occurred_start, field_name="occurred_start"))
        if occurred_stop is not None:
            clauses.append("occurred_at < %s")
            parameters.append(_encode_datetime(occurred_stop, field_name="occurred_stop"))
        if valid_at is not None:
            encoded_valid_at = _encode_datetime(valid_at, field_name="valid_at")
            clauses.extend(
                [
                    "valid_at IS NOT NULL",
                    "valid_at <= %s",
                    "(invalid_at IS NULL OR invalid_at > %s)",
                ]
            )
            parameters.extend([encoded_valid_at, encoded_valid_at])
        if overlaps is not None:
            clauses.extend(
                [
                    "valid_at IS NOT NULL",
                    "(invalid_at IS NULL OR invalid_at > %s)",
                ]
            )
            parameters.append(_encode_datetime(overlaps.valid_at, field_name="overlaps.valid_at"))
            if overlaps.invalid_at is not None:
                clauses.append("valid_at < %s")
                parameters.append(_encode_datetime(overlaps.invalid_at, field_name="overlaps.invalid_at"))
        parameters.append(limit)
        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT {_TEMPORAL_COLUMNS}
                FROM {self._temporal_entries}
                WHERE {" AND ".join(clauses)}
                ORDER BY block_id
                LIMIT %s
                """,
                parameters,
            ).fetchall()
            return [_temporal_from_row(row) for row in rows]

    # ------------------------------------------------------------------
    # Route catalog

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
                    f"INSERT INTO {self._route_entries} (block_id, route) VALUES (%s, %s)",
                    (entry.block_id, entry.route),
                )
            except self._integrity_errors as error:
                raise MemoryConflict(f"Block id {entry.block_id!r} already has a route catalog entry.") from error
            return entry

    def get_route(self, block_id: str) -> RouteInfo | None:
        """Return the route catalog entry of a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Stored entry, or `None` when the block id has no entry.
        """

        with self._read_transaction() as connection:
            row = connection.execute(
                f"SELECT block_id, route FROM {self._route_entries} WHERE block_id = %s",
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
            connection.execute(
                f"DELETE FROM {self._route_entries} WHERE block_id = ANY(%s)",
                (list(block_ids),),
            )

    # ------------------------------------------------------------------
    # Typed links and lineage DAG

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
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic graph; use atomic acyclic insertion.")
            self._validate_new_link(connection, link)
            return self._insert_link(connection, link)

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Add a link in one atomic step, unless the link makes a directed cycle.

        - **First call:** the current links with the same relation stay. The
          first call accepts them only if they already form a DAG.
        - **Protection:** after a successful call, a plain `add_link` with the
          relation raises `MemoryConflict`.

        Args:
            link: Link to store. Its relation identifies the protected graph of
                the cycle check.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, the relation already contains a cycle, or the new link
                makes one. A self-link is a cycle.
        """

        with self._write_transaction() as connection:
            self._validate_new_link(connection, link)
            if not self._relation_is_protected(connection, link.relation) and self._relation_has_cycle(connection, link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} already contains a cycle.")
            if link.source_id == link.target_id or self._link_path_exists(
                connection,
                link.target_id,
                link.source_id,
                relation=link.relation,
            ):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create a cycle in relation {link.relation!r}.")
            connection.execute(
                f"""
                INSERT INTO {self._acyclic_relations} (relation)
                VALUES (%s)
                ON CONFLICT (relation) DO NOTHING
                """,
                (link.relation,),
            )
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
                f"""
                UPDATE {self._links}
                SET metadata_json = %s
                WHERE id = %s
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
            endpoint_clause = "source_id = %s"
            parameters: list[Any] = [block_id]
        elif direction == "in":
            endpoint_clause = "target_id = %s"
            parameters = [block_id]
        else:
            endpoint_clause = "(source_id = %s OR target_id = %s)"
            parameters = [block_id, block_id]
        relation_clause = ""
        if relation is not None:
            relation_clause = " AND relation = %s"
            parameters.append(relation)
        with self._read_transaction() as connection:
            rows = connection.execute(
                f"""
                SELECT {_LINK_COLUMNS}
                FROM {self._links}
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
                f"""
                DELETE FROM {self._links}
                WHERE source_id = %s OR target_id = %s
                """,
                (block_id, block_id),
            )

    # ------------------------------------------------------------------
    # Portable search

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks with the portable matching behavior of the in-memory backend.

        - **SQL step:** SQL selects the candidate rows by scope only.
        - **Python step:** Python then matches the types, filters, and text. So
          the results stay the same as in the reference backend.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results in descending score order, at most `query.limit`
            results.
        """

        with self._read_transaction() as connection:
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
        results.sort(key=lambda result: result.score, reverse=True)
        return results[: query.limit]

    # ------------------------------------------------------------------
    # Connection, schema, and transaction helpers

    def _configure_connection(self) -> None:
        """Apply the session settings that the backend needs.

        Both settings are ordinary session parameters. So PostgreSQL raises an
        error for a value that is not valid, and does not ignore it. The
        backend does not read them back.
        """

        connection = self._require_connection()
        connection.execute(
            "SELECT set_config('lock_timeout', %s, false)",
            (f"{int(self.timeout * 1000)}ms",),
        )
        connection.execute(
            "SELECT set_config('synchronous_commit', %s, false)",
            ("on" if self.durability == "full" else "off",),
        )

    def _verify_server_role(self) -> None:
        """Refuse a connection to a read-only replica.

        - **Why:** a replica refuses all writes, so a backend on a replica fails
          on the first `put_block`.
        - **How:** the check reads `pg_is_in_recovery()`. This function needs
          the fewest privileges, and managed services keep it available.
        - **Refusal only on proof:** a rejected function call, a missing row,
          and an unreadable value all leave the role unknown, and an unknown
          role passes. So the probe never rejects a server that would work. A
          connection failure still gets to the caller.
        - **One read:** the role is read once. A failover or a proxy can still
          move this connection to a replica later, and the backend does not
          detect that.

        Raises:
            UnsupportedOperation: If the server reports that it is in recovery.
        """

        connection = self._require_connection()
        try:
            row = connection.execute("SELECT pg_is_in_recovery() AS in_recovery").fetchone()
        except self._exception_types as error:
            if _is_connection_failure(error):
                raise
            # The function was renamed, restricted, or is not permitted. The
            # role stays unknown, and unknown passes.
            return
        if row is None:
            return
        if row.get("in_recovery") is not True:
            return
        raise UnsupportedOperation(
            "PostgreSQL reports that this server is in recovery, so this connection lands on a read-only replica. A replica refuses "
            "every write. Point conninfo at the primary, or pass verify_server_role=False to accept failing writes and stale reads."
        )

    def _initialize_schema(self) -> None:
        """Create the schema namespace, run migrations, then validate.

        - **One locked transaction:** the namespace creation, the record read,
          the unowned-table check, and the migration run in one transaction
          under the advisory lock.
        - **Concurrent constructors:** a constructor that starts at the same
          time waits for the lock. Then it reads the record that the first
          constructor committed. A read outside the lock could miss the record,
          and then see the tables of that constructor.
        """

        with self._write_transaction() as transaction:
            transaction.execute(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"')
            state = self._read_schema_identity(transaction)
            version = 0 if state is None else self._schema_version(state, allow_older=True)
            if state is None and self._namespace_tables(transaction) - {"schema_info"}:
                raise UnsupportedOperation(f"The PostgreSQL schema {self.schema!r} holds tables with no Genie schema record; use an empty schema or a validated Genie schema.")
            for target_version in range(version + 1, _SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(target_version)
                if statements is None:
                    raise UnsupportedOperation(f"No PostgreSQL migration is available for version {target_version}.")
                for statement in statements:
                    transaction.execute(statement.format(schema=f'"{self.schema}"'))
                transaction.execute(
                    f"""
                    INSERT INTO {self._schema_info} (name, application_id, "version")
                    VALUES ('genie', %s, %s)
                    ON CONFLICT (name) DO UPDATE
                        SET "version" = EXCLUDED."version"
                        WHERE schema_info.application_id = EXCLUDED.application_id
                    """,
                    (_APPLICATION_ID, target_version),
                )
        self._validate_schema()

    def _validate_schema(self) -> None:
        """Raise `UnsupportedOperation` if the schema or its record does not exist or does not match, or the owned objects are not exact."""

        connection = self._require_connection()
        if not self._namespace_exists(connection):
            raise UnsupportedOperation(f"The Genie PostgreSQL schema {self.schema!r} is not initialized.")
        state = self._read_schema_identity(connection)
        if state is None:
            raise UnsupportedOperation(f"The Genie PostgreSQL schema {self.schema!r} is not initialized.")
        self._schema_version(state, allow_older=False)
        self._validate_schema_objects(connection)

    def _namespace_exists(self, connection: Any) -> bool:
        """Return whether the configured schema namespace exists."""

        row = connection.execute(
            "SELECT 1 AS present FROM information_schema.schemata WHERE schema_name = %s",
            (self.schema,),
        ).fetchone()
        return row is not None

    def _namespace_tables(self, connection: Any) -> set[str]:
        """Return all base table names inside the configured schema."""

        rows = connection.execute(
            """
            SELECT table_name
            FROM information_schema.tables
            WHERE table_schema = %s AND table_type = 'BASE TABLE'
            """,
            (self.schema,),
        ).fetchall()
        return {str(row["table_name"]) for row in rows}

    def _read_schema_identity(self, connection: Any) -> dict[str, Any] | None:
        """Return the stored Genie schema record, or `None` when absent."""

        if "schema_info" not in self._namespace_tables(connection):
            return None
        return connection.execute(
            f"""
            SELECT application_id, "version"
            FROM {self._schema_info}
            WHERE name = 'genie'
            """
        ).fetchone()

    def _schema_version(self, state: Mapping[str, Any], *, allow_older: bool) -> int:
        """Check the schema owner and version, and return the version, or raise `UnsupportedOperation`."""

        application_id = state.get("application_id")
        version = state.get("version")
        if application_id != _APPLICATION_ID:
            raise UnsupportedOperation(f"The PostgreSQL schema {self.schema!r} belongs to another application ({application_id!r}).")
        if isinstance(version, bool) or not isinstance(version, int):
            raise UnsupportedOperation("The Genie PostgreSQL schema version is invalid.")
        if version > _SCHEMA_VERSION:
            raise UnsupportedOperation(f"PostgreSQL schema version {version} is newer than supported version {_SCHEMA_VERSION}.")
        if version < _SCHEMA_VERSION and not allow_older:
            raise UnsupportedOperation(f"No PostgreSQL migration is available from schema version {version}.")
        return version

    def _validate_schema_objects(self, connection: Any) -> None:
        """Raise `UnsupportedOperation` if the tables, columns, indexes, or foreign keys do not match the Genie schema exactly."""

        actual_tables = self._namespace_tables(connection)
        expected_tables = set(_EXPECTED_TABLE_COLUMNS)
        if actual_tables != expected_tables:
            raise UnsupportedOperation(f"The Genie PostgreSQL schema has unexpected tables: expected {sorted(expected_tables)!r}, found {sorted(actual_tables)!r}.")

        column_rows = connection.execute(
            """
            SELECT table_name, column_name, data_type, is_nullable
            FROM information_schema.columns
            WHERE table_schema = %s
            ORDER BY table_name, ordinal_position
            """,
            (self.schema,),
        ).fetchall()
        actual_columns: dict[str, list[tuple[str, str, bool]]] = defaultdict(list)
        for row in column_rows:
            actual_columns[str(row["table_name"])].append(
                (
                    str(row["column_name"]),
                    str(row["data_type"]),
                    str(row["is_nullable"]).upper() == "YES",
                )
            )
        for table, expected_columns in _EXPECTED_TABLE_COLUMNS.items():
            if tuple(actual_columns[table]) != expected_columns:
                raise UnsupportedOperation(f"The Genie PostgreSQL table {table!r} has incompatible columns: expected {expected_columns!r}, found {tuple(actual_columns[table])!r}.")

        index_rows = connection.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s",
            (self.schema,),
        ).fetchall()
        actual_indexes = {str(row["indexname"]) for row in index_rows}
        if actual_indexes != _EXPECTED_INDEXES:
            raise UnsupportedOperation(f"The Genie PostgreSQL schema has incompatible indexes: expected {sorted(_EXPECTED_INDEXES)!r}, found {sorted(actual_indexes)!r}.")

        foreign_key_rows = connection.execute(_FOREIGN_KEY_QUERY, (self.schema,)).fetchall()
        actual_foreign_keys: dict[str, set[tuple[str, str, str, str]]] = defaultdict(set)
        for row in foreign_key_rows:
            actual_foreign_keys[str(row["table_name"])].add(
                (
                    str(row["column_name"]),
                    str(row["foreign_table_name"]),
                    str(row["foreign_column_name"]),
                    _FOREIGN_KEY_ACTIONS.get(str(row["delete_action"]), str(row["delete_action"])),
                )
            )
        for table in _EXPECTED_TABLE_COLUMNS:
            if actual_foreign_keys[table] != _EXPECTED_FOREIGN_KEYS.get(table, set()):
                raise UnsupportedOperation(f"The Genie PostgreSQL table {table!r} has incompatible foreign keys.")

    @contextmanager
    def _read_transaction(self) -> Generator[Any, None, None]:
        """Yield one repeatable-read, read-only transaction under the instance lock.

        - **Consistent:** a repeatable-read snapshot keeps a read with several
          statements consistent.
        - **No serialization errors:** a read-only transaction at that
          isolation level cannot fail with a serialization error.
        """

        with self._lock:
            connection = self._require_connection()
            try:
                with connection.transaction():
                    connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
                    yield connection
            except GenieError:
                raise
            except self._exception_types as error:
                raise _translate_postgres_error(error) from error

    @contextmanager
    def _write_transaction(self) -> Generator[Any, None, None]:
        """Yield one serialized write transaction and commit it atomically.

        - **Release:** the advisory lock is transaction scoped, so PostgreSQL
          releases it on commit or rollback.
        - **Atomic steps:** it makes each compound "read, check, then write"
          step atomic against other Genie writers of the same schema, in this
          process and in other processes. `SQLiteBackend` gets the same model
          from `BEGIN IMMEDIATE`.
        """

        with self._lock:
            connection = self._require_connection()
            try:
                with connection.transaction():
                    connection.execute(
                        "SELECT pg_advisory_xact_lock(%s, %s)",
                        (_ADVISORY_LOCK_CLASS, self._advisory_lock_key),
                    )
                    yield connection
            except GenieError:
                raise
            except self._exception_types as error:
                raise _translate_postgres_error(error) from error

    def _discard_connection(self) -> None:
        """Clean up after a constructor failure, as far as possible."""

        connection = self._connection
        self._connection = None
        if connection is None:
            return
        try:
            connection.close()
        except self._exception_types:
            pass

    def _require_connection(self) -> Any:
        """Return the open connection, or raise `RuntimeError` after `close`."""

        if self._connection is None:
            raise RuntimeError("PostgresBackend is closed.")
        return self._connection

    # ------------------------------------------------------------------
    # Transaction-local data helpers

    def _block_row(self, connection: Any, block_id: str) -> dict[str, Any] | None:
        """Return one raw block row by id."""

        return connection.execute(
            f"SELECT {_BLOCK_COLUMNS} FROM {self._blocks} WHERE id = %s",
            (block_id,),
        ).fetchone()

    def _link_row(self, connection: Any, link_id: str) -> dict[str, Any] | None:
        """Return one raw link row by id."""

        return connection.execute(
            f"SELECT {_LINK_COLUMNS} FROM {self._links} WHERE id = %s",
            (link_id,),
        ).fetchone()

    def _validate_parent(
        self,
        connection: Any,
        block_id: str,
        parent_id: str | None,
    ) -> None:
        """Raise `MemoryConflict` if the parent id is not valid for a create or a move."""

        if parent_id == block_id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if parent_id is not None and self._block_row(connection, parent_id) is None:
            raise MemoryConflict(f"Parent memory block {parent_id!r} does not exist.")

    def _ancestor_rows(self, connection: Any, block_id: str) -> list[dict[str, Any]]:
        """Return ancestor rows in nearest-parent-first order."""

        return connection.execute(
            f"""
            WITH RECURSIVE chain(id, depth) AS (
                SELECT parent_id, 1
                FROM {self._blocks}
                WHERE id = %s AND parent_id IS NOT NULL
                UNION ALL
                SELECT blocks.parent_id, chain.depth + 1
                FROM {self._blocks} AS blocks
                JOIN chain ON blocks.id = chain.id
                WHERE blocks.parent_id IS NOT NULL
            )
            SELECT {_qualified_columns("blocks", _BLOCK_COLUMNS)}
            FROM chain
            JOIN {self._blocks} AS blocks ON blocks.id = chain.id
            ORDER BY chain.depth
            """,
            (block_id,),
        ).fetchall()

    def _descendant_rows(
        self,
        connection: Any,
        block_id: str,
        *,
        include_self: bool,
    ) -> list[dict[str, Any]]:
        """Return descendant rows in depth-first creation order.

        The recursive term holds the storage order of each block on the path as
        an array. PostgreSQL compares arrays element by element. So an order by
        that path gives depth-first creation order.
        """

        return connection.execute(
            f"""
            WITH RECURSIVE tree(id, path, depth) AS (
                SELECT id, ARRAY[storage_order], 0
                FROM {self._blocks}
                WHERE id = %s
                UNION ALL
                SELECT blocks.id, tree.path || blocks.storage_order, tree.depth + 1
                FROM {self._blocks} AS blocks
                JOIN tree ON blocks.parent_id = tree.id
            )
            SELECT {_qualified_columns("blocks", _BLOCK_COLUMNS)}
            FROM tree
            JOIN {self._blocks} AS blocks ON blocks.id = tree.id
            WHERE (%s::boolean OR tree.depth > 0)
            ORDER BY tree.path
            """,
            (block_id, include_self),
        ).fetchall()

    def _contains(self, connection: Any, root_id: str, block_id: str) -> bool:
        """Check ownership containment with one recursive query."""

        row = connection.execute(
            f"""
            WITH RECURSIVE tree(id) AS (
                SELECT id
                FROM {self._blocks}
                WHERE id = %s
                UNION ALL
                SELECT blocks.id
                FROM {self._blocks} AS blocks
                JOIN tree ON blocks.parent_id = tree.id
            )
            SELECT 1 AS present
            FROM tree
            WHERE id = %s
            LIMIT 1
            """,
            (root_id, block_id),
        ).fetchone()
        return row is not None

    def _require_structure_blocks(
        self,
        connection: Any,
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

    def _validate_new_link(self, connection: Any, link: MemoryLink) -> None:
        """Raise `MemoryConflict` if the link id already exists or an endpoint is not stored."""

        if self._link_row(connection, link.id) is not None:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if self._block_row(connection, link.source_id) is None:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if self._block_row(connection, link.target_id) is None:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _insert_link(self, connection: Any, link: MemoryLink) -> MemoryLink:
        """Insert a checked link inside the transaction of the caller."""

        try:
            connection.execute(
                f"""
                INSERT INTO {self._links} (
                    id, source_id, target_id, relation, metadata_json, created_at
                ) VALUES (%s, %s, %s, %s, %s, %s)
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
        except self._integrity_errors as error:
            raise MemoryConflict(f"Memory link {link.id!r} could not be created.") from error
        row = self._link_row(connection, link.id)
        assert row is not None
        return _link_from_row(row)

    def _relation_is_protected(self, connection: Any, relation: str) -> bool:
        """Return whether a relation is protected as a DAG."""

        return (
            connection.execute(
                f"SELECT 1 AS present FROM {self._acyclic_relations} WHERE relation = %s",
                (relation,),
            ).fetchone()
            is not None
        )

    def _link_path_exists(
        self,
        connection: Any,
        start_id: str,
        target_id: str,
        *,
        relation: str,
    ) -> bool:
        """Return whether one relation has a directed path between 2 blocks."""

        row = connection.execute(
            f"""
            WITH RECURSIVE reachable(id) AS (
                SELECT (%s::text) COLLATE "C"
                UNION
                SELECT links.target_id
                FROM {self._links} AS links
                JOIN reachable ON links.source_id = reachable.id
                WHERE links.relation = %s
            )
            SELECT 1 AS present
            FROM reachable
            WHERE id = %s
            LIMIT 1
            """,
            (start_id, relation, target_id),
        ).fetchone()
        return row is not None

    def _relation_has_cycle(self, connection: Any, relation: str) -> bool:
        """Return whether the stored relation already contains a cycle."""

        rows = connection.execute(
            f"""
            SELECT source_id, target_id
            FROM {self._links}
            WHERE relation = %s
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

    def _scope_candidate_rows(self, connection: Any, query: MemoryQuery) -> list[dict[str, Any]]:
        """Return raw candidate rows for one normalized search scope."""

        if query.scope == Scope.GLOBAL or query.root_id is None:
            return self._all_block_rows(connection)
        if self._block_row(connection, query.root_id) is None:
            return []
        if query.scope == Scope.SELF:
            row = self._block_row(connection, query.root_id)
            return [row] if row is not None else []
        if query.scope == Scope.SUBTREE:
            return self._descendant_rows(connection, query.root_id, include_self=True)
        if query.scope == Scope.ANCESTORS:
            root = self._block_row(connection, query.root_id)
            rows = self._ancestor_rows(connection, query.root_id)
            return ([root] if root is not None else []) + rows
        if query.scope == Scope.LINKS:
            return connection.execute(
                f"""
                SELECT {_BLOCK_COLUMNS}
                FROM {self._blocks}
                WHERE id IN (
                    SELECT source_id
                    FROM {self._links}
                    WHERE source_id = %s OR target_id = %s
                    UNION
                    SELECT target_id
                    FROM {self._links}
                    WHERE source_id = %s OR target_id = %s
                )
                ORDER BY storage_order
                """,
                (query.root_id, query.root_id, query.root_id, query.root_id),
            ).fetchall()
        return self._all_block_rows(connection)

    def _all_block_rows(self, connection: Any) -> list[dict[str, Any]]:
        """Return all stored block rows in storage order."""

        return connection.execute(f"SELECT {_BLOCK_COLUMNS} FROM {self._blocks} ORDER BY storage_order").fetchall()


_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """
        CREATE TABLE IF NOT EXISTS {schema}.schema_info (
            name TEXT COLLATE "C" NOT NULL,
            application_id TEXT COLLATE "C" NOT NULL,
            "version" BIGINT NOT NULL,
            CONSTRAINT schema_info_pkey PRIMARY KEY (name)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS {schema}.blocks (
            storage_order BIGINT GENERATED BY DEFAULT AS IDENTITY,
            id TEXT COLLATE "C" NOT NULL,
            parent_id TEXT COLLATE "C",
            "type" TEXT COLLATE "C" NOT NULL,
            value_json TEXT COLLATE "C" NOT NULL,
            content_type TEXT COLLATE "C" NOT NULL,
            metadata_json TEXT COLLATE "C" NOT NULL,
            policy_json TEXT COLLATE "C",
            created_at TEXT COLLATE "C" NOT NULL,
            updated_at TEXT COLLATE "C" NOT NULL,
            created_by TEXT COLLATE "C",
            "version" BIGINT NOT NULL CHECK ("version" >= 1),
            CONSTRAINT blocks_pkey PRIMARY KEY (storage_order),
            CONSTRAINT blocks_id_key UNIQUE (id),
            CONSTRAINT blocks_parent_not_self CHECK (parent_id IS NULL OR parent_id <> id),
            CONSTRAINT blocks_parent_fkey FOREIGN KEY (parent_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS blocks_parent_idx ON {schema}.blocks(parent_id, storage_order)",
        'CREATE INDEX IF NOT EXISTS blocks_type_idx ON {schema}.blocks("type")',
        """
        CREATE TABLE IF NOT EXISTS {schema}.links (
            storage_order BIGINT GENERATED BY DEFAULT AS IDENTITY,
            id TEXT COLLATE "C" NOT NULL,
            source_id TEXT COLLATE "C" NOT NULL,
            target_id TEXT COLLATE "C" NOT NULL,
            relation TEXT COLLATE "C" NOT NULL,
            metadata_json TEXT COLLATE "C" NOT NULL,
            created_at TEXT COLLATE "C" NOT NULL,
            CONSTRAINT links_pkey PRIMARY KEY (storage_order),
            CONSTRAINT links_id_key UNIQUE (id),
            CONSTRAINT links_source_fkey FOREIGN KEY (source_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE,
            CONSTRAINT links_target_fkey FOREIGN KEY (target_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS links_source_idx ON {schema}.links(source_id, relation, storage_order)",
        "CREATE INDEX IF NOT EXISTS links_target_idx ON {schema}.links(target_id, relation, storage_order)",
        "CREATE INDEX IF NOT EXISTS links_relation_idx ON {schema}.links(relation, source_id, target_id)",
        """
        CREATE TABLE IF NOT EXISTS {schema}.acyclic_relations (
            relation TEXT COLLATE "C" NOT NULL,
            CONSTRAINT acyclic_relations_pkey PRIMARY KEY (relation)
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS {schema}.key_bindings (
            collection_id TEXT COLLATE "C" NOT NULL,
            namespace_json TEXT COLLATE "C" NOT NULL,
            "key" TEXT COLLATE "C" NOT NULL,
            block_id TEXT COLLATE "C" NOT NULL,
            CONSTRAINT key_bindings_pkey PRIMARY KEY (collection_id, namespace_json, "key"),
            CONSTRAINT key_bindings_collection_fkey FOREIGN KEY (collection_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE,
            CONSTRAINT key_bindings_block_fkey FOREIGN KEY (block_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS key_bindings_block_idx ON {schema}.key_bindings(block_id)",
        """
        CREATE TABLE IF NOT EXISTS {schema}.log_counters (
            log_id TEXT COLLATE "C" NOT NULL,
            next_sequence BIGINT NOT NULL CHECK (next_sequence >= 0),
            CONSTRAINT log_counters_pkey PRIMARY KEY (log_id),
            CONSTRAINT log_counters_log_fkey FOREIGN KEY (log_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS {schema}.log_entries (
            log_id TEXT COLLATE "C" NOT NULL,
            "sequence" BIGINT NOT NULL CHECK ("sequence" >= 0),
            block_id TEXT COLLATE "C" NOT NULL,
            CONSTRAINT log_entries_pkey PRIMARY KEY (log_id, "sequence"),
            CONSTRAINT log_entries_log_fkey FOREIGN KEY (log_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE,
            CONSTRAINT log_entries_block_fkey FOREIGN KEY (block_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS log_entries_block_idx ON {schema}.log_entries(block_id)",
        """
        CREATE TABLE IF NOT EXISTS {schema}.temporal_entries (
            collection_id TEXT COLLATE "C" NOT NULL,
            block_id TEXT COLLATE "C" NOT NULL,
            occurred_at TEXT COLLATE "C",
            valid_at TEXT COLLATE "C",
            invalid_at TEXT COLLATE "C",
            CONSTRAINT temporal_entries_pkey PRIMARY KEY (collection_id, block_id),
            CONSTRAINT temporal_entries_time_present CHECK (occurred_at IS NOT NULL OR valid_at IS NOT NULL),
            CONSTRAINT temporal_entries_validity_start CHECK (invalid_at IS NULL OR valid_at IS NOT NULL),
            CONSTRAINT temporal_entries_validity_order CHECK (invalid_at IS NULL OR invalid_at > valid_at),
            CONSTRAINT temporal_entries_collection_fkey FOREIGN KEY (collection_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE,
            CONSTRAINT temporal_entries_block_fkey FOREIGN KEY (block_id)
                REFERENCES {schema}.blocks(id) ON DELETE CASCADE
        )
        """,
        "CREATE INDEX IF NOT EXISTS temporal_occurred_idx ON {schema}.temporal_entries(collection_id, occurred_at, block_id)",
        "CREATE INDEX IF NOT EXISTS temporal_validity_idx ON {schema}.temporal_entries(collection_id, valid_at, invalid_at, block_id)",
        """
        CREATE TABLE IF NOT EXISTS {schema}.route_entries (
            block_id TEXT COLLATE "C" NOT NULL,
            route TEXT COLLATE "C" NOT NULL,
            CONSTRAINT route_entries_pkey PRIMARY KEY (block_id)
        )
        """,
    ),
}


def _qualified_columns(table: str, columns: str) -> str:
    """Add the table name to each column of a constant comma-separated column list, for joined queries."""

    return ", ".join(f"{table}.{column.strip()}" for column in columns.replace("\n", " ").split(",") if column.strip())


def _block_values(block: MemoryBlock) -> tuple[Any, ...]:
    """Serialize a block into its PostgreSQL column values."""

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


def _block_from_row(row: Mapping[str, Any]) -> MemoryBlock:
    """Reconstruct a memory block from one PostgreSQL row."""

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
        raise BackendCorruption("Stored PostgreSQL memory block is invalid.") from error


def _link_from_row(row: Mapping[str, Any]) -> MemoryLink:
    """Reconstruct a typed link from one PostgreSQL row."""

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
        raise BackendCorruption("Stored PostgreSQL memory link is invalid.") from error


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same stored relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _validate_expected_version(
    previous: MemoryBlock,
    replacement: MemoryBlock,
    expected_version: int | None,
) -> None:
    """Raise `MemoryConflict` if an optional compare-and-swap replacement does not match `expected_version` or does not increase the version by exactly one."""

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version {expected_version}.")
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


def _binding_from_row(row: Mapping[str, Any]) -> KeyBinding:
    """Reconstruct a keyed binding from one PostgreSQL row."""

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
        raise BackendCorruption("Stored PostgreSQL key binding is invalid.") from error


def _route_entry_from_row(row: Mapping[str, Any]) -> RouteInfo:
    """Reconstruct a route catalog entry from one PostgreSQL row."""

    from genie.backend.composite.routed import RouteInfo

    try:
        return RouteInfo(block_id=row["block_id"], route=row["route"])
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored PostgreSQL route catalog entry is invalid.") from error


def _log_entry_from_row(row: Mapping[str, Any]) -> LogEntry:
    """Reconstruct a log entry from one PostgreSQL row."""

    from genie.structures.records import LogEntry

    try:
        return LogEntry(
            log_id=str(row["log_id"]),
            sequence=int(row["sequence"]),
            block_id=str(row["block_id"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored PostgreSQL log entry is invalid.") from error


def _temporal_values(entry: TemporalEntry) -> tuple[str, str, str | None, str | None, str | None]:
    """Serialize one temporal entry into PostgreSQL values."""

    occurred_at = _encode_datetime(entry.occurred_at, field_name="occurred_at") if entry.occurred_at is not None else None
    valid_at = _encode_datetime(entry.validity.valid_at, field_name="valid_at") if entry.validity is not None else None
    invalid_at = _encode_datetime(entry.validity.invalid_at, field_name="invalid_at") if entry.validity is not None and entry.validity.invalid_at is not None else None
    return entry.collection_id, entry.block_id, occurred_at, valid_at, invalid_at


def _temporal_from_row(row: Mapping[str, Any]) -> TemporalEntry:
    """Reconstruct one temporal entry from PostgreSQL values."""

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
        raise BackendCorruption("Stored PostgreSQL temporal entry is invalid.") from error


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


def _decode_rule(payload: Mapping[str, Any]) -> AccessRule:
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
        raise TypeError(f"PostgresBackend {field_name} must be strict JSON data.")
    return json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _load_json(value: object) -> Any:
    """Decode one JSON text value into a new Python object, or raise `BackendCorruption`."""

    if not isinstance(value, str):
        raise BackendCorruption("Stored PostgreSQL JSON values must be strings.")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise BackendCorruption("Stored PostgreSQL JSON is invalid.") from error
    if not _is_strict_json(decoded):
        raise BackendCorruption("Stored PostgreSQL JSON is not strict JSON data.")
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
        raise BackendCorruption("Stored PostgreSQL timestamps must be strings.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise BackendCorruption("Stored PostgreSQL timestamp is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackendCorruption("Stored PostgreSQL timestamps must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _encode_namespace(namespace: tuple[str, ...]) -> str:
    """Encode a namespace tuple, and keep the segment boundaries."""

    return _dump_json(list(namespace), field_name="key namespace")


def _decode_namespace(value: object) -> tuple[str, ...]:
    """Decode one stored namespace tuple."""

    decoded = _load_json(value)
    if not isinstance(decoded, list) or not all(isinstance(segment, str) for segment in decoded):
        raise BackendCorruption("Stored PostgreSQL key namespaces must be string lists.")
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


def _advisory_lock_key(schema: str) -> int:
    """Return the stable advisory lock key for one Genie schema.

    - **One key for each schema:** 2 Genie datasets in the same database use
      different schemas, so the key comes from the schema name.
    - **`zlib.crc32`:** the built-in `hash` of Python changes for each process,
      so 2 backends would not agree on it.
    - **Collisions:** 2 schema names with the same CRC-32 share one lock. This
      only serializes more writes.

    Args:
        schema: PostgreSQL schema that holds the Genie tables.

    Returns:
        A signed 32-bit key for `pg_advisory_xact_lock`.
    """

    checksum = zlib.crc32(schema.encode("utf-8"))
    return checksum - 2**32 if checksum >= 2**31 else checksum


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


def _validate_nonempty_string(value: object, *, name: str) -> None:
    """Raise an error if a configuration value is not a non-empty string."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    if not value:
        raise ValueError(f"{name} must not be empty.")


def _validate_timeout(value: object, *, name: str) -> None:
    """Raise an error if a timeout is not a finite, non-negative number of seconds."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number.")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be a finite number greater than or equal to zero.")


def _validate_schema_name(value: object) -> None:
    """Raise an error if the schema is not a plain unquoted PostgreSQL identifier.

    - **Why:** the schema name goes into SQL as a quoted identifier, not as a
      parameter, because PostgreSQL does not accept a parameter where a table
      name goes.
    - **Safe characters:** the name has only ASCII letters, digits, and
      underscores, with no digit first and at most 63 bytes. This keeps the
      quoted name safe.

    Args:
        value: Schema name from the caller.

    Raises:
        TypeError: If the value is not a string.
        ValueError: If the value is empty, is not a plain identifier, or is
            longer than 63 bytes.
    """

    if not isinstance(value, str):
        raise TypeError("schema must be a string.")
    if not value:
        raise ValueError("schema must not be empty.")
    if _SCHEMA_NAME_PATTERN.fullmatch(value) is None:
        raise ValueError("schema must contain only letters, digits, and underscores, and must not start with a digit.")
    if len(value.encode("utf-8")) > _MAX_IDENTIFIER_BYTES:
        raise ValueError(f"schema must be at most {_MAX_IDENTIFIER_BYTES} bytes long.")


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Check an optional timezone-aware datetime, and return it in UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)


def _load_psycopg() -> tuple[Any, Any, tuple[type[BaseException], ...], tuple[type[BaseException], ...]]:
    """Import the optional driver and return the pieces the backend needs.

    Returns:
        The `psycopg` module, its `dict_row` row factory, the exception types
        that the backend translates, and the integrity error types that tell
        that a constraint rejected a write.

    Raises:
        UnsupportedOperation: If `psycopg` is not installed.
    """

    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as error:
        raise UnsupportedOperation("PostgresBackend requires the optional 'psycopg' package; install 'genie[postgres]'.") from error
    return psycopg, dict_row, (psycopg.Error,), (psycopg.IntegrityError,)


def _is_connection_failure(error: BaseException) -> bool:
    """Return whether a driver failure tells that the server cannot be reached: SQLSTATE class `08`, or no SQLSTATE on an `OperationalError` or `InterfaceError`."""

    sqlstate = str(getattr(error, "sqlstate", "") or "")
    if sqlstate.startswith("08"):
        return True
    if sqlstate:
        return False
    # A dropped socket comes as an OperationalError or InterfaceError with no SQLSTATE.
    return type(error).__name__ in {"OperationalError", "InterfaceError"}


def _translate_postgres_error(error: BaseException) -> GenieError:
    """Translate driver-specific failures into stable Genie backend errors.

    PostgreSQL reports a five-character SQLSTATE for each server-side error.
    So the mapping reads that code, and does not match message text.

    Args:
        error: Failure raised by `psycopg`.

    Returns:
        The matching `BackendError` subclass, or `MemoryConflict` when a
        constraint rejected the write.
    """

    sqlstate = str(getattr(error, "sqlstate", "") or "")
    message = str(error) or error.__class__.__name__

    if sqlstate in {"40001", "40P01", "55P03", "57014"}:
        return BackendBusy(f"PostgreSQL remained busy past the configured timeout: {message}")
    if sqlstate in {"25006", "42501"} or sqlstate.startswith("28"):
        return BackendReadOnly(f"PostgreSQL storage is read-only or does not permit this write: {message}")
    if sqlstate in {"53100", "53200", "54000", "54001"}:
        return BackendCapacityExceeded(f"PostgreSQL storage capacity was exceeded: {message}")
    if sqlstate.startswith("XX"):
        return BackendCorruption(f"PostgreSQL storage is corrupt or invalid: {message}")
    if sqlstate.startswith("23"):
        return MemoryConflict(f"PostgreSQL rejected a conflicting memory write: {message}")
    unavailable_codes = {"53300", "57P01", "57P02", "57P03", "58030", "3D000"}
    if sqlstate in unavailable_codes or sqlstate.startswith("08") or (not sqlstate and _is_connection_failure(error)):
        return BackendUnavailable(f"PostgreSQL storage is unavailable: {message}")
    return BackendError(f"PostgreSQL backend operation failed: {message}")
