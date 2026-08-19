"""Tests for relation-scoped lineage DAGs and atomic cycle protection."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier
from typing import cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    AcyclicLinkStore,
    InMemoryBackend,
    LinkRelations,
    Memory,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    MemoryNotFound,
    MemoryPolicy,
    RoutedBackend,
    UnsupportedOperation,
)
from genie.structures import LineageAccess, LineageDAG, PropertyGraph


def test_lineage_dag_uses_graph_edges_and_forward_lineage_traversal() -> None:
    """Lineage edges remain ordinary links with predecessor-first direction."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    d = memory.put(type="artifact", value="D")
    lineage = LineageDAG(memory, relation="derived_into")
    first = lineage.add(a, b, metadata={"method": "extract"})
    branch = lineage.add(a, c)
    lineage.add(b, d)
    lineage.add(c, d)

    updated = lineage.update_edge(first, metadata={"confidence": 0.9})

    assert updated.metadata == {"method": "extract", "confidence": 0.9}
    assert [neighbor.block for neighbor in lineage.successors(a)] == [b, c]
    assert [neighbor.block for neighbor in lineage.predecessors(d)] == [b, c]
    assert [path.block_ids for path in lineage.walk_successors(a)] == [
        (a.id, b.id),
        (a.id, c.id),
        (a.id, b.id, d.id),
        (a.id, c.id, d.id),
    ]
    assert [path.block_ids for path in lineage.walk_predecessors(d)] == [
        (d.id, b.id),
        (d.id, c.id),
        (d.id, b.id, a.id),
        (d.id, c.id, a.id),
    ]
    shortest = lineage.shortest_path(a, d)
    assert shortest is not None
    assert shortest.block_ids == (a.id, b.id, d.id)
    assert PropertyGraph(memory).edges(a, relation="derived_into") == [updated, branch]


def test_lineage_dag_rejects_self_links_and_closing_edges() -> None:
    """A proposed edge is rejected when its target already reaches its source."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    lineage = LineageDAG(memory, relation="derived_into")
    lineage.add(a, b)
    lineage.add(b, c)

    with pytest.raises(MemoryConflict, match="would create a cycle"):
        lineage.add(c, a)
    with pytest.raises(MemoryConflict, match="would create a cycle"):
        lineage.add(a, a)

    assert len(lineage.edges(a)) == 1
    assert len(lineage.edges(b)) == 1
    assert lineage.edges(c) == []


def test_lineage_relation_is_independent_from_other_graph_relations() -> None:
    """Cycle checks use only links with the lineage DAG's exact relation."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    lineage = LineageDAG(memory, relation="derived_into")
    lineage.add(a, b)

    unrelated = PropertyGraph(memory).add_edge(b, a, "references")

    assert lineage.successors(b) == []
    assert PropertyGraph(memory).edges(b, relation="references") == [unrelated]


def test_first_lineage_write_adopts_existing_acyclic_relation_edges() -> None:
    """Existing graph links join the protected relation when already acyclic."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    existing = memory.add_link(a, b, "derived_into")
    lineage = LineageDAG(memory, relation="derived_into")

    created = lineage.add(b, c)

    assert lineage.edges(a) == [existing]
    assert lineage.edges(b) == [created]
    with pytest.raises(MemoryConflict, match="protected as an acyclic graph"):
        memory.add_link(a, c, "derived_into")


def test_lineage_dag_refuses_to_adopt_an_existing_cyclic_relation() -> None:
    """A relation containing a prior cycle cannot become a lineage DAG."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    memory.add_link(a, b, "derived_into")
    memory.add_link(b, a, "derived_into")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(MemoryConflict, match="already contains a cycle"):
        lineage.add(b, c)


def test_opposite_concurrent_edges_cannot_both_commit() -> None:
    """Atomic check-and-insert prevents a two-writer cycle race."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    lineage = LineageDAG(memory, relation="derived_into")
    barrier = Barrier(2)

    def try_add(source: MemoryBlock, target: MemoryBlock) -> bool:
        barrier.wait()
        try:
            lineage.add(source, target)
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(try_add, a, b),
            executor.submit(try_add, b, a),
        ]
        outcomes = [future.result() for future in futures]

    assert outcomes.count(True) == 1
    assert len(memory.backend.list_links(a.id, relation="derived_into", direction="both")) == 1


def test_three_concurrent_edges_cannot_commit_a_cycle() -> None:
    """At most two sides of a concurrent three-node cycle are accepted."""

    memory = Memory()
    nodes = [memory.put(type="artifact", value=value) for value in "ABC"]
    lineage = LineageDAG(memory, relation="derived_into")
    barrier = Barrier(3)

    def try_add(index: int) -> bool:
        barrier.wait()
        try:
            lineage.add(nodes[index], nodes[(index + 1) % 3])
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=3) as executor:
        outcomes = list(executor.map(try_add, range(3)))

    assert outcomes.count(True) == 2


def test_lineage_dag_preserves_handle_scope_and_endpoint_policy() -> None:
    """Atomic insertion keeps normal handle and endpoint authorization checks."""

    memory = Memory()
    root = memory.put(type="root", value="root")
    first = memory.put(parent=root, type="artifact", value="first")
    second = memory.put(parent=root, type="artifact", value="second")
    locked = memory.put(
        parent=root,
        type="artifact",
        value="locked",
        policy=MemoryPolicy(write=AccessRule.deny()),
    )
    outside = memory.put(type="artifact", value="outside")
    scoped = LineageDAG(memory.get_handle(root=root), relation="derived_into")

    scoped.add(first, second)

    with pytest.raises(AccessDenied):
        scoped.add(second, outside)
    with pytest.raises(AccessDenied):
        scoped.add(locked, second)
    assert [neighbor.block for neighbor in scoped.successors(first)] == [second]


def test_lineage_dag_reports_missing_atomic_backend_capability() -> None:
    """Backends without atomic cycle protection fail before writing an edge."""

    backend = RoutedBackend({"main": InMemoryBackend()})
    memory = Memory(backend=backend)
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(UnsupportedOperation, match="atomic acyclic"):
        lineage.add(first, second)
    assert memory.list_links(first) == []


@pytest.mark.parametrize(
    "relation",
    ["", "   ", LinkRelations.MOUNT, cast(str, 1)],
)
def test_lineage_dag_rejects_invalid_or_mount_relations(relation: str) -> None:
    """Each lineage graph needs a non-empty, non-mount relation name."""

    with pytest.raises((TypeError, ValueError)):
        LineageDAG(Memory(), relation=relation)


def test_lineage_update_rejects_an_edge_from_another_relation() -> None:
    """Metadata updates cannot accidentally cross relation-scoped DAGs."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    other = memory.add_link(a, b, "references")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(ValueError, match="Expected lineage relation"):
        lineage.update_edge(other, metadata={"confidence": 1.0})


def test_lineage_update_rejects_a_missing_canonical_edge() -> None:
    """A caller-provided edge object is not trusted unless storage contains its id."""

    memory = Memory()
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")
    forged = MemoryLink(
        first.id,
        second.id,
        "derived_into",
        id="missing-link",
    )

    with pytest.raises(MemoryNotFound, match="missing-link"):
        LineageDAG(memory, relation="derived_into").update_edge(
            forged,
            metadata={"confidence": 1.0},
        )


def test_lineage_update_validates_the_canonical_stored_relation() -> None:
    """Reconstructing a link cannot move its metadata update into a DAG."""

    memory = Memory()
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")
    other = memory.add_link(
        first,
        second,
        "references",
        metadata={"source": "original"},
    )
    forged = replace(other, relation="derived_into")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(ValueError, match="Expected lineage relation"):
        lineage.update_edge(forged, metadata={"confidence": 1.0})

    assert memory.list_links(first, relation="references") == [other]


def test_memory_handle_and_backend_implement_lineage_protocols() -> None:
    """Public access objects and the reference backend expose DAG capabilities."""

    memory = Memory()
    root = memory.put(type="root", value="root")

    assert isinstance(memory, LineageAccess)
    assert isinstance(memory.get_handle(root=root), LineageAccess)
    assert isinstance(memory.backend, AcyclicLinkStore)
