from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


@runtime_checkable
class BlockStore(Protocol):
    def put_block(self, block: MemoryBlock) -> MemoryBlock: ...

    def get_block(self, block_id: str) -> MemoryBlock | None: ...

    def update_block(self, block: MemoryBlock) -> MemoryBlock: ...

    def delete_block(self, block_id: str) -> None: ...


@runtime_checkable
class TraversalIndex(Protocol):
    def children(self, block_id: str) -> list[MemoryBlock]: ...

    def ancestors(self, block_id: str) -> list[MemoryBlock]: ...

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]: ...

    def contains(self, root_id: str, block_id: str) -> bool: ...


@runtime_checkable
class SearchIndex(Protocol):
    def search(self, query: MemoryQuery) -> list[SearchResult]: ...


@runtime_checkable
class LinkStore(Protocol):
    def add_link(self, link: MemoryLink) -> MemoryLink: ...

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]: ...
