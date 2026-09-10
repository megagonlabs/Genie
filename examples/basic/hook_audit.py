"""Build an audit trail from the events a memory system reports.

`genie.hooks` is the observability surface. This example walks it end to end:

- scopes that name work Genie does not model;
- traversal events that report what one read disclosed;
- refusals recorded as observations;
- terminal events that close every operation;
- a registry whose mandatory hook can be neither detached nor silenced;
- suppression windows announced before dispatch stops; and
- `fail_open` for a sink whose own outages must not reach callers.

Covered elsewhere: transforming hooks that rewrite what is stored or returned
live in `write_hooks.py` and `search_hooks.py`. Structural membership events are
reported only by `genie.structures` facades, so they are in
`../advanced/structure_hooks.py`.

Run from the repository root:

    python examples/basic/hook_audit.py
"""

from __future__ import annotations

import datetime as dt
import logging

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    AccessDenied,
    HandleCapability,
    HookContext,
    HookOperation,
    HookOutcome,
    HookRegistry,
    InMemoryBackend,
    Memory,
    MemoryBlock,
    MemoryHook,
    Operation,
    Suppression,
    SuppressionScope,
    Traversal,
    TraversalKind,
)


class AuditHook(MemoryHook):
    """Record the audit surface of one memory system.

    Attributes:
        created: Ids of the blocks this hook observed being stored.
        disclosed: Kind, starting block, and record count of each structural
            read, so one call that returns many blocks stays one event.
        refusals: Refused operation, the check that refused it, the block it
            applies to, and the handle that attempted it.
        windows: Reach, silenced hook names, and witnessing hook names of each
            suppression window.
        outcomes: Operation, outcome, and turn label of every operation that
            started, whether it committed, was refused, or failed.
        pending: Start times stashed when an operation begins, released by the
            terminal event of the same operation.
    """

    def __init__(self) -> None:
        """Create an empty audit hook."""

        self.created: list[str] = []
        self.disclosed: list[tuple[TraversalKind, str, int]] = []
        self.refusals: list[tuple[HookOperation, str, str | None, str | None]] = []
        self.windows: list[tuple[SuppressionScope, tuple[str, ...], tuple[str, ...]]] = []
        self.outcomes: list[tuple[HookOperation, HookOutcome, str | None]] = []
        self.pending: dict[str, dt.datetime] = {}

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Stash when this write started and store the block unchanged."""

        self.pending[context.operation_id] = context.started_at
        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record stored block ids."""

        self.created.append(block.id)

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record how much memory one structural read disclosed."""

        self.disclosed.append(
            (
                traversal.kind,
                traversal.origin_id,
                len(traversal.blocks) + len(traversal.links),
            )
        )

    def on_denied(
        self,
        context: HookContext,
        operation: Operation | HandleCapability | str,
        subject_id: str | None,
        reason: str,
    ) -> None:
        """Record a refusal together with the sandbox that attempted it."""

        self.refusals.append(
            (
                context.operation,
                operation,
                subject_id,
                context.attributes.get("handle.id"),
            )
        )

    def on_suppressed(self, context: HookContext, suppression: Suppression) -> None:
        """Record a window in which other hooks stop reporting."""

        self.windows.append(
            (
                suppression.scope,
                tuple(type(hook).__name__ for hook in suppression.hooks),
                tuple(type(hook).__name__ for hook in suppression.mandatory),
            )
        )

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Record every operation's outcome and release the stashed start."""

        self.pending.pop(context.operation_id, None)
        self.outcomes.append((context.operation, outcome, context.attributes.get("run_id")))


class BestEffortSink(MemoryHook):
    """Observing hook whose own outages must not reach callers.

    Attributes:
        offline: Whether the sink fails instead of recording.
        observed: Ids of the blocks recorded while the sink was reachable.
    """

    fail_open = True

    def __init__(self) -> None:
        """Create a reachable sink."""

        self.offline = False
        self.observed: list[str] = []

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Send a stored block id to a sink that may be unreachable.

        Raises:
            RuntimeError: If the sink is offline. `fail_open` contains it.
        """

        if self.offline:
            raise RuntimeError("audit sink unreachable")
        self.observed.append(block.id)


class ContainedFailureLog(logging.Handler):
    """Collect the failures Genie contains for fail-open hooks.

    Attributes:
        messages: Messages logged by the `genie.hooks.registry` logger.
    """

    def __init__(self) -> None:
        """Create an empty handler."""

        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Record one contained failure.

        Args:
            record: Log record describing the contained failure.
        """

        self.messages.append(record.getMessage())


def main() -> None:
    """Walk the hook audit surface from registration to detachment."""

    print("== Hook audit ==")
    contained = ContainedFailureLog()
    logging.getLogger("genie.hooks.registry").addHandler(contained)

    # Which hooks are installed is itself auditable. A mandatory hook can be
    # neither unregistered nor suppressed by the code that runs after it, and
    # `Memory.hooks` is read-only, so the registry cannot be swapped either.
    audit = AuditHook()
    sink = BestEffortSink()
    registry = HookRegistry()
    registry.register(audit, mandatory=True)
    registry.register(sink)
    memory = Memory(
        backend=InMemoryBackend(),
        principal="agent:owner",
        hooks=registry,
        attributes={"service": "memory"},
    )

    assert memory.hooks.hooks == (audit, sink)
    assert memory.hooks.mandatory == (audit,)

    # A scope names work Genie does not model, such as one agent turn.
    # Operations started inside it become its children and inherit its labels.
    with memory.hooks.scope("agent.turn", attributes={"run_id": "run-1"}):
        goal = memory.put(type="goal", value="Summarize the launch thread")
        step = memory.put(type="step", value="Collect the open questions")
        recalled_goal = memory.get(goal)
        recalled_step = memory.get(step)
        # Backend access is a trusted boundary and reports no hook event.
        raw_goal = memory.backend.get_block(goal.id)

    assert recalled_goal is not None
    assert recalled_step is not None
    assert raw_goal is not None
    print(f"blocks observed by the audit hook: {len(audit.created)}")
    print(f"operations grouped under the turn: {len(audit.outcomes)}")

    assert audit.created == [goal.id, step.id]
    assert [operation for operation, _, _ in audit.outcomes] == (
        [HookOperation.PUT] * 2 + [HookOperation.GET] * 2
    )
    assert {outcome for _, outcome, _ in audit.outcomes} == {HookOutcome.SUCCESS}
    # Every operation inside the scope inherited its label.
    assert {label for _, _, label in audit.outcomes} == {"run-1"}

    # A traversal names the block a read starts from rather than the blocks it
    # returns, so the trail records how much one call disclosed.
    project = memory.put(type="project", value="Launch notes")
    note = memory.put(parent=project, type="note", value="Draft the outline")
    children = memory.children(project)
    ancestors = memory.ancestors(note)

    disclosures = ", ".join(f"{kind} disclosed {count}" for kind, _, count in audit.disclosed)
    print(f"structural reads recorded: {disclosures}")

    assert [block.id for block in children] == [note.id]
    assert [block.id for block in ancestors] == [project.id]
    assert audit.disclosed == [
        (TraversalKind.CHILDREN, project.id, 1),
        (TraversalKind.ANCESTORS, note.id, 1),
    ]

    # Refusals are observations rather than decisions: the hook records the
    # boundary that refused, the block it applies to, and the sandbox that
    # attempted it, and the operation reports a denied terminal event.
    handle = memory.get_handle(root=project)
    outside = memory.put(type="tenant", value="Another tenant's note")
    try:
        handle.get(outside)
    except AccessDenied:
        print(f"handle reading a block outside its root: {audit.refusals[0][1]}")
    else:
        raise AssertionError("A handle must not read a block outside its root.")

    assert audit.refusals == [(HookOperation.GET, HandleCapability.SUBTREE, outside.id, handle.id)]
    assert (HookOperation.GET, HookOutcome.DENIED, None) in audit.outcomes

    # Turning hooks off is an operation rather than a silent gap: every hook is
    # told before dispatch stops, the mandatory hook keeps reporting inside the
    # window, and a terminal event closes it.
    with memory.hooks.suppress(sink, attributes={"migration": "m1"}):
        migrated = memory.put(type="internal", value="Backfilled by a migration")
    with memory.hooks.suppress(attributes={"migration": "m1"}):
        bootstrapped = memory.put(type="internal", value="Bootstrap record")

    print(f"suppression windows announced to every hook: {len(audit.windows)}")

    assert audit.windows == [
        (SuppressionScope.SELECTED, ("BestEffortSink",), ("AuditHook",)),
        (SuppressionScope.REGISTRY, ("BestEffortSink",), ("AuditHook",)),
    ]
    assert {migrated.id, bootstrapped.id} <= set(audit.created)
    assert not {migrated.id, bootstrapped.id} & set(sink.observed)
    assert audit.outcomes.count((HookOperation.SUPPRESS, HookOutcome.SUCCESS, None)) == 2

    # A best-effort sink declares that its own outages must not reach callers.
    # Contained failures are logged with their traceback to the
    # `genie.hooks.registry` logger, so a failing sink stays visible.
    sink.offline = True
    resilient = memory.put(type="note", value="Stored while the sink is down")
    sink.offline = False

    print(f"contained sink failure: {contained.messages[0]}")

    assert memory.get(resilient) is not None
    assert resilient.id in audit.created
    assert resilient.id not in sink.observed
    assert len(contained.messages) == 1

    # Ordinary hooks can be detached; the mandatory audit hook cannot.
    memory.hooks.unregister(sink)
    try:
        memory.hooks.unregister(audit)
    except ValueError:
        print("unregistering the mandatory audit hook: refused")
    else:
        raise AssertionError("A mandatory hook must not be unregisterable.")

    print(f"operations with a terminal event: {len(audit.outcomes)}")

    assert memory.hooks.hooks == (audit,)
    # Every operation that started ended, so no stashed state was left behind.
    assert audit.pending == {}


if __name__ == "__main__":
    main()
