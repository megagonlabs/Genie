"""Assemble one backend from SQL blocks, a graph index, and a vector index.

- **Shared parts:** `RoutedBackend` accepts a shared traversal index, search
  index, and link store together with its block routes. Thus you can build one
  `MemoryBackend` from different kinds of storage.
- **This example:** SQLite stores the blocks. A graph component stores the
  ownership edges and typed links. A vector index answers searches.
- **Link store:** the application does not connect the link store to other
  code. `RoutedBackend` already sends each link write, read, and cleanup to it.
- **Derived indexes:** the traversal and search indexes hold derived state that
  the routed backend only reads. Thus a hook updates them after each committed
  write.

Run from the repository root:

    python examples/basic/heterogeneous_backend.py
"""

from __future__ import annotations

import re
import zlib
from collections.abc import Iterable
from typing import Literal

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    HookContext,
    InMemoryLinkStore,
    Memory,
    MemoryBlock,
    MemoryHook,
    MemoryLink,
    MemoryQuery,
    RoutedBackend,
    Scope,
    SearchResult,
    SQLiteBackend,
)
from genie.query import matches_filters, page_results

DIMENSIONS = 256
"""Number of components in each example embedding vector."""

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def embed(text: str) -> tuple[float, ...]:
    """Return a deterministic bag-of-tokens vector for one text value.

    A real deployment calls an embedding model here. This function hashes each
    token into one of the `DIMENSIONS` buckets. Thus the example has no dependencies
    and gives the same result each time.

    Args:
        text: Text to embed.

    Returns:
        A vector with `DIMENSIONS` components.
    """

    vector = [0.0] * DIMENSIONS
    for token in _TOKEN_PATTERN.findall(text.casefold()):
        vector[zlib.crc32(token.encode()) % DIMENSIONS] += 1.0
    return tuple(vector)


def cosine(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    """Return the cosine similarity of two vectors.

    Args:
        left: First vector.
        right: Second vector.

    Returns:
        Similarity in `[0.0, 1.0]` for the non-negative vectors from `embed`.
        The result is `0.0` if either vector is empty or has only zeros.
    """

    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not dot or not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def index_text(block: MemoryBlock) -> str:
    """Return the text that an external index embeds for one block.

    Args:
        block: Committed block as the backend stores it.

    Returns:
        The block type and the stored value in one searchable string.
    """

    return f"{block.type} {block.value}"


class GraphIndex:
    """Ownership edges and typed links outside the block store.

    The class implements the `TraversalIndex` and `LinkStore` capability
    protocols.

    - **Ids only:** the index stores only ids and links. It reads each block
      that it returns from the store that owns the block. Thus topology can
      live in one database and block values in a different database.
    - **Link storage:** the index gives link storage to `InMemoryLinkStore`.
      That class takes the place of the edge storage of a graph database.

    Attributes:
        blocks: Block store that the index reads to find the block for each id.
    """

    def __init__(self, blocks: SQLiteBackend) -> None:
        """Create an empty graph index over one block store.

        Args:
            blocks: Block store that owns the blocks this index describes.
        """

        self.blocks = blocks
        self._parents: dict[str, str | None] = {}
        self._children: dict[str, list[str]] = {}
        self._edges = InMemoryLinkStore()

    def track(self, block: MemoryBlock) -> None:
        """Record the ownership edge of one block, or move the edge to a new parent.

        Args:
            block: Committed block. The index records its parent edge.
        """

        if block.id in self._parents:
            previous = self._parents[block.id]
            if previous == block.parent_id:
                return
            if previous is not None:
                self._children[previous].remove(block.id)
        self._parents[block.id] = block.parent_id
        if block.parent_id is not None:
            self._children.setdefault(block.parent_id, []).append(block.id)

    def forget(self, block_id: str) -> None:
        """Remove the ownership edges of one deleted block.

        Args:
            block_id: Id of the deleted block.
        """

        parent = self._parents.pop(block_id, None)
        if parent is not None and block_id in self._children.get(parent, []):
            self._children[parent].remove(block_id)
        self._children.pop(block_id, None)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children for a block id.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks.
        """

        return self._resolve(self._children.get(block_id, ()))

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors from the nearest parent upward.

        Args:
            block_id: Block id to inspect.

        Returns:
            Ancestor blocks, nearest parent first.
        """

        ids: list[str] = []
        parent = self._parents.get(block_id)
        while parent is not None:
            ids.append(parent)
            parent = self._parents.get(parent)
        return self._resolve(ids)

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return every block below a root in breadth-first order.

        Args:
            block_id: Root block id.
            include_self: `True` to include `block_id` in the result.

        Returns:
            Descendant blocks.
        """

        ids: list[str] = [block_id] if include_self else []
        frontier = list(self._children.get(block_id, ()))
        while frontier:
            current = frontier.pop(0)
            ids.append(current)
            frontier.extend(self._children.get(current, ()))
        return self._resolve(ids)

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return `True` if a block is equal to a root or is below it.

        Args:
            root_id: Id of the possible subtree root.
            block_id: Id of the block to check.

        Returns:
            `True` when `block_id` is equal to or below `root_id`.
        """

        if root_id == block_id:
            return True
        parent = self._parents.get(block_id)
        while parent is not None:
            if parent == root_id:
                return True
            parent = self._parents.get(parent)
        return False

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store one typed link.

        Args:
            link: Link to store.

        Returns:
            Stored link.
        """

        return self._edges.add_link(link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace the metadata of one typed link.

        Args:
            link: Link that contains the replacement metadata.

        Returns:
            Stored replacement link.
        """

        return self._edges.update_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List the typed links connected to a block.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Links that match the relation and direction.
        """

        return self._edges.list_links(block_id, relation=relation, direction=direction)

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete every typed link connected to a block.

        Args:
            block_id: Id of the block whose links the index removes.
        """

        self._edges.delete_links_for_block(block_id)

    def _resolve(self, block_ids: Iterable[str]) -> list[MemoryBlock]:
        """Read the block for each id, and skip ids that the block store no longer has."""

        blocks = [self.blocks.get_block(block_id) for block_id in block_ids]
        return [block for block in blocks if block is not None]


class VectorIndex:
    """Similarity search over blocks that a different store holds.

    The class implements the `SearchIndex` capability protocol. It ranks blocks
    by the cosine similarity between the query vector and the vector of each
    block.

    - **No ownership:** a vector store holds embeddings and metadata, not
      ownership.
    - **Scope first:** Genie still gives the index a scope and a root id. Thus
      the index asks the graph index which blocks are in scope before it ranks
      blocks.
    - **Why:** without that step, the index ranks the whole corpus. Then
      `Memory` discards almost every result, because `Memory` applies the scope
      again after the backend applies `limit`.

    Attributes:
        blocks: Block store that the index reads to find the block for each id.
        graph: Graph index that the index reads to find the blocks in the query
            scope.
    """

    def __init__(self, blocks: SQLiteBackend, graph: GraphIndex) -> None:
        """Create an empty vector index.

        Args:
            blocks: Block store that owns the indexed blocks.
            graph: Graph index that knows ownership and link topology.
        """

        self.blocks = blocks
        self.graph = graph
        self._vectors: dict[str, tuple[float, ...]] = {}

    def index(self, block: MemoryBlock) -> None:
        """Embed one committed block.

        Args:
            block: Committed block to embed.
        """

        self._vectors[block.id] = embed(index_text(block))

    def forget(self, block_id: str) -> None:
        """Remove the vector of one deleted block.

        Args:
            block_id: Id of the deleted block.
        """

        self._vectors.pop(block_id, None)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Rank in-scope blocks by similarity to the query text.

        Args:
            query: Normalized query from the public API.

        Returns:
            In-scope blocks that match `query.types` and `query.filters`.
            `page_results` puts them in the search order of the `SearchIndex`
            contract and keeps at most `query.limit` results after
            `query.after`. The index drops blocks with a similarity of `0.0`
            or less. A query with no text returns each of these blocks with a
            score of `1.0`.
        """

        query_vector = embed(query.text) if query.text else None
        results: list[SearchResult] = []
        for block_id in self._scope_candidate_ids(query):
            block = self.blocks.get_block(block_id)
            if block is None:
                continue
            if query.types and block.type not in query.types:
                continue
            if query.filters and not matches_filters(block, query.filters):
                continue
            if query_vector is None:
                results.append(SearchResult(block=block, score=1.0, reason="in scope"))
                continue
            similarity = cosine(query_vector, self._vectors.get(block_id, ()))
            if similarity <= 0.0:
                continue
            results.append(
                SearchResult(
                    block=block,
                    score=similarity,
                    reason=f"cosine {similarity:.3f}",
                )
            )
        return page_results(results, query)

    def _scope_candidate_ids(self, query: MemoryQuery) -> list[str]:
        """Find the candidate ids for one query scope with the graph index."""

        if query.root_id is None or query.scope == Scope.GLOBAL:
            return list(self._vectors)
        if query.scope == Scope.SELF:
            return [query.root_id]
        if query.scope == Scope.SUBTREE:
            return [block.id for block in self.graph.descendants(query.root_id, include_self=True)]
        if query.scope == Scope.ANCESTORS:
            return [query.root_id, *(block.id for block in self.graph.ancestors(query.root_id))]
        if query.scope == Scope.LINKS:
            candidates = [query.root_id]
            for link in self.graph.list_links(query.root_id, direction="both"):
                for endpoint in (link.source_id, link.target_id):
                    if endpoint not in candidates:
                        candidates.append(endpoint)
            return candidates
        return list(self._vectors)


class IndexSync(MemoryHook):
    """Refresh the derived traversal and search indexes after each write.

    `RoutedBackend` sends every link operation to the shared link store, but it
    only reads the shared traversal and search indexes. The 2 indexes hold data
    that comes from committed blocks, so this hook updates them.

    Attributes:
        graph: Graph index that holds ownership edges.
        vectors: Vector index that holds block embeddings.
    """

    def __init__(self, graph: GraphIndex, vectors: VectorIndex) -> None:
        """Create a hook that keeps one graph index and one vector index current.

        Args:
            graph: Graph index to keep current.
            vectors: Vector index to keep current.
        """

        self.graph = graph
        self.vectors = vectors

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Add a new stored block to both indexes."""

        self.graph.track(block)
        self.vectors.index(block)

    def after_update(
        self,
        context: HookContext,
        previous: MemoryBlock,
        stored: MemoryBlock,
    ) -> None:
        """Index an updated block again.

        `Memory.update` and `Memory.set` can change the value, but not the
        parent. Thus `GraphIndex.track` keeps the same ownership edge, and only
        the vector can change.
        """

        self.graph.track(stored)
        self.vectors.index(stored)

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Remove every derived record of a deleted block."""

        self.graph.forget(block.id)
        self.vectors.forget(block.id)


def main() -> None:
    """Store blocks in SQLite, and answer queries from a graph index and a vector index."""

    print("== Heterogeneous routed backend ==")
    blocks = SQLiteBackend(":memory:")
    graph = GraphIndex(blocks)
    vectors = VectorIndex(blocks, graph)
    # One `MemoryBackend` from 3 components. SQL holds the blocks, the graph index
    # holds the topology, and the vector index ranks the results.
    backend = RoutedBackend(
        {"sql": blocks},
        default_route="sql",
        traversal_index=graph,
        search_index=vectors,
        link_store=graph,
    )
    memory = Memory(
        backend=backend,
        principal="app:demo",
        hooks=[IndexSync(graph, vectors)],
    )

    project = memory.put(type="project", value="Retrieval proposal")
    agnostic = memory.put(
        parent=project,
        type="requirement",
        value="Keep the proposal concise and database agnostic.",
        metadata={"priority": 3},
    )
    notes = memory.put(
        parent=project,
        type="requirement",
        value="Include optional implementation notes.",
        metadata={"priority": 1},
    )
    glossary = memory.put(type="note", value="Glossary of retrieval terms")
    reference = memory.add_link(project, glossary, "references")

    # The query reverses the word order, and the stored value does not contain the
    # phrase. Thus a substring matcher finds nothing. Cosine similarity still ranks
    # the requirement first.
    semantic_results = memory.search("agnostic database", scope="global")
    # The vector index does not know what a subtree is. It asks the graph index.
    subtree_results = memory.search("retrieval", root=project, scope="subtree")
    linked_results = memory.search("glossary", root=project, scope="links")
    filtered_results = memory.search(
        "proposal",
        root=project,
        scope="subtree",
        types=["requirement"],
        filters={"metadata.priority": {">=": 2}},
    )

    print(f"blocks stored in SQLite: {len(graph.descendants(project.id, include_self=True))}")
    print(f"top semantic match: {semantic_results[0].block.value}")
    print(f"score is a cosine, not a substring count: {semantic_results[0].score:.3f}")
    print(f"reason from the vector index: {semantic_results[0].reason!r}")
    print(f"subtree search skipped the unowned glossary: {len(subtree_results)} result")
    print(f"link search reached the glossary: {linked_results[0].block.value}")
    print(f"filtered subtree search: {filtered_results[0].block.value}")

    # The blocks are in SQLite and in no other component.
    assert blocks.get_block(agnostic.id) is not None
    assert graph.blocks is blocks

    # The ownership data comes from the graph index, not from SQLite.
    assert [block.id for block in memory.children(project)] == [agnostic.id, notes.id]
    assert graph.contains(project.id, notes.id)
    assert not graph.contains(project.id, glossary.id)

    # The vector index ranks the results.
    assert [result.block.id for result in semantic_results] == [agnostic.id]
    assert 0.0 < semantic_results[0].score < 1.0
    assert semantic_results[0].reason is not None
    assert semantic_results[0].reason.startswith("cosine ")

    # The vector index read the scope from the graph index before it ranked the blocks.
    assert [result.block.id for result in subtree_results] == [project.id]
    assert [result.block.id for result in linked_results] == [glossary.id]
    assert [result.block.id for result in filtered_results] == [agnostic.id]
    assert memory.list_links(project) == [reference]

    # A recursive delete removes the data from all 3 components together.
    memory.delete(project)

    assert memory.get(agnostic) is None
    assert blocks.get_block(agnostic.id) is None
    assert graph.descendants(project.id, include_self=True) == []
    assert memory.list_links(glossary, direction="in") == []
    assert list(memory.search("agnostic database", scope="global")) == []
    assert memory.get(glossary) is not None


if __name__ == "__main__":
    main()
