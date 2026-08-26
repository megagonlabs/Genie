"""Assemble one backend from SQL blocks, a graph index, and a vector index.

`RoutedBackend` accepts a shared traversal index, search index, and link store
alongside its block routes, so a single `MemoryBackend` can be built from
different kinds of storage. Here blocks live in SQLite, ownership edges and
typed links live in a graph component, and searches are answered by a vector
index.

The link store needs no application wiring, because `RoutedBackend` already
sends every link write, read, and cleanup to it. The traversal and search
indexes are derived state that the routed backend only reads, so a hook
refreshes them after each committed write.

Run from the repository root:

    conda run -n "genie" python examples/basic/heterogeneous_backend.py
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
from genie.query import matches_filters

DIMENSIONS = 256
"""Length of the stand-in embedding vectors."""

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


def embed(text: str) -> tuple[float, ...]:
    """Return a deterministic bag-of-tokens vector for one text value.

    A deployment would call an embedding model here. Hashing tokens into a
    fixed number of buckets keeps the example dependency-free and repeatable.

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
        Similarity in `[0.0, 1.0]`, or `0.0` when either vector is empty.
    """

    dot = sum(a * b for a, b in zip(left, right, strict=False))
    left_norm = sum(a * a for a in left) ** 0.5
    right_norm = sum(b * b for b in right) ** 0.5
    if not dot or not left_norm or not right_norm:
        return 0.0
    return dot / (left_norm * right_norm)


def index_text(block: MemoryBlock) -> str:
    """Return the text an external index would embed for one block.

    Args:
        block: Committed block as the backend stores it.

    Returns:
        Type and stored value joined into one searchable string.
    """

    return f"{block.type} {block.value}"


class GraphIndex:
    """Ownership edges and typed links held outside the block store.

    This class implements the `TraversalIndex` and `LinkStore` capability
    protocols. It stores ids and links only, and reads every block it returns
    back from the store that owns it. That is what lets topology live in one
    database while block values live in another. Link storage is delegated to
    `InMemoryLinkStore`, which stands in for a graph database's edge storage.

    Attributes:
        blocks: Block store used to resolve ids into blocks.
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
        """Record or move one block's ownership edge.

        Args:
            block: Committed block whose parent edge should be indexed.
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
        """Drop one block's ownership edges after it is deleted.

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
            include_self: Whether to include `block_id` in the result.

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
        """Return whether a block is equal to or below a root.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

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
        """Persist one typed edge.

        Args:
            link: Link to store.

        Returns:
            Stored link.
        """

        return self._edges.add_link(link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace the metadata of one typed edge.

        Args:
            link: Link containing the replacement metadata.

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
        """List typed edges connected to a block.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Edge direction relative to `block_id`.

        Returns:
            Matching links.
        """

        return self._edges.list_links(block_id, relation=relation, direction=direction)

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete every typed edge touching a block.

        Args:
            block_id: Block id whose edges should be removed.
        """

        self._edges.delete_links_for_block(block_id)

    def _resolve(self, block_ids: Iterable[str]) -> list[MemoryBlock]:
        """Read ids back as blocks, skipping ids the block store no longer has."""

        blocks = [self.blocks.get_block(block_id) for block_id in block_ids]
        return [block for block in blocks if block is not None]


class VectorIndex:
    """Similarity search over blocks that are stored somewhere else.

    This class implements the `SearchIndex` capability protocol and ranks by
    cosine similarity between the query vector and one vector per block.

    A vector store holds embeddings and metadata, not ownership. Genie still
    hands it a scope and a root id, so this index asks the graph index which
    blocks are in scope before ranking anything. Skipping that step would rank
    the whole corpus and leave `Memory` to discard almost every result, since
    `Memory` reapplies the scope after the backend has already applied `limit`.

    Attributes:
        blocks: Block store used to resolve ids into blocks.
        graph: Graph index consulted to resolve query scope.
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
        """Drop one block's vector after it is deleted.

        Args:
            block_id: Id of the deleted block.
        """

        self._vectors.pop(block_id, None)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Rank in-scope blocks by similarity to the query text.

        Args:
            query: Normalized query from the public API.

        Returns:
            Results ordered by descending similarity, limited to `query.limit`.
            A query with no text returns every in-scope block.
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
        results.sort(key=lambda result: (-result.score, result.block.id))
        return results[: query.limit]

    def _scope_candidate_ids(self, query: MemoryQuery) -> list[str]:
        """Resolve one query scope into candidate ids using the graph index."""

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

    `RoutedBackend` routes every link operation into the shared link store, but
    it only reads the shared traversal and search indexes. Both are derived from
    committed blocks, so they are rebuilt here instead.

    Attributes:
        graph: Graph index holding ownership edges.
        vectors: Vector index holding block embeddings.
    """

    def __init__(self, graph: GraphIndex, vectors: VectorIndex) -> None:
        """Create a synchronizer for one graph and vector index pair.

        Args:
            graph: Graph index to keep current.
            vectors: Vector index to keep current.
        """

        self.graph = graph
        self.vectors = vectors

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Index a newly stored block."""

        self.graph.track(block)
        self.vectors.index(block)

    def after_update(
        self,
        context: HookContext,
        previous: MemoryBlock,
        stored: MemoryBlock,
    ) -> None:
        """Re-index a block whose value or parent changed."""

        self.graph.track(stored)
        self.vectors.index(stored)

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Drop every derived record for a deleted block."""

        self.graph.forget(block.id)
        self.vectors.forget(block.id)


def main() -> None:
    """Store blocks in SQLite while a graph and a vector index serve queries."""

    print("== Heterogeneous routed backend ==")
    blocks = SQLiteBackend(":memory:")
    graph = GraphIndex(blocks)
    vectors = VectorIndex(blocks, graph)
    # One `MemoryBackend` assembled from three components: blocks in SQL, topology
    # in the graph index, ranking in the vector index.
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

    # Word order is reversed and the phrase never appears in the stored value,
    # so a substring matcher would find nothing. Cosine similarity still ranks
    # the requirement first.
    semantic_results = memory.search("agnostic database", scope="global")
    # The vector index has no idea what a subtree is; it asked the graph index.
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

    # Blocks really do live in SQLite and nowhere else.
    assert blocks.get_block(agnostic.id) is not None
    assert graph.blocks is blocks

    # Ownership really does come from the graph index, not from SQLite.
    assert [block.id for block in memory.children(project)] == [agnostic.id, notes.id]
    assert graph.contains(project.id, notes.id)
    assert not graph.contains(project.id, glossary.id)

    # Ranking really does come from the vector index.
    assert [result.block.id for result in semantic_results] == [agnostic.id]
    assert 0.0 < semantic_results[0].score < 1.0
    assert semantic_results[0].reason is not None
    assert semantic_results[0].reason.startswith("cosine ")

    # Scope resolution reached the graph index before ranking ran.
    assert [result.block.id for result in subtree_results] == [project.id]
    assert [result.block.id for result in linked_results] == [glossary.id]
    assert [result.block.id for result in filtered_results] == [agnostic.id]
    assert memory.list_links(project) == [reference]

    # A recursive delete prunes all three components together.
    memory.delete(project)

    assert memory.get(agnostic) is None
    assert blocks.get_block(agnostic.id) is None
    assert graph.descendants(project.id, include_self=True) == []
    assert memory.list_links(glossary, direction="in") == []
    assert memory.search("agnostic database", scope="global") == []
    assert memory.get(glossary) is not None


if __name__ == "__main__":
    main()
