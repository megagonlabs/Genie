"""Build an audit trail from the events that a memory system reports.

`genie.hooks` is the observability surface. The example shows each part of it:

- **Scopes:** a scope names work that Genie does not model.
- **Traversal events:** each event reports what one read disclosed.
- **Refusals:** the hook records each refusal as an observation.
- **Terminal events:** a terminal event closes each operation.
- **Mandatory hook:** code cannot unregister or suppress the mandatory hook of a
  registry.
- **Suppression windows:** the registry tells each hook about a window before
  dispatch stops.
- **`fail_open`:** a sink with `fail_open` does not send its own outages to
  callers.

Other examples:

- **Transform hooks:** `write_hooks.py` and `search_hooks.py` show hooks that
  rewrite what `Memory` stores or returns.
- **Structure membership events:** only `genie.structures` facades report
  them, so `../advanced/structure_hooks.py` shows them.

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
        created: Ids of the stored blocks that this hook observed.
        disclosed: Kind, start block id, and record count of each structural
            read. One call that returns many blocks stays one event.
        refusals: Refused operation, the check that refused it, the block that
            the refusal applies to, and the id of the handle that tried the
            operation.
        windows: `SuppressionScope` value, names of the suppressed hooks, and
            names of the mandatory hooks that continue to report, for each
            suppression window.
        outcomes: Operation, outcome, and `run_id` turn label of each operation
            that started. The list includes each outcome: success, refusal, and
            failure.
        pending: Start times that `before_put` keeps when an operation starts.
            The terminal event of the same operation removes each entry.
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
        """Keep the start time of this write, and return the block without a change."""

        self.pending[context.operation_id] = context.started_at
        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record the id of a stored block."""

        self.created.append(block.id)

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record how many blocks and links one structural read disclosed."""

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
        """Record a refusal and the handle that tried the operation."""

        self.refusals.append(
            (
                context.operation,
                operation,
                subject_id,
                context.attributes.get("handle.id"),
            )
        )

    def on_suppressed(self, context: HookContext, suppression: Suppression) -> None:
        """Record a suppression window, in which other hooks do not report."""

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
        """Record the outcome of each operation, and remove its kept start time."""

        self.pending.pop(context.operation_id, None)
        self.outcomes.append((context.operation, outcome, context.attributes.get("run_id")))


class BestEffortSink(MemoryHook):
    """Observer hook whose own outages do not reach callers.

    Attributes:
        offline: `True` if the sink fails and does not record.
        observed: Ids of the blocks that the sink recorded while it was
            reachable.
    """

    fail_open = True

    def __init__(self) -> None:
        """Create a reachable sink."""

        self.offline = False
        self.observed: list[str] = []

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Send the id of a stored block to a sink that can be unreachable.

        Raises:
            RuntimeError: If the sink is offline. Because of `fail_open`, the
                registry contains this error and logs it.
        """

        if self.offline:
            raise RuntimeError("audit sink unreachable")
        self.observed.append(block.id)


class ContainedFailureLog(logging.Handler):
    """Collect the failures that Genie contains for hooks with `fail_open`.

    Attributes:
        messages: Messages that the `genie.hooks.registry` logger logs.
    """

    def __init__(self) -> None:
        """Create an empty handler for `logging.ERROR` records and higher."""

        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Record one contained failure.

        Args:
            record: Log record that describes the contained failure.
        """

        self.messages.append(record.getMessage())


def main() -> None:
    """Show each part of the hook audit surface, from registration to unregistration."""

    print("== Hook audit ==")
    contained = ContainedFailureLog()
    logging.getLogger("genie.hooks.registry").addHandler(contained)

    # The list of installed hooks is also auditable.
    # - **Mandatory hook:** code that runs after the registration cannot
    #   unregister or suppress it.
    # - **Fixed registry:** `Memory.hooks` is read-only, so code cannot replace
    #   the registry.
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

    # A scope names work that Genie does not model, for example one agent turn.
    # Operations that start inside the scope become its children and get its labels.
    with memory.hooks.scope("agent.turn", attributes={"run_id": "run-1"}):
        goal = memory.put(type="goal", value="Summarize the launch thread")
        step = memory.put(type="step", value="Collect the open questions")
        recalled_goal = memory.get(goal)
        recalled_step = memory.get(step)
        # Direct backend access is a trusted boundary and reports no hook event.
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
    # Each operation inside the scope got the label of the scope.
    assert {label for _, _, label in audit.outcomes} == {"run-1"}

    # A traversal names the block where a read starts, not the blocks that the
    # read returns. Thus the trail records how much one call disclosed.
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

    # A refusal is an observation, not a decision.
    # - **Hook record:** the hook records the check that refused, the block that
    #   the refusal applies to, and the handle that tried the operation.
    # - **Terminal event:** the operation reports a `HookOutcome.DENIED` terminal
    #   event.
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

    # A suppression window is an operation, not a silent gap.
    # - **Notice:** the registry tells each hook before dispatch stops.
    # - **Mandatory hook:** the mandatory hook continues to report inside the
    #   window.
    # - **Close:** a terminal event closes the window.
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

    # A best-effort sink sets `fail_open`, so its own outages do not reach callers.
    # The registry logs each contained failure with its traceback to the
    # `genie.hooks.registry` logger. Thus a sink failure stays visible.
    sink.offline = True
    resilient = memory.put(type="note", value="Stored while the sink is down")
    sink.offline = False

    print(f"contained sink failure: {contained.messages[0]}")

    assert memory.get(resilient) is not None
    assert resilient.id in audit.created
    assert resilient.id not in sink.observed
    assert len(contained.messages) == 1

    # Code can unregister an ordinary hook. Code cannot unregister the mandatory audit hook.
    memory.hooks.unregister(sink)
    try:
        memory.hooks.unregister(audit)
    except ValueError:
        print("unregistering the mandatory audit hook: refused")
    else:
        raise AssertionError("A mandatory hook must not be unregisterable.")

    print(f"operations with a terminal event: {len(audit.outcomes)}")

    assert memory.hooks.hooks == (audit,)
    # Each operation that started also ended, so no kept start time stays.
    assert audit.pending == {}


if __name__ == "__main__":
    main()
