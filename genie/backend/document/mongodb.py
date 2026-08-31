"""Persistent MongoDB backend implementing every Genie storage capability.

Storage layout:
    One MongoDB database holds every Genie collection, and it defaults to
    `"genie"`. Several Genie datasets can share one server by using different
    database names, the same way `PostgresBackend` uses a schema. Collection
    names match the table names in `SQLiteBackend` and `PostgresBackend`, so
    the same records live in the same shape there.

Encodings:
    Values, metadata, policies, key namespaces, and timestamps use the same
    portable encodings `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend`
    use. Block values and metadata are stored as JSON text rather than as BSON
    subdocuments, for the same reason `PostgresBackend` stores `text` instead
    of `jsonb`: BSON cannot
    round-trip every JSON object. A key containing a dot, a key starting with
    `$`, and an integer larger than 64 bits all change or fail on the way in.
    Storing the encoded text keeps the value byte for byte and keeps caller
    data out of field names.

Writes:
    Every write runs inside one multi-document transaction, and the first
    thing it does is increment a shared guard document. Two writers cannot
    hold that document at once, so a compound "read, check, then write" step
    is atomic against every other Genie writer. This is the serialized-writer
    model `SQLiteBackend` gets from `BEGIN IMMEDIATE` and `PostgresBackend`
    gets from an advisory lock. MongoDB has no lock a transaction can wait on,
    so the writer that loses the guard is retried instead of blocked.

Reads:
    A read that fits in one command needs nothing extra, because MongoDB
    applies one command atomically. A read made of several commands runs
    inside a transaction so it sees one snapshot.

Deployment:
    Multi-document transactions need a replica set or a sharded cluster. A
    standalone `mongod` cannot run them, so construction refuses one unless
    `verify_transaction_support=False` is passed.
"""

from __future__ import annotations

import json
import math
from collections import defaultdict, deque
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from threading import Condition, RLock
from time import monotonic, sleep
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
_SCHEMA_DOCUMENT_ID = "genie"
_MUTATION_GUARD = "genie.mutations"
_BLOCK_ORDER_COUNTER = "blocks"
_LINK_ORDER_COUNTER = "links"

# MongoDB index and sort directions. They are plain integers, so the module
# stays importable without the optional driver.
_ASCENDING = 1
_DESCENDING = -1

# One `$in` list is part of a command document, and a command document may not
# exceed 16 MB. Long id lists are split into chunks of this size.
_QUERY_CHUNK = 500

_FIRST_BACKOFF_SECONDS = 0.001
_MAX_BACKOFF_SECONDS = 0.05

# MongoDB database name rules. The server rejects these characters, so the
# constructor reports them before the driver opens a connection.
_MAX_DATABASE_NAME_BYTES = 63
_INVALID_DATABASE_NAME_CHARACTERS = frozenset('/\\. "$*<>:|?\x00')

_SCHEMA_INFO = "schema_info"
_GUARD = "guard"
_COUNTERS = "counters"
_BLOCKS = "blocks"
_LINKS = "links"
_ACYCLIC_RELATIONS = "acyclic_relations"
_KEY_BINDINGS = "key_bindings"
_LOG_COUNTERS = "log_counters"
_LOG_ENTRIES = "log_entries"
_TEMPORAL_ENTRIES = "temporal_entries"

class _IndexSpec:
    """One MongoDB index owned by Genie.

    Attributes:
        name: Explicit index name. Genie names every index it creates, so the
            names stay the same on every server version.
        keys: Indexed fields in order, each with its direction.
        unique: Whether the index enforces uniqueness.
    """

    __slots__ = ("keys", "name", "unique")

    def __init__(self, name: str, keys: tuple[tuple[str, int], ...], *, unique: bool = False) -> None:
        """Describe one owned index.

        Args:
            name: Explicit index name.
            keys: Indexed fields in order, each with its direction.
            unique: Whether the index enforces uniqueness.
        """

        self.name = name
        self.keys = keys
        self.unique = unique


# Every Genie collection, with the indexes Genie creates for it. MongoDB adds
# the `_id_` index to each collection on its own, so it is not listed here.
_SCHEMA_COLLECTIONS: dict[str, tuple[_IndexSpec, ...]] = {
    _SCHEMA_INFO: (),
    _GUARD: (),
    _COUNTERS: (),
    _BLOCKS: (
        _IndexSpec("blocks_storage_order_idx", (("storage_order", _ASCENDING),), unique=True),
        _IndexSpec("blocks_parent_idx", (("parent_id", _ASCENDING), ("storage_order", _ASCENDING))),
        _IndexSpec("blocks_type_idx", (("type", _ASCENDING),)),
    ),
    _LINKS: (
        _IndexSpec("links_storage_order_idx", (("storage_order", _ASCENDING),), unique=True),
        _IndexSpec("links_source_idx", (("source_id", _ASCENDING), ("relation", _ASCENDING), ("storage_order", _ASCENDING))),
        _IndexSpec("links_target_idx", (("target_id", _ASCENDING), ("relation", _ASCENDING), ("storage_order", _ASCENDING))),
        _IndexSpec("links_relation_idx", (("relation", _ASCENDING), ("source_id", _ASCENDING), ("target_id", _ASCENDING))),
    ),
    _ACYCLIC_RELATIONS: (),
    _KEY_BINDINGS: (
        _IndexSpec("key_bindings_address_idx", (("collection_id", _ASCENDING), ("namespace_json", _ASCENDING), ("key", _ASCENDING)), unique=True),
        _IndexSpec("key_bindings_block_idx", (("block_id", _ASCENDING),)),
    ),
    _LOG_COUNTERS: (),
    _LOG_ENTRIES: (
        _IndexSpec("log_entries_sequence_idx", (("log_id", _ASCENDING), ("sequence", _ASCENDING)), unique=True),
        _IndexSpec("log_entries_block_idx", (("block_id", _ASCENDING),)),
    ),
    _TEMPORAL_ENTRIES: (
        _IndexSpec("temporal_entries_block_idx", (("collection_id", _ASCENDING), ("block_id", _ASCENDING)), unique=True),
        _IndexSpec("temporal_occurred_idx", (("collection_id", _ASCENDING), ("occurred_at", _ASCENDING), ("block_id", _ASCENDING))),
        _IndexSpec("temporal_validity_idx", (("collection_id", _ASCENDING), ("valid_at", _ASCENDING), ("invalid_at", _ASCENDING), ("block_id", _ASCENDING))),
    ),
}

# Server error codes that mean the same thing on every MongoDB deployment.
_CONFLICT_CODES = frozenset({11000, 11001})
_BUSY_CODES = frozenset({24, 46, 50, 112, 225, 239, 251, 262, 290})
_READ_ONLY_CODES = frozenset({13, 10107, 13435, 13436})
_CAPACITY_CODES = frozenset({10334, 17280, 292})
_UNAVAILABLE_CODES = frozenset({6, 7, 64, 89, 91, 134, 189, 11600, 11602})
# The subset that means the deployment itself could not be reached.
_TRANSPORT_CODES = frozenset({6, 7, 89, 91, 11600})


class _RetryTransaction(Exception):
    """Internal signal that the whole transaction must run again."""


class MongoDBBackend:
    """Persistent document backend backed by the official `pymongo` driver.

    The `pymongo` package is an optional dependency. Install it with
    `pip install 'genie[mongodb]'` before constructing this backend.

    The backend implements the required `MemoryBackend` behavior and every
    optional structure capability. One client is owned by each instance. Every
    write runs inside a multi-document transaction that first takes a shared
    guard document, which serializes Genie writers across instances and across
    processes.

    Multi-document transactions need a replica set or a sharded cluster, so a
    standalone `mongod` is not a supported target.

    Attributes:
        uri: MongoDB connection string this backend opened.
        database: MongoDB database that holds every Genie collection.
        connect_timeout: Seconds allowed while selecting a server and opening
            a connection.
        socket_timeout: Seconds allowed while waiting for one command reply.
        max_retry_time: Maximum seconds a transaction may keep losing conflicts
            before `BackendBusy` is raised.
        durability: Write acknowledgement mode in use.
    """

    def __init__(
        self,
        uri: str,
        *,
        auth: tuple[str, str] | None = None,
        database: str = "genie",
        connect_timeout: float = 30.0,
        socket_timeout: float = 30.0,
        max_retry_time: float = 30.0,
        durability: Literal["full", "normal"] = "full",
        verify_server_role: bool = True,
        verify_transaction_support: bool = True,
        initialize_schema: bool = True,
    ) -> None:
        """Connect and create, migrate, or validate the Genie collections.

        Construction talks to the server, so a successful construction already
        proves the deployment is reachable. There is no separate connectivity
        flag.

        Args:
            uri: MongoDB connection string, such as
                `"mongodb://localhost:27017/?replicaSet=rs0"`.
            auth: Optional `(username, password)` pair that overrides any
                credentials in `uri`.
            database: MongoDB database that holds every Genie collection.
            connect_timeout: Seconds allowed while selecting a server and
                opening a connection. It must be greater than zero, because
                server selection needs a budget.
            socket_timeout: Seconds allowed while waiting for one command
                reply. `0` disables the timeout.
            max_retry_time: Maximum seconds a transaction may keep losing
                conflicts to other writers before `BackendBusy` is raised.
                `0` disables the retry, so the first conflict reaches the
                caller.
            durability: Write acknowledgement mode. `"full"` waits for a
                majority of members and for the journal, and reads
                majority-committed data. `"normal"` acknowledges on the
                primary alone, which is faster but lets a failover roll back
                the most recent commits.
            verify_server_role: Whether to refuse a connection that lands on a
                node that does not accept writes, such as a direct connection
                to a secondary. The check refuses only when the server reports
                that it is not writable.
            verify_transaction_support: Whether to refuse a deployment that
                cannot run multi-document transactions. The check refuses only
                when the server reports that it is a standalone `mongod`.
            initialize_schema: Whether to create the missing Genie collections
                and indexes. Set this to `False` for runtime identities that
                may use existing data but may not administer collections.

        Raises:
            TypeError: If an argument has the wrong type.
            ValueError: If `uri`, `database`, or a timeout is invalid.
            UnsupportedOperation: If the driver is unavailable, the connection
                lands on a node that refuses writes, the deployment cannot run
                transactions, or the stored schema is incompatible.
            BackendError: If MongoDB cannot be reached or configured.
        """

        _validate_nonempty_string(uri, name="uri")
        _validate_database_name(database)
        _validate_positive_timeout(connect_timeout, name="connect_timeout")
        _validate_timeout(socket_timeout, name="socket_timeout")
        _validate_timeout(max_retry_time, name="max_retry_time")
        if auth is not None and (not isinstance(auth, tuple) or len(auth) != 2 or not all(isinstance(item, str) for item in auth)):
            raise TypeError("auth must be a (username, password) tuple of strings.")
        if not isinstance(durability, str):
            raise TypeError("durability must be a string.")
        if durability not in {"full", "normal"}:
            raise ValueError("durability must be either 'full' or 'normal'.")
        if not isinstance(verify_server_role, bool):
            raise TypeError("verify_server_role must be a boolean.")
        if not isinstance(verify_transaction_support, bool):
            raise TypeError("verify_transaction_support must be a boolean.")
        if not isinstance(initialize_schema, bool):
            raise TypeError("initialize_schema must be a boolean.")

        driver, read_concern_type, write_concern_type, exception_types = _load_pymongo()
        self.uri = uri
        self.database = database
        self.connect_timeout = float(connect_timeout)
        self.socket_timeout = float(socket_timeout)
        self.max_retry_time = float(max_retry_time)
        self.durability = durability
        self._driver = driver
        self._errors = driver.errors
        self._exception_types = exception_types
        self._lock = RLock()
        self._lifecycle = Condition(self._lock)
        self._active_operations = 0
        self._closing = False
        self._client: Any | None = None

        # `"full"` reads only majority-committed data, so nothing it returns
        # can be rolled back by a failover. `"normal"` acknowledges on the
        # primary alone, so its reads must use `"local"` to stay
        # read-your-own-writes consistent.
        if durability == "full":
            self._write_concern = write_concern_type(w="majority", j=True)
            self._read_concern = read_concern_type("majority")
            self._transaction_read_concern = read_concern_type("snapshot")
        else:
            self._write_concern = write_concern_type(w=1)
            self._read_concern = read_concern_type("local")
            self._transaction_read_concern = read_concern_type("local")
        self._read_preference = driver.ReadPreference.PRIMARY

        client_options: dict[str, Any] = {
            "appname": "genie",
            "connectTimeoutMS": int(self.connect_timeout * 1000),
            "serverSelectionTimeoutMS": int(self.connect_timeout * 1000),
            "socketTimeoutMS": int(self.socket_timeout * 1000) if self.socket_timeout else None,
        }
        if auth is not None:
            client_options["username"], client_options["password"] = auth

        try:
            client = driver.MongoClient(uri, **client_options)
            self._client = client
            self._database_handle = client.get_database(
                database,
                read_concern=self._read_concern,
                write_concern=self._write_concern,
                read_preference=self._read_preference,
            )
            self._schema_info = self._database_handle[_SCHEMA_INFO]
            self._guard = self._database_handle[_GUARD]
            self._counters = self._database_handle[_COUNTERS]
            self._blocks = self._database_handle[_BLOCKS]
            self._links = self._database_handle[_LINKS]
            self._acyclic_relations = self._database_handle[_ACYCLIC_RELATIONS]
            self._key_bindings = self._database_handle[_KEY_BINDINGS]
            self._log_counters = self._database_handle[_LOG_COUNTERS]
            self._log_entries = self._database_handle[_LOG_ENTRIES]
            self._temporal_entries = self._database_handle[_TEMPORAL_ENTRIES]
            if verify_server_role or verify_transaction_support:
                description = self._read_server_description()
                if verify_server_role:
                    self._verify_server_role(description)
                if verify_transaction_support:
                    self._verify_transaction_support(description)
            if initialize_schema:
                self._initialize_schema()
            else:
                self._validate_schema()
        except GenieError:
            self._discard_client()
            raise
        except exception_types as error:
            self._discard_client()
            raise _translate_mongodb_error(error) from error
        except BaseException:
            self._discard_client()
            raise

    def __enter__(self) -> Self:
        """Return this open backend for use as a context manager.

        Returns:
            This backend instance.

        Raises:
            RuntimeError: If the backend is already closed.
        """

        self._require_client()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the owned client when leaving a context manager.

        Args:
            exc_type: Type of the exception leaving the block, if any.
            exc: Exception instance leaving the block, if any.
            traceback: Traceback of the exception leaving the block, if any.
        """

        self.close()

    def close(self) -> None:
        """Wait for active operations and close the owned client safely.

        Calling `close` more than once is safe. Operations after closing raise
        `RuntimeError`.

        Raises:
            BackendError: If MongoDB fails while closing.
        """

        with self._lifecycle:
            while self._closing:
                self._lifecycle.wait()
            client = self._client
            if client is None:
                return
            self._closing = True
            while self._active_operations:
                self._lifecycle.wait()
            self._client = None
        try:
            client.close()
        except self._exception_types as error:
            raise _translate_mongodb_error(error) from error
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
            Stored block reconstructed from MongoDB.

        Raises:
            MemoryConflict: If the id exists or the parent is invalid.
            TypeError: If the encoded value or metadata is not strict JSON.
        """

        document = _block_document(block)

        def operation(session: Any) -> MemoryBlock:
            if self._find_block(session, block.id) is not None:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            self._validate_parent(session, block.id, block.parent_id)
            document["storage_order"] = self._next_storage_order(session, _BLOCK_ORDER_COUNTER)
            try:
                self._blocks.insert_one(document, session=session)
            except self._errors.DuplicateKeyError as error:
                raise MemoryConflict(f"Memory block {block.id!r} could not be created.") from error
            stored = self._find_block(session, block.id)
            assert stored is not None
            return _block_from_document(stored)

        return self._write(operation)

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Fetch a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            An isolated block value, or `None` when it does not exist.
        """

        def operation(session: Any) -> MemoryBlock | None:
            document = self._find_block(session, block_id)
            return _block_from_document(document) if document is not None else None

        return self._read_one(operation)

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
            Stored replacement block reconstructed from MongoDB.

        Raises:
            MemoryConflict: If the block is missing, its version changed, or
                its parent is invalid.
            TypeError: If the encoded value or metadata is not strict JSON.
        """

        document = _block_document(block)

        def operation(session: Any) -> MemoryBlock:
            previous_document = self._find_block(session, block.id)
            if previous_document is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            previous = _block_from_document(previous_document)
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._validate_parent(session, block.id, block.parent_id)
                if block.parent_id is not None and self._contains(session, block.id, block.parent_id):
                    raise MemoryConflict("A memory block cannot be moved under one of its descendants.")
            document["storage_order"] = int(previous_document["storage_order"])
            selector: dict[str, Any] = {"_id": block.id}
            if expected_version is not None:
                selector["version"] = expected_version
            result = self._blocks.replace_one(selector, document, session=session)
            if result.matched_count != 1:
                raise MemoryConflict(f"Memory block {block.id!r} changed before it could be updated.")
            stored = self._find_block(session, block.id)
            assert stored is not None
            return _block_from_document(stored)

        return self._write(operation)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a subtree, optionally from an exact version snapshot.

        MongoDB has no cascading reference, so the adapter removes the links,
        key bindings, log entries, and temporal entries of the deleted blocks
        itself. Every removal lands in the same transaction as the deletion.

        Args:
            block_id: Root block id to delete. Missing ids are ignored.
            expected_versions: Exact subtree id-to-version snapshot required
                for the deletion.

        Raises:
            MemoryConflict: If the supplied subtree snapshot is stale.
        """

        def operation(session: Any) -> None:
            documents = self._descendant_documents(session, block_id, include_self=True)
            current = {str(document["_id"]): int(document["version"]) for document in documents}
            if expected_versions is not None and current != dict(expected_versions):
                raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            if not current:
                return
            block_ids = list(current)
            for chunk in _chunked(block_ids, _QUERY_CHUNK):
                self._links.delete_many({"$or": [{"source_id": {"$in": chunk}}, {"target_id": {"$in": chunk}}]}, session=session)
                self._key_bindings.delete_many({"$or": [{"collection_id": {"$in": chunk}}, {"block_id": {"$in": chunk}}]}, session=session)
                self._log_entries.delete_many({"$or": [{"log_id": {"$in": chunk}}, {"block_id": {"$in": chunk}}]}, session=session)
                self._log_counters.delete_many({"_id": {"$in": chunk}}, session=session)
                self._temporal_entries.delete_many({"$or": [{"collection_id": {"$in": chunk}}, {"block_id": {"$in": chunk}}]}, session=session)
                self._blocks.delete_many({"_id": {"$in": chunk}}, session=session)

        self._write(operation)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children in creation order.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks in creation order.
        """

        def operation(session: Any) -> list[MemoryBlock]:
            cursor = self._blocks.find({"parent_id": block_id}, session=session).sort("storage_order", _ASCENDING)
            return [_block_from_document(document) for document in cursor]

        return self._read_one(operation)

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from the nearest parent upward.

        Args:
            block_id: Block id to inspect.

        Returns:
            Ancestor blocks in nearest-parent-first order.
        """

        return self._read(lambda session: [_block_from_document(document) for document in self._ancestor_documents(session, block_id)])

    def descendants(
        self,
        block_id: str,
        *,
        include_self: bool = False,
    ) -> list[MemoryBlock]:
        """Return descendants in deterministic depth-first order.

        Args:
            block_id: Root block id.
            include_self: Whether to include `block_id` in the result.

        Returns:
            Descendant blocks in depth-first creation order.
        """

        return self._read(lambda session: [_block_from_document(document) for document in self._descendant_documents(session, block_id, include_self=include_self)])

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is equal to or below `root_id`.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is the root or one of its descendants.
        """

        return self._read(lambda session: self._contains(session, root_id, block_id))

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

        document = _binding_document(binding)

        def operation(session: Any) -> KeyBinding:
            self._require_structure_blocks(
                session,
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            if self._key_bindings.find_one({"_id": document["_id"]}, session=session) is not None:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in collection {binding.collection_id!r}.")
            try:
                self._key_bindings.insert_one(document, session=session)
            except self._errors.DuplicateKeyError as error:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in collection {binding.collection_id!r}.") from error
            return binding

        return self._write(operation)

    def rebind_key(
        self,
        binding: KeyBinding,
        *,
        expected_block_id: str,
    ) -> KeyBinding:
        """Atomically replace a binding when its block id still matches.

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

        identity = _identity(binding.collection_id, _encode_namespace(binding.address.namespace), binding.address.key)

        def operation(session: Any) -> KeyBinding:
            self._require_structure_blocks(
                session,
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            result = self._key_bindings.update_one(
                {"_id": identity, "block_id": expected_block_id},
                {"$set": {"block_id": binding.block_id}},
                session=session,
            )
            if result.matched_count != 1:
                raise MemoryConflict(f"Key address {binding.address!r} in collection {binding.collection_id!r} no longer points to expected block {expected_block_id!r}.")
            return binding

        return self._write(operation)

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
            Stored binding, or `None` when the address is unused.
        """

        identity = _identity(collection_id, _encode_namespace(address.namespace), address.key)

        def operation(session: Any) -> KeyBinding | None:
            document = self._key_bindings.find_one({"_id": identity}, session=session)
            return _binding_from_document(document) if document is not None else None

        return self._read_one(operation)

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

        Namespaces are stored as JSON text, so the namespace filter and the
        address ordering are applied in Python. This keeps the result identical
        to the other shipped adapters.

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
        if limit == 0:
            return []
        documents = self._read_one(lambda session: list(self._key_bindings.find({"collection_id": collection_id}, session=session)))
        bindings = [
            _binding_from_document(document)
            for document in documents
            if _namespace_matches(_decode_namespace(document["namespace_json"]), namespace, recursive=recursive)
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
        """Atomically remove a binding when its block id still matches.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to remove.
            expected_block_id: Block id the binding must currently point to.

        Raises:
            MemoryConflict: If the address is unbound or it no longer points
                to `expected_block_id`.
        """

        identity = _identity(collection_id, _encode_namespace(address.namespace), address.key)

        def operation(session: Any) -> None:
            result = self._key_bindings.delete_one({"_id": identity, "block_id": expected_block_id}, session=session)
            if result.deleted_count != 1:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} no longer points to expected block {expected_block_id!r}.")

        self._write(operation)

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove bindings owned by or pointing to one deleted block.

        Args:
            block_id: Id of the deleted block. Bindings whose collection id or
                target block id match are removed.
        """

        self._write(lambda session: self._key_bindings.delete_many({"$or": [{"collection_id": block_id}, {"block_id": block_id}]}, session=session))

    # ------------------------------------------------------------------
    # Ordered event log

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Atomically allocate a sequence and append a block reference.

        The counter document is created or advanced by one update, so the
        allocation needs no separate read. Sequence numbers stay gapless,
        because an aborted transaction rolls the counter back with everything
        else it wrote.

        Args:
            log_id: Id of the ordered log to append to.
            block_id: Id of the memory block to place at the new position.

        Returns:
            Stored entry carrying the allocated sequence number.

        Raises:
            MemoryConflict: If the log or referenced block does not exist.
        """

        from genie.structures.records import LogEntry

        def operation(session: Any) -> LogEntry:
            self._require_structure_blocks(session, container_id=log_id, block_id=block_id, container_name="Event log")
            previous = self._log_counters.find_one_and_update(
                {"_id": log_id},
                {"$inc": {"next_sequence": 1}},
                upsert=True,
                return_document=self._driver.ReturnDocument.BEFORE,
                session=session,
            )
            sequence = 0 if previous is None else int(previous["next_sequence"])
            try:
                self._log_entries.insert_one(
                    {"_id": _identity(log_id, sequence), "log_id": log_id, "sequence": sequence, "block_id": block_id},
                    session=session,
                )
            except self._errors.DuplicateKeyError as error:
                raise MemoryConflict(f"Event log {log_id!r} could not allocate sequence {sequence}.") from error
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
        identity = _identity(log_id, sequence)

        def operation(session: Any) -> LogEntry | None:
            document = self._log_entries.find_one({"_id": identity}, session=session)
            return _log_entry_from_document(document) if document is not None else None

        return self._read_one(operation)

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
        if limit == 0:
            return []
        selector: dict[str, Any] = {"log_id": log_id}
        bounds: dict[str, int] = {}
        if start is not None:
            bounds["$gte"] = start
        if stop is not None:
            bounds["$lt"] = stop
        if bounds:
            selector["sequence"] = bounds
        direction = _DESCENDING if reverse else _ASCENDING

        def operation(session: Any) -> list[LogEntry]:
            cursor = self._log_entries.find(selector, session=session).sort("sequence", direction).limit(limit)
            return [_log_entry_from_document(document) for document in cursor]

        return self._read_one(operation)

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove a log and entries owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. A log owned by this id is
                dropped, along with entries pointing to it in any other log.
        """

        def operation(session: Any) -> None:
            self._log_entries.delete_many({"$or": [{"log_id": block_id}, {"block_id": block_id}]}, session=session)
            self._log_counters.delete_one({"_id": block_id}, session=session)

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

        document = _temporal_document(entry)

        def operation(session: Any) -> TemporalEntry:
            self._require_structure_blocks(
                session,
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            if self._temporal_entries.find_one({"_id": document["_id"]}, session=session) is not None:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal collection {entry.collection_id!r}.")
            try:
                self._temporal_entries.insert_one(document, session=session)
            except self._errors.DuplicateKeyError as error:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal collection {entry.collection_id!r}.") from error
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

        document = _temporal_document(entry)

        def operation(session: Any) -> TemporalEntry:
            self._require_structure_blocks(
                session,
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            result = self._temporal_entries.replace_one({"_id": document["_id"]}, document, session=session)
            if result.matched_count != 1:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal collection {entry.collection_id!r}.")
            return entry

        return self._write(operation)

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

        identity = _identity(collection_id, block_id)

        def operation(session: Any) -> TemporalEntry | None:
            document = self._temporal_entries.find_one({"_id": identity}, session=session)
            return _temporal_from_document(document) if document is not None else None

        return self._read_one(operation)

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
        if limit == 0:
            return []
        selector: dict[str, Any] = {"collection_id": collection_id}
        if after_block_id is not None:
            selector["block_id"] = {"$gt": after_block_id}

        def operation(session: Any) -> list[TemporalEntry]:
            cursor = self._temporal_entries.find(selector, session=session).sort("block_id", _ASCENDING).limit(limit)
            return [_temporal_from_document(document) for document in cursor]

        return self._read_one(operation)

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block to remove.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        identity = _identity(collection_id, block_id)

        def operation(session: Any) -> None:
            result = self._temporal_entries.delete_one({"_id": identity}, session=session)
            if result.deleted_count != 1:
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection {collection_id!r}.")

        self._write(operation)

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove temporal entries owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. Entries whose collection id or
                target block id match are removed.
        """

        self._write(lambda session: self._temporal_entries.delete_many({"$or": [{"collection_id": block_id}, {"block_id": block_id}]}, session=session))

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

        Timestamps are stored as ISO 8601 UTC strings and compared as strings.
        MongoDB compares strings byte by byte unless a collection declares a
        locale collation, and Genie refuses a collection that declares one, so
        the comparison stays the same on every server.

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
        if (occurred_start is not None and occurred_stop is not None) and occurred_stop < occurred_start:
            raise ValueError("occurred_stop must be greater than or equal to occurred_start.")
        if overlaps is not None and not isinstance(overlaps, ValidityInterval):
            raise TypeError("overlaps must be a ValidityInterval.")
        _validate_limit(limit)
        if limit == 0:
            return []

        clauses: list[dict[str, Any]] = [{"collection_id": collection_id}]
        if after_block_id is not None:
            clauses.append({"block_id": {"$gt": after_block_id}})
        occurred_bounds: dict[str, Any] = {}
        if occurred_only:
            occurred_bounds["$ne"] = None
        if occurred_start is not None:
            occurred_bounds["$gte"] = _encode_datetime(occurred_start, field_name="occurred_start")
        if occurred_stop is not None:
            occurred_bounds["$lt"] = _encode_datetime(occurred_stop, field_name="occurred_stop")
        if occurred_bounds:
            clauses.append({"occurred_at": occurred_bounds})
        if valid_at is not None:
            encoded_valid_at = _encode_datetime(valid_at, field_name="valid_at")
            clauses.append({"valid_at": {"$ne": None, "$lte": encoded_valid_at}})
            clauses.append({"$or": [{"invalid_at": None}, {"invalid_at": {"$gt": encoded_valid_at}}]})
        if overlaps is not None:
            encoded_overlap_start = _encode_datetime(overlaps.valid_at, field_name="overlaps.valid_at")
            clauses.append({"valid_at": {"$ne": None}})
            clauses.append({"$or": [{"invalid_at": None}, {"invalid_at": {"$gt": encoded_overlap_start}}]})
            if overlaps.invalid_at is not None:
                clauses.append({"valid_at": {"$lt": _encode_datetime(overlaps.invalid_at, field_name="overlaps.invalid_at")}})
        selector = clauses[0] if len(clauses) == 1 else {"$and": clauses}

        def operation(session: Any) -> list[TemporalEntry]:
            cursor = self._temporal_entries.find(selector, session=session).sort("block_id", _ASCENDING).limit(limit)
            return [_temporal_from_document(document) for document in cursor]

        return self._read_one(operation)

    # ------------------------------------------------------------------
    # Typed links and lineage DAG

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

        def operation(session: Any) -> MemoryLink:
            if self._relation_is_protected(session, link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic graph; use atomic acyclic insertion.")
            self._validate_new_link(session, link)
            return self._insert_link(session, link)

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

        def operation(session: Any) -> MemoryLink:
            self._validate_new_link(session, link)
            edges = [
                (str(document["source_id"]), str(document["target_id"]))
                for document in self._links.find({"relation": link.relation}, {"source_id": 1, "target_id": 1}, session=session)
            ]
            protected = self._relation_is_protected(session, link.relation)
            if not protected and _edges_have_cycle(edges):
                raise MemoryConflict(f"Link relation {link.relation!r} already contains a cycle.")
            if link.source_id == link.target_id or _edge_path_exists(edges, link.target_id, link.source_id):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create a cycle in relation {link.relation!r}.")
            if not protected:
                self._acyclic_relations.insert_one({"_id": link.relation}, session=session)
            return self._insert_link(session, link)

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

        def operation(session: Any) -> MemoryLink:
            document = self._find_link(session, link.id)
            if document is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            previous = _link_from_document(document)
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            self._links.update_one({"_id": link.id}, {"$set": {"metadata_json": metadata_json}}, session=session)
            updated = self._find_link(session, link.id)
            assert updated is not None
            return _link_from_document(updated)

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
            selector: dict[str, Any] = {"source_id": block_id}
        elif direction == "in":
            selector = {"target_id": block_id}
        else:
            selector = {"$or": [{"source_id": block_id}, {"target_id": block_id}]}
        if relation is not None:
            selector = {"$and": [selector, {"relation": relation}]}

        def operation(session: Any) -> list[MemoryLink]:
            cursor = self._links.find(selector, session=session).sort("storage_order", _ASCENDING)
            return [_link_from_document(document) for document in cursor]

        return self._read_one(operation)

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete incoming and outgoing links for one block.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        self._write(lambda session: self._links.delete_many({"$or": [{"source_id": block_id}, {"target_id": block_id}]}, session=session))

    # ------------------------------------------------------------------
    # Portable search

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks using the portable in-memory matching behavior.

        Candidate documents are narrowed by scope in MongoDB, then matched in
        Python so results stay consistent with the reference backend.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results ordered by descending score, limited to
            `query.limit`.
        """

        candidates = self._read(lambda session: [_block_from_document(document) for document in self._scope_candidate_documents(session, query)])
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
    # Client, schema, and transaction helpers

    def _read_server_description(self) -> Mapping[str, Any] | None:
        """Return one `hello` reply, or `None` when the server does not answer.

        A connection failure is left to reach the caller, because it means the
        deployment is unreachable rather than unknown. Every other failure
        leaves the description unknown, and unknown passes both checks.

        Returns:
            The server reply, or `None` when it cannot be read.
        """

        client = self._require_client()
        for command in ("hello", "ismaster"):
            try:
                return dict(client.admin.command(command))
            except self._exception_types as error:
                if _is_connection_failure(error):
                    raise
        return None

    def _verify_server_role(self, description: Mapping[str, Any] | None) -> None:
        """Refuse a connection that lands on a node which refuses writes.

        A secondary refuses every write, so a backend opened against one fails
        on the first `put_block`. With `initialize_schema=False` it can open,
        never write, and serve data that lags the primary with no signal.

        The check refuses only on proof. A missing field, an unreadable value,
        and a refused command all leave the role unknown, and unknown passes.

        The role is read once. A failover or a proxy can still move this
        connection to a secondary later, and that is not detected.

        Args:
            description: Server reply to read the role from.

        Raises:
            UnsupportedOperation: If the server reports that it is not
                writable.
        """

        writable = _reported_writable(description)
        if writable is not False:
            return
        raise UnsupportedOperation(
            "MongoDB reports that this connection lands on a node that does not accept writes, such as a secondary reached with a direct connection. Point the URI at the "
            "deployment instead of at one node, or pass verify_server_role=False to accept failing writes and stale reads."
        )

    def _verify_transaction_support(self, description: Mapping[str, Any] | None) -> None:
        """Refuse a deployment that cannot run multi-document transactions.

        Every Genie write is one transaction, so a standalone `mongod` fails on
        the first write. A replica set and a sharded cluster both support them.

        The check refuses only on proof: the reply must name no replica set,
        must not come from a `mongos`, and must report a writable server. Any
        other answer leaves the deployment unknown, and unknown passes.

        Args:
            description: Server reply to read the deployment kind from.

        Raises:
            UnsupportedOperation: If the server reports that it is a standalone
                `mongod`.
        """

        if description is None:
            return
        if description.get("msg") == "isdbgrid":
            return
        set_name = description.get("setName")
        if isinstance(set_name, str) and set_name:
            return
        if _reported_writable(description) is not True:
            return
        raise UnsupportedOperation(
            "MongoDB reports a standalone server, which cannot run the multi-document transactions every Genie write needs. Use a replica set, even a single-member one, or a "
            "sharded cluster, or pass verify_transaction_support=False to accept failing writes."
        )

    def _initialize_schema(self) -> None:
        """Create missing collections and indexes, then validate the result."""

        state = self._read_schema_identity()
        version = 0 if state is None else self._schema_version(state, allow_older=True)
        if state is None and self._database_collections() - {_SCHEMA_INFO}:
            raise UnsupportedOperation(f"The MongoDB database {self.database!r} holds collections with no Genie schema record; use an empty database or a validated Genie database.")
        for target_version in range(version + 1, _SCHEMA_VERSION + 1):
            self._apply_schema_migration(target_version)
            self._write_schema_identity(target_version)

        # Repair a missing owned object at the current version. Every step is
        # idempotent, so an interrupted initialization can be resumed safely.
        self._ensure_schema_objects()
        self._validate_schema()

    def _apply_schema_migration(self, target_version: int) -> None:
        """Apply one idempotent schema migration.

        Args:
            target_version: Schema version to move to.

        Raises:
            UnsupportedOperation: If no migration exists for the version.
        """

        if target_version != 1:
            raise UnsupportedOperation(f"No MongoDB migration is available for schema version {target_version}.")
        self._ensure_schema_objects()

    def _ensure_schema_objects(self) -> None:
        """Create every missing collection, index, and control document."""

        existing = self._database_collections()
        for name, indexes in _SCHEMA_COLLECTIONS.items():
            if name not in existing:
                with suppress(self._errors.CollectionInvalid, self._errors.OperationFailure):
                    self._database_handle.create_collection(name)
            collection = self._database_handle[name]
            for index in indexes:
                collection.create_index(list(index.keys), name=index.name, unique=index.unique)
        if self._guard.find_one({"_id": _MUTATION_GUARD}) is None:
            with suppress(self._errors.DuplicateKeyError):
                self._guard.insert_one({"_id": _MUTATION_GUARD, "lock_version": 0})

    def _write_schema_identity(self, version: int) -> None:
        """Advance the owned schema record without overwriting another owner.

        Args:
            version: Schema version to record.

        Raises:
            UnsupportedOperation: If the record belongs to another application.
        """

        state = self._read_schema_identity()
        if state is None:
            with suppress(self._errors.DuplicateKeyError):
                self._schema_info.insert_one({"_id": _SCHEMA_DOCUMENT_ID, "application_id": _APPLICATION_ID, "version": version})
            state = self._read_schema_identity()
        if state is None:
            raise UnsupportedOperation("The Genie MongoDB schema record could not be created.")
        if state.get("application_id") != _APPLICATION_ID:
            raise UnsupportedOperation(f"The MongoDB database {self.database!r} belongs to another application ({state.get('application_id')!r}).")
        self._schema_info.update_one(
            {"_id": _SCHEMA_DOCUMENT_ID, "application_id": _APPLICATION_ID, "version": {"$lt": version}},
            {"$set": {"version": version}},
        )

    def _validate_schema(self) -> None:
        """Require a matching schema record and the exact owned objects.

        Raises:
            UnsupportedOperation: If the database is not initialized or holds
                an incompatible schema.
        """

        state = self._read_schema_identity()
        if state is None:
            raise UnsupportedOperation(f"The Genie MongoDB database {self.database!r} is not initialized.")
        self._schema_version(state, allow_older=False)
        self._validate_schema_objects()

    def _read_schema_identity(self) -> Mapping[str, Any] | None:
        """Return the stored Genie schema record, or `None` when absent."""

        if _SCHEMA_INFO not in self._database_collections():
            return None
        return self._schema_info.find_one({"_id": _SCHEMA_DOCUMENT_ID})

    def _schema_version(self, state: Mapping[str, Any], *, allow_older: bool) -> int:
        """Validate schema ownership and return its supported version.

        Args:
            state: Stored schema record.
            allow_older: Whether an older version may be migrated forward.

        Returns:
            Stored schema version.

        Raises:
            UnsupportedOperation: If the record belongs to another application
                or holds an unsupported version.
        """

        application_id = state.get("application_id")
        version = state.get("version")
        if application_id != _APPLICATION_ID:
            raise UnsupportedOperation(f"The MongoDB database {self.database!r} belongs to another application ({application_id!r}).")
        if isinstance(version, bool) or not isinstance(version, int):
            raise UnsupportedOperation("The Genie MongoDB schema version is invalid.")
        if version > _SCHEMA_VERSION:
            raise UnsupportedOperation(f"MongoDB schema version {version} is newer than supported version {_SCHEMA_VERSION}.")
        if version < _SCHEMA_VERSION and not allow_older:
            raise UnsupportedOperation(f"No MongoDB migration is available from schema version {version}.")
        return version

    def _validate_schema_objects(self) -> None:
        """Require the exact collections, indexes, and control documents.

        Raises:
            UnsupportedOperation: If the database holds unexpected collections,
                an incompatible index, a locale collation, or no guard
                document.
        """

        actual_collections = self._database_collections()
        expected_collections = set(_SCHEMA_COLLECTIONS)
        if actual_collections != expected_collections:
            raise UnsupportedOperation(f"The Genie MongoDB database has unexpected collections: expected {sorted(expected_collections)!r}, found {sorted(actual_collections)!r}.")

        for specification in self._database_handle.list_collections():
            name = str(specification.get("name"))
            if name not in _SCHEMA_COLLECTIONS:
                continue
            if specification.get("type", "collection") != "collection":
                raise UnsupportedOperation(f"The Genie MongoDB name {name!r} is not a plain collection.")
            options = specification.get("options") or {}
            if options.get("collation") is not None:
                # A locale collation changes both index order and string
                # comparison, and Genie compares stored timestamps and pages
                # block ids as plain byte order.
                raise UnsupportedOperation(f"The Genie MongoDB collection {name!r} declares a locale collation, which changes string order; recreate it without one.")

        for name, indexes in _SCHEMA_COLLECTIONS.items():
            expected = {"_id_": ((("_id", _ASCENDING),), False)}
            for index in indexes:
                expected[index.name] = (index.keys, index.unique)
            actual = {
                str(information["name"]): (tuple((str(field), int(direction)) for field, direction in information["key"].items()), bool(information.get("unique", False)))
                for information in self._database_handle[name].list_indexes()
            }
            if actual != expected:
                raise UnsupportedOperation(f"The Genie MongoDB collection {name!r} has incompatible indexes: expected {sorted(expected)!r}, found {sorted(actual)!r}.")

        if self._guard.find_one({"_id": _MUTATION_GUARD}) is None:
            raise UnsupportedOperation("The Genie MongoDB mutation guard document is missing.")

    def _database_collections(self) -> set[str]:
        """Return every non-system collection name in the Genie database."""

        return {name for name in self._database_handle.list_collection_names() if not name.startswith("system.")}

    def _read_one(self, operation: Callable[[Any], _T]) -> _T:
        """Run one read that issues a single command, with no transaction.

        MongoDB applies one command atomically, so a read of this shape needs
        no snapshot and can never retry.

        Args:
            operation: Callable that receives the session and reads once.

        Returns:
            Whatever the callable returns.
        """

        try:
            with self._operation_client() as client, client.start_session() as session:
                return operation(session)
        except GenieError:
            raise
        except self._exception_types as error:
            raise _translate_mongodb_error(error) from error

    def _read(self, operation: Callable[[Any], _T]) -> _T:
        """Run one multi-command read inside a snapshot transaction.

        Args:
            operation: Callable that receives the session and reads.

        Returns:
            Whatever the callable returns.
        """

        return self._transaction(operation)

    def _write(self, operation: Callable[[Any], _T]) -> _T:
        """Run one serialized write transaction.

        The guard document is taken before the callable reads anything, so a
        compound "read, check, then write" step is atomic against every other
        Genie writer.

        Args:
            operation: Callable that receives the session and writes.

        Returns:
            Whatever the callable returns.
        """

        def guarded(session: Any) -> _T:
            self._lock_mutations(session)
            return operation(session)

        return self._transaction(guarded)

    def _lock_mutations(self, session: Any) -> None:
        """Take the database-resident lock shared by every Genie write.

        Args:
            session: Session running the write transaction.
        """

        self._guard.find_one_and_update(
            {"_id": _MUTATION_GUARD},
            {"$inc": {"lock_version": 1}},
            upsert=True,
            return_document=self._driver.ReturnDocument.AFTER,
            session=session,
        )

    def _transaction(self, operation: Callable[[Any], _T]) -> _T:
        """Run one transaction and retry it while it keeps losing conflicts.

        MongoDB has no lock a transaction can wait on, so a writer that loses
        the guard document is aborted rather than blocked. The loop restarts it
        until it wins or `max_retry_time` runs out.

        Args:
            operation: Callable that receives the session.

        Returns:
            Whatever the callable returns.

        Raises:
            BackendBusy: If the transaction kept conflicting past
                `max_retry_time`.
            BackendUnavailable: If the commit result stayed unknown past
                `max_retry_time`.
        """

        deadline = monotonic() + self.max_retry_time
        backoff = _FIRST_BACKOFF_SECONDS
        with self._operation_client() as client, client.start_session() as session:
            while True:
                session.start_transaction(
                    read_concern=self._transaction_read_concern,
                    write_concern=self._write_concern,
                    read_preference=self._read_preference,
                )
                try:
                    result = operation(session)
                except BaseException as error:
                    self._abort_quietly(session)
                    if self._is_transient(error):
                        if monotonic() < deadline:
                            backoff = _sleep_backoff(backoff)
                            continue
                        raise BackendBusy(f"MongoDB kept rejecting a conflicting transaction: {error}") from error
                    if isinstance(error, GenieError):
                        raise
                    if isinstance(error, self._exception_types):
                        raise _translate_mongodb_error(error) from error
                    raise
                try:
                    self._commit(session, deadline)
                except _RetryTransaction:
                    backoff = _sleep_backoff(backoff)
                    continue
                return result

    def _commit(self, session: Any, deadline: float) -> None:
        """Commit one transaction, retrying an unknown commit result.

        Args:
            session: Session holding the open transaction.
            deadline: Monotonic time the retry budget runs out at.

        Raises:
            _RetryTransaction: If the whole transaction must run again.
            BackendBusy: If the commit kept conflicting past the budget.
            BackendUnavailable: If the commit result stayed unknown past the
                budget.
        """

        while True:
            try:
                session.commit_transaction()
                return
            except self._errors.PyMongoError as error:
                unknown = error.has_error_label("UnknownTransactionCommitResult")
                transient = error.has_error_label("TransientTransactionError")
                if monotonic() < deadline:
                    if unknown:
                        sleep(_FIRST_BACKOFF_SECONDS)
                        continue
                    if transient:
                        raise _RetryTransaction from error
                if unknown:
                    raise BackendUnavailable(f"MongoDB did not report whether the transaction committed: {error}") from error
                if transient:
                    raise BackendBusy(f"MongoDB kept rejecting a conflicting transaction: {error}") from error
                raise _translate_mongodb_error(error) from error

    def _is_transient(self, error: BaseException) -> bool:
        """Return whether a failure means the transaction should run again.

        Args:
            error: Failure raised while the transaction ran.

        Returns:
            `True` when MongoDB labelled the failure as transient.
        """

        return isinstance(error, self._errors.PyMongoError) and error.has_error_label("TransientTransactionError")

    def _abort_quietly(self, session: Any) -> None:
        """Abort an open transaction, ignoring a failure while aborting.

        Args:
            session: Session that may hold an open transaction.
        """

        if session.in_transaction:
            with suppress(Exception):
                session.abort_transaction()

    @contextmanager
    def _operation_client(self) -> Generator[Any, None, None]:
        """Lease the client so `close` cannot race an active operation.

        Yields:
            The open MongoDB client.

        Raises:
            RuntimeError: If the backend is closed or closing.
        """

        with self._lifecycle:
            if self._client is None or self._closing:
                raise RuntimeError("MongoDBBackend is closed.")
            client = self._client
            self._active_operations += 1
        try:
            yield client
        finally:
            with self._lifecycle:
                self._active_operations -= 1
                if self._active_operations == 0:
                    self._lifecycle.notify_all()

    def _discard_client(self) -> None:
        """Drop the client reference and close it, ignoring failures."""

        with self._lifecycle:
            client = self._client
            self._client = None
        if client is None:
            return
        with suppress(Exception):
            client.close()

    def _require_client(self) -> Any:
        """Return the open client or fail after `close`.

        Returns:
            The open MongoDB client.

        Raises:
            RuntimeError: If the backend is closed or closing.
        """

        with self._lifecycle:
            if self._client is None or self._closing:
                raise RuntimeError("MongoDBBackend is closed.")
            return self._client

    # ------------------------------------------------------------------
    # Transaction-local data helpers

    def _next_storage_order(self, session: Any, name: str) -> int:
        """Allocate the next stable storage order value from a counter.

        Args:
            session: Session running the write transaction.
            name: Counter name, either `"blocks"` or `"links"`.

        Returns:
            The allocated storage order value.
        """

        previous = self._counters.find_one_and_update(
            {"_id": name},
            {"$inc": {"next_value": 1}},
            upsert=True,
            return_document=self._driver.ReturnDocument.BEFORE,
            session=session,
        )
        return 0 if previous is None else int(previous["next_value"])

    def _find_block(self, session: Any, block_id: str) -> Mapping[str, Any] | None:
        """Return one raw block document by id."""

        return self._blocks.find_one({"_id": block_id}, session=session)

    def _find_link(self, session: Any, link_id: str) -> Mapping[str, Any] | None:
        """Return one raw link document by id."""

        return self._links.find_one({"_id": link_id}, session=session)

    def _validate_parent(self, session: Any, block_id: str, parent_id: str | None) -> None:
        """Require a valid parent id for a create or move.

        Args:
            session: Session running the write transaction.
            block_id: Block being placed.
            parent_id: Requested parent id.

        Raises:
            MemoryConflict: If the parent is the block itself or is missing.
        """

        if parent_id == block_id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if parent_id is not None and self._find_block(session, parent_id) is None:
            raise MemoryConflict(f"Parent memory block {parent_id!r} does not exist.")

    def _ancestor_documents(self, session: Any, block_id: str) -> list[Mapping[str, Any]]:
        """Return ancestor documents in nearest-parent-first order.

        The walk keeps the ids it has seen, so a corrupt parent chain that
        loops stops instead of running forever.
        """

        document = self._find_block(session, block_id)
        ancestors: list[Mapping[str, Any]] = []
        seen = {block_id}
        while document is not None:
            parent_id = document.get("parent_id")
            if parent_id is None or parent_id in seen:
                break
            seen.add(str(parent_id))
            parent = self._find_block(session, str(parent_id))
            if parent is None:
                break
            ancestors.append(parent)
            document = parent
        return ancestors

    def _descendant_documents(self, session: Any, block_id: str, *, include_self: bool) -> list[Mapping[str, Any]]:
        """Return subtree documents in depth-first creation order.

        The subtree is read one ownership level at a time, then walked
        depth-first with each level of children in storage order. That gives
        the same order as the recursive query in `SQLiteBackend` and
        `PostgresBackend`.
        """

        root = self._find_block(session, block_id)
        if root is None:
            return []
        children: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        frontier = [block_id]
        seen = {block_id}
        while frontier:
            next_frontier: list[str] = []
            for chunk in _chunked(frontier, _QUERY_CHUNK):
                for document in self._blocks.find({"parent_id": {"$in": chunk}}, session=session):
                    child_id = str(document["_id"])
                    if child_id in seen:
                        continue
                    seen.add(child_id)
                    children[str(document["parent_id"])].append(document)
                    next_frontier.append(child_id)
            frontier = next_frontier
        for group in children.values():
            group.sort(key=lambda document: int(document["storage_order"]))
        ordered: list[Mapping[str, Any]] = []
        stack = [root]
        while stack:
            document = stack.pop()
            ordered.append(document)
            stack.extend(reversed(children[str(document["_id"])]))
        return ordered if include_self else ordered[1:]

    def _contains(self, session: Any, root_id: str, block_id: str) -> bool:
        """Return whether a block is equal to or below a subtree root.

        The check walks up from the candidate, which reads one document per
        level instead of the whole subtree.
        """

        if self._find_block(session, root_id) is None:
            return False
        if root_id == block_id:
            return True
        current = self._find_block(session, block_id)
        seen = {block_id}
        while current is not None:
            parent_id = current.get("parent_id")
            if parent_id is None or parent_id in seen:
                return False
            if parent_id == root_id:
                return True
            seen.add(str(parent_id))
            current = self._find_block(session, str(parent_id))
        return False

    def _require_structure_blocks(
        self,
        session: Any,
        *,
        container_id: str,
        block_id: str,
        container_name: str,
    ) -> None:
        """Require a structure container and referenced block to exist.

        Args:
            session: Session running the write transaction.
            container_id: Id of the structure container block.
            block_id: Id of the referenced member block.
            container_name: Human-readable container name for the error.

        Raises:
            MemoryConflict: If either block is missing.
        """

        if self._find_block(session, container_id) is None:
            raise MemoryConflict(f"{container_name} block {container_id!r} does not exist.")
        if self._find_block(session, block_id) is None:
            raise MemoryConflict(f"Memory block {block_id!r} does not exist.")

    def _validate_new_link(self, session: Any, link: MemoryLink) -> None:
        """Require a new link id and two stored endpoints.

        Args:
            session: Session running the write transaction.
            link: Link about to be stored.

        Raises:
            MemoryConflict: If the id exists or an endpoint is missing.
        """

        if self._find_link(session, link.id) is not None:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if self._find_block(session, link.source_id) is None:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if self._find_block(session, link.target_id) is None:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _insert_link(self, session: Any, link: MemoryLink) -> MemoryLink:
        """Insert a validated link inside the caller's transaction.

        Args:
            session: Session running the write transaction.
            link: Validated link to store.

        Returns:
            Stored link reconstructed from MongoDB.

        Raises:
            MemoryConflict: If the link could not be created.
        """

        document = _link_document(link)
        document["storage_order"] = self._next_storage_order(session, _LINK_ORDER_COUNTER)
        try:
            self._links.insert_one(document, session=session)
        except self._errors.DuplicateKeyError as error:
            raise MemoryConflict(f"Memory link {link.id!r} could not be created.") from error
        stored = self._find_link(session, link.id)
        assert stored is not None
        return _link_from_document(stored)

    def _relation_is_protected(self, session: Any, relation: str) -> bool:
        """Return whether a relation is protected as an acyclic graph."""

        return self._acyclic_relations.find_one({"_id": relation}, session=session) is not None

    def _scope_candidate_documents(self, session: Any, query: MemoryQuery) -> list[Mapping[str, Any]]:
        """Return raw candidate block documents for one normalized search scope."""

        if query.scope == Scope.GLOBAL or query.root_id is None:
            return self._all_block_documents(session)
        if self._find_block(session, query.root_id) is None:
            return []
        if query.scope == Scope.SELF:
            document = self._find_block(session, query.root_id)
            return [document] if document is not None else []
        if query.scope == Scope.SUBTREE:
            return self._descendant_documents(session, query.root_id, include_self=True)
        if query.scope == Scope.ANCESTORS:
            root = self._find_block(session, query.root_id)
            return ([root] if root is not None else []) + self._ancestor_documents(session, query.root_id)
        if query.scope == Scope.LINKS:
            connected: set[str] = set()
            selector = {"$or": [{"source_id": query.root_id}, {"target_id": query.root_id}]}
            for document in self._links.find(selector, {"source_id": 1, "target_id": 1}, session=session):
                connected.add(str(document["source_id"]))
                connected.add(str(document["target_id"]))
            if not connected:
                return []
            documents: list[Mapping[str, Any]] = []
            for chunk in _chunked(sorted(connected), _QUERY_CHUNK):
                documents.extend(self._blocks.find({"_id": {"$in": chunk}}, session=session))
            documents.sort(key=lambda document: int(document["storage_order"]))
            return documents
        return self._all_block_documents(session)

    def _all_block_documents(self, session: Any) -> list[Mapping[str, Any]]:
        """Return every stored block document in storage order."""

        return list(self._blocks.find({}, session=session).sort("storage_order", _ASCENDING))


def _block_document(block: MemoryBlock) -> dict[str, Any]:
    """Serialize a block into its stored MongoDB document.

    Args:
        block: Encoded block to serialize.

    Returns:
        Stored document without its storage order, which the caller allocates.

    Raises:
        TypeError: If the encoded value or metadata is not strict JSON.
        ValueError: If the block version is not a positive integer.
    """

    if isinstance(block.version, bool) or not isinstance(block.version, int) or block.version < 1:
        raise ValueError("block version must be an integer greater than or equal to one.")
    return {
        "_id": block.id,
        "parent_id": block.parent_id,
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


def _block_from_document(document: Mapping[str, Any]) -> MemoryBlock:
    """Reconstruct a memory block from one stored MongoDB document.

    Args:
        document: Stored block document.

    Returns:
        Reconstructed block.

    Raises:
        BackendCorruption: If the stored document cannot be read.
    """

    try:
        return MemoryBlock(
            id=str(document["_id"]),
            parent_id=str(document["parent_id"]) if document.get("parent_id") is not None else None,
            type=str(document["type"]),
            value=_load_json(document["value_json"]),
            content_type=str(document["content_type"]),
            metadata=_load_json(document["metadata_json"]),
            policy=_decode_policy(document.get("policy_json")),
            created_at=_decode_datetime(document["created_at"]),
            updated_at=_decode_datetime(document["updated_at"]),
            created_by=str(document["created_by"]) if document.get("created_by") is not None else None,
            version=int(document["version"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored MongoDB memory block is invalid.") from error


def _link_document(link: MemoryLink) -> dict[str, Any]:
    """Serialize a typed link into its stored MongoDB document."""

    return {
        "_id": link.id,
        "source_id": link.source_id,
        "target_id": link.target_id,
        "relation": link.relation,
        "metadata_json": _dump_json(link.metadata, field_name="link metadata"),
        "created_at": _encode_datetime(link.created_at, field_name="created_at"),
    }


def _link_from_document(document: Mapping[str, Any]) -> MemoryLink:
    """Reconstruct a typed link from one stored MongoDB document.

    Args:
        document: Stored link document.

    Returns:
        Reconstructed link.

    Raises:
        BackendCorruption: If the stored document cannot be read.
    """

    try:
        return MemoryLink(
            id=str(document["_id"]),
            source_id=str(document["source_id"]),
            target_id=str(document["target_id"]),
            relation=str(document["relation"]),
            metadata=_load_json(document["metadata_json"]),
            created_at=_decode_datetime(document["created_at"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored MongoDB memory link is invalid.") from error


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same stored relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _validate_expected_version(
    previous: MemoryBlock,
    replacement: MemoryBlock,
    expected_version: int | None,
) -> None:
    """Validate one optional compare-and-swap block replacement.

    Args:
        previous: Stored block being replaced.
        replacement: Replacement block.
        expected_version: Required current version, or `None`.

    Raises:
        MemoryConflict: If the version moved or the replacement does not
            increment it by one.
    """

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version {expected_version}.")
    if replacement.version != expected_version + 1:
        raise MemoryConflict("A conditional block replacement must increment its version by one.")


def _binding_document(binding: KeyBinding) -> dict[str, Any]:
    """Serialize a keyed binding into its stored MongoDB document."""

    namespace_json = _encode_namespace(binding.address.namespace)
    return {
        "_id": _identity(binding.collection_id, namespace_json, binding.address.key),
        "collection_id": binding.collection_id,
        "namespace_json": namespace_json,
        "key": binding.address.key,
        "block_id": binding.block_id,
    }


def _binding_from_document(document: Mapping[str, Any]) -> KeyBinding:
    """Reconstruct a keyed binding from one stored MongoDB document.

    Args:
        document: Stored binding document.

    Returns:
        Reconstructed binding.

    Raises:
        BackendCorruption: If the stored document cannot be read.
    """

    from genie.structures.records import KeyBinding, MemoryAddress

    try:
        return KeyBinding(
            collection_id=str(document["collection_id"]),
            address=MemoryAddress(namespace=_decode_namespace(document["namespace_json"]), key=str(document["key"])),
            block_id=str(document["block_id"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored MongoDB key binding is invalid.") from error


def _log_entry_from_document(document: Mapping[str, Any]) -> LogEntry:
    """Reconstruct a log entry from one stored MongoDB document.

    Args:
        document: Stored log entry document.

    Returns:
        Reconstructed entry.

    Raises:
        BackendCorruption: If the stored document cannot be read.
    """

    from genie.structures.records import LogEntry

    try:
        return LogEntry(log_id=str(document["log_id"]), sequence=int(document["sequence"]), block_id=str(document["block_id"]))
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored MongoDB log entry is invalid.") from error


def _temporal_document(entry: TemporalEntry) -> dict[str, Any]:
    """Serialize one temporal entry into its stored MongoDB document."""

    return {
        "_id": _identity(entry.collection_id, entry.block_id),
        "collection_id": entry.collection_id,
        "block_id": entry.block_id,
        "occurred_at": _encode_datetime(entry.occurred_at, field_name="occurred_at") if entry.occurred_at is not None else None,
        "valid_at": _encode_datetime(entry.validity.valid_at, field_name="valid_at") if entry.validity is not None else None,
        "invalid_at": (_encode_datetime(entry.validity.invalid_at, field_name="invalid_at") if entry.validity is not None and entry.validity.invalid_at is not None else None),
    }


def _temporal_from_document(document: Mapping[str, Any]) -> TemporalEntry:
    """Reconstruct one temporal entry from a stored MongoDB document.

    Args:
        document: Stored temporal entry document.

    Returns:
        Reconstructed entry.

    Raises:
        BackendCorruption: If the stored document cannot be read.
    """

    from genie.structures.records import TemporalEntry, ValidityInterval

    try:
        validity = None
        if document.get("valid_at") is not None:
            validity = ValidityInterval(
                valid_at=_decode_datetime(document["valid_at"]),
                invalid_at=_decode_datetime(document["invalid_at"]) if document.get("invalid_at") is not None else None,
            )
        return TemporalEntry(
            collection_id=str(document["collection_id"]),
            block_id=str(document["block_id"]),
            occurred_at=_decode_datetime(document["occurred_at"]) if document.get("occurred_at") is not None else None,
            validity=validity,
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored MongoDB temporal entry is invalid.") from error


def _encode_policy(policy: MemoryPolicy | None) -> str | None:
    """Serialize a memory policy as strict JSON text."""

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
    """Reconstruct a memory policy from its stored JSON text."""

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
    """Encode a strict JSON value without silently changing its type.

    Args:
        value: Value to encode.
        field_name: Field name used in the error message.

    Returns:
        Compact, key-sorted JSON text.

    Raises:
        TypeError: If the value is not strict JSON data.
    """

    if not _is_strict_json(value):
        raise TypeError(f"MongoDBBackend {field_name} must be strict JSON data.")
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_json(value: object) -> Any:
    """Decode one stored JSON text value into an isolated Python object.

    Args:
        value: Stored JSON text.

    Returns:
        Decoded strict JSON data.

    Raises:
        BackendCorruption: If the stored text is missing or invalid.
    """

    if not isinstance(value, str):
        raise BackendCorruption("Stored MongoDB JSON values must be strings.")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise BackendCorruption("Stored MongoDB JSON is invalid.") from error
    if not _is_strict_json(decoded):
        raise BackendCorruption("Stored MongoDB JSON is not strict JSON data.")
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
    """Normalize an aware datetime to an ISO 8601 UTC string.

    BSON dates hold milliseconds, so Genie stores the text form instead and
    keeps the full precision the caller supplied.

    Args:
        value: Datetime to encode.
        field_name: Field name used in the error message.

    Returns:
        ISO 8601 UTC text.

    Raises:
        TypeError: If the value is not a datetime.
        ValueError: If the datetime has no timezone.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _decode_datetime(value: object) -> datetime:
    """Decode one stored ISO 8601 datetime and normalize it to UTC.

    Args:
        value: Stored timestamp text.

    Returns:
        Aware UTC datetime.

    Raises:
        BackendCorruption: If the stored value is missing or invalid.
    """

    if not isinstance(value, str):
        raise BackendCorruption("Stored MongoDB timestamps must be strings.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise BackendCorruption("Stored MongoDB timestamp is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackendCorruption("Stored MongoDB timestamps must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _encode_namespace(namespace: tuple[str, ...]) -> str:
    """Encode a namespace tuple without losing segment boundaries."""

    return _dump_json(list(namespace), field_name="key namespace")


def _decode_namespace(value: object) -> tuple[str, ...]:
    """Decode one stored namespace tuple.

    Args:
        value: Stored namespace text.

    Returns:
        Namespace segments.

    Raises:
        BackendCorruption: If the stored value is not a list of strings.
    """

    decoded = _load_json(value)
    if not isinstance(decoded, list) or not all(isinstance(segment, str) for segment in decoded):
        raise BackendCorruption("Stored MongoDB key namespaces must be string lists.")
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


def _identity(*parts: object) -> str:
    """Build one stable composite `_id` from several record fields."""

    return json.dumps(parts, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _chunked(values: Sequence[str], size: int) -> Iterator[list[str]]:
    """Split a list of ids into command-sized chunks.

    Args:
        values: Ids to split.
        size: Maximum ids per chunk.

    Yields:
        One chunk of ids at a time.
    """

    for start in range(0, len(values), size):
        yield list(values[start : start + size])


def _edges_have_cycle(edges: Iterable[tuple[str, str]]) -> bool:
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


def _edge_path_exists(edges: Iterable[tuple[str, str]], start_id: str, target_id: str) -> bool:
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


def _sleep_backoff(backoff: float) -> float:
    """Wait once between two transaction attempts and grow the wait.

    Args:
        backoff: Seconds to wait before the next attempt.

    Returns:
        The wait to use after the next failure.
    """

    sleep(backoff)
    return min(backoff * 2, _MAX_BACKOFF_SECONDS)


def _reported_writable(description: Mapping[str, Any] | None) -> bool | None:
    """Return whether a server reply proves the node accepts writes.

    Args:
        description: Server reply, or `None` when it could not be read.

    Returns:
        `True` or `False` when the reply says so, and `None` when it does not.
    """

    if description is None:
        return None
    for field_name in ("isWritablePrimary", "ismaster"):
        value = description.get(field_name)
        if isinstance(value, bool):
            return value
    return None


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


def _validate_positive_timeout(value: object, *, name: str) -> None:
    """Require a finite timeout greater than zero.

    Server selection needs a real budget, so this timeout cannot be disabled.

    Args:
        value: Timeout supplied by the caller.
        name: Argument name used in the error message.

    Raises:
        TypeError: If the value is not a number.
        ValueError: If the value is not finite or is not greater than zero.
    """

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number.")
    if not math.isfinite(float(value)) or value <= 0:
        raise ValueError(f"{name} must be a finite number greater than zero.")


def _validate_database_name(value: object) -> None:
    """Require a database name MongoDB accepts.

    MongoDB refuses several characters in a database name, and limits its
    length. Checking here reports the problem before the driver opens a
    connection.

    Args:
        value: Database name supplied by the caller.

    Raises:
        TypeError: If the value is not a string.
        ValueError: If the value is empty, too long, or holds a character
            MongoDB refuses.
    """

    if not isinstance(value, str):
        raise TypeError("database must be a string.")
    if not value:
        raise ValueError("database must not be empty.")
    if len(value.encode("utf-8")) > _MAX_DATABASE_NAME_BYTES:
        raise ValueError(f"database must be at most {_MAX_DATABASE_NAME_BYTES} bytes long.")
    if any(character in _INVALID_DATABASE_NAME_CHARACTERS for character in value):
        raise ValueError("database must not contain a space, a dot, or any of /\\.\"$*<>:|?")


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Validate and normalize an optional aware datetime to UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)


def _load_pymongo() -> tuple[Any, Any, Any, tuple[type[BaseException], ...]]:
    """Import the optional driver and return the pieces the backend needs.

    Returns:
        The `pymongo` module, its `ReadConcern` type, its `WriteConcern` type,
        and the exception types the backend translates.

    Raises:
        UnsupportedOperation: If `pymongo` is not installed.
    """

    try:
        import bson.errors
        import pymongo
        import pymongo.errors
        from pymongo.read_concern import ReadConcern
        from pymongo.write_concern import WriteConcern
    except ImportError as error:
        raise UnsupportedOperation("MongoDBBackend requires the optional 'pymongo' package; install 'genie[mongodb]'.") from error
    return pymongo, ReadConcern, WriteConcern, (pymongo.errors.PyMongoError, bson.errors.BSONError)


def _error_code(error: BaseException) -> int:
    """Return the MongoDB server error code, or zero when there is none."""

    code = getattr(error, "code", None)
    return code if isinstance(code, int) and not isinstance(code, bool) else 0


def _is_connection_failure(error: BaseException) -> bool:
    """Return whether a driver failure means the deployment is unreachable."""

    if _error_code(error) in _TRANSPORT_CODES:
        return True
    names = {base.__name__ for base in type(error).__mro__}
    return bool(names & {"ConnectionFailure", "ServerSelectionTimeoutError", "AutoReconnect", "NetworkTimeout"})


def _translate_mongodb_error(error: BaseException) -> GenieError:
    """Translate driver-specific failures into stable Genie backend errors.

    MongoDB reports a numeric error code for every server-side failure, so the
    mapping reads that code instead of matching message text. Transport
    failures carry no code, and they are recognized by their driver exception
    type instead.

    Args:
        error: Failure raised by `pymongo` or `bson`.

    Returns:
        The matching `BackendError` subclass, or `MemoryConflict` when a
        uniqueness constraint rejected the write.
    """

    code = _error_code(error)
    message = str(error) or error.__class__.__name__
    names = {base.__name__ for base in type(error).__mro__}
    normalized = message.casefold()

    if code in _CONFLICT_CODES or "DuplicateKeyError" in names:
        return MemoryConflict(f"MongoDB rejected a conflicting memory write: {message}")
    if code in _BUSY_CODES or "ExecutionTimeout" in names or "WTimeoutError" in names:
        return BackendBusy(f"MongoDB remained busy past the configured timeout: {message}")
    if code in _READ_ONLY_CODES or "NotPrimaryError" in names:
        return BackendReadOnly(f"MongoDB storage is read-only or does not permit this write: {message}")
    if code in _CAPACITY_CODES or "DocumentTooLarge" in names or "quota" in normalized or "no space left" in normalized:
        return BackendCapacityExceeded(f"MongoDB storage capacity was exceeded: {message}")
    if "InvalidBSON" in names or "corrupt" in normalized:
        return BackendCorruption(f"MongoDB storage is corrupt or invalid: {message}")
    if code in _UNAVAILABLE_CODES or _is_connection_failure(error):
        return BackendUnavailable(f"MongoDB storage is unavailable: {message}")
    return BackendError(f"MongoDB backend operation failed: {message}")
