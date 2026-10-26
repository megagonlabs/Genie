"""Concurrency tests that use more than one optional structure."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

import pytest

from genie import InMemoryBackend, Memory, MemoryConflict
from genie.structures import LineageDAG, PropertyGraph, TemporalCollection


def test_graph_write_and_protection_cannot_race_into_a_cycle() -> None:
    """A graph write that closes a cycle and the protection of its relation cannot both commit, so a protected relation never holds a cycle."""

    memory = Memory()
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")
    graph = PropertyGraph(memory)
    graph.add_edge(second, first, "derived_into")
    lineage = LineageDAG(memory, relation="derived_into")
    barrier = Barrier(2)

    def add_graph_edge() -> bool:
        barrier.wait()
        try:
            graph.add_edge(first, second, "derived_into")
        except MemoryConflict:
            return False
        return True

    def protect() -> bool:
        barrier.wait()
        try:
            memory.protect_relation("derived_into")
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(add_graph_edge),
            executor.submit(protect),
        ]
        graph_committed, protected = [future.result() for future in outcomes]

    assert [graph_committed, protected].count(True) == 1
    if protected:
        assert len(memory.list_links(first, relation="derived_into", direction="both")) == 1
        with pytest.raises(MemoryConflict, match="would create a cycle"):
            lineage.add(first, second)
    else:
        assert len(memory.list_links(first, relation="derived_into", direction="both")) == 2
        with pytest.raises(MemoryConflict, match="not protected"):
            lineage.add(first, second)


def test_concurrent_temporal_membership_keeps_one_entry_per_block() -> None:
    """Concurrent adds cannot duplicate one collection and block pair."""

    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    root = memory.put(type="timeline", value="events")
    event = memory.put(type="event", value="shared")
    timeline = TemporalCollection(memory, backend, collection=root)
    barrier = Barrier(12)

    def add(_: int) -> bool:
        barrier.wait()
        try:
            timeline.add(event, occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(add, range(12)))

    assert outcomes.count(True) == 1
    assert [item.block for item in timeline.events()] == [event]
