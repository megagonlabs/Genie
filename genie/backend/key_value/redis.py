"""Persistent Redis backend implementing every Genie storage capability.

Redis is a key-value store with server-side data structures, so this adapter
keeps one JSON document per block and per link in a plain Redis string, and it
keeps its own ordering, ownership, and reverse indexes in sorted sets, hashes,
and sets. No Redis module such as RedisJSON is required.

Every write runs as one optimistic read-modify-write attempt. The read phase
records the value of a shared mutation guard, and the commit phase re-checks
that guard under `WATCH` before it applies the staged writes in one
`MULTI`/`EXEC` block. Any other Genie write bumps the guard, so a conflicting
attempt is retried instead of committing on stale reads. This gives the same
serialized-writer behavior as SQLite and Neo4j, and it makes compound checks
and writes atomic across backend instances.

Multi-command reads need the opposite guarantee: one consistent view. Redis has
no multi-key snapshot, so commands sent one after another can straddle a write.
A read scoped to one block, meaning its parent chain, its children, its subtree,
or one `limit` page of a collection, therefore runs as one read-only Lua script
through `EVAL_RO`. Redis runs a script from start to finish before it serves
another client, so those reads never see half of a write and never retry. Reads
that scan the dataset stay on the client side and keep the mutation-guard retry,
because a long script would block every other client on a single-threaded server.

Search follows the same split. A search narrowed to one block, to its subtree, or
to its parent chain reads through the block-scoped path. A global search, and a
search over the links of one block, keep the mutation guard, because neither has
a bound on how much it reads.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict, deque
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
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
_COMMAND_CHUNK = 256
_READ_CHUNK = 1000
_TEMPORAL_SCAN_PAGE = 500
_FIRST_BACKOFF_SECONDS = 0.001
_MAX_BACKOFF_SECONDS = 0.05

# Read-only Lua script behind `contains`. `KEYS[1]` only routes the call on
# Redis Cluster; the script builds every key it reads from the prefix in
# `ARGV[1]`, so that layout must stay in step with `RedisBackend._block_key`.
# `ARGV[2]` is the subtree root id and `ARGV[3]` is the candidate block id. The
# walk mirrors `RedisBackend._contains`, including its guard against a cycle in
# stored parent ids.
_CONTAINS_SCRIPT = """
local prefix = ARGV[1]
local root_id = ARGV[2]
local block_id = ARGV[3]
if redis.call('EXISTS', prefix .. ':block:' .. root_id) == 0 then
  return 0
end
if root_id == block_id then
  return 1
end
local seen = {}
seen[block_id] = true
local current = block_id
while true do
  local payload = redis.call('GET', prefix .. ':block:' .. current)
  if not payload then
    return 0
  end
  local parent_id = cjson.decode(payload)['parent_id']
  if parent_id == nil or parent_id == cjson.null then
    return 0
  end
  if parent_id == root_id then
    return 1
  end
  if seen[parent_id] then
    return 0
  end
  seen[parent_id] = true
  current = parent_id
end
"""

# Read-only Lua script behind `ancestors` and behind an `ANCESTORS` scoped
# search. `ARGV[1]` is the key prefix and `ARGV[2]` is the block to start from.
# The reply is the stored document of that block followed by the document of each
# ancestor, nearest parent first, and it is empty when the starting block is
# gone. `ancestors` drops the first document and the scoped search keeps it, so
# one script serves both reads. The walk mirrors
# `RedisBackend._ancestor_documents`, including its guard against a cycle in
# stored parent ids and its stop at a parent id that no longer resolves.
_ANCESTORS_SCRIPT = """
local prefix = ARGV[1]
local block_id = ARGV[2]
local documents = {}
local seen = {}
seen[block_id] = true
local payload = redis.call('GET', prefix .. ':block:' .. block_id)
if payload then
  documents[#documents + 1] = payload
end
while payload do
  local parent_id = cjson.decode(payload)['parent_id']
  if parent_id == nil or parent_id == cjson.null or seen[parent_id] then
    break
  end
  seen[parent_id] = true
  local parent = redis.call('GET', prefix .. ':block:' .. parent_id)
  if not parent then
    break
  end
  documents[#documents + 1] = parent
  payload = parent
end
return documents
"""

# Read-only Lua script behind `children`. `ARGV[1]` is the key prefix and
# `ARGV[2]` is the parent block. The reply is the stored document of each direct
# child, in the creation order the child sorted set keeps. Like
# `RedisBackend._block_documents`, a child id whose document is gone is skipped
# instead of returned as a hole.
_CHILDREN_SCRIPT = """
local prefix = ARGV[1]
local block_id = ARGV[2]
local child_ids = redis.call('ZRANGE', prefix .. ':children:' .. block_id, 0, -1)
local documents = {}
for index = 1, #child_ids do
  local payload = redis.call('GET', prefix .. ':block:' .. child_ids[index])
  if payload then
    documents[#documents + 1] = payload
  end
end
return documents
"""

# Read-only Lua script behind `list_links`. `ARGV[1]` is the key prefix,
# `ARGV[2]` is the block to inspect, and `ARGV[3]` is `"out"`, `"in"`, or
# `"both"`. `ARGV[4]` is `"1"` when the relation filter applies and `"0"` when it
# does not, and `ARGV[5]` carries that relation. The filter needs its own flag
# because an empty string is a valid relation.
#
# An endpoint sorted set stores the member that `_identity(link_id, source_id,
# target_id, relation)` builds, so the script reads the relation from the member
# and fetches only the documents it will return. The reply is the stored document
# of each matching link in creation order. For `"both"` the script merges the two
# endpoint sets on the member and then orders them by the stored `storage_order`
# score, which is what `RedisBackend._connected_link_members` does on the client.
# That score is a strictly increasing counter, so two different links never tie.
# A link whose document is gone is skipped, matching
# `RedisBackend._link_documents`.
_LIST_LINKS_SCRIPT = """
local prefix = ARGV[1]
local block_id = ARGV[2]
local direction = ARGV[3]
local filtered = ARGV[4] == '1'
local relation = ARGV[5]
local members
if direction == 'out' then
  members = redis.call('ZRANGE', prefix .. ':outlinks:' .. block_id, 0, -1)
elseif direction == 'in' then
  members = redis.call('ZRANGE', prefix .. ':inlinks:' .. block_id, 0, -1)
else
  local scores = {}
  local merged = {}
  local endpoints = {prefix .. ':outlinks:' .. block_id, prefix .. ':inlinks:' .. block_id}
  for index = 1, #endpoints do
    local reply = redis.call('ZRANGE', endpoints[index], 0, -1, 'WITHSCORES')
    for position = 1, #reply, 2 do
      local member = reply[position]
      if scores[member] == nil then
        merged[#merged + 1] = member
      end
      scores[member] = tonumber(reply[position + 1])
    end
  end
  table.sort(merged, function(left, right) return scores[left] < scores[right] end)
  members = merged
end
local documents = {}
for index = 1, #members do
  local record = cjson.decode(members[index])
  if not filtered or record[4] == relation then
    local payload = redis.call('GET', prefix .. ':link:' .. record[1])
    if payload then
      documents[#documents + 1] = payload
    end
  end
end
return documents
"""

# Read-only Lua script behind `list_temporal_entries`. `ARGV[1]` is the key
# prefix, `ARGV[2]` is the collection, `ARGV[3]` is the lex range start that
# `_temporal_lex_start` built, and `ARGV[4]` is the page limit. The reply is a
# flat list of block id and stored payload, one pair per entry, so the caller can
# rebuild entries that carry their own block id. An id whose payload is gone is
# skipped, matching `RedisBackend._temporal_entries`. The page is read one field
# at a time instead of with `HMGET`, because `limit` comes from the caller and
# `unpack` on a very large page would overflow the Lua stack.
_LIST_TEMPORAL_ENTRIES_SCRIPT = """
local prefix = ARGV[1]
local collection_id = ARGV[2]
local lowest = ARGV[3]
local limit = tonumber(ARGV[4])
local block_ids = redis.call('ZRANGEBYLEX', prefix .. ':temporalindex:' .. collection_id, lowest, '+', 'LIMIT', 0, limit)
local entries = {}
for index = 1, #block_ids do
  local payload = redis.call('HGET', prefix .. ':temporal:' .. collection_id, block_ids[index])
  if payload then
    entries[#entries + 1] = block_ids[index]
    entries[#entries + 1] = payload
  end
end
return entries
"""

# Read-only Lua script behind `descendants`. `ARGV[1]` is the key prefix and
# `ARGV[2]` is the subtree root. The reply has two flat lists: the parent and
# child of every ownership edge the walk followed, and the id and stored document
# of every block it could read. The caller turns the edges back into depth-first
# creation order, so that ordering lives only in `_depth_first_ids`.
#
# The walk mirrors `RedisBackend._descendant_documents`. It reads child ids from
# the child sorted sets, which stay the authority on child order, and it keeps
# following a child whose document is gone so a hole in the middle of a subtree
# does not hide the blocks below it. A visited set stops a cycle in stored
# parent ids. This is the one scripted read whose work follows the size of the
# subtree rather than one block, so it is also the one that can hold the server
# for a noticeable time.
_DESCENDANTS_SCRIPT = """
local prefix = ARGV[1]
local root_id = ARGV[2]
local root_payload = redis.call('GET', prefix .. ':block:' .. root_id)
if not root_payload then
  return {{}, {}}
end
local edges = {}
local documents = {root_id, root_payload}
local visited = {}
visited[root_id] = true
local frontier = {root_id}
while #frontier > 0 do
  local following = {}
  for index = 1, #frontier do
    local parent_id = frontier[index]
    local child_ids = redis.call('ZRANGE', prefix .. ':children:' .. parent_id, 0, -1)
    for position = 1, #child_ids do
      local child_id = child_ids[position]
      if not visited[child_id] then
        visited[child_id] = true
        edges[#edges + 1] = parent_id
        edges[#edges + 1] = child_id
        following[#following + 1] = child_id
        local payload = redis.call('GET', prefix .. ':block:' .. child_id)
        if payload then
          documents[#documents + 1] = child_id
          documents[#documents + 1] = payload
        end
      end
    end
  end
  frontier = following
end
return {edges, documents}
"""

# Read-only Lua scripts by name, with the digest each call sends first. Three
# rules apply to every script added here. Build key names exactly like the
# matching `_*_key` helper. Read only keys that start with the prefix in
# `ARGV[1]`, because a script on Redis Cluster may touch only keys in the slot
# of the keys the call declares, and a hash-tagged `key_prefix` is what keeps
# every Genie key in one slot. And never put a `nil` or a `false` inside a table
# you return: the reply conversion stops the list at the first one, which would
# silently drop the rest of a page.
_LUA_SCRIPTS: dict[str, str] = {
    "ancestors": _ANCESTORS_SCRIPT,
    "children": _CHILDREN_SCRIPT,
    "contains": _CONTAINS_SCRIPT,
    "descendants": _DESCENDANTS_SCRIPT,
    "list_links": _LIST_LINKS_SCRIPT,
    "list_temporal_entries": _LIST_TEMPORAL_ENTRIES_SCRIPT,
}
_LUA_DIGESTS: dict[str, str] = {name: hashlib.sha1(body.encode("utf-8")).hexdigest() for name, body in _LUA_SCRIPTS.items()}


class RedisBackend:
    """Persistent backend backed by the official `redis` Python client.

    The `redis` package is an optional dependency. Install it with
    `pip install 'genie[redis]'` before constructing this backend.

    The backend implements the required `MemoryBackend` behavior and every
    optional structure capability. It owns the client it creates so it can
    require decoded string replies, and `close()` releases that client after
    active operations finish.

    A read scoped to one block, meaning its parent chain, its children, its
    subtree, or one `limit` page of a collection, runs as one read-only Lua
    script when the server supports `EVAL_RO`, so a write that lands beside it
    cannot tear its view. A server without that support, and
    a deployment that blocks scripting, fall back to the mutation-guard retry.

    Every Genie key starts with `key_prefix`, so several Genie datasets can
    share one Redis database. Write through the backend: writing to these keys
    directly can skip the reference, temporal, and cycle checks, and it does
    not bump the mutation guard that protects concurrent Genie writes.

    Genie keys carry no expiry, so the server must not evict them. Construction
    refuses a server that has a memory limit together with an `allkeys-*` value
    for `maxmemory-policy`, because Redis evicts one key at a time while one
    block spans a stored document plus several index entries. Use `noeviction`,
    or pass `verify_eviction_policy=False` to accept the risk.

    Point `url` at the primary. Construction refuses a connection that lands on
    a replica, because a replica refuses every write and its reads can lag the
    primary by any amount with no signal. A primary that has replicas passes.
    Pass `verify_server_role=False` to open a backend against a replica on
    purpose.

    Attributes:
        url: Redis URL this backend connected to.
        key_prefix: Key namespace that holds every Genie key.
        connect_timeout: Seconds allowed while establishing a connection.
        socket_timeout: Seconds allowed while waiting for one command reply.
        max_retry_time: Maximum seconds spent retrying an attempt that lost an
            optimistic conflict before `BackendBusy` is raised.
    """

    def __init__(
        self,
        url: str,
        *,
        auth: tuple[str, str] | None = None,
        key_prefix: str = "genie",
        connect_timeout: float = 10.0,
        socket_timeout: float = 30.0,
        max_retry_time: float = 30.0,
        verify_connectivity: bool = True,
        verify_server_role: bool = True,
        verify_eviction_policy: bool = True,
        initialize_schema: bool = True,
    ) -> None:
        """Open a client and initialize or validate the Genie key namespace.

        Args:
            url: Redis URL, such as `"redis://localhost:6379/0"`. Use the
                `rediss://` scheme for TLS.
            auth: Optional `(username, password)` pair that overrides any
                credentials in `url`.
            key_prefix: Key namespace that holds every Genie key. On Redis
                Cluster use a hash-tagged prefix such as `"{genie}"` so every
                key stays in one hash slot.
            connect_timeout: Seconds allowed while establishing a connection.
                `0` disables the timeout.
            socket_timeout: Seconds allowed while waiting for one command
                reply. `0` disables the timeout.
            max_retry_time: Maximum seconds spent retrying an attempt that
                lost an optimistic conflict before `BackendBusy` is raised.
            verify_connectivity: Whether to ping the server during
                construction.
            verify_server_role: Whether to refuse a connection that lands on a
                replica. The check reads `INFO replication` and fails only when
                the server reports a replica role, so a primary that has
                replicas passes. Set this to `False` to open a backend against
                a replica, which refuses every write and can serve stale reads.
            verify_eviction_policy: Whether to refuse a server that may evict
                Genie keys. The check reads `INFO memory` and fails only when
                the server reports a memory limit together with an `allkeys-*`
                eviction policy. Set this to `False` to accept the risk of
                losing keys.
            initialize_schema: Whether to create the missing Genie schema
                record. Set this to `False` for runtime identities that may
                use existing data but may not create it.

        Raises:
            TypeError: If an argument has the wrong type.
            ValueError: If `url`, `key_prefix`, or a timeout is invalid.
            UnsupportedOperation: If the client is unavailable, the connection
                lands on a replica, the server may evict Genie keys, or the
                stored schema record is incompatible.
            BackendError: If Redis cannot be reached, configured, or
                initialized.
        """

        _validate_nonempty_string(url, name="url")
        _validate_key_prefix(key_prefix)
        _validate_timeout(connect_timeout, name="connect_timeout")
        _validate_timeout(socket_timeout, name="socket_timeout")
        _validate_timeout(max_retry_time, name="max_retry_time")
        if auth is not None and (not isinstance(auth, tuple) or len(auth) != 2 or not all(isinstance(item, str) for item in auth)):
            raise TypeError("auth must be a (username, password) tuple of strings.")
        if not isinstance(verify_connectivity, bool):
            raise TypeError("verify_connectivity must be a boolean.")
        if not isinstance(verify_server_role, bool):
            raise TypeError("verify_server_role must be a boolean.")
        if not isinstance(verify_eviction_policy, bool):
            raise TypeError("verify_eviction_policy must be a boolean.")
        if not isinstance(initialize_schema, bool):
            raise TypeError("initialize_schema must be a boolean.")

        redis_module, exception_types, watch_error, no_script_error = _load_redis_client()
        self.url = url
        self.key_prefix = key_prefix
        self.connect_timeout = float(connect_timeout)
        self.socket_timeout = float(socket_timeout)
        self.max_retry_time = float(max_retry_time)
        self._exception_types = exception_types
        self._watch_error = watch_error
        self._no_script_error = no_script_error
        self._readonly_scripts = False
        self._lock = RLock()
        self._lifecycle = Condition(self._lock)
        self._active_operations = 0
        self._closing = False
        self._client: Any | None = None

        client_options: dict[str, Any] = {
            "decode_responses": True,
            "socket_connect_timeout": self.connect_timeout or None,
            "socket_timeout": self.socket_timeout or None,
        }
        if auth is not None:
            client_options["username"], client_options["password"] = auth
        try:
            client = redis_module.Redis.from_url(url, **client_options)
            self._client = client
            if verify_connectivity:
                client.ping()
            # The role check runs first. A replica that also evicts would
            # otherwise be reported as an eviction problem, which points the
            # caller at a setting on a server it should not be talking to.
            if verify_server_role:
                self._verify_server_role()
            if verify_eviction_policy:
                self._verify_eviction_policy()
            if initialize_schema:
                self._initialize_schema()
            else:
                self._validate_schema()
            self._readonly_scripts = self._detect_readonly_scripts()
        except UnsupportedOperation:
            self._discard_client()
            raise
        except self._exception_types as error:
            self._discard_client()
            raise _translate_redis_error(error) from error
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
            BackendError: If the client fails while disconnecting.
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
            raise _translate_redis_error(error) from error
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
            TypeError: If the encoded value or metadata is not strict JSON.
            BackendError: If the Redis write fails.
        """

        document = _encode_block(block)

        def operation(transaction: _RedisTransaction) -> MemoryBlock:
            if transaction.client.exists(self._block_key(block.id)):
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            self._validate_parent(transaction.client, block.id, block.parent_id)
            storage_order = self._reserve_storage_order(transaction, "blocks")
            stored = {**document, "storage_order": storage_order}
            self._stage_block_document(transaction, stored)
            transaction.stage(lambda pipeline: pipeline.zadd(self._blocks_key(), {block.id: storage_order}))
            if block.parent_id is not None:
                transaction.stage(lambda pipeline: pipeline.zadd(self._children_key(str(block.parent_id)), {block.id: storage_order}))
            return _block_from_document(stored)

        return self._transact(operation)

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Fetch a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            Stored block, or `None` when it does not exist.

        Raises:
            BackendError: If the Redis read fails.
        """

        return self._direct(lambda client: self._block(client, block_id))

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
            TypeError: If the encoded value or metadata is not strict JSON.
            BackendError: If the Redis write fails.
        """

        document = _encode_block(block)

        def operation(transaction: _RedisTransaction) -> MemoryBlock:
            previous_document = self._block_document(transaction.client, block.id)
            if previous_document is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            previous = _block_from_document(previous_document)
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._validate_parent(transaction.client, block.id, block.parent_id)
                if block.parent_id is not None and self._contains(transaction.client, block.id, block.parent_id):
                    raise MemoryConflict("A memory block cannot be moved under one of its descendants.")
            storage_order = _stored_int(previous_document.get("storage_order"), field_name="storage order")
            stored = {**document, "storage_order": storage_order}
            self._stage_block_document(transaction, stored)
            if previous.parent_id != block.parent_id:
                if previous.parent_id is not None:
                    transaction.stage(lambda pipeline: pipeline.zrem(self._children_key(str(previous.parent_id)), block.id))
                if block.parent_id is not None:
                    transaction.stage(lambda pipeline: pipeline.zadd(self._children_key(str(block.parent_id)), {block.id: storage_order}))
            return _block_from_document(stored)

        return self._transact(operation)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a subtree, optionally from an exact version snapshot.

        Args:
            block_id: Root block id to delete. Missing ids are ignored.
            expected_versions: Exact subtree id-to-version snapshot required
                for the deletion.

        Raises:
            MemoryConflict: If `expected_versions` no longer describes the
                complete subtree.
            BackendError: If the Redis write fails.
        """

        def operation(transaction: _RedisTransaction) -> None:
            documents = self._descendant_documents(transaction.client, block_id, include_self=True)
            if expected_versions is not None:
                current = {str(document["id"]): _stored_int(document.get("version"), field_name="block version") for document in documents}
                if current != dict(expected_versions):
                    raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            if not documents:
                return
            block_ids = {str(document["id"]) for document in documents}
            self._stage_link_removal(transaction, block_ids)
            self._stage_key_binding_removal(transaction, block_ids)
            self._stage_log_removal(transaction, block_ids)
            self._stage_temporal_removal(transaction, block_ids)
            root = next((document for document in documents if str(document["id"]) == block_id), None)
            parent_id = None if root is None else root.get("parent_id")
            if parent_id is not None and str(parent_id) not in block_ids:
                owner = str(parent_id)
                transaction.stage(lambda pipeline: pipeline.zrem(self._children_key(owner), block_id))
            self._stage_zrem(transaction, self._blocks_key(), sorted(block_ids))
            self._stage_delete(transaction, [self._block_key(identifier) for identifier in sorted(block_ids)])
            self._stage_delete(transaction, [self._children_key(identifier) for identifier in sorted(block_ids)])

        self._transact(operation)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children in creation order.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks in creation order.
        """

        if self._readonly_scripts:
            return self._script_blocks(self._read_script("children", keys=[self._children_key(block_id)], args=[self.key_prefix, block_id]))

        def operation(client: Any) -> list[MemoryBlock]:
            child_ids = client.zrange(self._children_key(block_id), 0, -1)
            return [_block_from_document(document) for document in self._block_documents(client, child_ids)]

        return self._snapshot(operation)

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from the nearest parent upward.

        Args:
            block_id: Block id to inspect.

        Returns:
            Ancestor blocks in nearest-parent-first order.
        """

        if self._readonly_scripts:
            # The shared script returns the starting block first, and that block
            # is not one of its own ancestors.
            return self._script_ancestors(block_id)[1:]
        return self._snapshot(lambda client: [_block_from_document(document) for document in self._ancestor_documents(client, block_id)])

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return descendants in deterministic depth-first creation order.

        Args:
            block_id: Root block id.
            include_self: Whether to include `block_id` in the result.

        Returns:
            Descendant blocks in depth-first creation order.
        """

        if self._readonly_scripts:
            subtree_keys = [self._block_key(block_id), self._children_key(block_id)]
            reply = self._read_script("descendants", keys=subtree_keys, args=[self.key_prefix, block_id])
            return self._script_descendants(block_id, reply, include_self=include_self)
        return self._snapshot(lambda client: [_block_from_document(document) for document in self._descendant_documents(client, block_id, include_self=include_self)])

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is equal to or below `root_id`.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is the root or one of its descendants.
        """

        if self._readonly_scripts:
            return bool(self._read_script("contains", keys=[self._block_key(root_id)], args=[self.key_prefix, root_id, block_id]))
        return self._snapshot(lambda client: self._contains(client, root_id, block_id))

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

        address = _encode_address(binding.address)
        reverse = _identity(binding.collection_id, address)

        def operation(transaction: _RedisTransaction) -> KeyBinding:
            self._require_structure_blocks(
                transaction.client,
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            if transaction.client.hexists(self._keys_key(binding.collection_id), address):
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in collection {binding.collection_id!r}.")
            transaction.stage(lambda pipeline: pipeline.hset(self._keys_key(binding.collection_id), address, binding.block_id))
            transaction.stage(lambda pipeline: pipeline.sadd(self._keyowner_key(binding.block_id), reverse))
            return binding

        return self._transact(operation)

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

        address = _encode_address(binding.address)
        reverse = _identity(binding.collection_id, address)

        def operation(transaction: _RedisTransaction) -> KeyBinding:
            self._require_structure_blocks(
                transaction.client,
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            current = transaction.client.hget(self._keys_key(binding.collection_id), address)
            if current is None or str(current) != expected_block_id:
                raise MemoryConflict(f"Key address {binding.address!r} in collection {binding.collection_id!r} no longer points to expected block {expected_block_id!r}.")
            transaction.stage(lambda pipeline: pipeline.hset(self._keys_key(binding.collection_id), address, binding.block_id))
            transaction.stage(lambda pipeline: pipeline.srem(self._keyowner_key(expected_block_id), reverse))
            transaction.stage(lambda pipeline: pipeline.sadd(self._keyowner_key(binding.block_id), reverse))
            return binding

        return self._transact(operation)

    def get_key(self, collection_id: str, address: MemoryAddress) -> KeyBinding | None:
        """Return one exact key binding.

        Args:
            collection_id: Id of the collection that owns the key space.
            address: Exact namespace and key to look up.

        Returns:
            Stored binding, or `None` when the address is unused.
        """

        encoded = _encode_address(address)

        def operation(client: Any) -> KeyBinding | None:
            block_id = client.hget(self._keys_key(collection_id), encoded)
            if block_id is None:
                return None
            return _binding_from_field(collection_id, encoded, block_id)

        return self._direct(operation)

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
        stored = self._direct(lambda client: client.hgetall(self._keys_key(collection_id)))
        bindings = [_binding_from_field(collection_id, field, block_id) for field, block_id in stored.items()]
        bindings = [binding for binding in bindings if _namespace_matches(binding.address.namespace, namespace, recursive=recursive)]
        bindings.sort(key=lambda binding: (binding.address.namespace, binding.address.key))
        if after is not None:
            after_key = (after.namespace, after.key)
            bindings = [binding for binding in bindings if (binding.address.namespace, binding.address.key) > after_key]
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

        encoded = _encode_address(address)
        reverse = _identity(collection_id, encoded)

        def operation(transaction: _RedisTransaction) -> None:
            current = transaction.client.hget(self._keys_key(collection_id), encoded)
            if current is None or str(current) != expected_block_id:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} no longer points to expected block {expected_block_id!r}.")
            transaction.stage(lambda pipeline: pipeline.hdel(self._keys_key(collection_id), encoded))
            transaction.stage(lambda pipeline: pipeline.srem(self._keyowner_key(expected_block_id), reverse))

        self._transact(operation)

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove bindings owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. Bindings whose collection id or
                target block id match are removed.
        """

        self._transact(lambda transaction: self._stage_key_binding_removal(transaction, {block_id}))

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

        def operation(transaction: _RedisTransaction) -> LogEntry:
            self._require_structure_blocks(transaction.client, container_id=log_id, block_id=block_id, container_name="Event log")
            counter_key = self._logcounter_key(log_id)
            sequence = _stored_int(transaction.client.get(counter_key), field_name="log sequence")
            member = _identity(sequence, block_id)
            transaction.stage(lambda pipeline: pipeline.set(counter_key, sequence + 1))
            transaction.stage(lambda pipeline: pipeline.zadd(self._log_key(log_id), {member: sequence}))
            transaction.stage(lambda pipeline: pipeline.sadd(self._logmember_key(block_id), _identity(log_id, sequence)))
            return LogEntry(log_id=log_id, sequence=sequence, block_id=block_id)

        return self._transact(operation)

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

        def operation(client: Any) -> LogEntry | None:
            members = client.zrangebyscore(self._log_key(log_id), sequence, sequence)
            if not members:
                return None
            return _log_entry_from_member(log_id, members[0])

        return self._direct(operation)

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
        lowest = "-inf" if start is None else str(start)
        highest = "+inf" if stop is None else f"({stop}"

        def operation(client: Any) -> list[LogEntry]:
            if reverse:
                members = client.zrevrangebyscore(self._log_key(log_id), highest, lowest, start=0, num=limit)
            else:
                members = client.zrangebyscore(self._log_key(log_id), lowest, highest, start=0, num=limit)
            return [_log_entry_from_member(log_id, member) for member in members]

        return self._direct(operation)

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove a log and entries owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. A log owned by this id is
                dropped, along with entries pointing to it in any other log.
        """

        self._transact(lambda transaction: self._stage_log_removal(transaction, {block_id}))

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

        payload = _dump_json(_temporal_payload(entry), field_name="temporal entry")

        def operation(transaction: _RedisTransaction) -> TemporalEntry:
            self._require_structure_blocks(
                transaction.client,
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            if transaction.client.hexists(self._temporal_key(entry.collection_id), entry.block_id):
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal collection {entry.collection_id!r}.")
            transaction.stage(lambda pipeline: pipeline.hset(self._temporal_key(entry.collection_id), entry.block_id, payload))
            transaction.stage(lambda pipeline: pipeline.zadd(self._temporalindex_key(entry.collection_id), {entry.block_id: 0}))
            transaction.stage(lambda pipeline: pipeline.sadd(self._temporalmember_key(entry.block_id), entry.collection_id))
            return entry

        return self._transact(operation)

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

        payload = _dump_json(_temporal_payload(entry), field_name="temporal entry")

        def operation(transaction: _RedisTransaction) -> TemporalEntry:
            self._require_structure_blocks(
                transaction.client,
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            if not transaction.client.hexists(self._temporal_key(entry.collection_id), entry.block_id):
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal collection {entry.collection_id!r}.")
            transaction.stage(lambda pipeline: pipeline.hset(self._temporal_key(entry.collection_id), entry.block_id, payload))
            return entry

        return self._transact(operation)

    def get_temporal_entry(self, collection_id: str, block_id: str) -> TemporalEntry | None:
        """Return one exact temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block.

        Returns:
            Stored entry, or `None` when the collection does not contain the
            block.
        """

        def operation(client: Any) -> TemporalEntry | None:
            payload = client.hget(self._temporal_key(collection_id), block_id)
            if payload is None:
                return None
            return _temporal_from_payload(collection_id, block_id, payload)

        return self._direct(operation)

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

        if self._readonly_scripts:
            page_keys = [self._temporalindex_key(collection_id), self._temporal_key(collection_id)]
            reply = self._read_script("list_temporal_entries", keys=page_keys, args=[self.key_prefix, collection_id, _temporal_lex_start(after_block_id), limit])
            return self._script_temporal_entries(collection_id, reply)

        def operation(client: Any) -> list[TemporalEntry]:
            block_ids = self._temporal_page_ids(client, collection_id, after_block_id, limit)
            return self._temporal_entries(client, collection_id, block_ids)

        return self._snapshot(operation)

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Remove one temporal entry.

        Args:
            collection_id: Id of the temporal collection.
            block_id: Id of the timed memory block to remove.

        Raises:
            MemoryConflict: If the collection does not contain the block.
        """

        def operation(transaction: _RedisTransaction) -> None:
            if not transaction.client.hexists(self._temporal_key(collection_id), block_id):
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection {collection_id!r}.")
            transaction.stage(lambda pipeline: pipeline.hdel(self._temporal_key(collection_id), block_id))
            transaction.stage(lambda pipeline: pipeline.zrem(self._temporalindex_key(collection_id), block_id))
            transaction.stage(lambda pipeline: pipeline.srem(self._temporalmember_key(block_id), collection_id))

        self._transact(operation)

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove temporal entries owned by or pointing to one block.

        Args:
            block_id: Id of the deleted block. Entries whose collection id or
                target block id match are removed.
        """

        self._transact(lambda transaction: self._stage_temporal_removal(transaction, {block_id}))

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

        Redis cannot apply these filters server-side, so the backend pages
        through the collection in block-id order and matches each page in
        Python until it fills `limit`.

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
        if limit == 0:
            return []

        def operation(client: Any) -> list[TemporalEntry]:
            matched: list[TemporalEntry] = []
            cursor = after_block_id
            while len(matched) < limit:
                block_ids = self._temporal_page_ids(client, collection_id, cursor, _TEMPORAL_SCAN_PAGE)
                if not block_ids:
                    break
                for entry in self._temporal_entries(client, collection_id, block_ids):
                    if _temporal_matches(
                        entry,
                        occurred_only=occurred_only,
                        occurred_start=occurred_start,
                        occurred_stop=occurred_stop,
                        valid_at=valid_at,
                        overlaps=overlaps,
                    ):
                        matched.append(entry)
                        if len(matched) == limit:
                            break
                cursor = block_ids[-1]
            return matched

        return self._snapshot(operation)

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
            TypeError: If the link metadata is not strict JSON.
        """

        document = _encode_link(link)

        def operation(transaction: _RedisTransaction) -> MemoryLink:
            if transaction.client.sismember(self._acyclic_key(), link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic graph; use atomic acyclic insertion.")
            self._validate_new_link(transaction.client, link)
            return self._stage_link(transaction, document)

        return self._transact(operation)

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
            TypeError: If the link metadata is not strict JSON.
        """

        document = _encode_link(link)

        def operation(transaction: _RedisTransaction) -> MemoryLink:
            self._validate_new_link(transaction.client, link)
            edges = self._relation_edges(transaction.client, link.relation)
            protected = bool(transaction.client.sismember(self._acyclic_key(), link.relation))
            if not protected and _edges_have_cycle(edges):
                raise MemoryConflict(f"Link relation {link.relation!r} already contains a cycle.")
            if link.source_id == link.target_id or _edge_path_exists(edges, link.target_id, link.source_id):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create a cycle in relation {link.relation!r}.")
            transaction.stage(lambda pipeline: pipeline.sadd(self._acyclic_key(), link.relation))
            return self._stage_link(transaction, document)

        return self._transact(operation)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace only the metadata of an existing link.

        Args:
            link: Link containing the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link is missing or any field other than
                metadata changed.
            TypeError: If the link metadata is not strict JSON.
        """

        _require_strict_json(link.metadata, field_name="link metadata")

        def operation(transaction: _RedisTransaction) -> MemoryLink:
            previous_document = self._link_document(transaction.client, link.id)
            if previous_document is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            previous = _link_from_document(previous_document)
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            stored = {**previous_document, "metadata": link.metadata}
            payload = _dump_json(stored, field_name="memory link")
            transaction.stage(lambda pipeline: pipeline.set(self._link_key(link.id), payload))
            return _link_from_document(stored)

        return self._transact(operation)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to one block.

        The endpoint index carries the relation of every link it lists, so the
        relation filter runs before any link document is read.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links in creation order.

        Raises:
            ValueError: If `direction` is invalid.
        """

        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")

        if self._readonly_scripts:
            endpoints = [self._outlinks_key(block_id), self._inlinks_key(block_id)]
            arguments = [self.key_prefix, block_id, direction, "0" if relation is None else "1", "" if relation is None else relation]
            return self._script_links(self._read_script("list_links", keys=endpoints, args=arguments))

        def operation(client: Any) -> list[MemoryLink]:
            records = self._connected_link_members(client, block_id, direction)
            if relation is not None:
                records = [record for record in records if record[3] == relation]
            link_ids = [record[0] for record in records]
            return [_link_from_document(document) for document in self._link_documents(client, link_ids)]

        return self._snapshot(operation)

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete incoming and outgoing links for one block.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        self._transact(lambda transaction: self._stage_link_removal(transaction, {block_id}))

    # ------------------------------------------------------------------
    # Portable search

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks using the portable in-memory matching behavior.

        Candidate blocks are narrowed by scope with Redis indexes, then matched
        in Python so results stay consistent with the reference backend.
        `_scope_candidate_blocks` decides how each scope reads its candidates.

        Args:
            query: Normalized query from the public API.

        Returns:
            Search results ordered by descending score, limited to
            `query.limit`.
        """

        candidates = self._scope_candidate_blocks(query)
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
    # Key namespace

    def _schema_key(self) -> str:
        """Return the key that holds the Genie schema record."""

        return f"{self.key_prefix}:schema"

    def _guard_key(self) -> str:
        """Return the key that counts committed Genie writes."""

        return f"{self.key_prefix}:mutations"

    def _order_key(self, name: str) -> str:
        """Return the key that allocates one stable storage order."""

        return f"{self.key_prefix}:order:{name}"

    def _blocks_key(self) -> str:
        """Return the sorted set of every block id in storage order."""

        return f"{self.key_prefix}:blocks"

    def _block_key(self, block_id: str) -> str:
        """Return the key that holds one block document."""

        return f"{self.key_prefix}:block:{block_id}"

    def _children_key(self, block_id: str) -> str:
        """Return the sorted set of direct child ids in creation order."""

        return f"{self.key_prefix}:children:{block_id}"

    def _link_key(self, link_id: str) -> str:
        """Return the key that holds one link document."""

        return f"{self.key_prefix}:link:{link_id}"

    def _outlinks_key(self, block_id: str) -> str:
        """Return the sorted set of link ids that start at one block."""

        return f"{self.key_prefix}:outlinks:{block_id}"

    def _inlinks_key(self, block_id: str) -> str:
        """Return the sorted set of link ids that point at one block."""

        return f"{self.key_prefix}:inlinks:{block_id}"

    def _relation_key(self, relation: str) -> str:
        """Return the sorted set of stored edges for one link relation."""

        return f"{self.key_prefix}:relation:{relation}"

    def _acyclic_key(self) -> str:
        """Return the set of link relations protected as acyclic graphs."""

        return f"{self.key_prefix}:acyclic"

    def _keys_key(self, collection_id: str) -> str:
        """Return the hash of address-to-block bindings for one collection."""

        return f"{self.key_prefix}:keys:{collection_id}"

    def _keyowner_key(self, block_id: str) -> str:
        """Return the set of bindings that point at one block."""

        return f"{self.key_prefix}:keyowner:{block_id}"

    def _log_key(self, log_id: str) -> str:
        """Return the sorted set of entries for one ordered log."""

        return f"{self.key_prefix}:log:{log_id}"

    def _logcounter_key(self, log_id: str) -> str:
        """Return the key that allocates the next sequence for one log."""

        return f"{self.key_prefix}:logcounter:{log_id}"

    def _logmember_key(self, block_id: str) -> str:
        """Return the set of log entries that point at one block."""

        return f"{self.key_prefix}:logmember:{block_id}"

    def _temporal_key(self, collection_id: str) -> str:
        """Return the hash of temporal entries for one collection."""

        return f"{self.key_prefix}:temporal:{collection_id}"

    def _temporalindex_key(self, collection_id: str) -> str:
        """Return the sorted set that pages one collection by block id."""

        return f"{self.key_prefix}:temporalindex:{collection_id}"

    def _temporalmember_key(self, block_id: str) -> str:
        """Return the set of temporal collections that contain one block."""

        return f"{self.key_prefix}:temporalmember:{block_id}"

    # ------------------------------------------------------------------
    # Client, schema, and transaction helpers

    def _direct(self, operation: Callable[[Any], _T]) -> _T:
        """Run one read that needs no consistency across commands."""

        with self._operation_client() as client:
            try:
                return operation(client)
            except GenieError:
                raise
            except self._exception_types as error:
                raise _translate_redis_error(error) from error

    def _read_script(self, name: str, *, keys: Sequence[str], args: Sequence[object]) -> Any:
        """Run one read-only Lua script as a single atomic server step.

        Redis runs a script from start to finish before it serves another
        client, so the script sees one consistent state. That removes both the
        mutation guard and the retry that a multi-command read needs. Only use
        this for a read scoped to one block or to one `limit` page, never for a
        scan of the dataset, because the script holds the whole server for as
        long as it runs.

        Args:
            name: Name of a script in `_LUA_SCRIPTS`.
            keys: Keys the call declares. They only route the call.
            args: Script arguments.

        Returns:
            The reply the script returned.

        Raises:
            BackendError: If the call fails. The subclass follows the failure.
        """

        with self._operation_client() as client:
            try:
                try:
                    return client.evalsha_ro(_LUA_DIGESTS[name], len(keys), *keys, *args)
                except self._no_script_error:
                    # The server dropped its script cache. Sending the body runs
                    # the script and caches it again for the next call.
                    return client.eval_ro(_LUA_SCRIPTS[name], len(keys), *keys, *args)
            except GenieError:
                raise
            except self._exception_types as error:
                raise _translate_redis_error(error) from error

    def _script_blocks(self, payloads: Sequence[Any]) -> list[MemoryBlock]:
        """Rebuild blocks from the payloads a read-only script returned.

        Args:
            payloads: Stored block documents in the order the script returned
                them.

        Returns:
            Blocks in the same order.

        Raises:
            BackendCorruption: If a stored document is not a valid block.
        """

        return [_block_from_document(_load_document(payload, name="memory block")) for payload in payloads]

    def _script_links(self, payloads: Sequence[Any]) -> list[MemoryLink]:
        """Rebuild links from the payloads a read-only script returned.

        Args:
            payloads: Stored link documents in the order the script returned
                them.

        Returns:
            Links in the same order.

        Raises:
            BackendCorruption: If a stored document is not a valid link.
        """

        return [_link_from_document(_load_document(payload, name="memory link")) for payload in payloads]

    def _script_temporal_entries(self, collection_id: str, reply: Sequence[Any]) -> list[TemporalEntry]:
        """Rebuild temporal entries from the pairs a read-only script returned.

        Args:
            collection_id: Id of the temporal collection that was read.
            reply: Flat list of block id and stored payload, one pair per entry.

        Returns:
            Entries in the order the script returned them.

        Raises:
            BackendCorruption: If the reply is unpaired or an entry is invalid.
        """

        if len(reply) % 2:
            raise BackendCorruption("The Redis temporal page script returned an unpaired reply.")
        return [_temporal_from_payload(collection_id, str(reply[index]), reply[index + 1]) for index in range(0, len(reply), 2)]

    def _script_ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return one block and its ancestors from a single read-only script.

        Args:
            block_id: Block id to start from.

        Returns:
            The starting block followed by its ancestors, nearest parent first.
            The list is empty when the starting block is gone.

        Raises:
            BackendCorruption: If a stored document is not a valid block.
        """

        return self._script_blocks(self._read_script("ancestors", keys=[self._block_key(block_id)], args=[self.key_prefix, block_id]))

    def _script_descendants(self, block_id: str, reply: Sequence[Any], *, include_self: bool) -> list[MemoryBlock]:
        """Rebuild one subtree from the edges and documents a script returned.

        The script walks the subtree but does not order it. This method rebuilds
        the parent-to-children map from the returned edges and reuses
        `_depth_first_ids`, so the scripted read and the guarded read share one
        ordering rule.

        Args:
            block_id: Subtree root id that was read.
            reply: Two flat lists: ownership edges, then block documents.
            include_self: Whether to keep the root block in the result.

        Returns:
            Subtree blocks in depth-first creation order.

        Raises:
            BackendCorruption: If the reply is malformed or a document is
                invalid.
        """

        if len(reply) != 2 or len(reply[0]) % 2 or len(reply[1]) % 2:
            raise BackendCorruption("The Redis subtree script returned a malformed reply.")
        edges, documents = reply
        children: dict[str, list[str]] = defaultdict(list)
        for index in range(0, len(edges), 2):
            children[str(edges[index])].append(str(edges[index + 1]))
        payloads = {str(documents[index]): documents[index + 1] for index in range(0, len(documents), 2)}
        ordered = [identifier for identifier in _depth_first_ids(block_id, children) if identifier in payloads]
        blocks = self._script_blocks([payloads[identifier] for identifier in ordered])
        if include_self:
            return blocks
        return [block for block in blocks if block.id != block_id]

    def _verify_server_role(self) -> None:
        """Refuse a connection that lands on a replica.

        A replica accepts no writes, so it answers every Genie write with a
        read-only error that reaches the caller as `BackendReadOnly`. Reads are
        the silent case. With `initialize_schema=False` the backend opens,
        never writes, and serves whatever the replica has replicated, which can
        lag the primary by any amount and carries no signal at all.

        The check refuses connecting to a replica, not a deployment that has
        replicas. A primary that has replicas reports the primary role and
        passes.

        The role comes from `INFO replication` rather than `ROLE`, for the same
        reason the eviction check avoids `CONFIG GET`: `INFO` is the command a
        managed or restricted deployment is most likely to leave available.
        Only a reading that proves the server is a replica fails the check. A
        blocked `INFO`, a client without it, a missing field, an unreadable
        value, and an unrecognized role all leave the role unknown and pass, so
        the check never rejects a server that would otherwise work.

        The check runs once against the server that answered. A failover that
        demotes this server after construction is not caught, and its writes
        then fail with `BackendReadOnly`. A proxy that spreads one URL over a
        primary and its replicas is not caught either. The check rejects a
        wrong target; it does not make reads across replicas consistent.

        Raises:
            UnsupportedOperation: If the server reports a replica role.
        """

        client = self._require_client()
        try:
            report = client.info("replication")
        except AttributeError:
            return
        except self._exception_types as error:
            if _is_command_error(error):
                return
            raise
        if not isinstance(report, Mapping):
            return
        role = report.get("role")
        # Redis reports `role:slave`, and compatible servers report `replica`.
        # An exact match keeps `master` from ever matching by accident.
        if not isinstance(role, str) or role.strip().casefold() not in {"slave", "replica"}:
            return
        raise UnsupportedOperation(
            f"Redis reports the role {role!r}, so this connection lands on a replica. A replica refuses every write, and "
            "its reads can lag the primary by any amount with no signal. Connect to the primary of the deployment, or "
            "pass verify_server_role=False to accept failing writes and stale reads."
        )

    def _verify_eviction_policy(self) -> None:
        """Refuse a server that may drop Genie keys to reclaim memory.

        Genie sets no expiry on its keys, so only an `allkeys-*` value for
        `maxmemory-policy` together with a memory limit can remove them. A
        `volatile-*` policy finds nothing to evict and answers an over-limit
        write with the out-of-memory error, which reaches the caller as
        `BackendCapacityExceeded`. Eviction is worse than that error, because
        Redis drops one key at a time while one Genie block spans a stored
        document plus several index entries, so a dropped document leaves an
        index entry pointing at data that is gone.

        The two values come from `INFO memory` rather than `CONFIG GET`,
        because managed Redis services often disable or rename `CONFIG` and a
        restricted user may not be allowed to run it. Only a reading that
        proves the server evicts fails the check. A blocked `INFO`, a client
        without it, a missing field, and an unreadable value all leave the
        setting unknown and pass, so the check never rejects a server that
        would otherwise work. The check runs once, so a policy changed after
        construction is not caught.

        Raises:
            UnsupportedOperation: If the server reports a memory limit and an
                `allkeys-*` eviction policy.
        """

        client = self._require_client()
        try:
            report = client.info("memory")
        except AttributeError:
            return
        except self._exception_types as error:
            if _is_command_error(error):
                return
            raise
        if not isinstance(report, Mapping):
            return
        policy = report.get("maxmemory_policy")
        if not isinstance(policy, str) or not policy.strip().casefold().startswith("allkeys-"):
            return
        limit = report.get("maxmemory")
        if isinstance(limit, bool) or not isinstance(limit, int | str):
            return
        try:
            limit_bytes = int(limit)
        except ValueError:
            return
        if limit_bytes <= 0:
            return
        raise UnsupportedOperation(
            f"Redis is configured to evict keys: maxmemory-policy is {policy!r} with a maxmemory of {limit_bytes} bytes. "
            "Genie stores every block as a document plus index entries, so an eviction can remove data and leave the "
            "dataset inconsistent. Set maxmemory-policy to noeviction, or pass verify_eviction_policy=False to accept "
            "the risk of losing keys."
        )

    def _detect_readonly_scripts(self) -> bool:
        """Return whether the server can run read-only Lua scripts.

        The check runs a trivial `EVAL_RO` instead of comparing a reported
        version string, because that tests the capability the reads need. A
        server older than Redis 7.0, a client without `eval_ro`, and a
        deployment that blocks scripting all answer with a command error, and
        the affected reads then use the mutation guard instead. A transport
        failure still reaches the caller.

        Returns:
            `True` when a read-only script ran.
        """

        client = self._require_client()
        try:
            return client.eval_ro("return 1", 0) == 1
        except AttributeError:
            return False
        except self._exception_types as error:
            if _is_command_error(error):
                return False
            raise

    def _snapshot(self, operation: Callable[[Any], _T]) -> _T:
        """Run one multi-command read and retry when a write interleaves.

        The mutation guard is read before and after the operation. Every Genie
        write bumps that guard, so an unchanged value proves that the commands
        in between observed one consistent state.
        """

        guard = self._guard_key()
        with self._operation_client() as client:
            deadline = monotonic() + self.max_retry_time
            attempt = 0
            while True:
                try:
                    version = client.get(guard)
                    result = operation(client)
                    if client.get(guard) == version:
                        return result
                except GenieError:
                    raise
                except self._exception_types as error:
                    raise _translate_redis_error(error) from error
                attempt += 1
                self._wait_before_retry(attempt, deadline, action="read")

    def _transact(self, operation: Callable[[_RedisTransaction], _T]) -> _T:
        """Run one optimistic read-modify-write attempt until it commits.

        The operation reads current state and stages its writes. The commit
        block watches the mutation guard, re-checks the value seen by the read
        phase, and then applies every staged write together.
        """

        guard = self._guard_key()
        with self._operation_client() as client:
            deadline = monotonic() + self.max_retry_time
            attempt = 0
            while True:
                try:
                    version = client.get(guard)
                    transaction = _RedisTransaction(client)
                    result = operation(transaction)
                    with client.pipeline() as pipeline:
                        pipeline.watch(guard)
                        if pipeline.get(guard) == version:
                            pipeline.multi()
                            transaction.apply(pipeline)
                            pipeline.incr(guard)
                            pipeline.execute()
                            return result
                except self._watch_error:
                    pass
                except GenieError:
                    raise
                except self._exception_types as error:
                    raise _translate_redis_error(error) from error
                attempt += 1
                self._wait_before_retry(attempt, deadline, action="write")

    def _wait_before_retry(self, attempt: int, deadline: float, *, action: str) -> None:
        """Back off before the next attempt, or fail when the budget is spent.

        Args:
            attempt: Number of attempts already made.
            deadline: Monotonic time when the retry budget expires.
            action: Either `"read"` or `"write"`, used in the error message.

        Raises:
            BackendBusy: If the retry budget is exhausted.
        """

        if monotonic() >= deadline:
            raise BackendBusy(f"Redis {action} lost {attempt} optimistic attempts to concurrent Genie writes within {self.max_retry_time} seconds.")
        sleep(min(_FIRST_BACKOFF_SECONDS * 2 ** min(attempt, 8), _MAX_BACKOFF_SECONDS))

    def _batch(self, client: Any, plan: Callable[[Any], None]) -> list[Any]:
        """Run one atomic batch of read commands and return their replies."""

        with client.pipeline() as pipeline:
            plan(pipeline)
            return list(pipeline.execute())

    def _zrange_batch(self, client: Any, keys: Sequence[str]) -> list[list[str]]:
        """Return the complete members of several sorted sets in creation order."""

        if not keys:
            return []

        def plan(pipeline: Any) -> None:
            for key in keys:
                pipeline.zrange(key, 0, -1)

        return [[str(member) for member in reply] for reply in self._batch(client, plan)]

    def _smembers_batch(self, client: Any, keys: Sequence[str]) -> list[set[str]]:
        """Return the members of several sets in one round trip."""

        if not keys:
            return []

        def plan(pipeline: Any) -> None:
            for key in keys:
                pipeline.smembers(key)

        return [{str(member) for member in reply} for reply in self._batch(client, plan)]

    def _hgetall_batch(self, client: Any, keys: Sequence[str]) -> list[dict[str, str]]:
        """Return the fields of several hashes in one round trip."""

        if not keys:
            return []

        def plan(pipeline: Any) -> None:
            for key in keys:
                pipeline.hgetall(key)

        return [{str(field): str(value) for field, value in reply.items()} for reply in self._batch(client, plan)]

    def _require_client(self) -> Any:
        """Return the open client or fail after `close`."""

        with self._lifecycle:
            if self._client is None or self._closing:
                raise RuntimeError("RedisBackend is closed.")
            return self._client

    @contextmanager
    def _operation_client(self) -> Generator[Any, None, None]:
        """Lease the client so `close` cannot race an active operation."""

        with self._lifecycle:
            if self._client is None or self._closing:
                raise RuntimeError("RedisBackend is closed.")
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

    def _initialize_schema(self) -> None:
        """Create or advance the Genie schema record, then validate it."""

        state = self._read_schema_identity()
        if state is None and self._has_unowned_state():
            raise UnsupportedOperation(f"The Redis key namespace {self.key_prefix!r} holds Genie keys without a schema record; use an empty namespace or a validated Genie namespace.")
        version = 0 if state is None else self._schema_version(state, allow_older=True)
        for target_version in range(version + 1, _SCHEMA_VERSION + 1):
            self._apply_schema_migration(target_version)
            self._write_schema_identity(target_version)
        current = self._read_schema_identity()
        if current is None:
            raise UnsupportedOperation("The Genie Redis schema record is missing.")
        self._validate_schema_identity(current)

    def _apply_schema_migration(self, target_version: int) -> None:
        """Apply one idempotent schema migration.

        Redis has no schema objects to declare, so a migration only records
        which document and index layout the stored keys follow.

        Args:
            target_version: Schema version to migrate to.

        Raises:
            UnsupportedOperation: If no migration is defined for the version.
        """

        if target_version != 1:
            raise UnsupportedOperation(f"No Redis migration is available for schema version {target_version}.")

    def _write_schema_identity(self, version: int) -> None:
        """Advance the owned schema record without overwriting another owner."""

        def operation(transaction: _RedisTransaction) -> None:
            state = self._schema_record(transaction.client)
            stored = 0 if state is None else self._schema_version(state, allow_older=True)
            target = max(stored, version)
            transaction.stage(lambda pipeline: pipeline.hset(self._schema_key(), mapping={"application_id": _APPLICATION_ID, "version": str(target)}))

        self._transact(operation)

    def _validate_schema(self) -> None:
        """Validate an existing schema record without creating one."""

        state = self._read_schema_identity()
        if state is None:
            raise UnsupportedOperation("The Genie Redis schema is not initialized.")
        self._validate_schema_identity(state)

    def _read_schema_identity(self) -> dict[str, str] | None:
        """Read the stored Genie schema record, or `None` when it is absent."""

        return self._direct(self._schema_record)

    def _schema_record(self, client: Any) -> dict[str, str] | None:
        """Return the raw schema record, or `None` when it is absent."""

        record = client.hgetall(self._schema_key())
        if not record:
            return None
        return {str(field): str(value) for field, value in record.items()}

    def _validate_schema_identity(self, state: Mapping[str, str]) -> None:
        """Require the stored schema record to match this Genie version."""

        self._schema_version(state, allow_older=False)

    def _schema_version(self, state: Mapping[str, str], *, allow_older: bool) -> int:
        """Validate schema ownership and return its supported version.

        Args:
            state: Stored schema record fields.
            allow_older: Whether an older version may still be migrated.

        Returns:
            Stored schema version.

        Raises:
            UnsupportedOperation: If the record belongs to another
                application, its version is invalid or newer than supported,
                or it is older and cannot be migrated.
        """

        application_id = state.get("application_id")
        if application_id != _APPLICATION_ID:
            raise UnsupportedOperation(f"The Redis schema belongs to another application ({application_id!r}).")
        try:
            version = int(str(state.get("version")))
        except ValueError as error:
            raise UnsupportedOperation("The Genie Redis schema version is invalid.") from error
        if version < 0:
            raise UnsupportedOperation("The Genie Redis schema version is invalid.")
        if version > _SCHEMA_VERSION:
            raise UnsupportedOperation(f"Redis schema version {version} is newer than supported version {_SCHEMA_VERSION}.")
        if version < _SCHEMA_VERSION and not allow_older:
            raise UnsupportedOperation(f"No Redis migration is available from schema version {version}.")
        return version

    def _has_unowned_state(self) -> bool:
        """Return whether Genie keys exist without an owning schema record."""

        def operation(client: Any) -> bool:
            def plan(pipeline: Any) -> None:
                pipeline.exists(self._blocks_key())
                pipeline.exists(self._guard_key())
                pipeline.exists(self._acyclic_key())

            return any(bool(reply) for reply in self._batch(client, plan))

        return self._direct(operation)

    # ------------------------------------------------------------------
    # Stored state helpers

    def _block_document(self, client: Any, block_id: str) -> dict[str, Any] | None:
        """Return one stored block document, or `None` when it is absent."""

        payload = client.get(self._block_key(block_id))
        return None if payload is None else _load_document(payload, name="memory block")

    def _block(self, client: Any, block_id: str) -> MemoryBlock | None:
        """Return one stored block, or `None` when it is absent."""

        document = self._block_document(client, block_id)
        return None if document is None else _block_from_document(document)

    def _block_documents(self, client: Any, block_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Return stored block documents for the ids that still exist."""

        return self._documents(client, [self._block_key(block_id) for block_id in block_ids], name="memory block")

    def _link_document(self, client: Any, link_id: str) -> dict[str, Any] | None:
        """Return one stored link document, or `None` when it is absent."""

        payload = client.get(self._link_key(link_id))
        return None if payload is None else _load_document(payload, name="memory link")

    def _link_documents(self, client: Any, link_ids: Sequence[str]) -> list[dict[str, Any]]:
        """Return stored link documents for the ids that still exist."""

        return self._documents(client, [self._link_key(link_id) for link_id in link_ids], name="memory link")

    def _documents(self, client: Any, keys: Sequence[str], *, name: str) -> list[dict[str, Any]]:
        """Return stored documents for the keys that still exist.

        Keys are read in bounded groups so one command never carries an
        unbounded argument list, and the groups share one round trip.
        """

        groups = list(_chunks(keys, _READ_CHUNK))
        if not groups:
            return []
        if len(groups) == 1:
            replies = [client.mget(list(groups[0]))]
        else:

            def plan(pipeline: Any) -> None:
                for group in groups:
                    pipeline.mget(list(group))

            replies = self._batch(client, plan)
        return [_load_document(payload, name=name) for reply in replies for payload in reply if payload is not None]

    def _ancestor_documents(self, client: Any, block_id: str) -> list[dict[str, Any]]:
        """Return stored ancestor documents from the nearest parent upward."""

        documents: list[dict[str, Any]] = []
        current = self._block_document(client, block_id)
        seen = {block_id}
        while current is not None:
            parent_id = current.get("parent_id")
            if parent_id is None or str(parent_id) in seen:
                break
            parent_id = str(parent_id)
            seen.add(parent_id)
            parent = self._block_document(client, parent_id)
            if parent is None:
                break
            documents.append(parent)
            current = parent
        return documents

    def _descendant_documents(self, client: Any, block_id: str, *, include_self: bool) -> list[dict[str, Any]]:
        """Return stored subtree documents in depth-first creation order.

        Child ids are read one ownership level at a time, so the number of
        round trips follows the depth of the subtree instead of its size.
        """

        if self._block_document(client, block_id) is None:
            return []
        children: dict[str, list[str]] = {}
        frontier = [block_id]
        visited = {block_id}
        while frontier:
            replies = self._zrange_batch(client, [self._children_key(parent_id) for parent_id in frontier])
            following: list[str] = []
            for parent_id, child_ids in zip(frontier, replies, strict=True):
                selected = [child_id for child_id in child_ids if child_id not in visited]
                visited.update(selected)
                children[parent_id] = selected
                following.extend(selected)
            frontier = following
        ordered = _depth_first_ids(block_id, children)
        documents = self._block_documents(client, ordered)
        if include_self:
            return documents
        return [document for document in documents if str(document["id"]) != block_id]

    def _contains(self, client: Any, root_id: str, block_id: str) -> bool:
        """Return whether a block is equal to or below a subtree root."""

        if not client.exists(self._block_key(root_id)):
            return False
        if root_id == block_id:
            return True
        current = self._block_document(client, block_id)
        seen = {block_id}
        while current is not None:
            parent_id = current.get("parent_id")
            if parent_id is None:
                return False
            parent_id = str(parent_id)
            if parent_id == root_id:
                return True
            if parent_id in seen:
                return False
            seen.add(parent_id)
            current = self._block_document(client, parent_id)
        return False

    def _validate_parent(self, client: Any, block_id: str, parent_id: str | None) -> None:
        """Require a parent placement to exist and not be self-referential."""

        if parent_id == block_id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if parent_id is not None and not client.exists(self._block_key(parent_id)):
            raise MemoryConflict(f"Parent memory block {parent_id!r} does not exist.")

    def _require_structure_blocks(self, client: Any, *, container_id: str, block_id: str, container_name: str) -> None:
        """Require a structure container and referenced block to exist."""

        def plan(pipeline: Any) -> None:
            pipeline.exists(self._block_key(container_id))
            pipeline.exists(self._block_key(block_id))

        container_exists, block_exists = self._batch(client, plan)
        if not container_exists:
            raise MemoryConflict(f"{container_name} block {container_id!r} does not exist.")
        if not block_exists:
            raise MemoryConflict(f"Memory block {block_id!r} does not exist.")

    def _validate_new_link(self, client: Any, link: MemoryLink) -> None:
        """Validate identity and endpoints before storing a link."""

        def plan(pipeline: Any) -> None:
            pipeline.exists(self._link_key(link.id))
            pipeline.exists(self._block_key(link.source_id))
            pipeline.exists(self._block_key(link.target_id))

        link_exists, source_exists, target_exists = self._batch(client, plan)
        if link_exists:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if not source_exists:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if not target_exists:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _relation_edges(self, client: Any, relation: str) -> list[tuple[str, str]]:
        """Return every stored edge of one link relation."""

        members = client.zrange(self._relation_key(relation), 0, -1)
        return [_decode_edge_member(str(member)) for member in members]

    def _connected_link_members(self, client: Any, block_id: str, direction: str) -> list[tuple[str, str, str, str]]:
        """Return the endpoint records connected to one block in creation order.

        Each record holds the link id, its two endpoints, and its relation, so
        a caller can filter without reading link documents.
        """

        if direction == "out":
            members = [str(member) for member in client.zrange(self._outlinks_key(block_id), 0, -1)]
        elif direction == "in":
            members = [str(member) for member in client.zrange(self._inlinks_key(block_id), 0, -1)]
        else:

            def plan(pipeline: Any) -> None:
                pipeline.zrange(self._outlinks_key(block_id), 0, -1, withscores=True)
                pipeline.zrange(self._inlinks_key(block_id), 0, -1, withscores=True)

            # A self-link sits in both endpoint sets under the same member, so
            # merging on the member is what removes the duplicate.
            ordered: dict[str, float] = {}
            for reply in self._batch(client, plan):
                for member, score in reply:
                    ordered[str(member)] = float(score)
            members = sorted(ordered, key=lambda member: ordered[member])
        return [_decode_endpoint_member(member) for member in members]

    def _temporal_page_ids(self, client: Any, collection_id: str, after_block_id: str | None, limit: int) -> list[str]:
        """Return one page of collection block ids in ascending order."""

        members = client.zrangebylex(self._temporalindex_key(collection_id), _temporal_lex_start(after_block_id), "+", start=0, num=limit)
        return [str(member) for member in members]

    def _temporal_entries(self, client: Any, collection_id: str, block_ids: Sequence[str]) -> list[TemporalEntry]:
        """Return stored temporal entries for one ordered page of block ids."""

        if not block_ids:
            return []
        payloads = client.hmget(self._temporal_key(collection_id), list(block_ids))
        return [_temporal_from_payload(collection_id, block_id, payload) for block_id, payload in zip(block_ids, payloads, strict=True) if payload is not None]

    def _scope_candidate_blocks(self, query: MemoryQuery) -> list[MemoryBlock]:
        """Return the candidate blocks one normalized query scope allows.

        `Scope.SELF`, `Scope.SUBTREE`, and `Scope.ANCESTORS` read one block, one
        subtree, or one parent chain, so they take the same block-scoped read
        path as `get_block`, `descendants`, and `ancestors`. Those reads are
        atomic on the server and never retry.

        `Scope.GLOBAL` reads every stored block, and `Scope.LINKS` follows an
        endpoint fan-out that one block can grow without a bound. Neither has a
        size a script may safely hold the server for, so both keep the mutation
        guard and may raise `BackendBusy` under sustained writes.

        Args:
            query: Normalized query from the public API.

        Returns:
            Candidate blocks, before type, filter, and text matching.
        """

        if query.root_id is None or query.scope == Scope.GLOBAL:
            return self._snapshot(self._all_blocks)
        root_id = query.root_id
        if query.scope == Scope.SELF:
            # A single GET is atomic on its own, so this read needs neither the
            # guard nor a script, whatever the server supports.
            block = self._direct(lambda client: self._block(client, root_id))
            return [] if block is None else [block]
        if query.scope == Scope.SUBTREE:
            return self.descendants(root_id, include_self=True)
        if query.scope == Scope.ANCESTORS:
            if self._readonly_scripts:
                return self._script_ancestors(root_id)
            return self._snapshot(lambda client: self._self_and_ancestor_blocks(client, root_id))
        if query.scope == Scope.LINKS:
            return self._snapshot(lambda client: self._linked_blocks(client, root_id))
        return self._snapshot(self._all_blocks)

    def _all_blocks(self, client: Any) -> list[MemoryBlock]:
        """Return every stored block in creation order."""

        block_ids = [str(member) for member in client.zrange(self._blocks_key(), 0, -1)]
        return [_block_from_document(document) for document in self._block_documents(client, block_ids)]

    def _self_and_ancestor_blocks(self, client: Any, block_id: str) -> list[MemoryBlock]:
        """Return one block followed by its ancestors, nearest parent first."""

        document = self._block_document(client, block_id)
        if document is None:
            return []
        return [_block_from_document(record) for record in (document, *self._ancestor_documents(client, block_id))]

    def _linked_blocks(self, client: Any, block_id: str) -> list[MemoryBlock]:
        """Return the blocks a link connects to one block, in creation order.

        The starting block is included when at least one link reaches it,
        because it is an endpoint of its own links.
        """

        if self._block_document(client, block_id) is None:
            return []
        endpoint_ids: list[str] = []
        seen: set[str] = set()
        for _, source_id, target_id, _ in self._connected_link_members(client, block_id, "both"):
            for endpoint_id in (source_id, target_id):
                if endpoint_id not in seen:
                    seen.add(endpoint_id)
                    endpoint_ids.append(endpoint_id)
        documents = self._block_documents(client, endpoint_ids)
        documents.sort(key=lambda document: _stored_int(document.get("storage_order"), field_name="storage order"))
        return [_block_from_document(document) for document in documents]

    # ------------------------------------------------------------------
    # Staged writes

    def _stage_delete(self, transaction: _RedisTransaction, keys: Sequence[str]) -> None:
        """Stage bounded key deletions."""

        for chunk in _chunks(keys, _COMMAND_CHUNK):
            transaction.stage(_delete_command(chunk))

    def _stage_zrem(self, transaction: _RedisTransaction, key: str, members: Sequence[str]) -> None:
        """Stage bounded sorted-set member removals."""

        for chunk in _chunks(members, _COMMAND_CHUNK):
            transaction.stage(_zrem_command(key, chunk))

    def _stage_srem(self, transaction: _RedisTransaction, key: str, members: Sequence[str]) -> None:
        """Stage bounded set member removals."""

        for chunk in _chunks(members, _COMMAND_CHUNK):
            transaction.stage(_srem_command(key, chunk))

    def _stage_hdel(self, transaction: _RedisTransaction, key: str, fields: Sequence[str]) -> None:
        """Stage bounded hash field removals."""

        for chunk in _chunks(fields, _COMMAND_CHUNK):
            transaction.stage(_hdel_command(key, chunk))

    def _reserve_storage_order(self, transaction: _RedisTransaction, name: str) -> int:
        """Read and reserve the next stable storage order value."""

        key = self._order_key(name)
        order = _stored_int(transaction.client.get(key), field_name=f"{name} storage order")
        transaction.stage(lambda pipeline: pipeline.set(key, order + 1))
        return order

    def _stage_block_document(self, transaction: _RedisTransaction, document: Mapping[str, Any]) -> None:
        """Stage one complete block document write."""

        key = self._block_key(str(document["id"]))
        payload = _dump_json(dict(document), field_name="memory block")
        transaction.stage(lambda pipeline: pipeline.set(key, payload))

    def _stage_link(self, transaction: _RedisTransaction, document: Mapping[str, Any]) -> MemoryLink:
        """Stage one new link document together with its indexes."""

        storage_order = self._reserve_storage_order(transaction, "links")
        stored = {**document, "storage_order": storage_order}
        link_id = str(stored["id"])
        source_id = str(stored["source_id"])
        target_id = str(stored["target_id"])
        relation = str(stored["relation"])
        endpoint = _identity(link_id, source_id, target_id, relation)
        payload = _dump_json(stored, field_name="memory link")
        transaction.stage(lambda pipeline: pipeline.set(self._link_key(link_id), payload))
        transaction.stage(lambda pipeline: pipeline.zadd(self._outlinks_key(source_id), {endpoint: storage_order}))
        transaction.stage(lambda pipeline: pipeline.zadd(self._inlinks_key(target_id), {endpoint: storage_order}))
        transaction.stage(lambda pipeline: pipeline.zadd(self._relation_key(relation), {_identity(link_id, source_id, target_id): storage_order}))
        return _link_from_document(stored)

    def _stage_link_removal(self, transaction: _RedisTransaction, block_ids: set[str]) -> None:
        """Stage removal of every link that touches one of the block ids.

        The endpoint members carry everything the cleanup needs, so this reads
        no link documents. That matters for more than speed: a link id whose
        document is already gone would otherwise keep its index entries alive,
        because a missing document is skipped instead of reported.
        """

        owners = sorted(block_ids)
        endpoint_keys = [self._outlinks_key(block_id) for block_id in owners] + [self._inlinks_key(block_id) for block_id in owners]
        records: dict[str, tuple[str, str, str, str]] = {}
        for members in self._zrange_batch(transaction.client, endpoint_keys):
            for member in members:
                record = _decode_endpoint_member(member)
                records.setdefault(record[0], record)
        self._stage_delete(transaction, endpoint_keys)
        if not records:
            return
        self._stage_delete(transaction, [self._link_key(link_id) for link_id in records])
        removals: dict[str, list[str]] = defaultdict(list)
        for link_id, source_id, target_id, relation in records.values():
            endpoint = _identity(link_id, source_id, target_id, relation)
            if source_id not in block_ids:
                removals[self._outlinks_key(source_id)].append(endpoint)
            if target_id not in block_ids:
                removals[self._inlinks_key(target_id)].append(endpoint)
            removals[self._relation_key(relation)].append(_identity(link_id, source_id, target_id))
        for key, removed_members in removals.items():
            self._stage_zrem(transaction, key, removed_members)

    def _stage_key_binding_removal(self, transaction: _RedisTransaction, block_ids: set[str]) -> None:
        """Stage removal of key bindings owned by or pointing to the blocks."""

        owners = sorted(block_ids)
        collections = self._hgetall_batch(transaction.client, [self._keys_key(block_id) for block_id in owners])
        references = self._smembers_batch(transaction.client, [self._keyowner_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._keys_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._keyowner_key(block_id) for block_id in owners])
        reverse_removals: dict[str, list[str]] = defaultdict(list)
        field_removals: dict[str, list[str]] = defaultdict(list)
        for collection_id, bindings in zip(owners, collections, strict=True):
            for address, target_id in bindings.items():
                if target_id not in block_ids:
                    reverse_removals[self._keyowner_key(target_id)].append(_identity(collection_id, address))
        for reverse_members in references:
            for member in reverse_members:
                collection_id, address = _decode_binding_reference(member)
                if collection_id not in block_ids:
                    field_removals[self._keys_key(collection_id)].append(address)
        for key, removed_members in reverse_removals.items():
            self._stage_srem(transaction, key, removed_members)
        for key, removed_fields in field_removals.items():
            self._stage_hdel(transaction, key, removed_fields)

    def _stage_log_removal(self, transaction: _RedisTransaction, block_ids: set[str]) -> None:
        """Stage removal of logs owned by and entries pointing to the blocks."""

        owners = sorted(block_ids)
        owned = self._zrange_batch(transaction.client, [self._log_key(block_id) for block_id in owners])
        references = self._smembers_batch(transaction.client, [self._logmember_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._log_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._logcounter_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._logmember_key(block_id) for block_id in owners])
        reverse_removals: dict[str, list[str]] = defaultdict(list)
        member_removals: dict[str, list[str]] = defaultdict(list)
        for owned_log_id, entry_members in zip(owners, owned, strict=True):
            for member in entry_members:
                sequence, entry_block_id = _decode_log_member(member)
                if entry_block_id not in block_ids:
                    reverse_removals[self._logmember_key(entry_block_id)].append(_identity(owned_log_id, sequence))
        for block_id, reverse_members in zip(owners, references, strict=True):
            for member in reverse_members:
                log_id, sequence = _decode_log_reference(member)
                if log_id not in block_ids:
                    member_removals[self._log_key(log_id)].append(_identity(sequence, block_id))
        for key, removed_members in reverse_removals.items():
            self._stage_srem(transaction, key, removed_members)
        for key, removed_entries in member_removals.items():
            self._stage_zrem(transaction, key, removed_entries)

    def _stage_temporal_removal(self, transaction: _RedisTransaction, block_ids: set[str]) -> None:
        """Stage removal of temporal collections and entries for the blocks."""

        owners = sorted(block_ids)
        collections = self._hgetall_batch(transaction.client, [self._temporal_key(block_id) for block_id in owners])
        references = self._smembers_batch(transaction.client, [self._temporalmember_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._temporal_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._temporalindex_key(block_id) for block_id in owners])
        self._stage_delete(transaction, [self._temporalmember_key(block_id) for block_id in owners])
        reverse_removals: dict[str, list[str]] = defaultdict(list)
        field_removals: dict[str, list[str]] = defaultdict(list)
        index_removals: dict[str, list[str]] = defaultdict(list)
        for owned_collection_id, entry_block_ids in zip(owners, collections, strict=True):
            for entry_block_id in entry_block_ids:
                if entry_block_id not in block_ids:
                    reverse_removals[self._temporalmember_key(entry_block_id)].append(owned_collection_id)
        for block_id, collection_ids in zip(owners, references, strict=True):
            for collection_id in collection_ids:
                if collection_id not in block_ids:
                    field_removals[self._temporal_key(collection_id)].append(block_id)
                    index_removals[self._temporalindex_key(collection_id)].append(block_id)
        for key, removed_members in reverse_removals.items():
            self._stage_srem(transaction, key, removed_members)
        for key, removed_fields in field_removals.items():
            self._stage_hdel(transaction, key, removed_fields)
        for key, removed_ids in index_removals.items():
            self._stage_zrem(transaction, key, removed_ids)


class _RedisTransaction:
    """One optimistic read-modify-write attempt against Redis.

    The read phase runs ordinary commands through `client`. Writes are staged
    instead of sent, so the commit block can apply all of them inside one
    `MULTI`/`EXEC` block after it re-checks the mutation guard.

    Attributes:
        client: Redis client used by the read phase of this attempt.
    """

    __slots__ = ("_writes", "client")

    def __init__(self, client: Any) -> None:
        """Start one attempt against an open client.

        Args:
            client: Redis client used by the read phase.
        """

        self.client = client
        self._writes: list[Callable[[Any], None]] = []

    def stage(self, apply: Callable[[Any], None]) -> None:
        """Queue one write to run inside the commit block.

        Args:
            apply: Callable that queues commands on a buffered pipeline.
        """

        self._writes.append(apply)

    def apply(self, pipeline: Any) -> None:
        """Queue every staged write on one buffered pipeline.

        Args:
            pipeline: Pipeline already switched to buffered `MULTI` mode.
        """

        for write in self._writes:
            write(pipeline)


def _encode_block(block: MemoryBlock) -> dict[str, Any]:
    """Return the stored document for one encoded memory block."""

    if isinstance(block.version, bool) or not isinstance(block.version, int) or block.version < 1:
        raise ValueError("block version must be an integer greater than or equal to one.")
    _require_strict_json(block.value, field_name="block value")
    _require_strict_json(block.metadata, field_name="block metadata")
    return {
        "id": block.id,
        "parent_id": block.parent_id,
        "type": block.type,
        "value": block.value,
        "content_type": block.content_type,
        "metadata": block.metadata,
        "policy": _encode_policy(block.policy),
        "created_at": _encode_datetime(block.created_at, field_name="created_at"),
        "updated_at": _encode_datetime(block.updated_at, field_name="updated_at"),
        "created_by": block.created_by,
        "version": block.version,
    }


def _block_from_document(document: Mapping[str, Any]) -> MemoryBlock:
    """Rebuild a memory block from one stored Redis document."""

    try:
        return MemoryBlock(
            id=str(document["id"]),
            parent_id=str(document["parent_id"]) if document.get("parent_id") is not None else None,
            type=str(document["type"]),
            value=document["value"],
            content_type=str(document["content_type"]),
            metadata=_document_mapping(document["metadata"], field_name="metadata"),
            policy=_decode_policy(document.get("policy")),
            created_at=_decode_datetime(document["created_at"]),
            updated_at=_decode_datetime(document["updated_at"]),
            created_by=str(document["created_by"]) if document.get("created_by") is not None else None,
            version=_stored_int(document["version"], field_name="block version"),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Redis memory block is invalid.") from error


def _encode_link(link: MemoryLink) -> dict[str, Any]:
    """Return the stored document for one typed memory link."""

    _require_strict_json(link.metadata, field_name="link metadata")
    return {
        "id": link.id,
        "source_id": link.source_id,
        "target_id": link.target_id,
        "relation": link.relation,
        "metadata": link.metadata,
        "created_at": _encode_datetime(link.created_at, field_name="created_at"),
    }


def _link_from_document(document: Mapping[str, Any]) -> MemoryLink:
    """Rebuild a memory link from one stored Redis document."""

    try:
        return MemoryLink(
            id=str(document["id"]),
            source_id=str(document["source_id"]),
            target_id=str(document["target_id"]),
            relation=str(document["relation"]),
            metadata=_document_mapping(document["metadata"], field_name="metadata"),
            created_at=_decode_datetime(document["created_at"]),
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Redis memory link is invalid.") from error


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same stored relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _validate_expected_version(previous: MemoryBlock, replacement: MemoryBlock, expected_version: int | None) -> None:
    """Raise when a conditional replacement does not match its version."""

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version {expected_version}.")
    if replacement.version != expected_version + 1:
        raise MemoryConflict("A conditional block replacement must increment its version by one.")


def _binding_from_field(collection_id: str, field: object, block_id: object) -> KeyBinding:
    """Rebuild a key binding from one stored hash field and value."""

    from genie.structures.records import KeyBinding

    address = _decode_address(field)
    try:
        return KeyBinding(collection_id=collection_id, address=address, block_id=str(block_id))
    except (TypeError, ValueError) as error:
        raise BackendCorruption("Stored Redis key binding is invalid.") from error


def _encode_address(address: MemoryAddress) -> str:
    """Encode one namespaced address as a stable hash field."""

    return _identity(list(address.namespace), address.key)


def _decode_address(field: object) -> MemoryAddress:
    """Decode one stored hash field into a namespaced address."""

    from genie.structures.records import MemoryAddress

    namespace, key = _decode_identity(field, size=2, name="key address")
    if not isinstance(namespace, list) or not all(isinstance(segment, str) for segment in namespace):
        raise BackendCorruption("Stored Redis key namespaces must be string lists.")
    if not isinstance(key, str):
        raise BackendCorruption("Stored Redis key names must be strings.")
    return MemoryAddress(namespace=tuple(namespace), key=key)


def _decode_binding_reference(member: str) -> tuple[str, str]:
    """Decode one stored binding reference into its collection and address."""

    collection_id, address = _decode_identity(member, size=2, name="key binding reference")
    if not isinstance(collection_id, str) or not isinstance(address, str):
        raise BackendCorruption("Stored Redis key binding reference is invalid.")
    return collection_id, address


def _log_entry_from_member(log_id: str, member: object) -> LogEntry:
    """Rebuild a log entry from one stored sorted-set member."""

    from genie.structures.records import LogEntry

    sequence, block_id = _decode_log_member(str(member))
    try:
        return LogEntry(log_id=log_id, sequence=sequence, block_id=block_id)
    except (TypeError, ValueError) as error:
        raise BackendCorruption("Stored Redis log entry is invalid.") from error


def _decode_log_member(member: str) -> tuple[int, str]:
    """Decode one stored log member into its sequence and block id."""

    sequence, block_id = _decode_identity(member, size=2, name="log entry")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not isinstance(block_id, str):
        raise BackendCorruption("Stored Redis log entry is invalid.")
    return sequence, block_id


def _decode_log_reference(member: str) -> tuple[str, int]:
    """Decode one stored log reference into its log id and sequence."""

    log_id, sequence = _decode_identity(member, size=2, name="log entry reference")
    if not isinstance(log_id, str) or isinstance(sequence, bool) or not isinstance(sequence, int):
        raise BackendCorruption("Stored Redis log entry reference is invalid.")
    return log_id, sequence


def _decode_endpoint_member(member: str) -> tuple[str, str, str, str]:
    """Decode one stored endpoint member into its link, endpoints, and relation."""

    link_id, source_id, target_id, relation = _decode_identity(member, size=4, name="link endpoint member")
    if not all(isinstance(part, str) for part in (link_id, source_id, target_id, relation)):
        raise BackendCorruption("Stored Redis link endpoint member is invalid.")
    return link_id, source_id, target_id, relation


def _decode_edge_member(member: str) -> tuple[str, str]:
    """Decode one stored relation edge into its source and target ids."""

    _, source_id, target_id = _decode_identity(member, size=3, name="link relation edge")
    if not isinstance(source_id, str) or not isinstance(target_id, str):
        raise BackendCorruption("Stored Redis link relation edge is invalid.")
    return source_id, target_id


def _temporal_payload(entry: TemporalEntry) -> dict[str, Any]:
    """Return the stored payload for one temporal collection entry."""

    return {
        "occurred_at": _encode_datetime(entry.occurred_at, field_name="occurred_at") if entry.occurred_at is not None else None,
        "valid_at": _encode_datetime(entry.validity.valid_at, field_name="valid_at") if entry.validity is not None else None,
        "invalid_at": (_encode_datetime(entry.validity.invalid_at, field_name="invalid_at") if entry.validity is not None and entry.validity.invalid_at is not None else None),
    }


def _temporal_from_payload(collection_id: str, block_id: str, payload: object) -> TemporalEntry:
    """Rebuild one temporal entry from its stored payload."""

    from genie.structures.records import TemporalEntry, ValidityInterval

    document = _load_document(payload, name="temporal entry")
    try:
        validity = None
        if document.get("valid_at") is not None:
            validity = ValidityInterval(
                valid_at=_decode_datetime(document["valid_at"]),
                invalid_at=_decode_datetime(document["invalid_at"]) if document.get("invalid_at") is not None else None,
            )
        return TemporalEntry(
            collection_id=collection_id,
            block_id=block_id,
            occurred_at=_decode_datetime(document["occurred_at"]) if document.get("occurred_at") is not None else None,
            validity=validity,
        )
    except BackendCorruption:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BackendCorruption("Stored Redis temporal entry is invalid.") from error


def _encode_policy(policy: MemoryPolicy | None) -> dict[str, Any] | None:
    """Encode an access policy as stored JSON data, or `None` when unset."""

    if policy is None:
        return None
    return {
        "read": _encode_rule(policy.read),
        "write": _encode_rule(policy.write),
        "search": _encode_rule(policy.search),
        "traverse_up": _encode_rule(policy.traverse_up),
        "traverse_down": _encode_rule(policy.traverse_down),
        "operations": {name: _encode_rule(rule) for name, rule in sorted(policy.operations.items())},
    }


def _decode_policy(payload: object) -> MemoryPolicy | None:
    """Decode a stored access policy, or `None` when unset."""

    if payload is None:
        return None
    mapping = _document_mapping(payload, field_name="policy")
    operations = _document_mapping(mapping["operations"], field_name="policy operations")
    return MemoryPolicy(
        read=_decode_rule(mapping["read"]),
        write=_decode_rule(mapping["write"]),
        search=_decode_rule(mapping["search"]),
        traverse_up=_decode_rule(mapping["traverse_up"]),
        traverse_down=_decode_rule(mapping["traverse_down"]),
        operations={name: _decode_rule(rule) for name, rule in operations.items()},
    )


def _encode_rule(rule: AccessRule) -> dict[str, Any]:
    """Encode one access rule as JSON-compatible data."""

    return {
        "allow_all": rule.allow_all,
        "principals": sorted(rule.principals),
        "roles": sorted(rule.roles),
        "allow_owner": rule.allow_owner,
    }


def _decode_rule(payload: object) -> AccessRule:
    """Decode one access rule from its stored mapping."""

    mapping = _document_mapping(payload, field_name="access rule")
    return AccessRule(
        allow_all=mapping["allow_all"],
        principals=frozenset(mapping["principals"]),
        roles=frozenset(mapping["roles"]),
        allow_owner=mapping["allow_owner"],
    )


def _identity(*parts: object) -> str:
    """Build one stable composite identity for a stored member or field."""

    return json.dumps(parts, allow_nan=False, ensure_ascii=False, separators=(",", ":"))


def _decode_identity(value: object, *, size: int, name: str) -> list[Any]:
    """Decode one stored composite identity into its expected parts."""

    if not isinstance(value, str):
        raise BackendCorruption(f"Stored Redis {name} values must be strings.")
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError as error:
        raise BackendCorruption(f"Stored Redis {name} is invalid.") from error
    if not isinstance(decoded, list) or len(decoded) != size:
        raise BackendCorruption(f"Stored Redis {name} is invalid.")
    return decoded


def _dump_json(value: Any, *, field_name: str) -> str:
    """Encode a strict JSON value without silently changing its type."""

    _require_strict_json(value, field_name=field_name)
    return json.dumps(value, allow_nan=False, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _load_document(payload: object, *, name: str) -> dict[str, Any]:
    """Decode one stored JSON document into an isolated Python object."""

    if not isinstance(payload, str):
        raise BackendCorruption(f"Stored Redis {name} documents must be strings.")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise BackendCorruption(f"Stored Redis {name} JSON is invalid.") from error
    if not _is_strict_json(decoded):
        raise BackendCorruption(f"Stored Redis {name} JSON is not strict JSON data.")
    if not isinstance(decoded, dict):
        raise BackendCorruption(f"Stored Redis {name} documents must be JSON objects.")
    return decoded


def _document_mapping(value: object, *, field_name: str) -> dict[str, Any]:
    """Require one stored document field to be a JSON object."""

    if not isinstance(value, dict):
        raise TypeError(f"Stored Redis document field {field_name!r} must be a JSON object.")
    return value


def _require_strict_json(value: Any, *, field_name: str) -> None:
    """Require a value that round-trips through JSON without type changes."""

    if not _is_strict_json(value):
        raise TypeError(f"RedisBackend {field_name} must be strict JSON data.")


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


def _stored_int(value: object, *, field_name: str) -> int:
    """Return one stored integer, treating an absent value as zero."""

    if value is None:
        return 0
    if isinstance(value, bool):
        raise BackendCorruption(f"Stored Redis {field_name} must be an integer.")
    if isinstance(value, int):
        return value
    try:
        return int(str(value))
    except ValueError as error:
        raise BackendCorruption(f"Stored Redis {field_name} must be an integer.") from error


def _encode_datetime(value: datetime, *, field_name: str) -> str:
    """Encode an aware datetime as a UTC ISO 8601 string."""

    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware.")
    return value.astimezone(UTC).isoformat()


def _decode_datetime(value: object) -> datetime:
    """Decode one stored ISO 8601 timestamp and normalize it to UTC."""

    if not isinstance(value, str):
        raise BackendCorruption("Stored Redis timestamps must be strings.")
    try:
        timestamp = datetime.fromisoformat(value)
    except ValueError as error:
        raise BackendCorruption("Stored Redis timestamp is invalid.") from error
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise BackendCorruption("Stored Redis timestamps must be timezone-aware.")
    return timestamp.astimezone(UTC)


def _namespace_matches(candidate: tuple[str, ...], selected: tuple[str, ...] | None, *, recursive: bool) -> bool:
    """Return whether a namespace satisfies an exact or prefix filter."""

    if selected is None:
        return True
    if recursive:
        return candidate[: len(selected)] == selected
    return candidate == selected


def _temporal_matches(
    entry: TemporalEntry,
    *,
    occurred_only: bool,
    occurred_start: datetime | None,
    occurred_stop: datetime | None,
    valid_at: datetime | None,
    overlaps: ValidityInterval | None,
) -> bool:
    """Return whether an entry satisfies all temporal index filters."""

    occurred_at = entry.occurred_at
    if occurred_only and occurred_at is None:
        return False
    if occurred_start is not None and (occurred_at is None or occurred_at < occurred_start):
        return False
    if occurred_stop is not None and (occurred_at is None or occurred_at >= occurred_stop):
        return False
    validity = entry.validity
    if valid_at is not None:
        if validity is None or validity.valid_at > valid_at:
            return False
        if validity.invalid_at is not None and valid_at >= validity.invalid_at:
            return False
    if overlaps is not None:
        if validity is None:
            return False
        if validity.invalid_at is not None and validity.invalid_at <= overlaps.valid_at:
            return False
        if overlaps.invalid_at is not None and overlaps.invalid_at <= validity.valid_at:
            return False
    return True


def _depth_first_ids(root_id: str, children: Mapping[str, Sequence[str]]) -> list[str]:
    """Return subtree ids in depth-first creation order."""

    ordered: list[str] = []
    pending = [root_id]
    while pending:
        block_id = pending.pop()
        ordered.append(block_id)
        pending.extend(reversed(children.get(block_id, ())))
    return ordered


def _chunks(items: Sequence[str], size: int) -> Iterable[tuple[str, ...]]:
    """Split a sequence into bounded tuples of command arguments."""

    for start in range(0, len(items), size):
        yield tuple(items[start : start + size])


def _delete_command(keys: tuple[str, ...]) -> Callable[[Any], None]:
    """Return a staged write that deletes one bounded group of keys."""

    def apply(pipeline: Any) -> None:
        pipeline.delete(*keys)

    return apply


def _zrem_command(key: str, members: tuple[str, ...]) -> Callable[[Any], None]:
    """Return a staged write that removes sorted-set members."""

    def apply(pipeline: Any) -> None:
        pipeline.zrem(key, *members)

    return apply


def _srem_command(key: str, members: tuple[str, ...]) -> Callable[[Any], None]:
    """Return a staged write that removes set members."""

    def apply(pipeline: Any) -> None:
        pipeline.srem(key, *members)

    return apply


def _hdel_command(key: str, fields: tuple[str, ...]) -> Callable[[Any], None]:
    """Return a staged write that removes hash fields."""

    def apply(pipeline: Any) -> None:
        pipeline.hdel(key, *fields)

    return apply


def _edges_have_cycle(edges: Sequence[tuple[str, str]]) -> bool:
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


def _edge_path_exists(edges: Sequence[tuple[str, str]], start_id: str, target_id: str) -> bool:
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


def _temporal_lex_start(after_block_id: str | None) -> str:
    """Return the exclusive lex range start for one temporal page.

    Args:
        after_block_id: Optional block id to start after.

    Returns:
        `"-"` for the first page, or the exclusive lex bound for later pages.
    """

    return "-" if after_block_id is None else f"({after_block_id}"


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


def _validate_key_prefix(value: object) -> None:
    """Require a non-empty key prefix without whitespace."""

    _validate_nonempty_string(value, name="key_prefix")
    if any(character.isspace() for character in str(value)):
        raise ValueError("key_prefix must not contain whitespace.")


def _validate_timeout(value: object, *, name: str) -> None:
    """Require a finite, non-negative timeout in seconds."""

    if isinstance(value, bool) or not isinstance(value, int | float):
        raise TypeError(f"{name} must be a number.")
    if not math.isfinite(float(value)) or value < 0:
        raise ValueError(f"{name} must be a finite number greater than or equal to zero.")


class _UnavailableWatchError(Exception):
    """Placeholder used when the client exposes no watch conflict error."""


class _UnavailableNoScriptError(Exception):
    """Placeholder used when the client exposes no missing-script error."""


def _load_redis_client() -> tuple[Any, tuple[type[BaseException], ...], type[BaseException], type[BaseException]]:
    """Import the optional client and return it with its exception types."""

    try:
        # Import the submodule, not just the package. The `redis` package binds
        # `redis.exceptions` only as a side effect of its own imports, so type
        # checkers do not treat it as a known attribute of a plain `import redis`.
        import redis.exceptions
    except ImportError as error:
        raise UnsupportedOperation("RedisBackend requires the optional 'redis' package; install 'genie[redis]'.") from error
    exception_types = tuple(
        dict.fromkeys(
            exception_type
            for exception_type in (
                getattr(redis.exceptions, "RedisError", None),
                getattr(redis.exceptions, "RedisClusterException", None),
            )
            if isinstance(exception_type, type)
        )
    )
    watch_error = getattr(redis.exceptions, "WatchError", None)
    if not isinstance(watch_error, type):
        watch_error = _UnavailableWatchError
    no_script_error = getattr(redis.exceptions, "NoScriptError", None)
    if not isinstance(no_script_error, type):
        no_script_error = _UnavailableNoScriptError
    return redis, exception_types, watch_error, no_script_error


def _is_command_error(error: BaseException) -> bool:
    """Return whether Redis rejected the command itself.

    A rejected command means the server, the deployment, or the user's
    permissions do not offer it. A construction probe treats that as an unknown
    reading and passes. A transport failure is not this case and still reaches
    the caller.

    Args:
        error: Client failure raised while a command ran.

    Returns:
        `True` when the failure is a command rejection.
    """

    return "ResponseError" in {ancestor.__name__ for ancestor in type(error).__mro__}


def _translate_redis_error(error: BaseException) -> GenieError:
    """Translate client failures into stable Genie backend errors."""

    names = {ancestor.__name__ for ancestor in type(error).__mro__}
    message = str(error) or type(error).__name__
    normalized = f"{message} {type(error).__name__}".casefold()
    if "ClusterCrossSlotError" in names or "crossslot" in normalized:
        return UnsupportedOperation(f"Redis Cluster needs every Genie key in one hash slot; use a hash-tagged key prefix such as '{{genie}}': {message}")
    if names & {"OutOfMemoryError", "MaxConnectionsError"} or any(text in normalized for text in ("oom command not allowed", "maxmemory", "out of memory", "too many connections")):
        return BackendCapacityExceeded(f"Redis storage capacity was exceeded: {message}")
    if "ReadOnlyError" in names or any(text in normalized for text in ("readonly", "read only replica")):
        return BackendReadOnly(f"Redis storage is read-only: {message}")
    if names & {"AuthenticationError", "AuthorizationError", "NoPermissionError", "AuthenticationWrongNumberOfArgsError"} or any(
        text in normalized for text in ("wrongpass", "noauth", "noperm", "authentication", "invalid password", "invalid username")
    ):
        return BackendError(f"Redis authentication or authorization failed: {message}")
    if names & {"InvalidResponse", "ModuleError"} or "protocol error" in normalized:
        return BackendCorruption(f"Redis returned an invalid or unsupported reply: {message}")
    if "TimeoutError" in names or "timeout" in normalized:
        return BackendBusy(f"Redis remained busy past the configured timeout: {message}")
    if names & {"BusyLoadingError", "ConnectionError", "ClusterError", "ClusterDownError", "MasterDownError", "SlotNotCoveredError", "TryAgainError", "MovedError", "AskError", "RedisClusterException"} or any(
        text in normalized for text in ("misconf", "loading", "connection", "connecting", "network", "unreachable", "clusterdown", "no space", "unable to persist")
    ):
        return BackendUnavailable(f"Redis storage is unavailable: {message}")
    return BackendError(f"Redis backend operation failed: {message}")
