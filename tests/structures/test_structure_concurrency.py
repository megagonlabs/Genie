"""Concurrency tests that cross optional structure boundaries."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Barrier

from genie import InMemoryBackend, Memory, MemoryConflict
from genie.structures import LineageDAG, PropertyGraph, TemporalCollection


def test_graph_write_and_first_lineage_write_cannot_race_into_a_cycle() -> None:
    """Ordinary and protected link writes coordinate on one relation."""

    memory = Memory()
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")
    graph = PropertyGraph(memory)
    lineage = LineageDAG(memory, relation="derived_into")
    barrier = Barrier(2)

    def add_graph_edge() -> bool:
        barrier.wait()
        try:
            graph.add_edge(first, second, "derived_into")
        except MemoryConflict:
            return False
        return True

    def add_lineage_edge() -> bool:
        barrier.wait()
        try:
            lineage.add(second, first)
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = [
            executor.submit(add_graph_edge),
            executor.submit(add_lineage_edge),
        ]
        committed = [future.result() for future in outcomes]

    assert committed.count(True) == 1
    assert len(memory.list_links(first, relation="derived_into", direction="both")) == 1


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
