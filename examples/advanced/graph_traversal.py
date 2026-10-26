"""Walk a derivation graph several hops and annotate the edges you followed.

- **One-hop reads:** they answer "what is next to this block".
- **Walks:** they answer "how did this block come to exist". A walk returns
  bounded simple paths (no block occurs twice in one path) in breadth-first
  order, so shorter explanations come first.
- **Edge metadata:** an update can change it, so a link can record the job or
  the confidence that produced the relationship.
- **`LineageDAG`:** a view of one relation. It refuses an edge that makes a
  cycle.
- **`PropertyGraph`:** it reads the same links. It has no fixed relation, so
  this example gives `relation` and `max_depth` in each call.

Run from the repository root:

    python examples/advanced/graph_traversal.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import InMemoryBackend, Memory, MemoryBlock
from genie.structures import LineageDAG, PropertyGraph

DERIVES = "derives"


def main() -> None:
    """Traverse a derivation chain of 4 stages in both directions."""

    print("== Graph traversal ==")
    memory = Memory(backend=InMemoryBackend(), principal="app:pipeline")
    raw = memory.put(type="dataset", value="raw crawl")
    cleaned = memory.put(type="dataset", value="deduplicated crawl")
    features = memory.put(type="dataset", value="encoded features")
    ranker = memory.put(type="model", value="candidate ranker")

    # Trusted code protects the relation once, before the first lineage edge.
    memory.protect_relation(DERIVES)
    lineage = LineageDAG(memory, relation=DERIVES)
    first = lineage.add(raw, cleaned)
    lineage.add(cleaned, features)
    lineage.add(features, ranker)

    # Read one hop in each direction. A neighbor holds the block, the link to
    # the block, and the direction that the read followed.
    upstream = lineage.predecessors(features)
    downstream = lineage.successors(features)

    print(f"features came from: {[neighbor.block.value for neighbor in upstream]}")
    print(f"features fed into: {[neighbor.block.value for neighbor in downstream]}")

    assert [neighbor.block.id for neighbor in upstream] == [cleaned.id]
    assert [neighbor.block.id for neighbor in downstream] == [ranker.id]
    assert upstream[0].direction == "in"
    assert downstream[0].direction == "out"

    # A multi-hop walk returns each simple path from the start block, shortest
    # first. This chain has 1 path for each length.
    provenance = lineage.walk_predecessors(ranker, max_depth=3)
    products = lineage.walk_successors(raw, max_depth=3)

    print("how the ranker came to exist:")
    for path in provenance:
        names = [require(memory, block_id).value for block_id in path.block_ids]
        print(f"- {len(path)} hop(s): {' <- '.join(names)}")

    assert [len(path) for path in provenance] == [1, 2, 3]
    assert provenance[-1].block_ids == (ranker.id, features.id, cleaned.id, raw.id)
    assert [len(path) for path in products] == [1, 2, 3]
    assert products[-1].block_ids == (raw.id, cleaned.id, features.id, ranker.id)

    # A smaller `max_depth` stops the walk earlier. It does not cause an error.
    assert [len(path) for path in lineage.walk_predecessors(ranker, max_depth=2)] == [1, 2]

    # Edges hold application metadata. An update merges the given fields into
    # the edge metadata and returns a new link object. The old object keeps its
    # old metadata. The update does not change either endpoint block.
    annotated = lineage.update_edge(first, metadata={"job": "clean-2026-01-10"})

    print(f"annotated derivation: {annotated.metadata}")

    assert annotated.id == first.id
    assert annotated.metadata == {"job": "clean-2026-01-10"}
    assert first.metadata == {}
    assert memory.get(raw) == raw

    # A property graph reads the same links. It has no fixed relation, so the
    # script gives `relation` and `max_depth` in each call. Without `relation`,
    # a walk follows all relations. The default `max_depth` is 1.
    graph = PropertyGraph(memory)
    two_hops = graph.walk(raw, relation=DERIVES, max_depth=2)
    inbound = graph.walk(ranker, relation=DERIVES, direction="in", max_depth=1)

    print(f"two-hop paths from the raw crawl: {len(two_hops)}")
    print(f"one-hop inbound path to the ranker: {len(inbound)}")

    assert [len(path) for path in two_hops] == [1, 2]
    assert [path.block_ids for path in inbound] == [(ranker.id, features.id)]

    # An edge update through either facade changes the same stored link.
    documented = graph.update_edge(annotated, metadata={"job": "clean-2026-01-11"})

    assert documented.id == first.id
    assert documented.metadata == {"job": "clean-2026-01-11"}
    assert lineage.edges(raw)[0].metadata == {"job": "clean-2026-01-11"}
    print(f"edge metadata after a second annotation: {documented.metadata}")


def require(memory: Memory, block_id: str) -> MemoryBlock:
    """Return a stored block, and fail with `AssertionError` if it is missing.

    Args:
        memory: The memory to read.
        block_id: The id of the block to read.

    Returns:
        The stored memory block.
    """

    block = memory.get(block_id)
    assert block is not None
    return block


if __name__ == "__main__":
    main()
