from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Literal

from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict
from genie.memory.links import MemoryLink
from genie.query.filters import matches_filters
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult
from genie.query.scope import Scope


class InMemoryBackend:
    """Simple backend useful for tests, examples, and single-process agents."""

    def __init__(self) -> None:
        self._blocks: dict[str, MemoryBlock] = {}
        self._children: dict[str, list[str]] = defaultdict(list)
        self._links: dict[str, MemoryLink] = {}

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
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
        block = self._blocks.get(block_id)
        return _copy_block(block) if block is not None else None

    def update_block(self, block: MemoryBlock) -> MemoryBlock:
        previous = self._blocks.get(block.id)
        if previous is None:
            raise MemoryConflict(f"Memory block {block.id!r} does not exist.")
        if previous.parent_id != block.parent_id:
            self._move_parent(previous, block)
        block = _copy_block(block)
        self._blocks[block.id] = block
        return block

    def delete_block(self, block_id: str) -> None:
        block = self._blocks.get(block_id)
        if block is None:
            return
        for child in list(self._children.get(block_id, ())):
            self.delete_block(child)
        if block.parent_id is not None and block_id in self._children.get(block.parent_id, []):
            self._children[block.parent_id].remove(block_id)
        self._children.pop(block_id, None)
        self._blocks.pop(block_id, None)
        for link_id, link in list(self._links.items()):
            if link.source_id == block_id or link.target_id == block_id:
                self._links.pop(link_id, None)

    def children(self, block_id: str) -> list[MemoryBlock]:
        return [_copy_block(self._blocks[child_id]) for child_id in self._children.get(block_id, ())]

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
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
        blocks: list[MemoryBlock] = []
        if include_self and block_id in self._blocks:
            blocks.append(_copy_block(self._blocks[block_id]))
        for child_id in self._children.get(block_id, ()):
            blocks.extend(self.descendants(child_id, include_self=True))
        return blocks

    def contains(self, root_id: str, block_id: str) -> bool:
        if root_id == block_id:
            return True
        return any(block.id == block_id for block in self.descendants(root_id))

    def add_link(self, link: MemoryLink) -> MemoryLink:
        if link.source_id not in self._blocks:
            raise MemoryConflict(f"Source memory block {link.source_id!r} does not exist.")
        if link.target_id not in self._blocks:
            raise MemoryConflict(f"Target memory block {link.target_id!r} does not exist.")
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
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")
        selected: list[MemoryLink] = []
        for link in self._links.values():
            if relation is not None and link.relation != relation:
                continue
            if direction == "out" and link.source_id == block_id:
                selected.append(_copy_link(link))
            elif direction == "in" and link.target_id == block_id:
                selected.append(_copy_link(link))
            elif direction == "both" and block_id in {link.source_id, link.target_id}:
                selected.append(_copy_link(link))
        return selected

    def search(self, query: MemoryQuery) -> list[SearchResult]:
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
            block_ids: set[str] = set()
            for link in self.list_links(query.root_id, direction="both"):
                block_ids.add(link.source_id)
                block_ids.add(link.target_id)
            return [self._blocks[block_id] for block_id in block_ids if block_id in self._blocks]
        return list(self._blocks.values())

    def _move_parent(self, previous: MemoryBlock, updated: MemoryBlock) -> None:
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
    return block.with_updates(
        value=deepcopy(block.value),
        metadata=deepcopy(block.metadata),
    )


def _copy_link(link: MemoryLink) -> MemoryLink:
    return MemoryLink(
        id=link.id,
        source_id=link.source_id,
        target_id=link.target_id,
        relation=link.relation,
        metadata=deepcopy(link.metadata),
        created_at=link.created_at,
    )
