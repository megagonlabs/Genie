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
    LinkRelations,
    Memory,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    MemoryNotFound,
    MemoryPolicy,
    UnsupportedOperation,
)
from genie.structures import LineageAccess, LineageDAG, PropertyGraph
from tests.backends.test_backend_contracts import CoreOnlyBackend


def test_lineage_dag_uses_graph_edges_and_forward_lineage_traversal() -> None:
    """Lineage edges are ordinary links that point from a predecessor to its successor."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    d = memory.put(type="artifact", value="D")
    memory.protect_relation("derived_into")
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
    """`add` raises `MemoryConflict` for a self-link and for an edge whose target already reaches its source."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    memory.protect_relation("derived_into")
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
    memory.protect_relation("derived_into")
    lineage = LineageDAG(memory, relation="derived_into")
    lineage.add(a, b)

    unrelated = PropertyGraph(memory).add_edge(b, a, "references")

    assert lineage.successors(b) == []
    assert PropertyGraph(memory).edges(b, relation="references") == [unrelated]


def test_protection_adopts_existing_acyclic_relation_edges() -> None:
    """If the links that already use the relation have no cycle, `protect_relation` adds them to the protected relation."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    existing = memory.add_link(a, b, "derived_into")
    memory.protect_relation("derived_into")
    lineage = LineageDAG(memory, relation="derived_into")

    created = lineage.add(b, c)

    with pytest.raises(MemoryConflict, match="would create a cycle"):
        lineage.add(c, a)

    assert lineage.edges(a) == [existing]
    assert lineage.edges(b) == [created]
    with pytest.raises(MemoryConflict, match="protected as an acyclic graph"):
        memory.add_link(a, c, "derived_into")


def test_protection_refuses_to_adopt_an_existing_cyclic_relation() -> None:
    """A relation that already contains a cycle cannot become a lineage DAG, so its lineage writes stay refused."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    c = memory.put(type="artifact", value="C")
    memory.add_link(a, b, "derived_into")
    memory.add_link(b, a, "derived_into")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(MemoryConflict, match="already contains a cycle"):
        memory.protect_relation("derived_into")
    with pytest.raises(MemoryConflict, match="not protected"):
        lineage.add(b, c)


def test_lineage_add_needs_a_protected_relation() -> None:
    """`add` never protects a relation: it refuses an unprotected relation, stores no edge, and plain links keep working."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(MemoryConflict, match="not protected"):
        lineage.add(a, b)

    assert lineage.edges(a) == []
    assert memory.add_link(a, b, "derived_into").relation == "derived_into"


def test_a_handle_cannot_lock_a_relation_for_other_callers() -> None:
    """A rooted handle cannot protect a relation, so plain links of the same relation stay open to the other callers of the backend."""

    memory = Memory(principal="svc")
    sandbox = memory.put(type="sandbox", value=1, policy=MemoryPolicy.public())
    agent = memory.get_handle(root=sandbox, principal="agent")
    first = agent.put(type="node", value=1)
    second = agent.put(type="node", value=2)

    with pytest.raises(MemoryConflict, match="not protected"):
        agent.add_acyclic_link(first, second, "supports")
    with pytest.raises(MemoryConflict, match="not protected"):
        LineageDAG(agent, relation="supports").add(first, second)

    requirement = memory.put(type="requirement", value="r")
    proposal = memory.put(type="proposal", value="p")
    assert PropertyGraph(memory).add_edge(requirement, proposal, "supports").relation == "supports"
    assert PropertyGraph(memory).add_edge(proposal, requirement, "supports").relation == "supports"


def test_protection_before_handles_stops_cycles_from_plain_links() -> None:
    """After trusted code protects the relation, a handle cannot close a cycle with plain links, so another tenant can still add lineage edges."""

    memory = Memory(principal="svc")
    memory.protect_relation("derived_from")
    sandbox = memory.put(type="sandbox", value=1, policy=MemoryPolicy.public())
    agent = memory.get_handle(root=sandbox, principal="agent")
    first = agent.put(type="node", value=1)
    second = agent.put(type="node", value=2)

    with pytest.raises(MemoryConflict, match="protected as an acyclic graph"):
        agent.add_link(first, second, "derived_from")
    agent_edge = LineageDAG(agent, relation="derived_from").add(first, second)
    with pytest.raises(MemoryConflict, match="would create a cycle"):
        LineageDAG(agent, relation="derived_from").add(second, first)

    tenant = memory.put(type="tenant", value=1)
    draft = memory.put(parent=tenant, type="doc", value="a")
    final = memory.put(parent=tenant, type="doc", value="b")
    tenant_edge = LineageDAG(memory, relation="derived_from").add(draft, final)

    assert agent.list_links(first, relation="derived_from") == [agent_edge]
    assert memory.list_links(draft, relation="derived_from") == [tenant_edge]


def test_cycle_check_includes_links_that_the_caller_cannot_read() -> None:
    """The cycle check covers the whole relation, so a handle gets a refusal for a path through a block that it cannot read."""

    memory = Memory(principal="svc")
    memory.protect_relation("derived_from")
    sandbox = memory.put(type="sandbox", value=1, policy=MemoryPolicy.public())
    first = memory.put(parent=sandbox, type="node", value="first", policy=MemoryPolicy.public())
    second = memory.put(parent=sandbox, type="node", value="second", policy=MemoryPolicy.public())
    hidden = memory.put(type="secret", value="hidden")
    memory.add_acyclic_link(second, hidden, "derived_from")
    memory.add_acyclic_link(hidden, first, "derived_from")
    agent = memory.get_handle(root=sandbox, principal="agent")

    assert agent.list_links(second, relation="derived_from") == []
    with pytest.raises(MemoryConflict, match="would create a cycle"):
        agent.add_acyclic_link(first, second, "derived_from")


def test_opposite_concurrent_edges_cannot_both_commit() -> None:
    """Of 2 concurrent writers that add opposite edges, only 1 succeeds because the check and the insert are atomic."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    memory.protect_relation("derived_into")
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
    """`add` accepts at most 2 of 3 concurrent edges that together make a cycle of 3 nodes."""

    memory = Memory()
    nodes = [memory.put(type="artifact", value=value) for value in "ABC"]
    memory.protect_relation("derived_into")
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
    """The atomic insert still applies the normal handle scope and endpoint policy checks."""

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
    memory.protect_relation("derived_into")
    scoped = LineageDAG(memory.get_handle(root=root), relation="derived_into")

    scoped.add(first, second)

    with pytest.raises(AccessDenied):
        scoped.add(second, outside)
    with pytest.raises(AccessDenied):
        scoped.add(locked, second)
    assert [neighbor.block for neighbor in scoped.successors(first)] == [second]


def test_lineage_dag_reports_missing_atomic_backend_capability() -> None:
    """`add` raises `UnsupportedOperation` before it writes an edge if the backend has no atomic cycle protection."""

    memory = Memory(backend=CoreOnlyBackend())
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(UnsupportedOperation, match="atomic acyclic"):
        memory.protect_relation("derived_into")
    with pytest.raises(UnsupportedOperation, match="atomic acyclic"):
        lineage.add(first, second)
    assert memory.list_links(first) == []


@pytest.mark.parametrize(
    "relation",
    ["", "   ", LinkRelations.MOUNT, cast(str, 1)],
)
def test_lineage_dag_rejects_invalid_or_mount_relations(relation: str) -> None:
    """`LineageDAG` rejects an empty, blank, mount, or non-string relation name."""

    with pytest.raises((TypeError, ValueError)):
        LineageDAG(Memory(), relation=relation)


def test_lineage_update_rejects_an_edge_from_another_relation() -> None:
    """`update_edge` raises `ValueError` for an edge from another relation."""

    memory = Memory()
    a = memory.put(type="artifact", value="A")
    b = memory.put(type="artifact", value="B")
    other = memory.add_link(a, b, "references")
    lineage = LineageDAG(memory, relation="derived_into")

    with pytest.raises(ValueError, match="Expected lineage relation"):
        lineage.update_edge(other, metadata={"confidence": 1.0})


def test_lineage_update_rejects_a_missing_canonical_edge() -> None:
    """`update_edge` does not trust an edge object from the caller and raises `MemoryNotFound` if storage does not contain its id."""

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
    """`update_edge` checks the stored relation, so a copy with a changed relation cannot move into the DAG."""

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
    """`Memory` and `MemoryHandle` satisfy `LineageAccess`, and the reference backend satisfies `AcyclicLinkStore`."""

    memory = Memory()
    root = memory.put(type="root", value="root")

    assert isinstance(memory, LineageAccess)
    assert isinstance(memory.get_handle(root=root), LineageAccess)
    assert isinstance(memory.backend, AcyclicLinkStore)
