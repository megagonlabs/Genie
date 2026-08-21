"""In-memory reference backend.

The backend is intentionally small and dependency-free. It is useful for tests,
examples, and local prototypes, and also acts as executable documentation for
the backend interfaces.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from threading import RLock
from typing import TYPE_CHECKING, Literal

from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict
from genie.memory.links import MemoryLink
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


class InMemoryBackend:
    """Single-process backend implementing all Genie backend interfaces."""

    def __init__(self) -> None:
        """Create an empty in-memory backend."""

        self._blocks: dict[str, MemoryBlock] = {}
        self._children: dict[str, list[str]] = defaultdict(list)
        self._links: dict[str, MemoryLink] = {}
        self._acyclic_relations: set[str] = set()
        self._key_bindings: dict[
            tuple[str, tuple[str, ...], str],
            KeyBinding,
        ] = {}
        self._log_entries: dict[str, dict[int, LogEntry]] = defaultdict(dict)
        self._next_log_sequence: dict[str, int] = {}
        self._temporal_entries: dict[tuple[str, str], TemporalEntry] = {}
        self._structure_lock = RLock()

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store a new encoded block.

        Args:
            block: Block to store.

        Returns:
            Stored block.

        Raises:
            MemoryConflict: If the id already exists or the parent is missing.
        """

        with self._structure_lock:
            if block.id in self._blocks:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            if block.parent_id is not None and block.parent_id not in self._blocks:
                raise MemoryConflict(f"Parent memory block {block.parent_id!r} does not exist.")
            block = _copy_block(block)
            self._blocks[block.id] = block
            if block.parent_id is not None:
                self._children[block.parent_id].append(block.id)
            return block

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Fetch a block by id.

        Args:
            block_id: Memory block id.

        Returns:
            Defensive copy of the block, or `None` when missing.
        """

        with self._structure_lock:
            block = self._blocks.get(block_id)
            return _copy_block(block) if block is not None else None

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace an existing encoded block, optionally by version.

        Args:
            block: Replacement block.
            expected_version: Required current version for compare-and-swap.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block is missing, its version changed, the
                parent is missing, or the change would create a parent cycle.
        """

        with self._structure_lock:
            previous = self._blocks.get(block.id)
            if previous is None:
                raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
            _validate_expected_version(previous, block, expected_version)
            if previous.parent_id != block.parent_id:
                self._move_parent(previous, block)
            block = _copy_block(block)
            self._blocks[block.id] = block
            return _copy_block(block)

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a block and descendants, optionally from an exact snapshot.

        Args:
            block_id: Block id to delete. Missing ids are ignored.
            expected_versions: Exact subtree id-to-version snapshot required
                for the deletion.

        Raises:
            MemoryConflict: If the supplied subtree snapshot is stale.
        """

        with self._structure_lock:
            if expected_versions is not None:
                current = {block.id: block.version for block in self.descendants(block_id, include_self=True)}
                if current != dict(expected_versions):
                    raise MemoryConflict(f"Memory subtree {block_id!r} changed before it could be deleted.")
            self._delete_block_unchecked(block_id)

    def _delete_block_unchecked(self, block_id: str) -> None:
        """Delete one subtree while the structure lock is held."""

        block = self._blocks.get(block_id)
        if block is None:
            return
        for child in list(self._children.get(block_id, ())):
            self._delete_block_unchecked(child)
        if block.parent_id is not None and block_id in self._children.get(block.parent_id, []):
            self._children[block.parent_id].remove(block_id)
        self._children.pop(block_id, None)
        self._blocks.pop(block_id, None)
        self.delete_links_for_block(block_id)
        self.delete_key_bindings_for_block(block_id)
        self.delete_log_entries_for_block(block_id)
        self.delete_temporal_entries_for_block(block_id)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct child blocks for a parent id.

        Args:
            block_id: Parent block id.

        Returns:
            Defensive copies of direct child blocks.
        """

        with self._structure_lock:
            return [_copy_block(self._blocks[child_id]) for child_id in self._children.get(block_id, ())]

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from nearest parent upward.

        Args:
            block_id: Block id to inspect.

        Returns:
            Defensive copies of ancestor blocks.
        """

        with self._structure_lock:
            ancestors: list[MemoryBlock] = []
            block = self._blocks.get(block_id)
            while block is not None and block.parent_id is not None:
                parent = self._blocks.get(block.parent_id)
                if parent is None:
                    break
                ancestors.append(_copy_block(parent))
                block = parent
            return ancestors

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return descendants in depth-first order.

        Args:
            block_id: Root block id.
            include_self: Whether to include `block_id` in the result.

        Returns:
            Defensive copies of descendant blocks.
        """

        with self._structure_lock:
            blocks: list[MemoryBlock] = []
            if include_self and block_id in self._blocks:
                blocks.append(_copy_block(self._blocks[block_id]))
            for child_id in self._children.get(block_id, ()):
                blocks.extend(self.descendants(child_id, include_self=True))
            return blocks

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is in `root_id`'s subtree.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when `block_id` is equal to or below `root_id`.
        """

        with self._structure_lock:
            if root_id not in self._blocks:
                return False
            if root_id == block_id:
                return True
            return any(block.id == block_id for block in self.descendants(root_id))

    def bind_key(self, binding: KeyBinding) -> KeyBinding:
        """Atomically bind an unused collection address to a block.

        Args:
            binding: Key binding to create.

        Returns:
            Stored binding.

        Raises:
            MemoryConflict: If the collection or block is missing, or the
                address is already bound.
        """

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            slot = _key_slot(binding.collection_id, binding.address)
            if slot in self._key_bindings:
                raise MemoryConflict(f"Key address {binding.address!r} is already bound in " f"collection {binding.collection_id!r}.")
            self._key_bindings[slot] = binding
            return binding

    def rebind_key(
        self,
        binding: KeyBinding,
        *,
        expected_block_id: str,
    ) -> KeyBinding:
        """Atomically replace a key binding with compare-and-swap semantics."""

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=binding.collection_id,
                block_id=binding.block_id,
                container_name="Keyed collection",
            )
            slot = _key_slot(binding.collection_id, binding.address)
            current = self._key_bindings.get(slot)
            if current is None or current.block_id != expected_block_id:
                raise MemoryConflict(f"Key address {binding.address!r} in collection " f"{binding.collection_id!r} no longer points to expected " f"block {expected_block_id!r}.")
            self._key_bindings[slot] = binding
            return binding

    def get_key(
        self,
        collection_id: str,
        address: MemoryAddress,
    ) -> KeyBinding | None:
        """Return one exact key binding."""

        with self._structure_lock:
            return self._key_bindings.get(_key_slot(collection_id, address))

    def list_keys(
        self,
        collection_id: str,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        after: MemoryAddress | None = None,
        limit: int = 100,
    ) -> list[KeyBinding]:
        """Return one bounded page in stable namespace-and-key order."""

        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a boolean.")
        _validate_limit(limit)
        with self._structure_lock:
            bindings = [
                binding
                for binding in self._key_bindings.values()
                if binding.collection_id == collection_id
                and _namespace_matches(
                    binding.address.namespace,
                    namespace,
                    recursive=recursive,
                )
            ]
            bindings = sorted(
                bindings,
                key=lambda binding: (binding.address.namespace, binding.address.key),
            )
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
        """Atomically remove a key binding when its block id still matches."""

        with self._structure_lock:
            slot = _key_slot(collection_id, address)
            current = self._key_bindings.get(slot)
            if current is None or current.block_id != expected_block_id:
                raise MemoryConflict(f"Key address {address!r} in collection {collection_id!r} " f"no longer points to expected block {expected_block_id!r}.")
            self._key_bindings.pop(slot)

    def delete_key_bindings_for_block(self, block_id: str) -> None:
        """Remove bindings owned by or pointing to one deleted block."""

        with self._structure_lock:
            for slot, binding in list(self._key_bindings.items()):
                if binding.collection_id == block_id or binding.block_id == block_id:
                    self._key_bindings.pop(slot, None)

    def append_log_entry(self, log_id: str, block_id: str) -> LogEntry:
        """Atomically append a block reference with a unique sequence number."""

        from genie.structures.records import LogEntry

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=log_id,
                block_id=block_id,
                container_name="Event log",
            )
            sequence = self._next_log_sequence.get(log_id, 0)
            entry = LogEntry(log_id=log_id, sequence=sequence, block_id=block_id)
            self._log_entries[log_id][sequence] = entry
            self._next_log_sequence[log_id] = sequence + 1
            return entry

    def get_log_entry(self, log_id: str, sequence: int) -> LogEntry | None:
        """Return one exact log entry."""

        _validate_sequence(sequence, name="sequence")
        with self._structure_lock:
            return self._log_entries.get(log_id, {}).get(sequence)

    def list_log_entries(
        self,
        log_id: str,
        *,
        start: int | None = None,
        stop: int | None = None,
        reverse: bool = False,
        limit: int = 100,
    ) -> list[LogEntry]:
        """Return one bounded page in a half-open sequence range."""

        if not isinstance(reverse, bool):
            raise TypeError("reverse must be a boolean.")
        if start is not None:
            _validate_sequence(start, name="start")
        if stop is not None:
            _validate_sequence(stop, name="stop")
        if start is not None and stop is not None and stop < start:
            raise ValueError("stop must be greater than or equal to start.")
        _validate_limit(limit)
        with self._structure_lock:
            entries = [entry for sequence, entry in self._log_entries.get(log_id, {}).items() if (start is None or sequence >= start) and (stop is None or sequence < stop)]
            entries = sorted(
                entries,
                key=lambda entry: entry.sequence,
                reverse=reverse,
            )
            return entries[:limit]

    def delete_log_entries_for_block(self, block_id: str) -> None:
        """Remove an owned log or entries pointing to one deleted block."""

        with self._structure_lock:
            self._log_entries.pop(block_id, None)
            self._next_log_sequence.pop(block_id, None)
            for log_id, entries in list(self._log_entries.items()):
                for sequence, entry in list(entries.items()):
                    if entry.block_id == block_id:
                        entries.pop(sequence, None)
                if not entries:
                    self._log_entries.pop(log_id, None)

    def add_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Atomically add unique temporal membership for one block."""

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            slot = (entry.collection_id, entry.block_id)
            if slot in self._temporal_entries:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is already in temporal " f"collection {entry.collection_id!r}.")
            self._temporal_entries[slot] = entry
            return entry

    def replace_temporal_entry(self, entry: TemporalEntry) -> TemporalEntry:
        """Atomically replace temporal membership for one existing block."""

        with self._structure_lock:
            self._require_structure_blocks(
                container_id=entry.collection_id,
                block_id=entry.block_id,
                container_name="Temporal collection",
            )
            slot = (entry.collection_id, entry.block_id)
            if slot not in self._temporal_entries:
                raise MemoryConflict(f"Memory block {entry.block_id!r} is not in temporal " f"collection {entry.collection_id!r}.")
            self._temporal_entries[slot] = entry
            return entry

    def get_temporal_entry(
        self,
        collection_id: str,
        block_id: str,
    ) -> TemporalEntry | None:
        """Return one exact temporal entry."""

        with self._structure_lock:
            return self._temporal_entries.get((collection_id, block_id))

    def list_temporal_entries(
        self,
        collection_id: str,
        *,
        after_block_id: str | None = None,
        limit: int = 100,
    ) -> list[TemporalEntry]:
        """Return one temporal membership page in stable block-id order."""

        _validate_limit(limit)
        with self._structure_lock:
            entries = sorted(
                (entry for entry in self._temporal_entries.values() if entry.collection_id == collection_id and (after_block_id is None or entry.block_id > after_block_id)),
                key=lambda entry: entry.block_id,
            )
            return entries[:limit]

    def remove_temporal_entry(self, collection_id: str, block_id: str) -> None:
        """Atomically remove one temporal membership entry."""

        with self._structure_lock:
            slot = (collection_id, block_id)
            if slot not in self._temporal_entries:
                raise MemoryConflict(f"Memory block {block_id!r} is not in temporal collection " f"{collection_id!r}.")
            self._temporal_entries.pop(slot)

    def delete_temporal_entries_for_block(self, block_id: str) -> None:
        """Remove collections owned by and entries pointing to a deleted block."""

        with self._structure_lock:
            for slot, entry in list(self._temporal_entries.items()):
                if entry.collection_id == block_id or entry.block_id == block_id:
                    self._temporal_entries.pop(slot, None)

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
        """Return one indexed-contract page matching temporal filters."""

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
        with self._structure_lock:
            entries = sorted(
                (
                    entry
                    for entry in self._temporal_entries.values()
                    if entry.collection_id == collection_id
                    and (after_block_id is None or entry.block_id > after_block_id)
                    and _temporal_matches(
                        entry,
                        occurred_only=occurred_only,
                        occurred_start=occurred_start,
                        occurred_stop=occurred_stop,
                        valid_at=valid_at,
                        overlaps=overlaps,
                    )
                ),
                key=lambda entry: entry.block_id,
            )
            return entries[:limit]

    def _require_structure_blocks(
        self,
        *,
        container_id: str,
        block_id: str,
        container_name: str,
    ) -> None:
        """Require a structure container and referenced block to exist."""

        if container_id not in self._blocks:
            raise MemoryConflict(f"{container_name} block {container_id!r} does not exist.")
        if block_id not in self._blocks:
            raise MemoryConflict(f"Memory block {block_id!r} does not exist.")

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a typed link between existing blocks.

        Args:
            link: Link to store.

        Returns:
            Defensive copy of the stored link.

        Raises:
            MemoryConflict: If the link id already exists or either endpoint
                does not exist.
        """

        with self._structure_lock:
            if link.relation in self._acyclic_relations:
                raise MemoryConflict(f"Link relation {link.relation!r} is protected as an acyclic " "graph; use atomic acyclic insertion.")
            self._validate_new_link(link)
            return self._store_new_link(link)

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Atomically add a link when its relation remains acyclic.

        Existing links with the same relation are adopted on the first call
        only when they already form a DAG. The relation is then protected from
        ordinary link insertion.
        """

        with self._structure_lock:
            self._validate_new_link(link)
            if link.relation not in self._acyclic_relations and self._relation_has_cycle(link.relation):
                raise MemoryConflict(f"Link relation {link.relation!r} already contains a cycle.")
            if link.source_id == link.target_id or self._link_path_exists(
                link.target_id,
                link.source_id,
                relation=link.relation,
            ):
                raise MemoryConflict(f"Link {link.source_id!r} -> {link.target_id!r} would create " f"a cycle in relation {link.relation!r}.")
            self._acyclic_relations.add(link.relation)
            return self._store_new_link(link)

    def _validate_new_link(self, link: MemoryLink) -> None:
        """Validate identity and endpoints before storing a link."""

        if link.id in self._links:
            raise MemoryConflict(f"Memory link {link.id!r} already exists.")
        if link.source_id not in self._blocks:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if link.target_id not in self._blocks:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")

    def _store_new_link(self, link: MemoryLink) -> MemoryLink:
        """Store a validated link while the structure lock is held."""

        stored = _copy_link(link)
        self._links[stored.id] = stored
        return _copy_link(stored)

    def _link_path_exists(
        self,
        start_id: str,
        target_id: str,
        *,
        relation: str,
    ) -> bool:
        """Return whether one relation has a directed path between two nodes."""

        pending = deque([start_id])
        visited: set[str] = set()
        while pending:
            current_id = pending.popleft()
            if current_id == target_id:
                return True
            if current_id in visited:
                continue
            visited.add(current_id)
            pending.extend(link.target_id for link in self._links.values() if link.relation == relation and link.source_id == current_id)
        return False

    def _relation_has_cycle(self, relation: str) -> bool:
        """Return whether existing links of one relation contain a cycle."""

        adjacency: dict[str, list[str]] = defaultdict(list)
        indegree: dict[str, int] = defaultdict(int)
        nodes: set[str] = set()
        for link in self._links.values():
            if link.relation != relation:
                continue
            adjacency[link.source_id].append(link.target_id)
            indegree[link.target_id] += 1
            nodes.add(link.source_id)
            nodes.add(link.target_id)
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

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace the metadata of an existing typed link.

        Args:
            link: Link containing the replacement metadata.

        Returns:
            Defensive copy of the stored replacement link.

        Raises:
            MemoryConflict: If the link is missing or any field other than
                metadata changed.
        """

        with self._structure_lock:
            previous = self._links.get(link.id)
            if previous is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            link = _copy_link(link)
            self._links[link.id] = link
            return _copy_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to a block.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Defensive copies of matching links.

        Raises:
            ValueError: If `direction` is invalid.
        """

        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")
        with self._structure_lock:
            selected: list[MemoryLink] = []
            for link in self._links.values():
                if relation is not None and link.relation != relation:
                    continue
                if (
                    (direction == "out" and link.source_id == block_id)
                    or (direction == "in" and link.target_id == block_id)
                    or (direction == "both" and block_id in {link.source_id, link.target_id})
                ):
                    selected.append(_copy_link(link))
            return selected

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete incoming and outgoing links for a block.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        with self._structure_lock:
            for link_id, link in list(self._links.items()):
                if link.source_id == block_id or link.target_id == block_id:
                    self._links.pop(link_id, None)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks using portable text and metadata matching.

        Args:
            query: Normalized memory query.

        Returns:
            Search results sorted by descending score.
        """

        with self._structure_lock:
            candidates = self._scope_candidates(query)
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
                results.append(SearchResult(block=_copy_block(block), score=score, reason=reason))
            results.sort(key=lambda result: result.score, reverse=True)
            return results[: query.limit]

    def _scope_candidates(self, query: MemoryQuery) -> list[MemoryBlock]:
        """Return candidate blocks before text/type/filter matching."""

        if query.scope == Scope.GLOBAL or query.root_id is None:
            return list(self._blocks.values())
        if query.root_id not in self._blocks:
            return []
        if query.scope == Scope.SELF:
            return [self._blocks[query.root_id]]
        if query.scope == Scope.SUBTREE:
            return self.descendants(query.root_id, include_self=True)
        if query.scope == Scope.ANCESTORS:
            return [self._blocks[query.root_id], *self.ancestors(query.root_id)]
        if query.scope == Scope.LINKS:
            block_ids: list[str] = []
            seen: set[str] = set()
            for link in self.list_links(query.root_id, direction="both"):
                for block_id in (link.source_id, link.target_id):
                    if block_id not in seen:
                        seen.add(block_id)
                        block_ids.append(block_id)
            return [self._blocks[block_id] for block_id in block_ids if block_id in self._blocks]
        return list(self._blocks.values())

    def _move_parent(self, previous: MemoryBlock, updated: MemoryBlock) -> None:
        """Update parent indexes while rejecting invalid parent changes."""

        if updated.parent_id is not None and updated.parent_id not in self._blocks:
            raise MemoryConflict(f"Parent memory block {updated.parent_id!r} does not exist.")
        if updated.parent_id == updated.id:
            raise MemoryConflict("A memory block cannot be its own parent.")
        if updated.parent_id is not None and self.contains(updated.id, updated.parent_id):
            raise MemoryConflict("A memory block cannot be moved under one of its descendants.")
        if previous.parent_id is not None and previous.id in self._children.get(previous.parent_id, []):
            self._children[previous.parent_id].remove(previous.id)
        if updated.parent_id is not None:
            self._children[updated.parent_id].append(updated.id)


def _copy_block(block: MemoryBlock) -> MemoryBlock:
    """Return a block copy with isolated mutable payloads."""

    return block.with_updates(
        value=deepcopy(block.value),
        metadata=deepcopy(block.metadata),
    )


def _copy_link(link: MemoryLink) -> MemoryLink:
    """Return a link copy with isolated mutable metadata."""

    return MemoryLink(
        id=link.id,
        source_id=link.source_id,
        target_id=link.target_id,
        relation=link.relation,
        metadata=deepcopy(link.metadata),
        created_at=link.created_at,
    )


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at


def _validate_expected_version(
    previous: MemoryBlock,
    replacement: MemoryBlock,
    expected_version: int | None,
) -> None:
    """Validate one optional compare-and-swap block replacement."""

    if expected_version is None:
        return
    if previous.version != expected_version:
        raise MemoryConflict(f"Memory block {previous.id!r} changed from expected version " f"{expected_version}.")
    if replacement.version != expected_version + 1:
        raise MemoryConflict("A conditional block replacement must increment its version by one.")


def _key_slot(
    collection_id: str,
    address: MemoryAddress,
) -> tuple[str, tuple[str, ...], str]:
    """Return the unique in-memory slot for a key binding."""

    return collection_id, address.namespace, address.key


def _namespace_matches(
    candidate: tuple[str, ...],
    selected: tuple[str, ...] | None,
    *,
    recursive: bool,
) -> bool:
    """Return whether a namespace satisfies an exact or prefix selection."""

    if selected is None:
        return True
    if recursive:
        return candidate[: len(selected)] == selected
    return candidate == selected


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


def _normalize_optional_time(value: object, *, name: str) -> datetime | None:
    """Validate and normalize an optional aware datetime to UTC."""

    if value is None:
        return None
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware.")
    return value.astimezone(UTC)
