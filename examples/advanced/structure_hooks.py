"""Audit structure membership changes through structural hook events.

Structure facades report membership changes to the same registry that reports
block writes. `before_structure_change` receives the proposed `StructureChange`
and `after_structure_change` receives the committed one, so a trail records how
memory was organized as well as what it stored.

This example walks every `StructureKind` and `StructureAction`, shows that a
refused membership change reports a terminal event instead of a structural one,
and shows a composed facade reusing the events of the structures it is built
from rather than inventing its own.

Run from the repository root:

    python examples/advanced/structure_hooks.py
"""

from __future__ import annotations

from datetime import UTC, datetime

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
    HookOperation,
    HookOutcome,
    HookRegistry,
    InMemoryBackend,
    Memory,
    MemoryConflict,
    MemoryHook,
    StructureAction,
    StructureChange,
    StructureKind,
)
from genie.structures import (
    EventLog,
    KeyedCollection,
    MemoryAddress,
    TemporalCollection,
    ValidityInterval,
    VersionedRecords,
)

# The operations that change structure membership. Every other operation on a
# facade is a read and reports no structural event.
STRUCTURAL_OPERATIONS = frozenset(
    {
        HookOperation.KEY_BIND,
        HookOperation.KEY_REBIND,
        HookOperation.KEY_UNBIND,
        HookOperation.LOG_APPEND,
        HookOperation.TEMPORAL_ADD,
        HookOperation.TEMPORAL_REPLACE,
        HookOperation.TEMPORAL_REMOVE,
    }
)


class StructureAudit(MemoryHook):
    """Record how the structures around a memory system changed.

    Attributes:
        proposed: Kind, action, and whether the structural record already
            existed when the change was proposed. A backend that allocates the
            record itself, such as a log sequence, leaves it unset.
        committed: Kind, action, and container of each committed change.
        outcomes: Outcome of every structural operation that started, whether
            it committed or was refused.
        uncorrelated: Committed changes whose operation id was never proposed.
    """

    def __init__(self) -> None:
        """Create an empty structure audit."""

        self.proposed: list[tuple[StructureKind, StructureAction, bool]] = []
        self.committed: list[tuple[StructureKind, StructureAction, str]] = []
        self.outcomes: list[tuple[HookOperation, HookOutcome]] = []
        self.uncorrelated: list[str] = []
        self._open: set[str] = set()

    def before_structure_change(
        self,
        context: HookContext,
        change: StructureChange[object],
    ) -> None:
        """Record a membership change before the backend applies it."""

        self.proposed.append((change.kind, change.action, change.after is not None))
        self._open.add(context.operation_id)

    def after_structure_change(
        self,
        context: HookContext,
        change: StructureChange[object],
    ) -> None:
        """Record a membership change the backend committed."""

        if context.operation_id not in self._open:
            self.uncorrelated.append(context.operation_id)
        self.committed.append((change.kind, change.action, change.container_id))

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Record the outcome of every structural operation that started."""

        if context.operation in STRUCTURAL_OPERATIONS:
            self.outcomes.append((context.operation, outcome))


def at(day: int) -> datetime:
    """Return a fixed UTC time in January 2026.

    Args:
        day: Day of the month.

    Returns:
        Timezone-aware datetime for that day.
    """

    return datetime(2026, 1, day, tzinfo=UTC)


def main() -> None:
    """Audit every structural action a facade can report."""

    print("== Structural hook events ==")
    audit = StructureAudit()
    backend = InMemoryBackend()
    # Facades reuse the access object's registry, so a structure built on this
    # memory reports to the audit hook without being told about it.
    memory = Memory(backend=backend, hooks=HookRegistry([audit]))

    catalog_root = memory.put(type="collection", value="model catalog")
    events_root = memory.put(type="log", value="training events")
    timeline_root = memory.put(type="collection", value="deployment timeline")
    versions_root = memory.put(type="collection", value="model versions")
    baseline = memory.put(type="model", value="baseline ranker")
    tuned = memory.put(type="model", value="tuned ranker")

    catalog = KeyedCollection(memory, backend, collection=catalog_root)
    events = EventLog(memory, backend, log=events_root)
    timeline = TemporalCollection(memory, backend, collection=timeline_root)
    active = MemoryAddress(("serving",), "active_model")

    # Keyed membership: bind an address, move it, then release it.
    catalog.bind(active, baseline)
    catalog.rebind(active, tuned, expected=baseline)
    catalog.unbind(active, expected=tuned)

    # Log membership: the backend allocates the sequence, so the proposed
    # change carries no record yet.
    events.append(baseline)

    # Temporal membership: add a validity period, correct it, then drop it.
    timeline.add(baseline, occurred_at=at(10), validity=ValidityInterval(at(10), at(12)))
    timeline.replace(baseline, occurred_at=at(10), validity=ValidityInterval(at(10), at(20)))
    timeline.remove(baseline)

    print("proposed changes:")
    for kind, action, allocated in audit.proposed:
        print(f"- {kind} {action} (record allocated when proposed: {allocated})")
    print(f"committed changes: {len(audit.committed)}")

    assert [action for _, action, _ in audit.proposed] == [
        StructureAction.BIND,
        StructureAction.REBIND,
        StructureAction.UNBIND,
        StructureAction.APPEND,
        StructureAction.ADD,
        StructureAction.REPLACE,
        StructureAction.REMOVE,
    ]
    assert [kind for kind, _, _ in audit.proposed] == [
        StructureKind.KEYED_COLLECTION,
        StructureKind.KEYED_COLLECTION,
        StructureKind.KEYED_COLLECTION,
        StructureKind.EVENT_LOG,
        StructureKind.TEMPORAL_COLLECTION,
        StructureKind.TEMPORAL_COLLECTION,
        StructureKind.TEMPORAL_COLLECTION,
    ]
    # Every committed change was proposed first, in the same operation.
    assert audit.uncorrelated == []
    assert [action for _, action, _ in audit.committed] == [
        action for _, action, _ in audit.proposed
    ]
    assert {outcome for _, outcome in audit.outcomes} == {HookOutcome.SUCCESS}

    # Reads report traversal and block events, never structural ones.
    committed_before_reads = len(audit.committed)
    catalog.items(namespace=("serving",))
    events.read(limit=10)
    timeline.valid_at(at(11))

    assert len(audit.committed) == committed_before_reads
    print("structural changes reported by three reads: none")

    # A refused membership change never reaches the backend, so it reports no
    # structural event. Its terminal event carries the failure instead.
    structural_operations_before = len(audit.outcomes)
    try:
        catalog.rebind(active, baseline, expected=tuned)
    except MemoryConflict:
        print("rebinding a released address: refused")
    else:
        raise AssertionError("A released address must not be rebindable.")

    assert len(audit.committed) == committed_before_reads
    assert audit.outcomes[structural_operations_before:] == [
        (HookOperation.KEY_REBIND, HookOutcome.ERROR)
    ]

    # A composed facade reuses the structures it is built from. Registering a
    # record is one key binding and each revision is one log append, anchored
    # on the record's own identity block rather than on the collection.
    versions = VersionedRecords(
        memory,
        backend,
        collection=versions_root,
        hooks=memory.hooks,
    )
    record = MemoryAddress(("rankers",), "production")
    composed_before = len(audit.committed)

    versions.register(record, baseline)
    versions.append(record, tuned)

    composed = audit.committed[composed_before:]
    print(f"versioned record reported: {[str(action) for _, action, _ in composed]}")

    assert [(kind, action) for kind, action, _ in composed] == [
        (StructureKind.KEYED_COLLECTION, StructureAction.BIND),
        (StructureKind.EVENT_LOG, StructureAction.APPEND),
    ]
    assert [container for _, _, container in composed] == [versions_root.id, baseline.id]
    assert versions.identity(record) == baseline

    current = versions.current(record)
    assert current is not None
    print(f"revision {current.entry.revision} of the production ranker: {current.block.value}")


if __name__ == "__main__":
    main()
