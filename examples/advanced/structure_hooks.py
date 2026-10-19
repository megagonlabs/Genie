"""Audit structure membership changes through structural hook events.

- **One registry:** structure facades report membership changes to the same
  registry that reports block writes.
- **Paired callbacks:** `before_structure_change` receives the proposed
  `StructureChange`, and `after_structure_change` receives the committed one.
  So a trail records how the application organized memory, and also what
  memory stored.
- **Each kind and action:** the example uses each `StructureKind` and each
  `StructureAction`.
- **Refused change:** the facade refuses a rebind of an unbound address. That
  rebind reports a terminal event, not a structural event.
- **Composed facade:** a composed facade reuses the events of the structures
  that it is built from. It does not define its own events.

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

# The operations that change structure membership. All other operations, for
# example reads and block or link writes, report no structural event.
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
        proposed: Kind, action, and a flag for each proposed change. The flag
            is `True` if the proposed change already has its new structural
            record (`after`). It is `False` if the backend allocates the
            record, such as a log sequence, or if the change removes a record
            (unbind or remove).
        committed: Kind, action, and container id of each committed change.
        outcomes: Operation and outcome of each structural operation that
            started. This includes committed and refused operations.
        uncorrelated: Operation ids of committed changes that had no proposed
            change in the same operation.
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
        """Record a membership change that the backend committed."""

        if context.operation_id not in self._open:
            self.uncorrelated.append(context.operation_id)
        self.committed.append((change.kind, change.action, change.container_id))

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Record the outcome of each structural operation that started."""

        if context.operation in STRUCTURAL_OPERATIONS:
            self.outcomes.append((context.operation, outcome))


def at(day: int) -> datetime:
    """Return a fixed UTC time in January 2026.

    Args:
        day: Day of the month.

    Returns:
        A timezone-aware datetime at midnight UTC on that day.
    """

    return datetime(2026, 1, day, tzinfo=UTC)


def main() -> None:
    """Audit each structural action that a facade can report."""

    print("== Structural hook events ==")
    audit = StructureAudit()
    backend = InMemoryBackend()
    # Facades reuse the registry of the access object. So each structure on
    # this memory reports to the audit hook without a `hooks` argument.
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

    # Keyed membership: bind an address, rebind it to another block, then
    # unbind it.
    catalog.bind(active, baseline)
    catalog.rebind(active, tuned, expected=baseline)
    catalog.unbind(active, expected=tuned)

    # Log membership: the backend allocates the sequence, so the proposed
    # change has no record yet.
    events.append(baseline)

    # Temporal membership: add a validity period, correct it, then remove it.
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
    # Each committed change had a proposed change first, in the same operation.
    assert audit.uncorrelated == []
    assert [action for _, action, _ in audit.committed] == [
        action for _, action, _ in audit.proposed
    ]
    assert {outcome for _, outcome in audit.outcomes} == {HookOutcome.SUCCESS}

    # These reads report only block read (`get`) events. They report no
    # structural event.
    committed_before_reads = len(audit.committed)
    catalog.items(namespace=("serving",))
    events.read(limit=10)
    timeline.valid_at(at(11))

    assert len(audit.committed) == committed_before_reads
    print("structural changes reported by three reads: none")

    # The address is unbound. The facade refuses this rebind before it proposes
    # the change, and the backend write does not occur. Thus the rebind reports
    # no structural event. Its terminal event records the failure.
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

    # A composed facade reuses the structures that it is built from. The
    # registration of a record is one key bind. Each revision is one log append
    # on the identity block of the record, not on the collection.
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
