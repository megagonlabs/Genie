"""Tests for the property-graph facade."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    InMemoryBackend,
    Memory,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    MemoryNotFound,
    MemoryPolicy,
)
from genie.structures import GraphAccess, LinkDirection, PropertyGraph


def test_graph_creates_lists_and_updates_typed_edges() -> None:
    """The facade stores, lists, and updates edges as ordinary memory links."""

    memory = Memory()
    alice = memory.put(type="person", value="Alice")
    company = memory.put(type="company", value="Example")
    graph = PropertyGraph(memory)

    edge = graph.add_edge(
        alice,
        company,
        "works_at",
        metadata={"valid_at": "2026-01-01"},
    )
    updated = graph.update_edge(edge, metadata={"confidence": 0.9})

    assert graph.edges(alice) == [updated]
    assert graph.edges(company, direction="in") == [updated]
    assert graph.edges(alice, relation="missing") == []
    assert updated.id == edge.id
    assert updated.source_id == alice.id
    assert updated.target_id == company.id
    assert updated.relation == "works_at"
    assert updated.metadata == {"valid_at": "2026-01-01", "confidence": 0.9}


def test_graph_neighbors_keep_direction_and_parallel_edges() -> None:
    """Each neighbor result keeps its edge and its direction relative to the node, and parallel edges stay separate."""

    memory = Memory()
    center = memory.put(type="node", value="center")
    outgoing = memory.put(type="node", value="outgoing")
    incoming = memory.put(type="node", value="incoming")
    graph = PropertyGraph(memory)
    first = graph.add_edge(center, outgoing, "related", metadata={"order": 1})
    second = graph.add_edge(incoming, center, "related")
    parallel = graph.add_edge(center, outgoing, "related", metadata={"order": 2})

    neighbors = graph.neighbors(center, direction="both")

    assert [neighbor.block.id for neighbor in neighbors] == [
        outgoing.id,
        incoming.id,
        outgoing.id,
    ]
    assert [neighbor.link.id for neighbor in neighbors] == [
        first.id,
        second.id,
        parallel.id,
    ]
    assert [neighbor.direction for neighbor in neighbors] == ["out", "in", "out"]


def test_graph_neighbors_handle_self_links_in_requested_direction() -> None:
    """A self-link is one edge, and the neighbor direction matches the requested direction."""

    memory = Memory()
    node = memory.put(type="node", value="node")
    graph = PropertyGraph(memory)
    edge = graph.add_edge(node, node, "self")

    assert graph.neighbors(node, direction="out")[0].direction == "out"
    assert graph.neighbors(node, direction="in")[0].direction == "in"
    assert graph.neighbors(node, direction="both")[0].link == edge


def test_graph_walk_returns_breadth_first_simple_paths() -> None:
    """Bounded traversal returns shallow paths first and does not repeat nodes."""

    memory = Memory()
    a = memory.put(type="node", value="A")
    b = memory.put(type="node", value="B")
    c = memory.put(type="node", value="C")
    d = memory.put(type="node", value="D")
    ignored = memory.put(type="node", value="ignored")
    graph = PropertyGraph(memory)
    graph.add_edge(a, b, "route")
    graph.add_edge(a, c, "route")
    graph.add_edge(b, d, "route")
    graph.add_edge(c, d, "route")
    graph.add_edge(d, a, "route")
    graph.add_edge(a, ignored, "other")

    paths = graph.walk(a, relation="route", max_depth=3)

    assert [path.block_ids for path in paths] == [
        (a.id, b.id),
        (a.id, c.id),
        (a.id, b.id, d.id),
        (a.id, c.id, d.id),
    ]
    assert [path.block_ids for path in graph.walk(a, max_depth=3, limit=2)] == [
        (a.id, b.id),
        (a.id, c.id),
    ]
    assert graph.walk(a, max_depth=0) == []
    assert graph.walk(a, limit=0) == []


def test_graph_walk_can_follow_incoming_edges() -> None:
    """Traversal direction applies consistently at every step."""

    memory = Memory()
    a = memory.put(type="node", value="A")
    b = memory.put(type="node", value="B")
    c = memory.put(type="node", value="C")
    graph = PropertyGraph(memory)
    graph.add_edge(a, b, "next")
    graph.add_edge(b, c, "next")

    paths = graph.walk(c, relation="next", direction="in", max_depth=2)

    assert [path.block_ids for path in paths] == [
        (c.id, b.id),
        (c.id, b.id, a.id),
    ]


def test_graph_shortest_path_is_depth_bounded() -> None:
    """Shortest-path lookup uses breadth-first order and an explicit depth bound."""

    memory = Memory()
    a = memory.put(type="node", value="A")
    b = memory.put(type="node", value="B")
    c = memory.put(type="node", value="C")
    d = memory.put(type="node", value="D")
    graph = PropertyGraph(memory)
    graph.add_edge(a, b, "route")
    graph.add_edge(a, c, "route")
    graph.add_edge(b, d, "route")
    graph.add_edge(c, d, "route")

    path = graph.shortest_path(a, d, relation="route", max_depth=2)

    assert path is not None
    assert path.block_ids == (a.id, b.id, d.id)
    assert graph.shortest_path(a, d, relation="route", max_depth=1) is None
    assert graph.shortest_path(a, d, relation="route", max_depth=0) is None
    same_node = graph.shortest_path(a, a, max_depth=0)
    assert same_node is not None
    assert same_node.block_ids == (a.id,)


def test_graph_walk_terminates_on_cycles_and_self_links() -> None:
    """A property graph allows cycles, but each path that `walk` returns is a simple path."""

    memory = Memory()
    a = memory.put(type="node", value="A")
    b = memory.put(type="node", value="B")
    graph = PropertyGraph(memory)
    graph.add_edge(a, a, "cycle")
    graph.add_edge(a, b, "cycle")
    graph.add_edge(b, a, "cycle")

    assert [path.block_ids for path in graph.walk(a, max_depth=20)] == [
        (a.id, b.id)
    ]
    unreachable = memory.put(type="node", value="unreachable")
    assert graph.shortest_path(a, unreachable, max_depth=20) is None


def test_graph_rejects_an_unrelated_edge_returned_by_an_adapter() -> None:
    """Graph traversal raises `MemoryConflict` if the backend returns an edge that does not connect to the requested node."""

    class UnrelatedEdgeBackend(InMemoryBackend):
        """Add one visible but unrelated edge to every `list_links` result."""

        unrelated: MemoryLink | None = None

        def list_links(
            self,
            block_id: str,
            *,
            relation: str | None = None,
            direction: LinkDirection = "out",
        ) -> list[MemoryLink]:
            """Return the normal links and the invalid edge that the test adds on purpose."""

            links = super().list_links(
                block_id,
                relation=relation,
                direction=direction,
            )
            if self.unrelated is not None:
                links.append(self.unrelated)
            return links

    backend = UnrelatedEdgeBackend()
    memory = Memory(backend=backend)
    requested = memory.put(type="node", value="requested")
    left = memory.put(type="node", value="left")
    right = memory.put(type="node", value="right")
    backend.unrelated = MemoryLink(left.id, right.id, "related")

    with pytest.raises(MemoryConflict, match="is not connected"):
        PropertyGraph(memory).neighbors(requested)


def test_graph_preserves_memory_handle_scope() -> None:
    """A scoped graph cannot read, traverse, or connect nodes outside its handle."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    first = memory.put(parent=root, type="node", value="first")
    second = memory.put(parent=root, type="node", value="second")
    outside = memory.put(type="node", value="outside")
    inside_edge = memory.add_link(first, second, "related")
    memory.add_link(first, outside, "related")
    full_graph = PropertyGraph(memory)
    scoped_graph = PropertyGraph(memory.get_handle(root=root))

    assert len(full_graph.edges(first)) == 2
    assert scoped_graph.edges(first) == [inside_edge]
    assert [neighbor.block.id for neighbor in scoped_graph.neighbors(first)] == [
        second.id
    ]
    assert scoped_graph.shortest_path(first, second) is not None
    with pytest.raises(AccessDenied):
        scoped_graph.add_edge(first, outside, "related")
    with pytest.raises(AccessDenied):
        scoped_graph.shortest_path(first, outside)


def test_graph_hides_edges_with_unreadable_endpoints() -> None:
    """`edges`, `neighbors`, and `walk` hide an edge when the principal cannot read its other endpoint."""

    owner = Memory(principal="owner")
    source = owner.put(type="node", value="source", policy=MemoryPolicy.public())
    hidden = owner.put(
        type="node",
        value="hidden",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    owner.add_link(source, hidden, "related")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )
    graph = PropertyGraph(reader)

    assert graph.edges(source) == []
    assert graph.neighbors(source) == []
    assert graph.walk(source, max_depth=3) == []


@pytest.mark.parametrize(
    ("method", "error"),
    [
        (lambda graph, node: graph.walk(node, max_depth=-1), ValueError),
        (lambda graph, node: graph.walk(node, limit=-1), ValueError),
        (lambda graph, node: graph.walk(node, max_depth=True), TypeError),
        (lambda graph, node: graph.walk(node, limit=1.5), TypeError),
        (
            lambda graph, node: graph.walk(
                node,
                direction=cast(LinkDirection, "sideways"),
            ),
            ValueError,
        ),
        (
            lambda graph, node: graph.shortest_path(
                node,
                node,
                direction=cast(LinkDirection, "sideways"),
            ),
            ValueError,
        ),
    ],
)
def test_graph_rejects_invalid_traversal_options(
    method: Callable[[PropertyGraph, MemoryBlock], object],
    error: type[Exception],
) -> None:
    """Invalid traversal bounds and directions raise `ValueError` or `TypeError`."""

    memory = Memory()
    node = memory.put(type="node", value="node")
    graph = PropertyGraph(memory)

    with pytest.raises(error):
        method(graph, node)


def test_graph_missing_nodes_raise_clear_errors() -> None:
    """Graph operations raise `MemoryNotFound` for a node that does not exist, but `shortest_path` returns `None` when no path exists."""

    memory = Memory()
    node = memory.put(type="node", value="node")
    graph = PropertyGraph(memory)

    with pytest.raises(MemoryNotFound, match="Graph node"):
        graph.walk("missing")
    with pytest.raises(MemoryNotFound, match="Graph node"):
        graph.shortest_path(node, "missing")
    with pytest.raises(MemoryNotFound):
        graph.neighbors("missing")


def test_memory_and_handle_implement_graph_access() -> None:
    """`Memory` and `MemoryHandle` both satisfy `GraphAccess`, so `PropertyGraph` accepts both."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)

    assert isinstance(memory, GraphAccess)
    assert isinstance(handle, GraphAccess)
