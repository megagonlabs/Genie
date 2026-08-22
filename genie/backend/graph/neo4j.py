"""Persistent Neo4j backend implementing every Genie storage capability.

The adapter uses only Neo4j Community-compatible uniqueness constraints and
range indexes. All writes acquire one database-resident mutation guard before
reading or changing Genie state. This mirrors SQLite's serialized-writer model
and makes compound checks and writes atomic across backend instances.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from threading import Condition, RLock
from types import TracebackType
from typing import TYPE_CHECKING, Any, Literal, Self, TypeVar

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
    from genie.structures.records import (
        KeyBinding,
        LogEntry,
        MemoryAddress,
        TemporalEntry,
        ValidityInterval,
    )


_T = TypeVar("_T")
_SCHEMA_VERSION = 1
_APPLICATION_ID = "genie"
_MUTATION_GUARD = "genie.mutations"
_SCHEMA_INDEX_TIMEOUT_SECONDS = 300
_UNIQUENESS_CONSTRAINT_TYPES = frozenset({"UNIQUENESS", "NODE_PROPERTY_UNIQUENESS"})

_CONSTRAINTS: dict[str, tuple[str, str]] = {
    "genie_schema_name_unique": ("GenieSchema", "name"),
    "genie_guard_name_unique": ("GenieGuard", "name"),
    "genie_counter_name_unique": ("GenieCounter", "name"),
    "genie_block_id_unique": ("GenieBlock", "id"),
    "genie_link_id_unique": ("GenieLink", "id"),
    "genie_acyclic_relation_unique": ("GenieAcyclicRelation", "relation"),
    "genie_key_identity_unique": ("GenieKeyBinding", "identity"),
    "genie_log_counter_id_unique": ("GenieLogCounter", "log_id"),
    "genie_log_entry_identity_unique": ("GenieLogEntry", "identity"),
    "genie_temporal_identity_unique": ("GenieTemporalEntry", "identity"),
}

_INDEXES: dict[str, tuple[str, tuple[str, ...]]] = {
    "genie_block_storage_order_idx": ("GenieBlock", ("storage_order",)),
    "genie_block_type_idx": ("GenieBlock", ("type",)),
    "genie_link_source_idx": ("GenieLink", ("source_id", "relation", "storage_order")),
    "genie_link_target_idx": ("GenieLink", ("target_id", "relation", "storage_order")),
    "genie_link_relation_idx": ("GenieLink", ("relation", "source_id", "target_id")),
    "genie_key_collection_idx": ("GenieKeyBinding", ("collection_id", "namespace_json", "key")),
    "genie_key_block_idx": ("GenieKeyBinding", ("block_id",)),
    "genie_log_entry_log_idx": ("GenieLogEntry", ("log_id", "sequence")),
    "genie_log_entry_block_idx": ("GenieLogEntry", ("block_id",)),
    "genie_temporal_block_idx": ("GenieTemporalEntry", ("collection_id", "block_id")),
    "genie_temporal_occurred_idx": ("GenieTemporalEntry", ("collection_id", "occurred_at", "block_id")),
    "genie_temporal_validity_idx": ("GenieTemporalEntry", ("collection_id", "valid_at", "invalid_at", "block_id")),
}


class Neo4jBackend:
    """Persistent backend backed by the official Neo4j Python driver.

    The `neo4j` package is an optional dependency. Install it with
    `pip install 'genie[neo4j]'` before constructing this backend.

    Attributes:
        uri: Neo4j URI this backend connected to.
        database: Neo4j database in which Genie state is stored.
        connection_timeout: Seconds allowed while establishing a connection.
        max_transaction_retry_time: Maximum seconds the driver retries managed
            transactions after retryable failures.
        encrypted: Driver encryption override, or `None` for the driver
            default.
    """

    def __init__(
        self,
        uri: str,
        *,
        auth: tuple[str, str] | None = None,
        database: str = "neo4j",
        connection_timeout: float = 30.0,
        max_transaction_retry_time: float = 30.0,
        encrypted: bool | None = None,
        verify_connectivity: bool = True,
        initialize_schema: bool = True,
    ) -> None:
        """Open a driver and initialize or validate the Genie graph schema.

        Args:
            uri: Neo4j URI, such as `"neo4j://localhost:7687"`.
            auth: Optional `(username, password)` authentication pair.
            database: Existing Neo4j database in which Genie state is stored.
            connection_timeout: Seconds allowed while establishing a
                connection.
            max_transaction_retry_time: Maximum seconds the driver retries
                managed transactions after retryable failures.
            encrypted: Optional driver encryption override.
            verify_connectivity: Whether to verify the server during
                construction.
            initialize_schema: Whether to create missing Genie constraints and
                indexes. Set this to `False` for runtime identities that may
                use existing data but may not administer schema.

        Raises:
            TypeError: If an argument has the wrong type.
            ValueError: If `uri`, `database`, or a timeout is invalid.
            UnsupportedOperation: If the driver is unavailable or the stored
                schema is incompatible.
            BackendError: If Neo4j cannot be reached, configured, or
                initialized.
        """

        _validate_nonempty_string(uri, name="uri")
        _validate_nonempty_string(database, name="database")
        _validate_timeout(connection_timeout, name="connection_timeout")
        _validate_timeout(max_transaction_retry_time, name="max_transaction_retry_time")
        if auth is not None and (not isinstance(auth, tuple) or len(auth) != 2 or not all(isinstance(item, str) for item in auth)):
            raise TypeError("auth must be a (username, password) tuple of strings.")
        if encrypted is not None and not isinstance(encrypted, bool):
            raise TypeError("encrypted must be a boolean or None.")
        if not isinstance(verify_connectivity, bool):
            raise TypeError("verify_connectivity must be a boolean.")
        if not isinstance(initialize_schema, bool):
            raise TypeError("initialize_schema must be a boolean.")

        graph_database, exception_types = _load_neo4j_driver()
        self.uri = uri
        self.database = database
        self.connection_timeout = float(connection_timeout)
        self.max_transaction_retry_time = float(max_transaction_retry_time)
        self.encrypted = encrypted
        self._exception_types = exception_types
        self._lock = RLock()
        self._lifecycle = Condition(self._lock)
        self._active_operations = 0
        self._closing = False
        self._driver: Any | None = None
        self._bookmark_manager = graph_database.bookmark_manager()

        driver_options: dict[str, Any] = {
            "connection_timeout": self.connection_timeout,
            "max_transaction_retry_time": self.max_transaction_retry_time,
        }
        if encrypted is not None:
            driver_options["encrypted"] = encrypted
        try:
            driver = graph_database.driver(uri, auth=auth, **driver_options)
            self._driver = driver
            if verify_connectivity:
                driver.verify_connectivity()
            if initialize_schema:
                self._initialize_schema()
            else:
                self._validate_schema()
        except UnsupportedOperation:
            self._discard_driver()
            raise
        except self._exception_types as error:
            self._discard_driver()
            raise _translate_neo4j_error(error) from error
        except BaseException:
            self._discard_driver()
            raise

    def __enter__(self) -> Self:
        """Return this open backend for use as a context manager.

        Returns:
            This backend instance.

        Raises:
            RuntimeError: If the backend is already closed.
        """

        self._require_driver()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the owned driver when leaving a context manager.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc: Exception instance leaving the block, if any.
            traceback: Traceback of the exception leaving the block, if any.
        """

        self.close()

    def close(self) -> None:
        """Wait for active operations and close the owned driver safely."""

        with self._lifecycle:
            while self._closing:
                self._lifecycle.wait()
            driver = self._driver
            if driver is None:
                return
            self._closing = True
            while self._active_operations:
                self._lifecycle.wait()
            self._driver = None
        try:
            driver.close()
        except self._exception_types as error:
            raise _translate_neo4j_error(error) from error
        finally:
            with self._lifecycle:
                self._closing = False
                self._lifecycle.notify_all()

    # ------------------------------------------------------------------
    # Core block storage and ownership traversal

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Persist a new encoded memory block.

        Args:
            block: Encoded block to store.

        Returns:
            Stored block.

        Raises:
            MemoryConflict: If the id already exists or the parent is missing
                or self-referential.
            BackendError: If the Neo4j write fails.
        """

        properties = _block_properties(block)

        def operation(tx: Any) -> MemoryBlock:
            if self._block_record(tx, block.id) is not None:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            self._validate_parent(tx, block.id, block.parent_id)
            properties["storage_order"] = self._next_storage_order(tx, "blocks")
            _consume(tx, "CREATE (b:GenieBlock) SET b = $properties", properties=properties)
            if block.parent_id is not None:
                _consume(
                    tx,
                    """
                    MATCH (parent:GenieBlock {id: $parent_id})
                    MATCH (child:GenieBlock {id: $block_id})
                    CREATE (parent)-[:GENIE_OWNS]->(child)
                    """,
                    parent_id=block.parent_id,
                    block_id=block.id,
                )
            stored = self._block_record(tx, block.id)
            assert stored is not None
            return _block_from_record(stored)

        return self._write(operation)

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Fetch a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            Stored block, or `None` when it does not exist.

        Raises:
            BackendError: If the Neo4j read fails.
        """

        def operation(tx: Any) -> MemoryBlock | None:
            record = self._block_record(tx, block_id)
            return _block_from_record(record) if record is not None else None

        return self._read(operation)

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace an existing block, optionally using compare-and-swap.

        Args:
            block: Encoded replacement block.
            expected_version: Required current version. When supplied, the
                replacement must have version `expected_version + 1`.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block is missing, its version changed, or
                the new parent is missing or would create an ownership cycle.
            BackendError: If the Neo4j write fails.
        """

        properties = _block_properties(block)

        def operation(tx: Any) -> MemoryBlock:
            previous_record = self._block_record(tx, block.id)
            if previous_record is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            previous = _block_from_record(previous_record)
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._validate_parent(tx, block.id, block.parent_id)
                if block.parent_id is not None and self._contains(tx, block.id, block.parent_id):
                    raise MemoryConflict("A memory block cannot be moved under one of its descendants.")
            properties["storage_order"] = int(previous_record["block"]["storage_order"])
            _consume(
                tx,
                """
                MATCH (b:GenieBlock {id: $block_id})
                SET b = $properties
                WITH b
                OPTIONAL MATCH (:GenieBlock)-[old:GENIE_OWNS]->(b)
                DELETE old
                """,
                block_id=block.id,
                properties=properties,
            )
            if block.parent_id is not None:
                _consume(
                    tx,
                    """
                    MATCH (parent:GenieBlock {id: $parent_id})
                    MATCH (child:GenieBlock {id: $block_id})
                    CREATE (parent)-[:GENIE_OWNS]->(child)
                    """,
                    parent_id=block.parent_id,
                    block_id=block.id,
                )
            stored = self._block_record(tx, block.id)
            assert stored is not None
            return _block_from_record(stored)

        return self._write(operation)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a subtree, optionally from an exact version snapshot.

        Args:
            block_id: Memory block id. Missing ids are ignored.
            expected_versions: Exact id-to-version snapshot of the subtree to
                delete. When supplied, the delete fails if subtree membership
                or any version changed.

        Raises:
            MemoryConflict: If `expected_versions` no longer describes the
                complete subtree.
            BackendError: If the Neo4j write fails.
        """

        def operation(tx: Any) -> None:
            records = self._descendant_records(tx, block_id, include_self=True)
            current = {str(record["block"]["id"]): int(record["block"]["version"]) for record in records}
            if expected_versions is not None and current != dict(expected_versions):
                raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            block_ids = list(current)
            if not block_ids:
                return
            _consume(
                tx,
                "MATCH (s:GenieBlock)-[e:GENIE_EDGE]->(t:GenieBlock) WHERE s.id IN $ids OR t.id IN $ids DELETE e",
                ids=block_ids,
            )
            for label, predicate in (
                ("GenieLink", "n.source_id IN $ids OR n.target_id IN $ids"),
                ("GenieKeyBinding", "n.collection_id IN $ids OR n.block_id IN $ids"),
                ("GenieLogEntry", "n.log_id IN $ids OR n.block_id IN $ids"),
                ("GenieLogCounter", "n.log_id IN $ids"),
                ("GenieTemporalEntry", "n.collection_id IN $ids OR n.block_id IN $ids"),
            ):
                _consume(tx, f"MATCH (n:{label}) WHERE {predicate} DETACH DELETE n", ids=block_ids)
            _consume(tx, "MATCH (b:GenieBlock) WHERE b.id IN $ids DETACH DELETE b", ids=block_ids)

        self._write(operation)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children in creation order.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks in creation order.
        """

        return self._read(
            lambda tx: [
                _block_from_record(record)
                for record in _records(
                    tx,
                    """
                    MATCH (parent:GenieBlock {id: $block_id})-[:GENIE_OWNS]->(b:GenieBlock)
                    OPTIONAL MATCH (owner:GenieBlock)-[:GENIE_OWNS]->(b)
                    RETURN properties(b) AS block, owner.id AS parent_id
                    ORDER BY b.storage_order
                    """,
                    block_id=block_id,
                )
            ]
        )

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from the nearest parent upward.

        Args:
            block_id: Block id to inspect.

        Returns:
            Ancestor blocks in nearest-parent-first order.
        """

        return self._read(
            lambda tx: [
                _block_from_record(record)
                for record in _records(
                    tx,
                    """
                    MATCH path=(ancestor:GenieBlock)-[:GENIE_OWNS*1..]->(b:GenieBlock {id: $block_id})
                    OPTIONAL MATCH (owner:GenieBlock)-[:GENIE_OWNS]->(ancestor)
                    RETURN properties(ancestor) AS block, owner.id AS parent_id, length(path) AS depth
                    ORDER BY depth
                    """,
                    block_id=block_id,
                )
            ]
        )

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return descendants in deterministic depth-first creation order.

        Args:
            block_id: Root block id.
            include_self: Whether to include `block_id` in the result.

        Returns:
            Descendant blocks in depth-first creation order.
        """

        return self._read(lambda tx: [_block_from_record(record) for record in self._descendant_records(tx, block_id, include_self=include_self)])

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is equal to or below `root_id`.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is the root or one of its descendants.
        """

        return self._read(lambda tx: self._contains(tx, root_id, block_id))

    # ------------------------------------------------------------------
    # Keyed collection

    def bind_key(self, binding: KeyBinding) -> KeyBinding:
        """Atomically bind an unused namespaced address to a block.

        Args:
            binding: Key binding to create.

        Returns:
            Stored binding.

        Raises:
            MemoryConflict: If the collection or block is missing, or the
                address is already bound.
        """

        namespace_json = _encode_namespace(binding.address.namespace)
        identity = _identity(binding.collection_id, namespace_json, binding.address.key)

        def operation(tx: Any) -> KeyBinding:
            self._require_structure_blocks(tx, container_id=binding.collection_id, block_id=binding.block_id, container_name="Keyed collection")
            if _single(tx, "MATCH (n:GenieKeyBinding {identity: $identity}) RETURN n.identity AS identity", identity=identity) is not None:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in collection {binding.collection_id!r}.")
            _consume(
                tx,
                """
                CREATE (n:GenieKeyBinding {
                    identity: $identity, collection_id: $collection_id,
                    namespace_json: $namespace_json, key: $key, block_id: $block_id
                })
                """,
                identity=identity,
                collection_id=binding.collection_id,
                namespace_json=namespace_json,
                key=binding.address.key,
                block_id=binding.block_id,
            )
            return binding

        return self._write(operation)

    def rebind_key(self, binding: KeyBinding, *, expected_block_id: str) -> KeyBinding:
        """Replace a binding only when its current block id matches.

        Args:
            binding: Replacement binding. Its collection id and address select
                the existing binding to replace.
            expected_block_id: Block id the binding must currently point to.

        Returns:
            Stored replacement binding.

        Raises:
            MemoryConflict: If the collection or block is missing, the address
                is unbound, or it no longer points to `expected_block_id`.
        """

        namespace_json = _encode_namespace(binding.address.namespace)
        identity = _identity(binding.collection_id, namespace_json, binding.address.key)

        def operation(tx: Any) -> KeyBinding:
            self._require_structure_blocks(tx, container_id=binding.collection_id, block_id=binding.block_id, container_name="Keyed collection")
            record = _single(
                tx,
                """
                MATCH (n:GenieKeyBinding {identity: $identity, block_id: $expected_block_id})
                SET n.block_id = $block_id
                RETURN n.identity AS identity
                """,
                identity=identity,
                expected_block_id=expected_block_id,
                block_id=binding.block_id,
            )
            if record is None:
                raise MemoryConflict(f"Key address {binding.address!r} in collection {binding.collection_id!r} no longer points to expected block {expected_block_id!r}.")
            return binding

        return self._write(operation)

    def get_key(self, collection_id: str, address: MemoryAddress) -> KeyBinding | None:
        """Return one exact key binding.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to look up.

        Returns:
            Stored binding, or `None` when the address is unused.
        """

        namespace_json = _encode_namespace(address.namespace)
        identity = _identity(collection_id, namespace_json, address.key)
        return self._read(
            lambda tx: (
                _binding_from_record(record)
                if (record := _single(tx, "MATCH (n:GenieKeyBinding {identity: $identity}) RETURN properties(n) AS binding", identity=identity)) is not None
                else None
            )
        )

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

        Args:
            collection_id: Id of the collection to list.
            namespace: Optional exact namespace filter. With `recursive=True`
                it is treated as a namespace prefix.
            recursive: Whether to include nested namespaces below `namespace`.
            after: Optional exclusive start address used to page through
                results in address order.
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
        records = self._read(
            lambda tx: _records(
                tx,
                "MATCH (n:GenieKeyBinding {collection_id: $collection_id}) RETURN properties(n) AS binding",
                collection_id=collection_id,
            )
        )
        bindings = [_binding_from_record(record) for record in records if _namespace_matches(_decode_namespace(record["binding"]["namespace_json"]), namespace, recursive=recursive)]
        bindings.sort(key=lambda item: (item.address.namespace, item.address.key))
        if after is not None:
            after_key = (after.namespace, after.key)
            bindings = [item for item in bindings if (item.address.namespace, item.address.key) > after_key]
        return bindings[:limit]

    def unbind_key(self, collection_id: str, address: MemoryAddress, *, expected_block_id: str) -> None:
        """Remove a binding only when its current block id matches.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to remove.
            expected_block_id: Block id the binding must currently point to.

        Raises:
            MemoryConflict: If the address is unbound or it no longer points
                to `expected_block_id`.
        """

        namespace_json = _encode_namespace(address.namespace)
        identity = _identity(collection_id, namespace_json, address.key)

        def operation(tx: Any) -> None:
            record = _single(
                tx,
                """
                MATCH (n:GenieKeyBinding {identity: $identity, block_id: $expected_block_id})
                WITH n, n.identity AS identity
                DELETE n
                RETURN identity
                """,
                identity=identity,
                expected_block_id=expected_block_id,
            )
            if record is None:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} no longer points to expected block {expected_block_id!r}.")

        self._write(operation)

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove bindings owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. Bindings whose collection id or
                target block id match are removed.
        """

        self._write(
            lambda tx: _consume(
                tx,
                "MATCH (n:GenieKeyBinding) WHERE n.collection_id = $block_id OR n.block_id = $block_id DELETE n",
                block_id=block_id,
            )
        )

    # ------------------------------------------------------------------
    # Ordered event log

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Atomically allocate a sequence and append a block reference.

        Args:
            log_id: Id of the ordered log to append to.
            block_id: Id of the memory block to place at the new position.

        Returns:
            Stored entry carrying the allocated sequence number.

        Raises:
            MemoryConflict: If the log or referenced block does not exist.
        """

        from genie.structures.records import LogEntry

        def operation(tx: Any) -> LogEntry:
            self._require_structure_blocks(tx, container_id=log_id, block_id=block_id, container_name="Event log")
            record = _single(tx, "MATCH (c:GenieLogCounter {log_id: $log_id}) RETURN c.next_sequence AS next_sequence", log_id=log_id)
            if record is None:
                sequence = 0
                _consume(tx, "CREATE (:GenieLogCounter {log_id: $log_id, next_sequence: 1})", log_id=log_id)
            else:
                sequence = int(record["next_sequence"])
                _consume(
                    tx,
                    "MATCH (c:GenieLogCounter {log_id: $log_id}) SET c.next_sequence = $next_sequence",
                    log_id=log_id,
                    next_sequence=sequence + 1,
                )
            _consume(
                tx,
                "CREATE (:GenieLogEntry {identity: $identity, log_id: $log_id, sequence: $sequence, block_id: $block_id})",
                identity=_identity(log_id, sequence),
                log_id=log_id,
                sequence=sequence,
                block_id=block_id,
            )
            return LogEntry(log_id=log_id, sequence=sequence, block_id=block_id)

        return self._write(operation)

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry.

        Args:
            log_id: Id of the ordered log.
            sequence: Exact position within the log.

        Returns:
            Stored entry, or `None` when the position holds no entry.

        Raises:
            TypeError: If `sequence` is not an integer.
            ValueError: If `sequence` is negative.
        """

        _validate_sequence(sequence, name="sequence")
        return self._read(
            lambda tx: (
                _log_entry_from_record(record)
                if (
                    record := _single(
                        tx,
                        "MATCH (n:GenieLogEntry {identity: $identity}) RETURN properties(n) AS entry",
                        identity=_identity(log_id, sequence),
                    )
                )
                is not None
                else None
            )
        )

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
            reverse: Whether to return entries in descending sequence order.
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
        clauses = ["n.log_id = $log_id"]
        parameters: dict[str, Any] = {"log_id": log_id, "limit": limit}
        if start is not None:
            clauses.append("n.sequence >= $start")
            parameters["start"] = start
        if stop is not None:
            clauses.append("n.sequence < $stop")
            parameters["stop"] = stop
        direction = "DESC" if reverse else "ASC"
        return self._read(
            lambda tx: [
                _log_entry_from_record(record)
                for record in _records(
                    tx,
                    f"MATCH (n:GenieLogEntry) WHERE {' AND '.join(clauses)} RETURN properties(n) AS entry ORDER BY n.sequence {direction} LIMIT $limit",
                    **parameters,
                )
            ]
        )

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove a log and entries owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. A log owned by this id is
                dropped, along with entries pointing to it in any other log.
        """

        def operation(tx: Any) -> None:
            _consume(
                tx,
                "MATCH (n:GenieLogEntry) WHERE n.log_id = $block_id OR n.block_id = $block_id DELETE n",
                block_id=block_id,
            )
            _consume(tx, "MATCH (c:GenieLogCounter {log_id: $block_id}) DELETE c", block_id=block_id)

        self._write(operation)

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
            MemoryConflict: If the collection or block is missing, or the
                collection already contains the block.
        """

        properties = _temporal_properties(entry)

        def operation(tx: Any) -> TemporalEntry:
            self._require_structure_blocks(tx, container_id=entry.collection_id, block_id=entry.block_id, container_name="Temporal collection")
            if _single(tx, "MATCH (n:GenieTemporalEntry {identity: $identity}) RETURN n.identity AS identity", identity=properties["identity"]) is not None:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal collection {entry.collection_id!r}.")
            _consume(tx, "CREATE (n:GenieTemporalEntry) SET n = $properties", properties=properties)
            return entry

        return self._write(operation)

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Replace an existing temporal collection entry.

        Args:
            entry: Replacement entry. Its collection id and block id select
                the existing entry to replace.

        Returns:
            Stored replacement entry.

        Raises:
            MemoryConflict: If the collection or block is missing, or the
                collection does not contain the block.
        """

        properties = _temporal_properties(entry)

        def operation(tx: Any) -> TemporalEntry:
            self._require_structure_blocks(tx, container_id=entry.collection_id, block_id=entry.block_id, container_name="Temporal collection")
            record = _single(
                tx,
                "MATCH (n:GenieTemporalEntry {identity: $identity}) SET n = $properties RETURN n.identity AS identity",
                identity=properties["identity"],
                properties=properties,
            )
            if record is None:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal collection {entry.collection_id!r}.")
            return entry

        return self._write(operation)

    def get_temporal_entry(self, collection_id: str, block_id: str) -> TemporalEntry | None:
        """Return one exact temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block.

        Returns:
            Stored entry, or `None` when the collection does not contain the
            block.
        """

        identity = _identity(collection_id, block_id)
        return self._read(
            lambda tx: (
                _temporal_from_record(record)
                if (
                    record := _single(
                        tx,
                        "MATCH (n:GenieTemporalEntry {identity: $identity}) RETURN properties(n) AS entry",
                        identity=identity,
                    )
                )
                is not None
                else None
            )
        )

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
            after_block_id: Optional exclusive start block id used to page
                through results in block-id order.
            limit: Maximum number of entries to return.

        Returns:
            Entries in ascending block-id order.

        Raises:
            TypeError: If `limit` is not an integer.
            ValueError: If `limit` is negative.
        """

        _validate_limit(limit)
        clauses = ["n.collection_id = $collection_id"]
        parameters: dict[str, Any] = {"collection_id": collection_id, "limit": limit}
        if after_block_id is not None:
            clauses.append("n.block_id > $after_block_id")
            parameters["after_block_id"] = after_block_id
        return self._read(
            lambda tx: [
                _temporal_from_record(record)
                for record in _records(
                    tx,
                    f"MATCH (n:GenieTemporalEntry) WHERE {' AND '.join(clauses)} RETURN properties(n) AS entry ORDER BY n.block_id LIMIT $limit",
                    **parameters,
                )
            ]
        )

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block to remove.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        def operation(tx: Any) -> None:
            record = _single(
                tx,
                """
                MATCH (n:GenieTemporalEntry {identity: $identity})
                WITH n, n.identity AS identity
                DELETE n
                RETURN identity
                """,
                identity=_identity(collection_id, block_id),
            )
            if record is None:
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection {collection_id!r}.")

        self._write(operation)

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove temporal entries owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. Entries whose collection id or
                target block id match are removed.
        """

        self._write(
            lambda tx: _consume(
                tx,
                "MATCH (n:GenieTemporalEntry) WHERE n.collection_id = $block_id OR n.block_id = $block_id DELETE n",
                block_id=block_id,
            )
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
        """Return one temporal page matching every supplied time filter.

        Args:
            collection_id: Id of the temporal collection.
            occurred_only: Whether to keep only entries that carry an event
                time.
            occurred_start: Optional inclusive lower bound on event time.
            occurred_stop: Optional exclusive upper bound on event time.
            valid_at: Optional instant that an entry's validity period must
                contain.
            overlaps: Optional validity period an entry's own period must
                overlap.
            after_block_id: Optional exclusive start block id used to page
                through results in block-id order.
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
        if occurred_start is not None and occurred_stop is not None and occurred_stop < occurred_start:
            raise ValueError("occurred_stop must be greater than or equal to occurred_start.")
        if overlaps is not None and not isinstance(overlaps, ValidityInterval):
            raise TypeError("overlaps must be a ValidityInterval.")
        _validate_limit(limit)

        clauses = ["n.collection_id = $collection_id"]
        parameters: dict[str, Any] = {"collection_id": collection_id, "limit": limit}
        if after_block_id is not None:
            clauses.append("n.block_id > $after_block_id")
            parameters["after_block_id"] = after_block_id
        if occurred_only:
            clauses.append("n.occurred_at IS NOT NULL")
        if occurred_start is not None:
            clauses.append("n.occurred_at >= $occurred_start")
            parameters["occurred_start"] = _encode_datetime(occurred_start, field_name="occurred_start")
        if occurred_stop is not None:
            clauses.append("n.occurred_at < $occurred_stop")
            parameters["occurred_stop"] = _encode_datetime(occurred_stop, field_name="occurred_stop")
        if valid_at is not None:
            clauses.extend(["n.valid_at IS NOT NULL", "n.valid_at <= $valid_at", "(n.invalid_at IS NULL OR n.invalid_at > $valid_at)"])
            parameters["valid_at"] = _encode_datetime(valid_at, field_name="valid_at")
        if overlaps is not None:
            clauses.extend(["n.valid_at IS NOT NULL", "(n.invalid_at IS NULL OR n.invalid_at > $overlap_start)"])
            parameters["overlap_start"] = _encode_datetime(overlaps.valid_at, field_name="overlaps.valid_at")
            if overlaps.invalid_at is not None:
                clauses.append("n.valid_at < $overlap_stop")
                parameters["overlap_stop"] = _encode_datetime(overlaps.invalid_at, field_name="overlaps.invalid_at")
        return self._read(
            lambda tx: [
                _temporal_from_record(record)
                for record in _records(
                    tx,
                    f"MATCH (n:GenieTemporalEntry) WHERE {' AND '.join(clauses)} RETURN properties(n) AS entry ORDER BY n.block_id LIMIT $limit",
                    **parameters,
                )
            ]
        )

    # ------------------------------------------------------------------
    # Typed property graph and lineage DAG

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Persist a typed link between existing blocks.

        Args:
            link: Link to store.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, either endpoint is
                missing, or the relation is protected as an acyclic graph.
        """

        def operation(tx: Any) -> MemoryLink:
            if self._relation_is_protected(tx, link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic graph; use atomic acyclic insertion.")
            self._validate_new_link(tx, link)
            return self._insert_link(tx, link)

        return self._write(operation)

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Atomically add a link unless it would create a directed cycle.

        Existing links with the same relation are adopted on the first call
        only when they already form a DAG. The relation is then protected from
        ordinary link insertion.

        Args:
            link: Link to store. Its relation identifies the protected graph
                the cycle check applies to.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, either endpoint is
                missing, the relation already contains a cycle, or the new
                link would create one.
        """

        def operation(tx: Any) -> MemoryLink:
            self._validate_new_link(tx, link)
            edges = [
                (str(record["source_id"]), str(record["target_id"]))
                for record in _records(
                    tx,
                    "MATCH (l:GenieLink {relation: $relation}) RETURN l.source_id AS source_id, l.target_id AS target_id",
                    relation=link.relation,
                )
            ]
            if not self._relation_is_protected(tx, link.relation) and _edges_have_cycle(edges):
                raise MemoryConflict(f"Link relation {link.relation!r} already contains a cycle.")
            if link.source_id == link.target_id or _edge_path_exists(edges, link.target_id, link.source_id):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create a cycle in relation {link.relation!r}.")
            _consume(
                tx,
                "MERGE (:GenieAcyclicRelation {relation: $relation})",
                relation=link.relation,
            )
            return self._insert_link(tx, link)

        return self._write(operation)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace only the metadata of an existing link.

        Args:
            link: Link containing the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link is missing or any field other than
                metadata changed.
        """

        metadata_json = _dump_json(link.metadata, field_name="link metadata")

        def operation(tx: Any) -> MemoryLink:
            record = self._link_record(tx, link.id)
            if record is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            previous = _link_from_record(record)
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            _consume(
                tx,
                "MATCH (l:GenieLink {id: $link_id}) SET l.metadata_json = $metadata_json",
                link_id=link.id,
                metadata_json=metadata_json,
            )
            updated = self._link_record(tx, link.id)
            assert updated is not None
            return _link_from_record(updated)

        return self._write(operation)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to one block.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.

        Raises:
            ValueError: If `direction` is invalid.
        """

        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")
        if direction == "out":
            endpoint = "l.source_id = $block_id"
        elif direction == "in":
            endpoint = "l.target_id = $block_id"
        else:
            endpoint = "(l.source_id = $block_id OR l.target_id = $block_id)"
        relation_clause = "" if relation is None else " AND l.relation = $relation"
        return self._read(
            lambda tx: [
                _link_from_record(record)
                for record in _records(
                    tx,
                    f"MATCH (l:GenieLink) WHERE {endpoint}{relation_clause} RETURN properties(l) AS link ORDER BY l.storage_order",
                    block_id=block_id,
                    relation=relation,
                )
            ]
        )

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete incoming and outgoing links for one block.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        def operation(tx: Any) -> None:
            _consume(
                tx,
                "MATCH (s:GenieBlock)-[e:GENIE_EDGE]->(t:GenieBlock) WHERE s.id = $block_id OR t.id = $block_id DELETE e",
                block_id=block_id,
            )
            _consume(
                tx,
                "MATCH (l:GenieLink) WHERE l.source_id = $block_id OR l.target_id = $block_id DETACH DELETE l",
                block_id=block_id,
            )

        self._write(operation)

    # ------------------------------------------------------------------
    # Portable search

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks using the portable in-memory matching behavior.

        Candidate records are narrowed in Cypher by scope, then matched in
        Python so results stay consistent with the reference backend.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results ordered by descending score, limited to
            `query.limit`.
        """

        candidates = self._read(lambda tx: [_block_from_record(record) for record in self._scope_candidate_records(tx, query)])
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
    # Driver, schema, and transaction helpers

    def _read(self, operation: Callable[[Any], _T]) -> _T:
        """Run one managed read transaction and translate driver failures."""

        try:
            with self._operation_driver() as driver, driver.session(database=self.database, bookmark_manager=self._bookmark_manager) as session:
                return session.execute_read(operation)
        except GenieError:
            raise
        except self._exception_types as error:
            raise _translate_neo4j_error(error) from error

    def _write(self, operation: Callable[[Any], _T]) -> _T:
        """Run one serialized managed write transaction."""

        def guarded(tx: Any) -> _T:
            self._lock_mutations(tx)
            return operation(tx)

        try:
            with self._operation_driver() as driver, driver.session(database=self.database, bookmark_manager=self._bookmark_manager) as session:
                return session.execute_write(guarded)
        except GenieError:
            raise
        except self._exception_types as error:
            raise _translate_neo4j_error(error) from error

    def _lock_mutations(self, tx: Any) -> None:
        """Acquire the database-resident lock shared by every Genie write."""

        _single(
            tx,
            """
            MERGE (g:GenieGuard {name: $name})
            ON CREATE SET g.lock_version = 0
            SET g.lock_version = g.lock_version + 1
            RETURN g.lock_version AS lock_version
            """,
            name=_MUTATION_GUARD,
        )

    def _initialize_schema(self) -> None:
        """Apply resumable migrations and validate the current graph schema."""

        existing_state = self._read_schema_identity()
        version = 0 if existing_state is None else self._schema_version(existing_state, allow_older=True)
        for target_version in range(version + 1, _SCHEMA_VERSION + 1):
            self._apply_schema_migration(target_version)
            state = self._write_schema_identity(target_version)
            self._validate_schema_identity(state)

        # Repair a missing owned object at the current version. Each statement
        # is idempotent so interrupted Neo4j DDL can be resumed safely.
        self._ensure_schema_objects()
        self._await_schema_indexes()
        self._validate_schema_objects()
        current_state = self._read_schema_identity()
        if current_state is None:
            raise UnsupportedOperation("The Genie Neo4j schema identity is missing.")
        self._validate_schema_identity(current_state)

    def _apply_schema_migration(self, target_version: int) -> None:
        """Apply one idempotent schema migration."""

        if target_version != 1:
            raise UnsupportedOperation(f"No Neo4j migration is available for schema version {target_version}.")
        self._ensure_schema_objects()
        self._await_schema_indexes()
        self._validate_schema_objects()

    def _ensure_schema_objects(self) -> None:
        """Create every missing constraint and index owned by Genie."""

        for name, (label, property_name) in _CONSTRAINTS.items():
            self._schema_statement(f"CREATE CONSTRAINT {name} IF NOT EXISTS FOR (n:{label}) REQUIRE n.{property_name} IS UNIQUE")
        for name, (label, properties) in _INDEXES.items():
            joined = ", ".join(f"n.{property_name}" for property_name in properties)
            self._schema_statement(f"CREATE INDEX {name} IF NOT EXISTS FOR (n:{label}) ON ({joined})")

    def _await_schema_indexes(self) -> None:
        """Wait until every owned range index is online."""

        for name in _INDEXES:
            self._await_schema_index(name)

    def _await_schema_index(self, index_name: str) -> None:
        """Wait until one owned range index is online."""

        self._raw_read(
            lambda tx: _consume(
                tx,
                "CALL db.awaitIndex($index_name, $timeout_seconds)",
                index_name=index_name,
                timeout_seconds=_SCHEMA_INDEX_TIMEOUT_SECONDS,
            )
        )

    def _write_schema_identity(self, version: int) -> dict[str, Any]:
        """Advance the owned schema identity without overwriting another owner."""

        record = self._raw_write(
            lambda tx: _single(
                tx,
                """
                MERGE (m:GenieSchema {name: 'genie'})
                ON CREATE SET m.application_id = $application_id, m.version = $version
                WITH m
                WHERE m.application_id = $application_id
                SET m.version = CASE
                    WHEN m.version IS NULL OR m.version < $version THEN $version
                    ELSE m.version
                END
                RETURN m.application_id AS application_id, m.version AS version
                """,
                application_id=_APPLICATION_ID,
                version=version,
            )
        )
        if record is None:
            raise UnsupportedOperation("The Neo4j schema identity belongs to another application.")
        return record

    def _validate_schema(self) -> None:
        """Validate an existing schema without attempting DDL."""

        state = self._read_schema_identity()
        if state is None:
            raise UnsupportedOperation("The Genie Neo4j schema is not initialized.")
        self._validate_schema_identity(state)
        self._validate_schema_objects()

    def _read_schema_identity(self) -> dict[str, Any] | None:
        """Read the stored Genie schema identity, or `None` when absent."""

        return self._raw_read(
            lambda tx: _single(
                tx,
                "MATCH (m:GenieSchema {name: 'genie'}) RETURN m.application_id AS application_id, m.version AS version",
            )
        )

    def _validate_schema_identity(self, state: Mapping[str, Any]) -> None:
        """Require the stored schema identity to match this Genie version."""

        self._schema_version(state, allow_older=False)

    def _schema_version(
        self,
        state: Mapping[str, Any],
        *,
        allow_older: bool,
    ) -> int:
        """Validate schema ownership and return its supported version."""

        application_id = state.get("application_id")
        version = state.get("version")
        if application_id != _APPLICATION_ID:
            raise UnsupportedOperation(f"The Neo4j schema belongs to another application ({application_id!r}).")
        if isinstance(version, bool) or not isinstance(version, int):
            raise UnsupportedOperation("The Genie Neo4j schema version is invalid.")
        if version > _SCHEMA_VERSION:
            raise UnsupportedOperation(f"Neo4j schema version {version} is newer than supported version {_SCHEMA_VERSION}.")
        if version < _SCHEMA_VERSION and not allow_older:
            raise UnsupportedOperation(f"No Neo4j migration is available from schema version {version}.")
        return version

    def _validate_schema_objects(self) -> None:
        """Require every constraint and index owned by Genie to exist."""

        constraint_records = self._raw_read(
            lambda tx: _records(
                tx,
                """
                SHOW CONSTRAINTS
                YIELD name, type, entityType, labelsOrTypes, properties
                RETURN name, type, entityType, labelsOrTypes, properties
                """,
            )
        )
        constraints = {
            str(record["name"]): (
                str(record["type"]),
                str(record["entityType"]),
                tuple(record["labelsOrTypes"]),
                tuple(record["properties"]),
            )
            for record in constraint_records
            if record["name"] in _CONSTRAINTS
        }
        for name, (label, property_name) in _CONSTRAINTS.items():
            constraint = constraints.get(name)
            if constraint is None or constraint[0] not in _UNIQUENESS_CONSTRAINT_TYPES or constraint[1:] != ("NODE", (label,), (property_name,)):
                raise UnsupportedOperation(f"The Genie Neo4j constraint {name!r} is missing or incompatible.")

        index_records = self._raw_read(
            lambda tx: _records(
                tx,
                """
                SHOW INDEXES
                YIELD name, type, entityType, labelsOrTypes, properties, state
                RETURN name, type, entityType, labelsOrTypes, properties, state
                """,
            )
        )
        indexes = {
            str(record["name"]): (
                str(record["type"]),
                str(record["entityType"]),
                tuple(record["labelsOrTypes"]),
                tuple(record["properties"]),
                str(record["state"]),
            )
            for record in index_records
            if record["name"] in _INDEXES
        }
        for name, (label, properties) in _INDEXES.items():
            if indexes.get(name) != ("RANGE", "NODE", (label,), properties, "ONLINE"):
                raise UnsupportedOperation(f"The Genie Neo4j index {name!r} is missing or incompatible.")

    def _schema_statement(self, statement: str) -> None:
        """Run one schema DDL statement in its own write transaction."""

        self._raw_write(lambda tx: _consume(tx, statement))

    def _raw_read(self, operation: Callable[[Any], _T]) -> _T:
        """Run one managed read transaction without the mutation guard."""

        try:
            with self._operation_driver() as driver, driver.session(database=self.database, bookmark_manager=self._bookmark_manager) as session:
                return session.execute_read(operation)
        except UnsupportedOperation:
            raise
        except self._exception_types as error:
            raise _translate_neo4j_error(error) from error

    def _raw_write(self, operation: Callable[[Any], _T]) -> _T:
        """Run one managed write transaction without the mutation guard."""

        try:
            with self._operation_driver() as driver, driver.session(database=self.database, bookmark_manager=self._bookmark_manager) as session:
                return session.execute_write(operation)
        except UnsupportedOperation:
            raise
        except self._exception_types as error:
            raise _translate_neo4j_error(error) from error

    def _require_driver(self) -> Any:
        """Return the open driver or fail after `close`."""

        with self._lifecycle:
            if self._driver is None or self._closing:
                raise RuntimeError("Neo4jBackend is closed.")
            return self._driver

    @contextmanager
    def _operation_driver(self) -> Generator[Any, None, None]:
        """Lease the driver so `close` cannot race an active session."""

        with self._lifecycle:
            if self._driver is None or self._closing:
                raise RuntimeError("Neo4jBackend is closed.")
            driver = self._driver
            self._active_operations += 1
        try:
            yield driver
        finally:
            with self._lifecycle:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._lifecycle.notify_all()

    def _discard_driver(self) -> None:
        """Drop the driver reference and close it, ignoring failures."""

        with self._lifecycle:
            driver = self._driver
            self._driver = None
        if driver is None:
            return
        with suppress(Exception):
            driver.close()

    # ------------------------------------------------------------------
    # Transaction-local data helpers

    def _next_storage_order(self, tx: Any, name: str) -> int:
        """Allocate the next stable storage order value from a counter."""

        record = _single(
            tx,
            """
            MERGE (c:GenieCounter {name: $name})
            ON CREATE SET c.next_value = 0
            WITH c, c.next_value AS storage_order
            SET c.next_value = c.next_value + 1
            RETURN storage_order
            """,
            name=name,
        )
        assert record is not None
        return int(record["storage_order"])

    def _block_record(self, tx: Any, block_id: str) -> dict[str, Any] | None:
        """Return one raw block record together with its parent id."""

        return _single(
            tx,
            """
            MATCH (b:GenieBlock {id: $block_id})
            OPTIONAL MATCH (parent:GenieBlock)-[:GENIE_OWNS]->(b)
            RETURN properties(b) AS block, parent.id AS parent_id
            """,
            block_id=block_id,
        )

    def _link_record(self, tx: Any, link_id: str) -> dict[str, Any] | None:
        """Return one raw link record by id."""

        return _single(
            tx,
            "MATCH (l:GenieLink {id: $link_id}) RETURN properties(l) AS link",
            link_id=link_id,
        )

    def _validate_parent(self, tx: Any, block_id: str, parent_id: str | None) -> None:
        """Require a parent placement to exist and not be self-referential."""

        if parent_id == block_id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if parent_id is not None and self._block_record(tx, parent_id) is None:
            raise MemoryConflict(f"Parent memory block {parent_id!r} does not exist.")

    def _descendant_records(self, tx: Any, block_id: str, *, include_self: bool) -> list[dict[str, Any]]:
        """Return raw subtree records in depth-first creation order."""

        records = _records(
            tx,
            """
            MATCH path=(root:GenieBlock {id: $block_id})-[:GENIE_OWNS*0..]->(b:GenieBlock)
            OPTIONAL MATCH (parent:GenieBlock)-[:GENIE_OWNS]->(b)
            RETURN properties(b) AS block, parent.id AS parent_id,
                   [node IN nodes(path) | node.storage_order] AS path_order,
                   length(path) AS depth
            """,
            block_id=block_id,
        )
        records.sort(key=lambda record: tuple(int(item) for item in record["path_order"]))
        if include_self:
            return records
        return [record for record in records if int(record["depth"]) > 0]

    def _contains(self, tx: Any, root_id: str, block_id: str) -> bool:
        """Return whether a block is equal to or below a subtree root."""

        return (
            _single(
                tx,
                """
                MATCH (root:GenieBlock {id: $root_id})-[:GENIE_OWNS*0..]->(candidate:GenieBlock {id: $block_id})
                RETURN candidate.id AS id LIMIT 1
                """,
                root_id=root_id,
                block_id=block_id,
            )
            is not None
        )

    def _require_structure_blocks(self, tx: Any, *, container_id: str, block_id: str, container_name: str) -> None:
        """Require a structure container and referenced block to exist."""

        if self._block_record(tx, container_id) is None:
            raise MemoryConflict(f"{container_name} block {container_id!r} does not exist.")
        if self._block_record(tx, block_id) is None:
            raise MemoryConflict(f"Memory block {block_id!r} does not exist.")

    def _validate_new_link(self, tx: Any, link: MemoryLink) -> None:
        """Validate identity and endpoints before storing a link."""

        if self._link_record(tx, link.id) is not None:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if self._block_record(tx, link.source_id) is None:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if self._block_record(tx, link.target_id) is None:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _insert_link(self, tx: Any, link: MemoryLink) -> MemoryLink:
        """Create a link node with its source, target, and edge relationships."""

        properties = _link_properties(link)
        properties["storage_order"] = self._next_storage_order(tx, "links")
        _consume(tx, "CREATE (l:GenieLink) SET l = $properties", properties=properties)
        _consume(
            tx,
            """
            MATCH (source:GenieBlock {id: $source_id})
            MATCH (target:GenieBlock {id: $target_id})
            MATCH (link:GenieLink {id: $link_id})
            CREATE (source)-[:GENIE_LINK_SOURCE]->(link)
            CREATE (link)-[:GENIE_LINK_TARGET]->(target)
            CREATE (source)-[:GENIE_EDGE {link_id: $link_id, relation: $relation}]->(target)
            """,
            source_id=link.source_id,
            target_id=link.target_id,
            link_id=link.id,
            relation=link.relation,
        )
        record = self._link_record(tx, link.id)
        assert record is not None
        return _link_from_record(record)

    def _relation_is_protected(self, tx: Any, relation: str) -> bool:
        """Return whether a link relation is protected as an acyclic graph."""

        return (
            _single(
                tx,
                "MATCH (r:GenieAcyclicRelation {relation: $relation}) RETURN r.relation AS relation",
                relation=relation,
            )
            is not None
        )

    def _scope_candidate_records(self, tx: Any, query: MemoryQuery) -> list[dict[str, Any]]:
        """Return raw candidate block records for one query scope."""

        if query.scope == Scope.GLOBAL or query.root_id is None:
            return _records(
                tx,
                """
                MATCH (b:GenieBlock)
                OPTIONAL MATCH (parent:GenieBlock)-[:GENIE_OWNS]->(b)
                RETURN properties(b) AS block, parent.id AS parent_id
                ORDER BY b.storage_order
                """,
            )
        if self._block_record(tx, query.root_id) is None:
            return []
        if query.scope == Scope.SELF:
            record = self._block_record(tx, query.root_id)
            return [record] if record is not None else []
        if query.scope == Scope.SUBTREE:
            return self._descendant_records(tx, query.root_id, include_self=True)
        if query.scope == Scope.ANCESTORS:
            return _records(
                tx,
                """
                MATCH path=(ancestor:GenieBlock)-[:GENIE_OWNS*0..]->(root:GenieBlock {id: $root_id})
                OPTIONAL MATCH (parent:GenieBlock)-[:GENIE_OWNS]->(ancestor)
                RETURN properties(ancestor) AS block, parent.id AS parent_id, length(path) AS depth
                ORDER BY depth
                """,
                root_id=query.root_id,
            )
        if query.scope == Scope.LINKS:
            return _records(
                tx,
                """
                MATCH (l:GenieLink)
                WHERE l.source_id = $root_id OR l.target_id = $root_id
                WITH collect(l.source_id) + collect(l.target_id) AS ids
                MATCH (b:GenieBlock) WHERE b.id IN ids
                OPTIONAL MATCH (parent:GenieBlock)-[:GENIE_OWNS]->(b)
                RETURN properties(b) AS block, parent.id AS parent_id
                ORDER BY b.storage_order
                """,
                root_id=query.root_id,
            )
        return _records(
            tx,
            """
            MATCH (b:GenieBlock)
            OPTIONAL MATCH (parent:GenieBlock)-[:GENIE_OWNS]->(b)
            RETURN properties(b) AS block, parent.id AS parent_id
            ORDER BY b.storage_order
            """,
        )


def _records(tx: Any, query: str, **parameters: Any) -> list[dict[str, Any]]:
    """Execute Cypher and fully materialize result records in the transaction."""

    return [dict(record) for record in tx.run(query, **parameters)]


def _single(tx: Any, query: str, **parameters: Any) -> dict[str, Any] | None:
    """Execute Cypher and return its first fully materialized record."""

    result = tx.run(query, **parameters)
    record = result.single()
    return dict(record) if record is not None else None


def _consume(tx: Any, query: str, **parameters: Any) -> None:
    """Execute a statement and consume its result before leaving the transaction."""

    tx.run(query, **parameters).consume()


def _block_properties(block: MemoryBlock) -> dict[str, Any]:
    """Return stored Neo4j node properties for an encoded block."""

    if isinstance(block.version, bool) or not isinstance(block.version, int) or block.version < 1:
        raise ValueError("block version must be an integer greater than or equal to one.")
    return {
        "id": block.id,
        "type": block.type,
        "value_json": _dump_json(block.value, field_name="block value"),
        "content_type": block.content_type,
        "metadata_json": _dump_json(block.metadata, field_name="block metadata"),
        "policy_json": _encode_policy(block.policy),
        "created_at": _encode_datetime(block.created_at, field_name="created_at"),
        "updated_at": _encode_datetime(block.updated_at, field_name="updated_at"),
        "created_by": block.created_by,
        "version": block.version,
    }


def _block_from_record(record: Mapping[str, Any]) -> MemoryBlock:
    """Rebuild a memory block from one stored Neo4j record."""

    try:
        data = record["block"]
        return MemoryBlock(
            id=str(data["id"]),
            parent_id=str(record["parent_id"]) if record.get("parent_id") is not None else None,
            type=str(data["type"]),
            value=_load_json(data["value_json"]),
            content_type=str(data["content_type"]),
            metadata=_load_json(data["metadata_json"]),
            policy=_decode_policy(data.get("policy_json")),
            created_at=_decode_datetime(data["created_at"]),
            updated_at=_decode_datetime(data["updated_at"]),
            created_by=str(data["created_by"]) if data.get("created_by") is not None else None,
            version=int(data["version"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Neo4j memory block is invalid.") from error


def _link_properties(link: MemoryLink) -> dict[str, Any]:
    """Return stored Neo4j node properties for a link."""

    return {
        "id": link.id,
        "source_id": link.source_id,
        "target_id": link.target_id,
        "relation": link.relation,
        "metadata_json": _dump_json(link.metadata, field_name="link metadata"),
        "created_at": _encode_datetime(link.created_at, field_name="created_at"),
    }


def _link_from_record(record: Mapping[str, Any]) -> MemoryLink:
    """Rebuild a memory link from one stored Neo4j record."""

    try:
        data = record["link"]
        return MemoryLink(
            id=str(data["id"]),
            source_id=str(data["source_id"]),
            target_id=str(data["target_id"]),
            relation=str(data["relation"]),
            metadata=_load_json(data["metadata_json"]),
            created_at=_decode_datetime(data["created_at"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Neo4j memory link is invalid.") from error


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _validate_expected_version(previous: MemoryBlock, replacement: MemoryBlock, expected_version: int | None) -> None:
    """Raise when a conditional replacement does not match its version."""

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version {expected_version}.")
    if replacement.version != expected_version + 1:
        raise MemoryConflict("A conditional block replacement must increment its version by one.")


def _binding_from_record(record: Mapping[str, Any]) -> KeyBinding:
    """Rebuild a key binding from one stored Neo4j record."""

    from genie.structures.records import KeyBinding, MemoryAddress

    try:
        data = record["binding"]
        return KeyBinding(
            collection_id=str(data["collection_id"]),
            address=MemoryAddress(namespace=_decode_namespace(data["namespace_json"]), key=str(data["key"])),
            block_id=str(data["block_id"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Neo4j key binding is invalid.") from error


def _log_entry_from_record(record: Mapping[str, Any]) -> LogEntry:
    """Rebuild a log entry from one stored Neo4j record."""

    from genie.structures.records import LogEntry

    try:
        data = record["entry"]
        return LogEntry(log_id=str(data["log_id"]), sequence=int(data["sequence"]), block_id=str(data["block_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Neo4j log entry is invalid.") from error


def _temporal_properties(entry: TemporalEntry) -> dict[str, Any]:
    """Return stored Neo4j node properties for a temporal entry."""

    return {
        "identity": _identity(entry.collection_id, entry.block_id),
        "collection_id": entry.collection_id,
        "block_id": entry.block_id,
        "occurred_at": _encode_datetime(entry.occurred_at, field_name="occurred_at") if entry.occurred_at is not None else None,
        "valid_at": _encode_datetime(entry.validity.valid_at, field_name="valid_at") if entry.validity is not None else None,
        "invalid_at": (_encode_datetime(entry.validity.invalid_at, field_name="invalid_at") if entry.validity is not None and entry.validity.invalid_at is not None else None),
    }


def _temporal_from_record(record: Mapping[str, Any]) -> TemporalEntry:
    """Rebuild a temporal entry from one stored Neo4j record."""

    from genie.structures.records import TemporalEntry, ValidityInterval

    try:
        data = record["entry"]
        validity = None
        if data.get("valid_at") is not None:
            validity = ValidityInterval(
                valid_at=_decode_datetime(data["valid_at"]),
                invalid_at=_decode_datetime(data["invalid_at"]) if data.get("invalid_at") is not None else None,
            )
        return TemporalEntry(
            collection_id=str(data["collection_id"]),
            block_id=str(data["block_id"]),
            occurred_at=_decode_datetime(data["occurred_at"]) if data.get("occurred_at") is not None else None,
            validity=validity,
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Neo4j temporal entry is invalid.") from error


def _encode_policy(policy: MemoryPolicy | None) -> str | None:
    """Encode an access policy as stored JSON, or `None` when unset."""

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
    """Decode a stored access policy, or `None` when unset."""

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
    """Encode one access rule as a JSON-compatible mapping."""

    return {
        "allow_all": rule.allow_all,
        "principals": sorted(rule.principals),
        "roles": sorted(rule.roles),
        "allow_owner": rule.allow_owner,
    }


def _decode_rule(payload: Mapping[str, Any]) -> AccessRule:
    """Decode one access rule from its stored mapping."""

    return AccessRule(
        allow_all=payload["allow_all"],
        principals=frozenset(payload["principals"]),
        roles=frozenset(payload["roles"]),
        allow_owner=payload["allow_owner"],
    )


def _dump_json(value: Any, *, field_name: str) -> str:
    """Serialize strict JSON data to a compact, key-sorted string."""

    if not _is_strict_json(value):
        raise TypeError(f"Neo4jBackend {field_name} must be strict JSON data.")
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: object) -> Any:
    """Parse a stored JSON string and require strict JSON data."""

    if not isinstance(value, str):
        raise BackendCorruption("Stored Neo4j JSON values must be strings.")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise BackendCorruption("Stored Neo4j JSON is invalid.") from error
    if not _is_strict_json(decoded):
        raise BackendCorruption("Stored Neo4j JSON is not strict JSON data.")
    return decoded


def _is_strict_json(value: Any) -> bool:
    """Return whether a value is JSON data with finite numbers only."""

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
    """Encode an aware datetime as a UTC ISO 8601 string."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _decode_datetime(value: object) -> datetime:
    """Decode a stored ISO 8601 timestamp into an aware UTC datetime."""

    if not isinstance(value, str):
        raise BackendCorruption("Stored Neo4j timestamps must be strings.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise BackendCorruption("Stored Neo4j timestamp is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackendCorruption("Stored Neo4j timestamps must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _encode_namespace(namespace: tuple[str, ...]) -> str:
    """Encode key namespace segments as a stored JSON string."""

    return _dump_json(list(namespace), field_name="key namespace")


def _decode_namespace(value: object) -> tuple[str, ...]:
    """Decode stored key namespace segments into a tuple."""

    decoded = _load_json(value)
    if not isinstance(decoded, list) or not all(isinstance(segment, str) for segment in decoded):
        raise BackendCorruption("Stored Neo4j key namespaces must be string lists.")
    return tuple(decoded)


def _namespace_matches(candidate: tuple[str, ...], selected: tuple[str, ...] | None, *, recursive: bool) -> bool:
    """Return whether a namespace satisfies an exact or prefix filter."""

    if selected is None:
        return True
    if recursive:
        return candidate[: len(selected)] == selected
    return candidate == selected


def _identity(*parts: object) -> str:
    """Build one stable composite identity for a uniqueness constraint."""

    return json.dumps(parts, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _edges_have_cycle(edges: list[tuple[str, str]]) -> bool:
    """Return whether a set of directed edges contains a cycle."""

    adjacency: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = defaultdict(int)
    nodes: set[str] = set()
    for source_id, target_id in edges:
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


def _edge_path_exists(edges: list[tuple[str, str]], start_id: str, target_id: str) -> bool:
    """Return whether directed edges connect a start block to a target."""

    adjacency: dict[str, list[str]] = defaultdict(list)
    for source_id, next_id in edges:
        adjacency[source_id].append(next_id)
    pending = deque([start_id])
    seen: set[str] = set()
    while pending:
        node_id = pending.popleft()
        if node_id == target_id:
            return True
        if node_id in seen:
            continue
        seen.add(node_id)
        pending.extend(adjacency[node_id])
    return False


def _validate_sequence(value: object, *, name: str) -> None:
    """Require a non-negative integer sequence value."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an integer.")
    if value < 0:
        raise ValueError(f"{name} must be greater than or equal to zero.")


def _validate_limit(value: object) -> None:
    """Require a non-negative integer page limit."""

    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("limit must be an integer.")
    if value < 0:
        raise ValueError("limit must be greater than or equal to zero.")


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Validate and normalize an optional aware datetime to UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)


def _validate_nonempty_string(value: object, *, name: str) -> None:
    """Require a non-empty string configuration value."""

    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string.")
    if not value:
        raise ValueError(f"{name} must not be empty.")


def _validate_timeout(value: object, *, name: str) -> None:
    """Require a finite, non-negative timeout in seconds."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number.")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be a finite number greater than or equal to zero.")


def _load_neo4j_driver() -> tuple[Any, tuple[type[BaseException], ...]]:
    """Import the optional driver and return it with its exception types."""

    try:
        import neo4j
    except ImportError as error:
        raise UnsupportedOperation("Neo4jBackend requires the optional 'neo4j' package; install 'genie[neo4j]'.") from error
    exception_types = tuple(
        dict.fromkeys(
            exception_type
            for exception_type in (
                getattr(neo4j.exceptions, "Neo4jError", None),
                getattr(neo4j.exceptions, "DriverError", None),
            )
            if isinstance(exception_type, type)
        )
    )
    return neo4j.GraphDatabase, exception_types


def _translate_neo4j_error(error: BaseException) -> GenieError:
    """Translate official-driver failures into stable Genie backend errors."""

    code = str(getattr(error, "code", "") or "")
    message = str(error) or error.__class__.__name__
    normalized = f"{code} {message} {error.__class__.__name__}".casefold()
    if any(text in normalized for text in ("deadlock", "lockclient", "lock timeout", "concurrentaccess")):
        return BackendBusy(f"Neo4j remained busy past its retry window: {message}")
    if any(text in normalized for text in ("readonly", "read only", "write operations are not allowed")):
        return BackendReadOnly(f"Neo4j storage is read-only or does not permit writes: {message}")
    if any(text in normalized for text in ("memory limit", "outofmemory", "out of memory", "no space", "capacity")):
        return BackendCapacityExceeded(f"Neo4j storage capacity was exceeded: {message}")
    if any(text in normalized for text in ("corrupt", "storecopyfailed", "invalid store")):
        return BackendCorruption(f"Neo4j storage is corrupt or invalid: {message}")
    if any(text in normalized for text in ("constraintvalidationfailed", "constraintviolation", "already exists with label")):
        return MemoryConflict(f"Neo4j rejected a conflicting memory write: {message}")
    if any(text in normalized for text in ("security.unauthorized", "security.forbidden", "authentication")):
        return BackendError(f"Neo4j authentication or authorization failed: {message}")
    if any(
        text in normalized
        for text in (
            "serviceunavailable",
            "sessionexpired",
            "routingserviceunavailable",
            "database.database not found",
            "databasenotfound",
            "connection",
            "network",
        )
    ):
        return BackendUnavailable(f"Neo4j storage is unavailable: {message}")
    is_retryable = getattr(error, "is_retryable", None)
    if callable(is_retryable) and is_retryable():
        return BackendUnavailable(f"Neo4j remained unavailable past its retry window: {message}")
    return BackendError(f"Neo4j backend operation failed: {message}")
