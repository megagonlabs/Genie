"""Tests for memory hook dispatch and customization."""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Lock
from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    HandleCapability,
    HookContext,
    HookOperation,
    HookOrigin,
    HookOutcome,
    HookRecursionError,
    HookRegistry,
    InMemoryBackend,
    KeyedStore,
    LinkRelations,
    Memory,
    MemoryBlock,
    MemoryConflict,
    MemoryHook,
    MemoryNotFound,
    MemoryPolicy,
    MemoryQuery,
    Operation,
    Scope,
    SearchResult,
    StructureAction,
    StructureChange,
    Suppression,
    SuppressionScope,
    Traversal,
    TraversalKind,
)
from genie.structures import (
    EventLog,
    KeyedCollection,
    MemoryAddress,
    OwnershipTree,
    TemporalCollection,
    VersionedRecords,
)


def test_default_hook_is_a_complete_noop_extension() -> None:
    """The base hook can observe every operation family without changing behavior."""

    memory = Memory(hooks=[MemoryHook()])
    root = memory.put(type="collection", value="root")
    note = memory.put(parent=root, type="note", value={"status": "new"})
    target = memory.put(parent=root, type="note", value="target")

    assert memory.get(note) == note
    updated_note = memory.update(note, value={"status": "done"})
    assert updated_note.value == {"status": "done"}
    assert [result.block.id for result in memory.search("done")] == [note.id]
    assert [block.id for block in memory.children(root)] == [note.id, target.id]
    assert [block.id for block in memory.ancestors(note)] == [root.id]
    assert memory.contains(root, note) is True
    link = memory.add_link(note, target, "related")
    assert memory.list_links(note) == [link]
    updated_link = memory.update_link(link, metadata={"reviewed": True})
    assert updated_link.metadata == {"reviewed": True}
    mount = memory.add_mount(note, target)
    assert memory.list_mounts(note) == [mount]
    assert memory.discover_mounts(root) == [mount]
    assert memory.resolve_mount(mount) == target

    keyed = KeyedCollection(memory, cast(KeyedStore, memory.backend), collection=root)
    address = MemoryAddress((), "target")
    keyed.bind(address, target)
    keyed.unbind(address, expected=target)
    memory.delete(note)

    assert memory.get(note) is None


def test_hooks_can_transform_and_observe_blocks() -> None:
    """Put hooks can transform blocks and observe stored ids."""

    class SourceHook(MemoryHook):
        """Hook that annotates created blocks and records their ids."""

        def __init__(self) -> None:
            self.created: list[str] = []

        def before_put(self, context, block):
            """Add marker metadata before storage."""

            return block.with_updates(metadata={**block.metadata, "hooked": True})

        def after_put(self, context, block):
            """Record the stored block id."""

            self.created.append(block.id)

    hook = SourceHook()
    memory = Memory(hooks=[hook])

    block = memory.put(type="note", value="hello")

    assert block.metadata["hooked"] is True
    assert hook.created == [block.id]


def test_before_put_hook_cannot_change_parent_after_authorization() -> None:
    """Put hooks cannot reparent a block after scope and policy checks."""

    class ReparentHook(MemoryHook):
        """Hook that tries to move new blocks to another parent."""

        def __init__(self, parent_id: str) -> None:
            self.parent_id = parent_id

        def before_put(self, context, block):
            """Attempt to change the authorized parent."""

            return block.with_updates(parent_id=self.parent_id)

    memory = Memory()
    root = memory.put(type="root", value="root")
    outside = memory.put(type="root", value="outside")
    memory.hooks.register(ReparentHook(outside.id))
    scope = memory.get_handle(root=root)

    with pytest.raises(ValueError, match="parent_id"):
        scope.put(type="note", value="should not be stored")

    assert memory.children(root) == []
    assert memory.children(outside) == []


def test_transforming_hooks_cannot_store_nonportable_metadata() -> None:
    """Hook-produced metadata still follows the public strict JSON contract."""

    class InvalidMetadataHook(MemoryHook):
        """Hook that returns a value forbidden by the metadata contract."""

        def before_put(
            self,
            context: HookContext,
            block: MemoryBlock,
        ) -> MemoryBlock:
            """Attach a non-JSON object."""

            return block.with_updates(metadata={"invalid": object()})

    memory = Memory(hooks=[InvalidMetadataHook()])

    with pytest.raises(TypeError, match="strict JSON"):
        memory.put(id="invalid", type="note", value="not stored")
    assert memory.backend.get_block("invalid") is None


def test_search_hooks_can_rewrite_and_trim_results() -> None:
    """Search hooks can rewrite queries and trim result lists."""

    class SearchHook(MemoryHook):
        """Hook that rewrites the query and trims results."""

        def __init__(self) -> None:
            self.calls: list[tuple[str, str | None]] = []

        def before_search(
            self,
            context: HookContext,
            query: MemoryQuery,
        ) -> MemoryQuery:
            """Replace the incoming text query with a known needle."""

            self.calls.append(("before", query.text))
            return MemoryQuery(text="needle", limit=query.limit)

        def after_search(
            self,
            context: HookContext,
            query: MemoryQuery,
            results: list[Any],
        ) -> list[Any]:
            """Keep only the first result."""

            self.calls.append(("after", query.text))
            return results[:1]

    hook = SearchHook()
    memory = Memory(hooks=[hook])
    first = memory.put(type="note", value="needle one")
    memory.put(type="note", value="needle two")

    results = memory.search("ignored")

    assert [result.block.id for result in results] == [first.id]
    assert hook.calls == [("before", "ignored"), ("after", "needle")]


def test_before_search_hook_cannot_widen_query_after_authorization() -> None:
    """Search hooks cannot switch a query to an unauthorized root after checks."""

    class EscalatingHook(MemoryHook):
        """Hook that tries to redirect search into a private subtree."""

        def __init__(self, private_root_id: str) -> None:
            self.private_root_id = private_root_id

        def before_search(
            self,
            context: HookContext,
            query: MemoryQuery,
        ) -> MemoryQuery:
            """Replace the authorized query with a private subtree query."""

            return MemoryQuery(
                text="secret",
                root_id=self.private_root_id,
                scope=Scope.SUBTREE,
                limit=query.limit,
            )

    owner = Memory(principal="owner")
    public_root = owner.put(type="root", value="public")
    private_root = owner.put(
        type="root",
        value="private",
        policy=MemoryPolicy(
            search=AccessRule.only(principals={"owner"}),
            traverse_down=AccessRule.only(principals={"owner"}),
        ),
    )
    owner.put(parent=private_root, type="note", value="secret")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=[EscalatingHook(private_root.id)],
        codecs=owner.codecs,
    )

    with pytest.raises(AccessDenied):
        reader.search("public", root=public_root, scope="subtree")


def test_after_search_hook_results_are_rechecked_for_policy_and_scope() -> None:
    """Search results returned by hooks stay inside policy and query scope."""

    class InjectingHook(MemoryHook):
        """Hook that tries to add unreadable and out-of-scope results."""

        def __init__(self, injected: list[SearchResult]) -> None:
            self.injected = injected

        def after_search(
            self,
            context: HookContext,
            query: MemoryQuery,
            results: list[SearchResult],
        ) -> list[SearchResult]:
            """Append injected results after normal policy filtering."""

            return [*results, *self.injected]

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    inside = owner.put(parent=root, type="note", value="needle inside", policy=MemoryPolicy.public())
    private_inside = owner.put(
        parent=root,
        type="note",
        value="needle private",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    outside = owner.put(type="note", value="needle outside", policy=MemoryPolicy.public())
    injected = [
        SearchResult(block=private_inside, score=99, reason="injected private"),
        SearchResult(block=outside, score=98, reason="injected outside"),
        SearchResult(
            block=MemoryBlock(id="missing", type="note", value="missing"),
            score=97,
            reason="injected missing",
        ),
    ]
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=[InjectingHook(injected)],
        codecs=owner.codecs,
    )

    results = reader.search("needle", root=root, scope="subtree")

    assert [result.block.id for result in results] == [inside.id]


def test_get_and_delete_hooks_observe_missing_and_deleted_blocks() -> None:
    """Hooks observe missing gets and every deleted subtree block."""

    class LifecycleHook(MemoryHook):
        """Hook that records get and delete events."""

        def __init__(self) -> None:
            self.before_get_calls: list[str] = []
            self.after_get_calls: list[str | None] = []
            self.before_deleted: list[str] = []
            self.deleted: list[str] = []

        def before_get(self, context: HookContext, block_id: str) -> None:
            """Record ids before lookup."""

            self.before_get_calls.append(block_id)

        def after_get(self, context: HookContext, block: Any) -> None:
            """Record looked-up block ids or missing values."""

            self.after_get_calls.append(block.id if block is not None else None)

        def before_delete(self, context: HookContext, block: Any) -> None:
            """Record ids before deletion."""

            self.before_deleted.append(block.id)

        def after_delete(self, context: HookContext, block: Any) -> None:
            """Record deleted block ids."""

            self.deleted.append(block.id)

    hook = LifecycleHook()
    memory = Memory(hooks=[hook])
    block = memory.put(type="note", value="note")
    child = memory.put(parent=block, type="note", value="child")

    assert memory.get("missing") is None
    memory.delete(block)

    assert hook.before_get_calls == ["missing"]
    assert hook.after_get_calls == [None]
    assert hook.before_deleted == [block.id, child.id]
    assert hook.deleted == [block.id, child.id]


def test_hook_registry_runs_hooks_in_registration_order() -> None:
    """HookRegistry applies transformations in deterministic order."""

    class OrderedHook(MemoryHook):
        """Hook that appends a marker to block metadata."""

        def __init__(self, marker: str, calls: list[str]) -> None:
            self.marker = marker
            self.calls = calls

        def before_put(self, context, block):
            """Append the marker to the ordered metadata list."""

            self.calls.append(self.marker)
            markers = [*block.metadata.get("markers", ()), self.marker]
            return block.with_updates(metadata={**block.metadata, "markers": markers})

    calls: list[str] = []
    registry = HookRegistry([OrderedHook("first", calls)])
    registry.register(OrderedHook("second", calls))
    memory = Memory(hooks=registry)

    block = memory.put(type="note", value="ordered")

    assert calls == ["first", "second"]
    assert block.metadata["markers"] == ["first", "second"]


def test_hook_registry_dispatch_requires_an_active_explicit_context() -> None:
    """Registry dispatch never creates a compatibility operation implicitly."""

    registry = HookRegistry()
    block = MemoryBlock(type="note", value="encoded")

    with registry.operation("put") as context:
        assert context.operation is HookOperation.PUT
        assert registry.before_put(context, block) is block

    with pytest.raises(ValueError, match="active HookRegistry.operation"):
        registry.before_put(context, block)
    with pytest.raises(ValueError), registry.operation("missing"):
        pytest.fail("Invalid operations must fail before entering context.")


def test_transforming_hooks_reject_values_outside_their_contracts() -> None:
    """Transform callbacks fail clearly instead of passing invalid values onward."""

    class BadPutHook(MemoryHook):
        """Return the wrong type from a block transformation."""

        def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
            return cast(MemoryBlock, object())

    with pytest.raises(TypeError, match="before_put hooks must return MemoryBlock"):
        Memory(hooks=[BadPutHook()]).put(type="note", value="bad")

    class BadSearchHook(MemoryHook):
        """Return a non-list result collection."""

        def after_search(
            self,
            context: HookContext,
            query: MemoryQuery,
            results: list[SearchResult],
        ) -> list[SearchResult]:
            return cast(list[SearchResult], tuple(results))

    with pytest.raises(TypeError, match="after_search hooks must return a list"):
        Memory(hooks=[BadSearchHook()]).search()

    class BadSearchElementHook(MemoryHook):
        """Return a list containing a value outside the result contract."""

        def after_search(
            self,
            context: HookContext,
            query: MemoryQuery,
            results: list[SearchResult],
        ) -> list[SearchResult]:
            return cast(list[SearchResult], [object()])

    with pytest.raises(TypeError, match="only SearchResult values"):
        Memory(hooks=[BadSearchElementHook()]).search()


def test_recursive_hook_put_skips_only_the_active_callback() -> None:
    """A hook-generated PUT completes without invoking the same callback again."""

    class AuditHook(MemoryHook):
        """Write one audit block for an outer PUT."""

        def __init__(self) -> None:
            self.memory: Memory | None = None
            self.contexts: list[HookContext] = []

        def after_put(self, context: HookContext, block) -> None:
            """Record context and create an audit block."""

            self.contexts.append(context)
            assert self.memory is not None
            self.memory.put(type="audit", value={"source": block.id})

    class ObserverHook(MemoryHook):
        """Observe both outer and nested PUTs."""

        def __init__(self) -> None:
            self.contexts: list[HookContext] = []

        def after_put(self, context: HookContext, block) -> None:
            """Record the active causal context."""

            self.contexts.append(context)

    audit = AuditHook()
    observer = ObserverHook()
    memory = Memory(hooks=[audit, observer], principal="agent:writer")
    audit.memory = memory

    memory.put(type="note", value="start")

    assert len(audit.contexts) == 1
    assert len(observer.contexts) == 2
    outer = audit.contexts[0]
    nested, observed_outer = observer.contexts
    assert outer == observed_outer
    assert outer.origin == "caller"
    assert outer.depth == 0
    assert nested.origin == "hook"
    assert nested.depth == 1
    assert nested.parent_operation_id == outer.operation_id
    assert len(memory.search(limit=10)) == 2


def test_alternating_put_update_recursion_stops_at_active_callback() -> None:
    """A PUT-to-UPDATE-to-PUT cycle stops when its active callback repeats."""

    class AlternatingHook(MemoryHook):
        """Create the cross-operation cycle under test."""

        def __init__(self) -> None:
            self.enabled = False
            self.memory: Memory | None = None
            self.target: MemoryBlock | None = None
            self.calls: list[tuple[HookOperation, int]] = []

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Turn every observed PUT into an UPDATE while enabled."""

            if not self.enabled:
                return
            self.calls.append((context.operation, context.depth))
            assert self.memory is not None
            assert self.target is not None
            self.memory.update(self.target, metadata={"observed_put": block.id})

        def after_update(
            self,
            context: HookContext,
            previous: MemoryBlock,
            stored: MemoryBlock,
        ) -> None:
            """Turn the nested UPDATE back into a PUT."""

            self.calls.append((context.operation, context.depth))
            assert self.memory is not None
            self.memory.put(type="nested", value={"source": stored.id})

    hook = AlternatingHook()
    memory = Memory(hooks=[hook])
    hook.memory = memory
    target = memory.put(type="target", value="target")
    hook.target = target
    hook.enabled = True

    trigger = memory.put(type="trigger", value="start")

    assert hook.calls == [
        (HookOperation.PUT, 0),
        (HookOperation.UPDATE, 1),
    ]
    updated_target = memory.get(target)
    assert updated_target is not None
    assert updated_target.metadata == {"observed_put": trigger.id}
    assert [result.block.type for result in memory.search(limit=10)].count("nested") == 1


def test_hook_suppression_is_scoped_and_resets_after_exit() -> None:
    """Trusted code can suppress one hook or a complete registry temporarily."""

    class CountingHook(MemoryHook):
        """Count stored blocks observed after commit."""

        def __init__(self) -> None:
            self.calls = 0

        def after_put(self, context: HookContext, block) -> None:
            """Increment the call count."""

            self.calls += 1

    first = CountingHook()
    second = CountingHook()
    registry = HookRegistry([first, second])
    memory = Memory(hooks=registry)

    with registry.suppress(first):
        memory.put(type="note", value="first")
    with registry.suppress():
        memory.put(type="note", value="second")
    memory.put(type="note", value="third")

    assert first.calls == 1
    assert second.calls == 2


class RecordingRegistryHook(MemoryHook):
    """Hook that records suppression windows and the operations it observes."""

    def __init__(self) -> None:
        self.windows: list[tuple[HookContext, Suppression]] = []
        self.events: list[tuple[HookOperation, HookOutcome]] = []
        self.contexts: list[HookContext] = []

    def on_suppressed(self, context: HookContext, suppression: Suppression) -> None:
        """Record a window this hook was told about."""

        self.windows.append((context, suppression))

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Record the terminal event of an observed operation."""

        self.events.append((context.operation, outcome))
        self.contexts.append(context)


def test_registry_lists_its_hooks_and_registers_each_one_once() -> None:
    """Registration is by identity, so one hook cannot report an operation twice."""

    first = RecordingRegistryHook()
    second = RecordingRegistryHook()
    registry = HookRegistry([first])
    registry.register(second)

    assert registry.hooks == (first, second)
    assert registry.mandatory == ()
    with pytest.raises(ValueError, match="already registered"):
        registry.register(first)
    with pytest.raises(ValueError, match="already registered"):
        HookRegistry([first, first])
    with pytest.raises(TypeError, match="MemoryHook"):
        registry.register(cast(MemoryHook, object()))

    Memory(hooks=registry).put(type="note", value="once")

    assert registry.hooks == (first, second)
    assert [len(first.events), len(second.events)] == [1, 1]


def test_unregistering_a_hook_stops_its_dispatch() -> None:
    """A removed hook observes nothing further and cannot be removed twice."""

    first = RecordingRegistryHook()
    second = RecordingRegistryHook()
    registry = HookRegistry([first, second])
    memory = Memory(hooks=registry)

    memory.put(type="note", value="observed by both")
    registry.unregister(first)
    memory.put(type="note", value="observed by one")

    assert registry.hooks == (second,)
    assert [len(first.events), len(second.events)] == [1, 2]
    with pytest.raises(ValueError, match="not registered"):
        registry.unregister(first)


def test_registry_changes_take_effect_on_the_next_event() -> None:
    """A callback that reshapes the registry cannot cut short the event it is in."""

    class CountingPutHook(MemoryHook):
        """Count the stored blocks this hook is told about."""

        def __init__(self) -> None:
            self.calls = 0

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Count one reported block."""

            self.calls += 1

    class DetachingHook(MemoryHook):
        """Unregister another hook from inside a dispatching callback."""

        def __init__(self, registry: HookRegistry, target: MemoryHook) -> None:
            self.registry = registry
            self.target = target

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Remove the target hook while the event is still dispatching."""

            if self.target in self.registry.hooks:
                self.registry.unregister(self.target)

    counting = CountingPutHook()
    registry = HookRegistry()
    registry.register(DetachingHook(registry, counting))
    registry.register(counting)
    memory = Memory(hooks=registry)

    memory.put(type="note", value="detaches mid-dispatch")
    memory.put(type="note", value="after detachment")

    assert counting.calls == 1
    assert counting not in registry.hooks


def test_a_mandatory_hook_cannot_be_detached_or_suppressed() -> None:
    """A mandatory hook survives unregistration and every suppression window."""

    audit = RecordingRegistryHook()
    ordinary = RecordingRegistryHook()
    registry = HookRegistry([ordinary])
    registry.register(audit, mandatory=True)
    memory = Memory(hooks=registry)

    assert registry.mandatory == (audit,)
    with pytest.raises(ValueError, match="mandatory"):
        registry.unregister(audit)
    with pytest.raises(ValueError, match="mandatory"), registry.suppress(audit):
        pytest.fail("A mandatory hook must not open a suppression window.")

    assert audit.windows == []
    assert audit.events == []

    with registry.suppress():
        memory.put(type="note", value="migration")

    assert [operation for operation, _ in ordinary.events] == [HookOperation.SUPPRESS]
    assert [operation for operation, _ in audit.events] == [
        HookOperation.PUT,
        HookOperation.SUPPRESS,
    ]


def test_a_memory_keeps_the_registry_it_dispatches_through() -> None:
    """Auditing cannot be detached by replacing the registry that reports it."""

    audit = RecordingRegistryHook()
    registry = HookRegistry()
    registry.register(audit, mandatory=True)
    memory = Memory(hooks=registry)

    with pytest.raises(AttributeError):
        memory.hooks = HookRegistry()  # type: ignore[misc]

    added = RecordingRegistryHook()
    memory.hooks.register(added)
    memory.put(type="note", value="still audited")

    assert memory.hooks is registry
    assert registry.hooks == (audit, added)
    assert [len(audit.events), len(added.events)] == [1, 1]


def test_suppressing_selected_hooks_announces_a_bounded_window() -> None:
    """A suppressed hook is told the window opens and observes it close."""

    silenced = RecordingRegistryHook()
    observer = RecordingRegistryHook()
    registry = HookRegistry([silenced, observer])
    memory = Memory(hooks=registry)

    with registry.suppress(silenced, attributes={"migration": "m1"}) as window:
        memory.put(type="note", value="unreported")

    assert window.operation is HookOperation.SUPPRESS
    for hook in (silenced, observer):
        opened, suppression = hook.windows[0]
        assert opened.operation_id == window.operation_id
        assert opened.attributes == {"migration": "m1"}
        assert suppression == Suppression(
            scope=SuppressionScope.SELECTED,
            hooks=(silenced,),
            mandatory=(),
        )
    assert [operation for operation, _ in silenced.events] == [HookOperation.SUPPRESS]
    assert silenced.contexts[0].operation_id == window.operation_id
    assert [operation for operation, _ in observer.events] == [
        HookOperation.PUT,
        HookOperation.SUPPRESS,
    ]


def test_suppressing_the_registry_names_what_goes_quiet() -> None:
    """A registry-wide window names the silenced hooks and the ones left."""

    audit = RecordingRegistryHook()
    first = RecordingRegistryHook()
    second = RecordingRegistryHook()
    registry = HookRegistry([first, second])
    registry.register(audit, mandatory=True)
    memory = Memory(hooks=registry)

    with registry.suppress(principal_id="ops:migrator") as window:
        note = memory.put(type="note", value="bulk import")

    _, suppression = audit.windows[0]
    assert suppression == Suppression(
        scope=SuppressionScope.REGISTRY,
        hooks=(first, second),
        mandatory=(audit,),
    )
    assert [len(first.windows), len(second.windows)] == [1, 1]
    put_context = audit.contexts[0]
    assert put_context.operation is HookOperation.PUT
    assert put_context.parent_operation_id == window.operation_id
    assert window.principal_id == "ops:migrator"
    assert memory.get(note) is not None


def test_a_suppression_no_hook_can_record_does_not_happen() -> None:
    """A failing announcement leaves dispatch enabled and the window unopened."""

    class FailingSuppressionHook(MemoryHook):
        """Sink that cannot record the one event it exists to record."""

        def on_suppressed(
            self,
            context: HookContext,
            suppression: Suppression,
        ) -> None:
            """Fail while the window is being announced."""

            raise RuntimeError("audit sink down")

    observer = RecordingRegistryHook()
    registry = HookRegistry([FailingSuppressionHook(), observer])
    memory = Memory(hooks=registry)
    entered = False

    with pytest.raises(RuntimeError, match="audit sink down"), registry.suppress():
        entered = True

    memory.put(type="note", value="still reported")

    assert entered is False
    assert len(observer.windows) == 1
    assert [event for event in observer.events] == [
        (HookOperation.SUPPRESS, HookOutcome.ERROR),
        (HookOperation.PUT, HookOutcome.SUCCESS),
    ]


def test_a_fail_open_sink_cannot_block_a_suppression(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A best-effort sink's outage is contained when a window opens."""

    class FlakySuppressionHook(MemoryHook):
        """Best-effort sink that must not break callers when it fails."""

        fail_open = True

        def on_suppressed(
            self,
            context: HookContext,
            suppression: Suppression,
        ) -> None:
            """Fail without blocking the window."""

            raise RuntimeError("audit sink down")

    observer = RecordingRegistryHook()
    registry = HookRegistry([FlakySuppressionHook(), observer])
    memory = Memory(hooks=registry)

    with caplog.at_level(logging.ERROR), registry.suppress():
        memory.put(type="note", value="unreported")

    assert observer.events == [(HookOperation.SUPPRESS, HookOutcome.SUCCESS)]
    reported = [record.getMessage() for record in caplog.records]
    assert sum("on_suppressed" in message for message in reported) == 1
    assert all("FlakySuppressionHook" in message for message in reported)


def test_only_registered_hooks_can_be_suppressed() -> None:
    """Suppressing a hook the registry never dispatches is refused."""

    registered = RecordingRegistryHook()
    stranger = RecordingRegistryHook()
    registry = HookRegistry([registered])

    with pytest.raises(ValueError, match="not registered"), registry.suppress(stranger):
        pytest.fail("An unregistered hook must not open a suppression window.")

    assert registered.windows == []


def test_suppression_windows_are_opened_only_by_suppress() -> None:
    """The suppression operation cannot be forged through the generic context."""

    registry = HookRegistry([RecordingRegistryHook()])

    for operation in (HookOperation.SUPPRESS, "hooks.suppress"):
        with pytest.raises(ValueError, match="suppress"), registry.operation(operation):
            pytest.fail("A suppression window must come from suppress().")


def test_hook_depth_limit_and_exception_cleanup() -> None:
    """Depth limits fail clearly and context always resets after exceptions."""

    class DeepHook(MemoryHook):
        """Attempt operations beyond the hook-originated depth limit."""

        def __init__(self, registry: HookRegistry) -> None:
            self.registry = registry

        def after_put(self, context: HookContext, block) -> None:
            """Create two nested operation contexts from one callback."""

            with self.registry.operation(HookOperation.GET), self.registry.operation(HookOperation.SEARCH):
                pytest.fail("Depth overflow should occur before entering context.")

    registry = HookRegistry(max_depth=1)
    registry.register(DeepHook(registry))
    with pytest.raises(HookRecursionError, match="depth 2"):
        Memory(hooks=registry).put(type="note", value="too deep")

    class FailOnceHook(MemoryHook):
        """Raise only on the first PUT callback."""

        def __init__(self) -> None:
            self.calls = 0
            self.contexts: list[HookContext] = []

        def before_put(self, context: HookContext, block):
            """Fail once, then return the proposed block."""

            self.calls += 1
            self.contexts.append(context)
            if self.calls == 1:
                raise RuntimeError("fail once")
            return block

    hook = FailOnceHook()
    memory = Memory(hooks=[hook])
    with pytest.raises(RuntimeError, match="fail once"):
        memory.put(type="note", value="first")
    memory.put(type="note", value="second")
    assert hook.calls == 2
    assert [context.depth for context in hook.contexts] == [0, 0]
    assert [context.origin for context in hook.contexts] == ["caller", "caller"]
    assert [context.parent_operation_id for context in hook.contexts] == [None, None]


def test_max_depth_rejects_an_alternating_memory_operation_before_it_runs() -> None:
    """The depth guard stops a cross-operation chain before backend mutation."""

    class DepthHook(MemoryHook):
        """Create a PUT-to-UPDATE-to-PUT chain."""

        def __init__(self) -> None:
            self.enabled = False
            self.memory: Memory | None = None
            self.target: MemoryBlock | None = None

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Start the allowed depth-one UPDATE."""

            if not self.enabled:
                return
            assert self.memory is not None
            assert self.target is not None
            self.memory.update(self.target, metadata={"updated": True})

        def after_update(
            self,
            context: HookContext,
            previous: MemoryBlock,
            stored: MemoryBlock,
        ) -> None:
            """Attempt a depth-two PUT that must be rejected."""

            assert self.memory is not None
            self.memory.put(type="forbidden", value="must not persist")

    hook = DepthHook()
    registry = HookRegistry([hook], max_depth=1)
    memory = Memory(hooks=registry)
    hook.memory = memory
    target = memory.put(type="target", value="target")
    hook.target = target
    hook.enabled = True

    with pytest.raises(HookRecursionError, match="depth 2"):
        memory.put(type="trigger", value="start")

    updated_target = memory.get(target)
    assert updated_target is not None
    assert updated_target.metadata == {"updated": True}
    assert all(result.block.type != "forbidden" for result in memory.search(limit=10))


@pytest.mark.parametrize("max_depth", [True, 1.5, "1"])
def test_hook_registry_rejects_non_integer_max_depth(max_depth: object) -> None:
    """The depth limit accepts integers but not bools or coercible values."""

    with pytest.raises(TypeError, match="max_depth must be an integer"):
        HookRegistry(max_depth=max_depth)  # type: ignore[arg-type]


def test_hook_registry_rejects_negative_max_depth() -> None:
    """The depth limit cannot be negative."""

    with pytest.raises(ValueError, match="max_depth must be non-negative"):
        HookRegistry(max_depth=-1)


def test_hook_context_is_isolated_across_threads() -> None:
    """Concurrent callers receive independent operation ids and callbacks."""

    class ContextHook(MemoryHook):
        """Collect top-level PUT contexts safely."""

        def __init__(self) -> None:
            self.contexts: list[HookContext] = []
            self.lock = Lock()

        def before_put(self, context: HookContext, block):
            """Record one context per concurrent call."""

            with self.lock:
                self.contexts.append(context)
            return block

    hook = ContextHook()
    memory = Memory(hooks=[hook])
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda index: memory.put(type="note", value=index), range(24)))

    assert len(hook.contexts) == 24
    assert len({context.operation_id for context in hook.contexts}) == 24
    assert {context.depth for context in hook.contexts} == {0}
    assert {context.origin for context in hook.contexts} == {"caller"}


def test_set_and_update_hooks_transform_and_observe_committed_blocks() -> None:
    """Both block update forms share typed callbacks and distinct operations."""

    class UpdateHook(MemoryHook):
        """Annotate updates and collect causal contexts."""

        def __init__(self) -> None:
            self.before: list[HookContext] = []
            self.after: list[HookContext] = []

        def before_update(self, context: HookContext, current, proposed):
            """Add metadata without changing protected block fields."""

            self.before.append(context)
            return proposed.with_updates(metadata={**proposed.metadata, "hooked": True})

        def after_update(self, context: HookContext, previous, stored) -> None:
            """Observe a successfully committed update."""

            self.after.append(context)

    hook = UpdateHook()
    memory = Memory(hooks=[hook])
    block = memory.put(type="record", value={"count": 0})

    replaced = memory.set(block, {"count": 1})
    merged = memory.update(replaced, value={"ready": True})

    assert replaced.metadata == {"hooked": True}
    assert merged.value == {"count": 1, "ready": True}
    assert merged.metadata == {"hooked": True}
    assert [context.operation for context in hook.before] == [
        HookOperation.SET,
        HookOperation.UPDATE,
    ]
    assert [context.operation_id for context in hook.before] == [context.operation_id for context in hook.after]


def test_update_hooks_cannot_change_protected_block_fields() -> None:
    """Update hooks cannot reparent or rewrite block identity and history."""

    class ReparentHook(MemoryHook):
        """Attempt an invalid ownership change."""

        def before_update(self, context: HookContext, current, proposed):
            """Return an invalid replacement block."""

            return proposed.with_updates(parent_id="mem_outside")

    memory = Memory(hooks=[ReparentHook()])
    block = memory.put(type="note", value="before")

    with pytest.raises(ValueError, match="changed protected fields: parent_id"):
        memory.set(block, "after")

    stored = memory.get(block)
    assert stored is not None
    assert stored.value == "before"


def test_update_link_hooks_cannot_change_topology() -> None:
    """Link update hooks may change metadata but cannot retarget a relationship."""

    class RetargetHook(MemoryHook):
        """Attempt to change a protected link endpoint during metadata update."""

        def before_update_link(self, context, current, proposed):
            """Return a link whose target no longer matches stored topology."""

            return type(proposed)(
                source_id=proposed.source_id,
                target_id="other-target",
                relation=proposed.relation,
                id=proposed.id,
                metadata=proposed.metadata,
                created_at=proposed.created_at,
            )

    memory = Memory(hooks=[RetargetHook()])
    source = memory.put(type="node", value="source")
    target = memory.put(type="node", value="target")
    link = memory.add_link(source, target, "related")

    with pytest.raises(ValueError, match="changed protected fields: target_id"):
        memory.update_link(link, metadata={"reviewed": True})

    assert memory.list_links(source) == [link]


def test_link_hooks_cover_regular_acyclic_mount_and_update_operations() -> None:
    """All link mutation paths share callbacks without losing operation type."""

    class LinkHook(MemoryHook):
        """Annotate new links and record link operation contexts."""

        def __init__(self) -> None:
            self.added: list[HookContext] = []
            self.added_after: list[HookContext] = []
            self.updated: list[HookContext] = []
            self.updated_after: list[HookContext] = []

        def before_add_link(self, context: HookContext, link):
            """Add one metadata property before persistence."""

            self.added.append(context)
            return type(link)(
                source_id=link.source_id,
                target_id=link.target_id,
                relation=link.relation,
                id=link.id,
                metadata={**link.metadata, "hooked": True},
                created_at=link.created_at,
            )

        def after_add_link(self, context: HookContext, link) -> None:
            """Record successful link persistence."""

            self.added_after.append(context)

        def before_update_link(self, context: HookContext, current, proposed):
            """Record metadata updates."""

            self.updated.append(context)
            return proposed

        def after_update_link(self, context: HookContext, previous, stored) -> None:
            """Record successful link metadata persistence."""

            self.updated_after.append(context)

    hook = LinkHook()
    memory = Memory(hooks=[hook])
    source = memory.put(type="node", value="source")
    target = memory.put(type="node", value="target")
    mounted = memory.put(type="root", value="mounted")

    regular = memory.add_link(source, target, "supports")
    memory.add_acyclic_link(source, target, "derived_into")
    memory.add_mount(source, mounted)
    updated = memory.update_link(regular, metadata={"confidence": 0.9})

    assert regular.metadata == {"hooked": True}
    assert updated.metadata == {"hooked": True, "confidence": 0.9}
    assert [context.operation for context in hook.added] == [
        HookOperation.ADD_LINK,
        HookOperation.ADD_ACYCLIC_LINK,
        HookOperation.ADD_MOUNT,
    ]
    assert [context.operation for context in hook.updated] == [HookOperation.UPDATE_LINK]
    assert [context.operation_id for context in hook.added] == [context.operation_id for context in hook.added_after]
    assert [context.operation_id for context in hook.updated] == [context.operation_id for context in hook.updated_after]


def test_structure_hooks_cover_keys_logs_and_temporal_membership() -> None:
    """Atomic facade writes emit one before and after structural event."""

    class StructureHook(MemoryHook):
        """Collect structural events and their contexts."""

        def __init__(self) -> None:
            self.before: list[tuple[StructureChange[Any], HookContext]] = []
            self.after: list[tuple[StructureChange[Any], HookContext]] = []

        def before_structure_change(self, context: HookContext, change):
            """Record the proposed change."""

            self.before.append((change, context))

        def after_structure_change(self, context: HookContext, change):
            """Record the persisted change."""

            self.after.append((change, context))

    hook = StructureHook()
    backend = InMemoryBackend()
    memory = Memory(backend=backend, hooks=[hook])
    keyed_root = memory.put(type="collection", value="keys")
    log_root = memory.put(type="log", value="events")
    temporal_root = memory.put(type="collection", value="time")
    first = memory.put(type="item", value="first")
    second = memory.put(type="item", value="second")
    address = MemoryAddress(("agents",), "active")

    keyed = KeyedCollection(memory, backend, collection=keyed_root)
    keyed.bind(address, first)
    keyed.rebind(address, second, expected=first)
    keyed.unbind(address, expected=second)
    EventLog(memory, backend, log=log_root).append(first)
    temporal = TemporalCollection(memory, backend, collection=temporal_root)
    temporal.add(first, occurred_at=datetime(2026, 1, 1, tzinfo=UTC))
    temporal.replace(first, occurred_at=datetime(2026, 1, 2, tzinfo=UTC))
    temporal.remove(first)

    actions = [change.action for change, _ in hook.before]
    assert actions == [
        StructureAction.BIND,
        StructureAction.REBIND,
        StructureAction.UNBIND,
        StructureAction.APPEND,
        StructureAction.ADD,
        StructureAction.REPLACE,
        StructureAction.REMOVE,
    ]
    assert [change.action for change, _ in hook.after] == actions
    assert [context.operation_id for _, context in hook.before] == [context.operation_id for _, context in hook.after]
    append_before = hook.before[3][0]
    append_after = hook.after[3][0]
    assert append_before.after is None
    assert append_after.after is not None


def test_versioned_records_reuse_key_and_log_hook_events() -> None:
    """Composed versioned records do not emit duplicate structural events."""

    class StructureHook(MemoryHook):
        """Collect committed structural actions."""

        def __init__(self) -> None:
            self.actions: list[StructureAction] = []

        def after_structure_change(self, context: HookContext, change) -> None:
            """Record one committed action."""

            self.actions.append(change.action)

    hook = StructureHook()
    backend = InMemoryBackend()
    memory = Memory(backend=backend)
    collection = memory.put(type="collection", value="versions")
    identity = memory.put(type="identity", value="record")
    revision = memory.put(type="revision", value="v1")
    records = VersionedRecords(
        memory,
        backend,
        collection=collection,
        hooks=HookRegistry([hook]),
    )
    address = MemoryAddress(("records",), "one")

    records.register(address, identity)
    records.append(address, revision)

    assert hook.actions == [StructureAction.BIND, StructureAction.APPEND]


def test_after_hooks_run_only_after_successful_mutations() -> None:
    """Failed atomic link and structure writes emit no after callback."""

    class CommitHook(MemoryHook):
        """Count acyclic-link and key callbacks by phase."""

        def __init__(self) -> None:
            self.link_before = 0
            self.link_after = 0
            self.key_before = 0
            self.key_after = 0

        def before_add_link(self, context: HookContext, link):
            """Count proposed acyclic links."""

            if context.operation == HookOperation.ADD_ACYCLIC_LINK:
                self.link_before += 1
            return link

        def after_add_link(self, context: HookContext, link) -> None:
            """Count committed acyclic links."""

            if context.operation == HookOperation.ADD_ACYCLIC_LINK:
                self.link_after += 1

        def before_structure_change(self, context: HookContext, change) -> None:
            """Count proposed key bindings."""

            if change.action == StructureAction.BIND:
                self.key_before += 1

        def after_structure_change(self, context: HookContext, change) -> None:
            """Count committed key bindings."""

            if change.action == StructureAction.BIND:
                self.key_after += 1

    hook = CommitHook()
    backend = InMemoryBackend()
    memory = Memory(backend=backend, hooks=[hook])
    first = memory.put(type="node", value="first")
    second = memory.put(type="node", value="second")
    collection = memory.put(type="collection", value="keys")

    memory.add_acyclic_link(first, second, "derived_into")
    with pytest.raises(MemoryConflict):
        memory.add_acyclic_link(second, first, "derived_into")

    keyed = KeyedCollection(memory, backend, collection=collection)
    address = MemoryAddress((), "one")
    keyed.bind(address, first)
    with pytest.raises(MemoryConflict):
        keyed.bind(address, second)

    assert (hook.link_before, hook.link_after) == (2, 1)
    assert (hook.key_before, hook.key_after) == (2, 1)


class RecordingSinkHook(MemoryHook):
    """Hook that records the committed work and reads reported to it."""

    def __init__(self) -> None:
        self.stored: list[str] = []
        self.deleted: list[str] = []
        self.disclosed: list[TraversalKind] = []

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a stored block."""

        self.stored.append(block.id)

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a deleted block."""

        self.deleted.append(block.id)

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record a completed structural read."""

        self.disclosed.append(traversal.kind)


class BrokenSinkHook(MemoryHook):
    """Hook whose observing callbacks fail like an unreachable audit sink."""

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Fail after the block is already stored."""

        raise RuntimeError("audit sink down")

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Fail after the block is already deleted."""

        raise RuntimeError("audit sink down")

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Fail after the read has already disclosed its blocks."""

        raise RuntimeError("audit sink down")


def test_recursive_delete_records_every_block_when_one_sink_fails() -> None:
    """A sink that drops out mid-subtree costs no block and no hook its record."""

    class FlakySinkHook(MemoryHook):
        """Record every deletion but fail on one of them."""

        def __init__(self, fail_on: int) -> None:
            self.fail_on = fail_on
            self.deleted: list[str] = []

        def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
            """Record the deletion, then fail once."""

            self.deleted.append(block.id)
            if len(self.deleted) == self.fail_on:
                raise RuntimeError("audit sink down")

    flaky = FlakySinkHook(fail_on=3)
    recording = RecordingSinkHook()
    memory = Memory(hooks=[flaky, recording])
    root = memory.put(type="root", value="root")
    parent = root
    for level in range(4):
        parent = memory.put(parent=parent, type="note", value=f"level {level}")

    with pytest.raises(RuntimeError, match="audit sink down"):
        memory.delete(root)

    assert memory.get(root) is None
    assert len(recording.deleted) == 5
    assert recording.deleted == flaky.deleted


def test_after_hook_failure_does_not_skip_the_hooks_behind_it() -> None:
    """One failing observer cannot cost the hooks behind it their record."""

    first = RecordingSinkHook()
    second = RecordingSinkHook()
    memory = Memory(hooks=[first, BrokenSinkHook(), second])

    with pytest.raises(RuntimeError, match="audit sink down"):
        memory.put(type="root", value="root")
    stored_id = first.stored[0]
    with pytest.raises(RuntimeError, match="audit sink down"):
        memory.children(stored_id)

    assert memory.get(stored_id) is not None
    assert first.stored == second.stored == [stored_id]
    assert first.disclosed == second.disclosed == [TraversalKind.CHILDREN]


def test_after_hook_failures_are_reported_together() -> None:
    """Every collected observer failure surfaces instead of hiding the rest."""

    class NamedFailingHook(MemoryHook):
        """Fail with an identifiable message."""

        def __init__(self, name: str) -> None:
            self.name = name

        def after_update(self, context: HookContext, previous, stored) -> None:
            """Fail so the registry must report this hook."""

            raise RuntimeError(f"sink {self.name} down")

    memory = Memory(hooks=[NamedFailingHook("a"), NamedFailingHook("b")])
    block = memory.put(type="note", value="one")

    with pytest.raises(RuntimeError, match="sink a down") as raised:
        memory.set(block, "two")

    assert any("sink b down" in note for note in raised.value.__notes__)
    assert any("after_update" in note for note in raised.value.__notes__)

    stored = memory.get(block)
    assert stored is not None
    assert stored.value == "two"


def test_transforming_callbacks_stop_at_the_first_failure() -> None:
    """A failed transform never passes an unreviewed value to the next hook."""

    class BrokenTransformHook(MemoryHook):
        """Fail while transforming a proposed block and search results."""

        def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
            """Fail before the block is authorized to be stored."""

            raise RuntimeError("transform down")

        def after_search(self, context: HookContext, query, results) -> list[SearchResult]:
            """Fail before results can be redacted."""

            raise RuntimeError("transform down")

    class CountingHook(MemoryHook):
        """Count the transforming callbacks that reach this hook."""

        def __init__(self) -> None:
            self.proposed = 0
            self.reviewed = 0

        def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
            """Count proposed blocks."""

            self.proposed += 1
            return block

        def after_search(self, context: HookContext, query, results) -> list[SearchResult]:
            """Count reviewed result lists."""

            self.reviewed += 1
            return results

    counting = CountingHook()
    memory = Memory(hooks=[BrokenTransformHook(), counting])

    with pytest.raises(RuntimeError, match="transform down"):
        memory.put(type="note", value="rejected")
    with pytest.raises(RuntimeError, match="transform down"):
        memory.search("needle")

    assert (counting.proposed, counting.reviewed) == (0, 0)


def test_fail_open_sink_failures_never_reach_the_caller(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fail-open sink's outage is logged instead of breaking the operation."""

    class FlakyAuditHook(BrokenSinkHook):
        """Best-effort sink that must not break callers when it fails."""

        fail_open = True

    recording = RecordingSinkHook()
    memory = Memory(hooks=[FlakyAuditHook(), recording])

    with caplog.at_level(logging.ERROR):
        root = memory.put(type="root", value="root")
        child = memory.put(parent=root, type="note", value="child")
        memory.children(root)
        memory.delete(root)

    assert recording.stored == [root.id, child.id]
    assert recording.deleted == [root.id, child.id]
    assert recording.disclosed == [TraversalKind.CHILDREN]
    reported = [record.getMessage() for record in caplog.records]
    assert sum("after_put" in message for message in reported) == 2
    assert sum("after_delete" in message for message in reported) == 2
    assert sum("after_traverse" in message for message in reported) == 1
    assert all("FlakyAuditHook" in message for message in reported)
    assert all(record.exc_info is not None for record in caplog.records)


def test_fail_open_does_not_contain_transforming_failures() -> None:
    """A fail-open hook that fails while transforming still stops the operation."""

    class FlakyTransformHook(MemoryHook):
        """Declare fail-open while failing in transforming callbacks."""

        fail_open = True

        def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
            """Fail while rewriting a proposed block."""

            raise RuntimeError("transform down")

        def after_search(self, context: HookContext, query, results) -> list[SearchResult]:
            """Fail while filtering results the caller would receive."""

            raise RuntimeError("transform down")

    memory = Memory(hooks=[FlakyTransformHook()])

    with pytest.raises(RuntimeError, match="transform down"):
        memory.put(type="note", value="rejected")
    with pytest.raises(RuntimeError, match="transform down"):
        memory.search("needle")


def test_fail_open_is_decided_per_hook() -> None:
    """Containing one sink's failure does not contain another hook's failure."""

    class FlakyAuditHook(BrokenSinkHook):
        """Sink whose failures stay contained."""

        fail_open = True

    class StrictAuditHook(MemoryHook):
        """Sink whose failures must still reach the caller."""

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Fail as a hook that has not opted out of breaking callers."""

            raise RuntimeError("strict sink down")

    memory = Memory(hooks=[FlakyAuditHook(), StrictAuditHook()])

    with pytest.raises(RuntimeError, match="strict sink down") as raised:
        memory.put(type="note", value="one")

    assert getattr(raised.value, "__notes__", []) == []


class RecordingTraversalHook(MemoryHook):
    """Hook that records every structural read reported to it."""

    def __init__(self) -> None:
        self.before: list[Traversal] = []
        self.after: list[Traversal] = []
        self.contexts: list[HookContext] = []

    def before_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record the read that is about to run."""

        self.before.append(traversal)
        self.contexts.append(context)

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Record the read and everything it disclosed."""

        self.after.append(traversal)
        self.contexts.append(context)


def test_traverse_hooks_cover_children_ancestors_links_and_containment() -> None:
    """Every structural read reports what it followed and what it disclosed."""

    hook = RecordingTraversalHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    note = memory.put(parent=root, type="note", value="one")
    link = memory.add_link(note, root, "related")

    memory.children(root)
    memory.ancestors(note)
    memory.list_links(note, relation="related", direction="out")
    memory.contains(root, note)

    children, ancestors, links, contains = hook.after
    assert [traversal.kind for traversal in hook.after] == [
        TraversalKind.CHILDREN,
        TraversalKind.ANCESTORS,
        TraversalKind.LINKS,
        TraversalKind.CONTAINS,
    ]
    assert [traversal.origin_id for traversal in hook.after] == [
        root.id,
        note.id,
        note.id,
        root.id,
    ]
    assert [block.id for block in children.blocks] == [note.id]
    assert [block.id for block in ancestors.blocks] == [root.id]
    assert [stored.id for stored in links.links] == [link.id]
    assert (links.relation, links.direction) == ("related", "out")
    assert (contains.target_id, contains.contained) == (note.id, True)


def test_before_traverse_names_a_read_that_has_disclosed_nothing_yet() -> None:
    """The before callback describes the read; only the after callback carries results."""

    hook = RecordingTraversalHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    note = memory.put(parent=root, type="note", value="one")
    memory.add_link(note, root, "related")

    memory.children(root)
    memory.ancestors(note)
    memory.list_links(note)
    memory.contains(root, note)

    assert [(traversal.blocks, traversal.links, traversal.contained) for traversal in hook.before] == [((), (), None)] * 4
    assert [traversal.kind for traversal in hook.before] == [traversal.kind for traversal in hook.after]


def test_traverse_hooks_separate_one_block_of_mounts_from_a_subtree_scan() -> None:
    """Mount reads name the mount relation and distinguish a scan from a listing."""

    hook = RecordingTraversalHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    branch = memory.put(parent=root, type="branch", value="branch")
    outside = memory.put(type="other", value="outside")
    root_mount = memory.add_mount(root, outside)
    branch_mount = memory.add_mount(branch, outside)
    hook.before.clear()
    hook.after.clear()

    assert memory.list_mounts(root) == [root_mount]
    assert memory.discover_mounts(root) == [root_mount, branch_mount]

    listed, discovered = hook.after
    assert [traversal.kind for traversal in hook.before] == [
        TraversalKind.MOUNTS,
        TraversalKind.SUBTREE_MOUNTS,
    ]
    assert [traversal.links for traversal in hook.before] == [(), ()]
    assert (listed.origin_id, [link.id for link in listed.links]) == (root.id, [root_mount.id])
    assert (discovered.origin_id, [link.id for link in discovered.links]) == (
        root.id,
        [root_mount.id, branch_mount.id],
    )
    assert {(traversal.relation, traversal.direction) for traversal in hook.after} == {(LinkRelations.MOUNT, "out")}


def test_traversal_events_report_only_what_policy_disclosed() -> None:
    """Filtered blocks are absent from the traversal rather than reported as refusals."""

    class DenialAwareHook(RecordingTraversalHook):
        """Record refusals alongside the reads that were allowed."""

        def __init__(self) -> None:
            super().__init__()
            self.denials: list[HookOperation] = []

        def on_denied(self, context: HookContext, operation, subject_id, reason) -> None:
            """Record the operation that was refused."""

            self.denials.append(context.operation)

    hook = DenialAwareHook()
    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    visible = owner.put(parent=root, type="note", value="public", policy=MemoryPolicy.public())
    hidden = owner.put(parent=root, type="note", value="secret")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
    )

    assert [block.id for block in reader.children(root)] == [visible.id]
    assert reader.contains(root, hidden) is False

    children, contains = hook.after
    assert [block.id for block in children.blocks] == [visible.id]
    assert (contains.target_id, contains.contained) == (hidden.id, False)
    assert hook.denials == []


def test_traversal_events_pair_with_the_operation_that_performed_them() -> None:
    """Both traversal callbacks and the terminal event share one operation."""

    class PairedHook(RecordingTraversalHook):
        """Record the terminal event of every traversal operation."""

        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[HookOperation, HookOutcome]] = []

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record the operation and its outcome."""

            self.events.append((context.operation, outcome))

    hook = PairedHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    memory.put(parent=root, type="note", value="one")
    hook.events.clear()

    memory.children(root)
    memory.contains(root, root)

    assert hook.events == [
        (HookOperation.CHILDREN, HookOutcome.SUCCESS),
        (HookOperation.CONTAINS, HookOutcome.SUCCESS),
    ]
    assert [context.operation for context in hook.contexts] == [
        HookOperation.CHILDREN,
        HookOperation.CHILDREN,
        HookOperation.CONTAINS,
        HookOperation.CONTAINS,
    ]
    assert len({context.operation_id for context in hook.contexts}) == 2


def test_refused_traversal_reports_no_disclosure() -> None:
    """A read that policy refuses never reaches the traversal callbacks."""

    class TerminalTraversalHook(RecordingTraversalHook):
        """Record the outcome of every traversal operation."""

        def __init__(self) -> None:
            super().__init__()
            self.outcomes: list[tuple[HookOperation, HookOutcome]] = []

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record the operation and its outcome."""

            self.outcomes.append((context.operation, outcome))

    hook = TerminalTraversalHook()
    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    child = owner.put(parent=root, type="note", value="secret")
    attacker = Memory(
        backend=owner.backend,
        principal="attacker",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
    )

    for attempt in (
        lambda: attacker.children(root),
        lambda: attacker.ancestors(child),
        lambda: attacker.list_links(root),
        lambda: attacker.contains(root, child),
        lambda: attacker.list_mounts(root),
        lambda: attacker.discover_mounts(root),
    ):
        with pytest.raises(AccessDenied):
            attempt()

    assert (hook.before, hook.after) == ([], [])
    assert hook.outcomes == [
        (HookOperation.CHILDREN, HookOutcome.DENIED),
        (HookOperation.ANCESTORS, HookOutcome.DENIED),
        (HookOperation.LIST_LINKS, HookOutcome.DENIED),
        (HookOperation.CONTAINS, HookOutcome.DENIED),
        (HookOperation.LIST_MOUNTS, HookOutcome.DENIED),
        (HookOperation.DISCOVER_MOUNTS, HookOutcome.DENIED),
    ]


def test_subtree_walk_reports_one_traversal_per_visited_block() -> None:
    """A facade walk over a subtree is audited as the reads it performs."""

    hook = RecordingTraversalHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    branch = memory.put(parent=root, type="branch", value="branch")
    leaf = memory.put(parent=branch, type="note", value="leaf")
    tree = OwnershipTree(memory, root=root)
    hook.after.clear()

    assert [block.id for block in tree.descendants()] == [branch.id, leaf.id]

    assert [(traversal.origin_id, tuple(block.id for block in traversal.blocks)) for traversal in hook.after] == [
        (root.id, (branch.id,)),
        (branch.id, (leaf.id,)),
        (leaf.id, ()),
    ]


def test_traversal_through_a_handle_reports_its_sandbox() -> None:
    """Reads performed through a handle carry the handle's boundary labels."""

    hook = RecordingTraversalHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    note = memory.put(parent=root, type="note", value="one")
    handle = memory.get_handle(root=root)
    hook.contexts.clear()

    assert [block.id for block in handle.children()] == [note.id]

    assert {context.attributes["handle.root"] for context in hook.contexts} == {root.id}


class RecordingTerminalHook(MemoryHook):
    """Hook that records the terminal event of every operation it observes."""

    def __init__(self) -> None:
        self.events: list[tuple[HookOperation, HookOutcome, str | None]] = []
        self.contexts: list[HookContext] = []

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Record the operation, outcome, and error type."""

        self.events.append((context.operation, outcome, type(error).__name__ if error is not None else None))
        self.contexts.append(context)


def test_operation_end_reports_success_for_committed_operations() -> None:
    """Successful operations report one terminal event with no error."""

    hook = RecordingTerminalHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="collection", value="root")
    note = memory.put(parent=root, type="note", value={"status": "new"})
    memory.get(note)
    memory.set(note, {"status": "open"})
    memory.update(note, metadata={"reviewed": True})
    memory.search("open")
    link = memory.add_link(note, root, "related")
    memory.update_link(link, metadata={"checked": True})
    memory.delete(note)

    assert [operation for operation, _, _ in hook.events] == [
        HookOperation.PUT,
        HookOperation.PUT,
        HookOperation.GET,
        HookOperation.SET,
        HookOperation.UPDATE,
        HookOperation.SEARCH,
        HookOperation.ADD_LINK,
        HookOperation.UPDATE_LINK,
        HookOperation.DELETE,
    ]
    assert {outcome for _, outcome, _ in hook.events} == {HookOutcome.SUCCESS}
    assert {error for _, _, error in hook.events} == {None}


def test_operation_end_shares_the_operation_id_with_paired_callbacks() -> None:
    """The terminal event carries the same context as before and after callbacks."""

    class PairedHook(MemoryHook):
        """Record operation ids seen by each put callback."""

        def __init__(self) -> None:
            self.ids: list[str] = []

        def before_put(self, context: HookContext, block):
            """Record the before id."""

            self.ids.append(context.operation_id)
            return block

        def after_put(self, context: HookContext, block) -> None:
            """Record the after id."""

            self.ids.append(context.operation_id)

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record the terminal id."""

            self.ids.append(context.operation_id)

    hook = PairedHook()
    Memory(hooks=[hook]).put(type="note", value="one")

    assert len(hook.ids) == 3
    assert len(set(hook.ids)) == 1


def test_operation_end_reports_policy_refusals_no_other_callback_observes() -> None:
    """Denied operations report a terminal event even when no before callback ran."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    note = owner.put(parent=root, type="note", value="secret")
    hook = RecordingTerminalHook()
    attacker = Memory(
        backend=owner.backend,
        principal="attacker",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
    )

    attempts = {
        "get": lambda: attacker.get(note),
        "set": lambda: attacker.set(note, "overwritten"),
        "update": lambda: attacker.update(note, metadata={"tampered": True}),
        "delete": lambda: attacker.delete(note),
        "put": lambda: attacker.put(parent=root, type="note", value="injected"),
        "search": lambda: attacker.search("secret", root=root, scope="subtree"),
        "add_link": lambda: attacker.add_link(note, root, "related"),
    }
    denied: list[str] = []
    for label, attempt in attempts.items():
        try:
            attempt()
        except AccessDenied:
            denied.append(label)

    assert denied == list(attempts)
    assert [operation for operation, _, _ in hook.events] == [
        HookOperation.GET,
        HookOperation.SET,
        HookOperation.UPDATE,
        HookOperation.DELETE,
        HookOperation.PUT,
        HookOperation.SEARCH,
        HookOperation.ADD_LINK,
    ]
    assert {outcome for _, outcome, _ in hook.events} == {HookOutcome.DENIED}
    assert {error for _, _, error in hook.events} == {"AccessDenied"}


def test_operation_end_separates_refusals_from_other_failures() -> None:
    """Non-policy failures report `ERROR` and carry the raised exception."""

    hook = RecordingTerminalHook()
    memory = Memory(hooks=[hook])
    note = memory.put(type="note", value="one")

    with pytest.raises(MemoryNotFound):
        memory.set("blk_missing", "value")
    with pytest.raises(TypeError):
        memory.put(type="note", value=object())
    with pytest.raises(MemoryConflict):
        memory.add_acyclic_link(note, note, "derived_into")

    assert [(outcome, error) for _, outcome, error in hook.events[1:]] == [
        (HookOutcome.ERROR, "MemoryNotFound"),
        (HookOutcome.ERROR, "TypeError"),
        (HookOutcome.ERROR, "MemoryConflict"),
    ]


def test_operation_end_always_releases_state_stashed_by_a_before_callback() -> None:
    """A failing operation still reaches the terminal callback, so state cannot leak."""

    class TimingHook(MemoryHook):
        """Track in-flight operations between the before and terminal callbacks."""

        def __init__(self) -> None:
            self.in_flight: dict[str, HookOperation] = {}
            self.completed = 0

        def before_put(self, context: HookContext, block):
            """Start tracking one operation."""

            self.in_flight[context.operation_id] = context.operation
            return block

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Stop tracking the operation regardless of outcome."""

            self.in_flight.pop(context.operation_id, None)
            self.completed += 1

    class VetoHook(MemoryHook):
        """Reject every proposed block."""

        def before_put(self, context: HookContext, block):
            """Fail after the tracking hook has already started."""

            raise RuntimeError("veto")

    timing = TimingHook()
    memory = Memory(hooks=[timing, VetoHook()])
    for _ in range(25):
        with pytest.raises(RuntimeError, match="veto"):
            memory.put(type="note", value="rejected")

    assert timing.in_flight == {}
    assert timing.completed == 25


def test_operation_end_failure_does_not_mask_the_operation_error() -> None:
    """A failing terminal hook cannot replace the exception leaving the operation."""

    class BrokenTerminalHook(MemoryHook):
        """Fail inside the terminal callback."""

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Simulate an unreachable audit sink."""

            raise RuntimeError("audit sink down")

    recording = RecordingTerminalHook()
    memory = Memory(hooks=[BrokenTerminalHook(), recording])

    with pytest.raises(MemoryNotFound) as raised:
        memory.set("blk_missing", "value")

    assert any("audit sink down" in note for note in raised.value.__notes__)
    assert [(outcome, error) for _, outcome, error in recording.events] == [
        (HookOutcome.ERROR, "MemoryNotFound"),
    ]


def test_operation_end_failure_does_not_skip_other_hooks() -> None:
    """One unreliable terminal hook cannot cost another hook its terminal event."""

    class BrokenTerminalHook(MemoryHook):
        """Fail inside the terminal callback."""

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Simulate an unreachable audit sink."""

            raise RuntimeError("audit sink down")

    first = RecordingTerminalHook()
    second = RecordingTerminalHook()
    memory = Memory(hooks=[first, BrokenTerminalHook(), second])

    with pytest.raises(RuntimeError, match="audit sink down"):
        memory.put(type="note", value="one")

    assert [outcome for _, outcome, _ in first.events] == [HookOutcome.SUCCESS]
    assert [outcome for _, outcome, _ in second.events] == [HookOutcome.SUCCESS]


def test_operation_end_reports_every_terminal_hook_failure_once() -> None:
    """Multiple terminal failures surface together instead of hiding each other."""

    class NamedFailingHook(MemoryHook):
        """Fail with an identifiable message."""

        def __init__(self, name: str) -> None:
            self.name = name

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Fail so the registry must report this hook."""

            raise RuntimeError(f"sink {self.name} down")

    memory = Memory(hooks=[NamedFailingHook("a"), NamedFailingHook("b")])

    with pytest.raises(RuntimeError, match="sink a down") as raised:
        memory.put(type="note", value="one")

    assert any("sink b down" in note for note in raised.value.__notes__)


def test_fail_open_contains_terminal_and_denial_failures(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A fail-open sink adds no note to a refusal and no error to a success."""

    class FlakyTerminalHook(MemoryHook):
        """Fail in both unpaired callbacks without breaking callers."""

        fail_open = True

        def on_denied(self, context: HookContext, operation, subject_id, reason) -> None:
            """Fail while reporting a refusal."""

            raise RuntimeError("audit sink down")

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Fail while reporting a terminal event."""

            raise RuntimeError("audit sink down")

    recording = RecordingTerminalHook()
    memory = Memory(principal="owner", hooks=[FlakyTerminalHook(), recording])

    with caplog.at_level(logging.ERROR):
        root = memory.put(type="root", value="root")
        outside = memory.put(type="other", value="outside")
        handle = memory.get_handle(root=root)
        with pytest.raises(AccessDenied) as refusal:
            handle.get(outside)
        with pytest.raises(MemoryNotFound) as missing:
            memory.set("blk_missing", "value")

    for raised in (refusal.value, missing.value):
        notes = getattr(raised, "__notes__", [])
        assert not [note for note in notes if "audit sink down" in note]
    assert [outcome for _, outcome, _ in recording.events[-2:]] == [
        HookOutcome.DENIED,
        HookOutcome.ERROR,
    ]
    assert sum("on_denied" in record.getMessage() for record in caplog.records) == 1


def test_operation_end_respects_suppression() -> None:
    """Suppressed hooks skip the operations inside a window, not the window."""

    first = RecordingTerminalHook()
    second = RecordingTerminalHook()
    registry = HookRegistry([first, second])
    memory = Memory(hooks=registry)

    with registry.suppress(first):
        memory.put(type="note", value="first")
    with registry.suppress():
        memory.put(type="note", value="second")
    memory.put(type="note", value="third")

    suppress = HookOperation.SUPPRESS
    assert [operation for operation, _, _ in first.events] == [
        suppress,
        suppress,
        HookOperation.PUT,
    ]
    assert [operation for operation, _, _ in second.events] == [
        HookOperation.PUT,
        suppress,
        suppress,
        HookOperation.PUT,
    ]


def test_operation_end_can_write_derived_records_without_recursing() -> None:
    """A terminal callback may use memory again; its own callback is skipped."""

    class AuditingHook(MemoryHook):
        """Write one audit block per caller operation."""

        def __init__(self) -> None:
            self.memory: Memory | None = None
            self.observed: list[tuple[HookOperation, HookOrigin, int]] = []

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record the event and persist it through memory."""

            self.observed.append((context.operation, context.origin, context.depth))
            if context.origin == "hook" or self.memory is None:
                return
            self.memory.put(type="audit", value={"operation": str(context.operation), "outcome": str(outcome)})

    hook = AuditingHook()
    memory = Memory(hooks=[hook])
    hook.memory = memory

    memory.put(type="note", value="one")

    assert hook.observed == [(HookOperation.PUT, "caller", 0)]
    assert [result.block.value["operation"] for result in memory.search(types=["audit"])] == ["put"]


def test_operation_end_covers_structure_facade_operations() -> None:
    """Structure facades report terminal events through the shared registry."""

    hook = RecordingTerminalHook()
    memory = Memory(hooks=[hook])
    collection = memory.put(type="collection", value="keys")
    target = memory.put(type="note", value="target")
    keyed = KeyedCollection(memory, cast(KeyedStore, memory.backend), collection=collection)
    address = MemoryAddress((), "one")

    keyed.bind(address, target)
    with pytest.raises(MemoryConflict):
        keyed.bind(address, target)

    assert (HookOperation.KEY_BIND, HookOutcome.SUCCESS, None) in hook.events
    assert (HookOperation.KEY_BIND, HookOutcome.ERROR, "MemoryConflict") in hook.events


def test_no_terminal_event_when_an_operation_never_starts() -> None:
    """An operation rejected by the depth guard reports no terminal event."""

    class DeepHook(MemoryHook):
        """Start a nested operation beyond the configured depth."""

        def __init__(self, registry: HookRegistry) -> None:
            self.registry = registry
            self.terminal: list[HookOperation] = []

        def after_put(self, context: HookContext, block) -> None:
            """Exceed the depth limit from inside a callback."""

            with self.registry.operation(HookOperation.GET):
                pytest.fail("Depth overflow should occur before entering context.")

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record which operations reached a terminal event."""

            self.terminal.append(context.operation)

    registry = HookRegistry(max_depth=0)
    hook = DeepHook(registry)
    registry.register(hook)

    with pytest.raises(HookRecursionError):
        Memory(hooks=registry).put(type="note", value="too deep")

    assert hook.terminal == [HookOperation.PUT]


class RecordingDenialHook(MemoryHook):
    """Hook that records every refusal reported to it."""

    def __init__(self) -> None:
        self.denials: list[tuple[HookOperation, str, str | None]] = []
        self.reasons: list[str] = []
        self.contexts: list[HookContext] = []

    def on_denied(
        self,
        context: HookContext,
        operation: Operation | HandleCapability | str,
        subject_id: str | None,
        reason: str,
    ) -> None:
        """Record the attempted operation, the refusing check, and the subject."""

        self.denials.append((context.operation, str(operation), subject_id))
        self.reasons.append(reason)
        self.contexts.append(context)


def _private_memory(hook: MemoryHook) -> tuple[Memory, Memory, MemoryBlock, MemoryBlock]:
    """Return an owner memory, a denied memory, and two owner-private blocks."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    child = owner.put(parent=root, type="note", value="secret")
    attacker = Memory(
        backend=owner.backend,
        principal="attacker",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
    )
    return owner, attacker, root, child


def test_on_denied_reports_the_checked_policy_operation_and_subject() -> None:
    """Policy refusals name the operation that was checked and the block."""

    hook = RecordingDenialHook()
    _owner, attacker, root, child = _private_memory(hook)

    attempts = {
        "get": lambda: attacker.get(child),
        "set": lambda: attacker.set(child, "overwritten"),
        "update": lambda: attacker.update(child, metadata={"tampered": True}),
        "delete": lambda: attacker.delete(child),
        "put": lambda: attacker.put(parent=root, type="note", value="injected"),
        "search": lambda: attacker.search("secret", root=root, scope="subtree"),
        "add_link": lambda: attacker.add_link(child, root, "related"),
    }
    denied: list[str] = []
    for label, attempt in attempts.items():
        try:
            attempt()
        except AccessDenied:
            denied.append(label)

    assert denied == list(attempts)
    assert hook.denials == [
        (HookOperation.GET, Operation.READ, child.id),
        (HookOperation.SET, Operation.WRITE, child.id),
        (HookOperation.UPDATE, Operation.WRITE, child.id),
        (HookOperation.DELETE, Operation.WRITE, child.id),
        (HookOperation.PUT, Operation.WRITE, root.id),
        (HookOperation.SEARCH, Operation.SEARCH, root.id),
        (HookOperation.ADD_LINK, Operation.WRITE, child.id),
    ]
    assert all("cannot" in reason for reason in hook.reasons)


def test_on_denied_covers_calls_without_paired_callbacks() -> None:
    """Traversal, listing, and handle creation report refusals."""

    hook = RecordingDenialHook()
    owner, attacker, root, child = _private_memory(hook)
    mount = owner.add_mount(root, child)

    attempts = {
        "children": lambda: attacker.children(root),
        "ancestors": lambda: attacker.ancestors(child),
        "list_links": lambda: attacker.list_links(root),
        "contains": lambda: attacker.contains(root, child),
        "list_mounts": lambda: attacker.list_mounts(root),
        "discover_mounts": lambda: attacker.discover_mounts(root),
        "resolve_mount": lambda: attacker.resolve_mount(mount),
        "get_handle": lambda: attacker.get_handle(root=root),
    }
    denied: list[str] = []
    for label, attempt in attempts.items():
        try:
            attempt()
        except AccessDenied:
            denied.append(label)

    assert denied == list(attempts)
    assert hook.denials == [
        (HookOperation.CHILDREN, Operation.TRAVERSE_DOWN, root.id),
        (HookOperation.ANCESTORS, Operation.TRAVERSE_UP, child.id),
        (HookOperation.LIST_LINKS, Operation.READ, root.id),
        (HookOperation.CONTAINS, Operation.TRAVERSE_DOWN, root.id),
        (HookOperation.LIST_MOUNTS, Operation.READ, root.id),
        (HookOperation.DISCOVER_MOUNTS, Operation.SEARCH, root.id),
        (HookOperation.RESOLVE_MOUNT, Operation.READ, root.id),
        (HookOperation.CREATE_HANDLE, Operation.READ, root.id),
    ]


def test_on_denied_reports_handle_boundary_escapes() -> None:
    """Blocks outside a handle root report the subtree boundary and the target."""

    hook = RecordingDenialHook()
    memory = Memory(principal="owner", hooks=[hook])
    root = memory.put(type="root", value="root")
    inside = memory.put(parent=root, type="note", value="inside")
    outside = memory.put(type="other-tenant", value="outside")
    handle = memory.get_handle(root=inside, allow_mounts=True)

    attempts = {
        "get": lambda: handle.get(outside),
        "set": lambda: handle.set(outside, "x"),
        "update": lambda: handle.update(outside, metadata={"x": 1}),
        "delete": lambda: handle.delete(outside),
        "put": lambda: handle.put(parent=outside, type="note", value="x"),
        "children": lambda: handle.children(outside),
        "list_links": lambda: handle.list_links(outside),
        "add_link": lambda: handle.add_link(outside, inside, "related"),
        "add_mount": lambda: handle.add_mount(outside, inside),
        "search": lambda: handle.search("x", root=outside),
    }
    denied: list[str] = []
    for label, attempt in attempts.items():
        try:
            attempt()
        except AccessDenied:
            denied.append(label)

    assert denied == list(attempts)
    assert [operation for operation, _, _ in hook.denials] == [
        HookOperation.GET,
        HookOperation.SET,
        HookOperation.UPDATE,
        HookOperation.DELETE,
        HookOperation.PUT,
        HookOperation.CHILDREN,
        HookOperation.LIST_LINKS,
        HookOperation.ADD_LINK,
        HookOperation.ADD_MOUNT,
        HookOperation.SEARCH,
    ]
    assert {check for _, check, _ in hook.denials} == {HandleCapability.SUBTREE}
    assert {subject for _, _, subject in hook.denials} == {outside.id}


def test_on_denied_reports_handle_capability_refusals() -> None:
    """Disabled handle capabilities report the capability rather than a policy."""

    hook = RecordingDenialHook()
    memory = Memory(principal="owner", hooks=[hook])
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)
    unbounded = memory.get_unbounded_handle()

    attempts = {
        "global_search": lambda: handle.search("x", scope="global"),
        "ancestor_search": lambda: handle.search("x", scope="ancestors"),
        "ancestors": lambda: handle.ancestors(),
        "mounts": lambda: handle.add_mount(root, root),
        "policy_override": lambda: handle.put(type="note", value="x", policy=MemoryPolicy.public()),
        "unbounded_children": lambda: unbounded.children(),
        "unbounded_search": lambda: unbounded.search("x", scope="subtree"),
    }
    denied: list[str] = []
    for label, attempt in attempts.items():
        try:
            attempt()
        except AccessDenied:
            denied.append(label)

    assert denied == list(attempts)
    assert hook.denials == [
        (HookOperation.SEARCH, HandleCapability.GLOBAL_SEARCH, root.id),
        (HookOperation.SEARCH, HandleCapability.ANCESTORS, root.id),
        (HookOperation.ANCESTORS, HandleCapability.ANCESTORS, root.id),
        (HookOperation.ADD_MOUNT, HandleCapability.MOUNTS, root.id),
        (HookOperation.PUT, HandleCapability.POLICY_OVERRIDE, root.id),
        (HookOperation.CHILDREN, HandleCapability.ROOT_REQUIRED, None),
        (HookOperation.SEARCH, HandleCapability.ROOT_REQUIRED, None),
    ]


def test_on_denied_reports_child_handle_escalation_attempts() -> None:
    """A child handle that tries to widen access reports the capability it wanted."""

    hook = RecordingDenialHook()
    memory = Memory(principal="owner", hooks=[hook])
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)

    attempts = {
        "principal": lambda: handle.get_handle(principal="someone-else"),
        "allow_up": lambda: handle.get_handle(allow_up=True),
        "allow_global": lambda: handle.get_handle(allow_global=True),
        "allow_mounts": lambda: handle.get_handle(allow_mounts=True),
        "allow_policy_override": lambda: handle.get_handle(allow_policy_override=True),
    }
    denied: list[str] = []
    for label, attempt in attempts.items():
        try:
            attempt()
        except AccessDenied:
            denied.append(label)

    assert denied == list(attempts)
    assert hook.denials == [
        (HookOperation.CREATE_HANDLE, HandleCapability.PRINCIPAL, root.id),
        (HookOperation.CREATE_HANDLE, HandleCapability.ANCESTORS, root.id),
        (HookOperation.CREATE_HANDLE, HandleCapability.GLOBAL_SEARCH, root.id),
        (HookOperation.CREATE_HANDLE, HandleCapability.MOUNTS, root.id),
        (HookOperation.CREATE_HANDLE, HandleCapability.POLICY_OVERRIDE, root.id),
    ]


def test_on_denied_reports_the_acting_handle_principal() -> None:
    """Refusals carry the principal that attempted the operation."""

    class PrincipalHook(RecordingDenialHook):
        """Record refusals with their principal."""

    hook = PrincipalHook()
    memory = Memory(principal="owner", hooks=[hook])
    root = memory.put(type="root", value="root", policy=MemoryPolicy.public())
    outside = memory.put(type="other", value="outside")
    agent_handle = memory.get_handle(root=root, principal="agent-7")

    with pytest.raises(AccessDenied):
        agent_handle.get(outside)
    with pytest.raises(AccessDenied):
        memory.get_handle(root=outside, principal="agent-9")

    assert [context.principal_id for context in hook.contexts] == ["agent-7", "agent-9"]


def test_on_denied_pairs_with_a_denied_terminal_event() -> None:
    """Each refusal shares one operation id with a denied terminal event."""

    class PairedHook(MemoryHook):
        """Record refusal and terminal ids for one operation."""

        def __init__(self) -> None:
            self.denied_ids: list[str] = []
            self.terminal: list[tuple[str, HookOutcome]] = []

        def on_denied(self, context: HookContext, operation, subject_id, reason) -> None:
            """Record the refusal id."""

            self.denied_ids.append(context.operation_id)

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record the terminal id and outcome."""

            self.terminal.append((context.operation_id, outcome))

    hook = PairedHook()
    memory = Memory(principal="owner", hooks=[hook])
    root = memory.put(type="root", value="root")
    outside = memory.put(type="other", value="outside")
    handle = memory.get_handle(root=root)

    with pytest.raises(AccessDenied):
        handle.get(outside)

    assert len(hook.denied_ids) == 1
    assert hook.terminal[-1] == (hook.denied_ids[0], HookOutcome.DENIED)


def test_on_denied_hook_failure_cannot_replace_the_refusal() -> None:
    """A failing denial hook never converts a refusal into another error."""

    class BrokenDenialHook(MemoryHook):
        """Fail while reporting a refusal."""

        def on_denied(self, context: HookContext, operation, subject_id, reason) -> None:
            """Simulate an unreachable audit sink."""

            raise RuntimeError("audit sink down")

    recording = RecordingDenialHook()
    memory = Memory(principal="owner", hooks=[BrokenDenialHook(), recording])
    root = memory.put(type="root", value="root")
    outside = memory.put(type="other", value="outside")
    handle = memory.get_handle(root=root)

    with pytest.raises(AccessDenied) as policy_refusal:
        handle.get(outside)
    with pytest.raises(AccessDenied) as boundary_refusal:
        memory.get_handle(root=outside, principal="stranger")

    for refusal in (policy_refusal, boundary_refusal):
        assert any("audit sink down" in note for note in refusal.value.__notes__)
    assert len(recording.denials) == 2


def test_on_denied_is_not_dispatched_for_allowed_or_filtered_operations() -> None:
    """Allowed work and ordinary search filtering report no refusal."""

    hook = RecordingDenialHook()
    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    owner.put(parent=root, type="note", value="needle visible", policy=MemoryPolicy.public())
    owner.put(parent=root, type="note", value="needle hidden")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
    )

    results = reader.search("needle", root=root, scope="subtree")
    reader.children(root)
    reader.contains(root, root)

    assert len(results) == 1
    assert hook.denials == []


def test_on_denied_respects_suppression() -> None:
    """Suppressed hooks and registries skip refusal reporting."""

    first = RecordingDenialHook()
    second = RecordingDenialHook()
    registry = HookRegistry([first, second])
    memory = Memory(principal="owner", hooks=registry)
    root = memory.put(type="root", value="root")
    outside = memory.put(type="other", value="outside")
    handle = memory.get_handle(root=root)

    with registry.suppress(first), pytest.raises(AccessDenied):
        handle.get(outside)
    with registry.suppress(), pytest.raises(AccessDenied):
        handle.get(outside)
    with pytest.raises(AccessDenied):
        handle.get(outside)

    assert len(first.denials) == 1
    assert len(second.denials) == 2


def test_recursive_delete_reports_the_first_refused_block() -> None:
    """A denied subtree delete names the block that refused it."""

    hook = RecordingDenialHook()
    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    owner.put(parent=root, type="note", value="unwritable")
    deleter = Memory(
        backend=owner.backend,
        principal="deleter",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
    )

    with pytest.raises(AccessDenied):
        deleter.delete(root)

    assert [operation for operation, _, _ in hook.denials] == [HookOperation.DELETE]
    assert owner.get(root) is not None


def test_unreportable_refusals_still_reach_the_caller_as_access_denied() -> None:
    """A registry that cannot report a refusal never changes the caller's error."""

    class DeepHook(MemoryHook):
        """Attempt refused operations from beyond the depth limit."""

        def __init__(self) -> None:
            self.attempts: list[Any] = []
            self.errors: list[str] = []
            self.notes: list[str] = []

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Trigger the handle and policy refusal paths from inside a callback."""

            for attempt in self.attempts:
                try:
                    attempt()
                except AccessDenied as raised:
                    self.errors.append(type(raised).__name__)
                    self.notes.extend(getattr(raised, "__notes__", []))

    hook = DeepHook()
    registry = HookRegistry([hook], max_depth=0)
    owner = Memory(principal="owner", hooks=registry)
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    outside = owner.put(type="other", value="outside")
    agent = Memory(
        backend=owner.backend,
        principal="agent",
        policy_engine=owner.policy_engine,
        hooks=registry,
        codecs=owner.codecs,
    )
    agent_handle = agent.get_handle(root=root)
    hook.attempts = [
        lambda: agent_handle.get(outside.id),
        lambda: agent.get_handle(root=outside.id),
    ]

    agent.put(parent=root, type="note", value="triggers the callback")

    assert hook.errors == ["AccessDenied", "AccessDenied"]
    assert [note for note in hook.notes if "could not be reported" in note] == hook.notes
    assert len(hook.notes) == 2


class RecordingContextHook(MemoryHook):
    """Hook that records the context of every operation it observes."""

    def __init__(self) -> None:
        self.contexts: list[HookContext] = []
        self.lock = Lock()

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Record one context per finished operation."""

        with self.lock:
            self.contexts.append(context)

    def attributes_for(self, operation: HookOperation) -> list[dict[str, str]]:
        """Return recorded attributes for one kind of operation."""

        return [dict(context.attributes) for context in self.contexts if context.operation == operation]


def test_context_reports_no_attributes_and_one_shared_start_time_by_default() -> None:
    """A context carries an immutable empty mapping and the operation's start."""

    class PairedContextHook(MemoryHook):
        """Collect the context seen by each put callback."""

        def __init__(self) -> None:
            self.contexts: list[HookContext] = []

        def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
            """Record the before context."""

            self.contexts.append(context)
            return block

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Record the after context."""

            self.contexts.append(context)

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Record the terminal context."""

            self.contexts.append(context)

    hook = PairedContextHook()
    opened = datetime.now(UTC)
    Memory(hooks=[hook]).put(type="note", value="one")
    closed = datetime.now(UTC)

    context = hook.contexts[0]
    assert [seen.started_at for seen in hook.contexts] == [context.started_at] * 3
    assert opened <= context.started_at <= closed
    assert dict(context.attributes) == {}
    with pytest.raises(TypeError):
        cast(dict[str, str], context.attributes)["injected"] = "value"


def test_memory_attributes_reach_every_operation_it_opens() -> None:
    """Configured memory attributes label every operation and refusal context."""

    hook = RecordingContextHook()
    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    agent = Memory(
        backend=owner.backend,
        principal="agent",
        policy_engine=owner.policy_engine,
        hooks=[hook],
        codecs=owner.codecs,
        attributes={"service": "genie", "tenant": "acme"},
    )
    note = agent.put(type="note", value="one")
    agent.get(note)
    agent.search("one")
    with pytest.raises(AccessDenied):
        agent.get(root)

    assert len(hook.contexts) == 4
    assert all(dict(context.attributes) == {"service": "genie", "tenant": "acme"} for context in hook.contexts)


@pytest.mark.parametrize(
    "attributes",
    [{"run": 7}, {7: "run"}, "run=7", [("run", "7")]],
)
def test_memory_rejects_attributes_that_are_not_string_pairs(attributes: object) -> None:
    """Attributes are validated when memory is configured, not at first use."""

    with pytest.raises(TypeError, match="attributes must be a mapping"):
        Memory(attributes=cast(Any, attributes))


def test_scope_groups_the_operations_of_one_caller_defined_span() -> None:
    """Operations inside a scope become its children and inherit its labels."""

    hook = RecordingContextHook()
    memory = Memory(hooks=[hook])
    with memory.hooks.scope("agent.turn", attributes={"run_id": "r1", "turn": "7"}) as span:
        note = memory.put(type="note", value="one")
        memory.get(note)
    memory.get(note)

    inside, outside = hook.contexts[:2], hook.contexts[2:]
    assert (span.is_scope, span.operation, span.scope_name) == (
        True,
        HookOperation.SCOPE,
        "agent.turn",
    )
    assert {context.scope_name for context in inside} == {None}
    assert {context.parent_operation_id for context in inside} == {span.operation_id}
    assert {context.depth for context in inside} == {1}
    assert {context.origin for context in inside} == {"caller"}
    assert all(dict(context.attributes) == {"run_id": "r1", "turn": "7"} for context in inside)
    assert [context.parent_operation_id for context in outside] == [None]
    assert [dict(context.attributes) for context in outside] == [{}]


def test_scope_attributes_reach_nested_and_hook_originated_operations() -> None:
    """Everything caused inside a scope inherits it, whatever the nesting depth."""

    class DerivingHook(RecordingContextHook):
        """Write one derived block from inside a callback."""

        def __init__(self) -> None:
            super().__init__()
            self.memory: Memory | None = None

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Record an audit block for caller-initiated notes."""

            if block.type == "note" and self.memory is not None:
                self.memory.put(type="audit", value={"block": block.id})

    hook = DerivingHook()
    backend = InMemoryBackend()
    memory = Memory(backend=backend, hooks=[hook])
    hook.memory = memory
    collection = memory.put(type="collection", value="keys")
    keyed = KeyedCollection(memory, backend, collection=collection)
    hook.contexts.clear()

    with memory.hooks.scope("agent.turn", attributes={"run_id": "r1"}):
        note = memory.put(type="note", value="one")
        keyed.bind(MemoryAddress((), "one"), note)

    assert all(dict(context.attributes) == {"run_id": "r1"} for context in hook.contexts)
    derived = next(context for context in hook.contexts if context.operation == HookOperation.PUT)
    assert (derived.origin, derived.depth) == ("hook", 2)
    assert HookOperation.KEY_BIND in {context.operation for context in hook.contexts}


def test_scope_dispatches_no_callbacks_of_its_own() -> None:
    """A scope opens context without becoming an observable operation."""

    class CountingHook(RecordingContextHook):
        """Count every callback the registry dispatches."""

        def __init__(self) -> None:
            super().__init__()
            self.calls: list[str] = []

        def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
            """Count one before callback."""

            self.calls.append("before_put")
            return block

        def on_operation_end(self, context: HookContext, outcome, error) -> None:
            """Count one terminal event and record its context."""

            self.calls.append("on_operation_end")
            super().on_operation_end(context, outcome, error)

    hook = CountingHook()
    memory = Memory(hooks=[hook])
    with memory.hooks.scope("agent.turn"):
        pass

    assert hook.calls == []
    assert hook.contexts == []

    with memory.hooks.scope("agent.turn"):
        memory.put(type="note", value="one")

    assert hook.calls == ["before_put", "on_operation_end"]
    assert [context.operation for context in hook.contexts] == [HookOperation.PUT]


def test_nested_scopes_inherit_and_override_attributes() -> None:
    """An inner scope adds labels and replaces the outer ones it repeats."""

    hook = RecordingContextHook()
    memory = Memory(hooks=[hook])
    with memory.hooks.scope("agent.run", attributes={"run_id": "r1", "turn": "6"}) as run, memory.hooks.scope("agent.turn", attributes={"turn": "7"}) as turn:
        memory.put(type="note", value="one")

    context = hook.contexts[0]
    assert turn.parent_operation_id == run.operation_id
    assert dict(turn.attributes) == {"run_id": "r1", "turn": "7"}
    assert context.parent_operation_id == turn.operation_id
    assert context.depth == 2
    assert dict(context.attributes) == {"run_id": "r1", "turn": "7"}


def test_operation_attributes_extend_the_ones_a_scope_provides() -> None:
    """Operation attributes and principals refine what the scope established."""

    registry = HookRegistry()
    with registry.scope(
        "agent.turn",
        principal_id="agent",
        attributes={"run_id": "r1", "turn": "7"},
    ):
        with registry.operation(
            HookOperation.PUT,
            attributes={"turn": "8", "tool": "search"},
        ) as inherited:
            pass
        with registry.operation(HookOperation.GET, principal_id="auditor") as replaced:
            pass

    assert dict(inherited.attributes) == {"run_id": "r1", "turn": "8", "tool": "search"}
    assert inherited.principal_id == "agent"
    assert replaced.principal_id == "auditor"


def test_operations_cannot_be_opened_as_spans() -> None:
    """`HookOperation.SCOPE` belongs to `scope`, which dispatches nothing."""

    registry = HookRegistry()
    for operation in (HookOperation.SCOPE, "scope"):
        with pytest.raises(ValueError, match="Open it with HookRegistry.scope"), registry.operation(operation):
            pytest.fail("A span must not start as a dispatched operation.")


@pytest.mark.parametrize(
    ("operation", "scope_name", "message"),
    [
        (HookOperation.SCOPE, None, "requires a scope_name"),
        (HookOperation.PUT, "agent.turn", "can carry a scope_name"),
    ],
)
def test_context_pairs_the_scope_name_with_the_scope_operation(
    operation: HookOperation,
    scope_name: str | None,
    message: str,
) -> None:
    """A context is either a named span or a performed operation, never both."""

    with pytest.raises(ValueError, match=message):
        HookContext(
            operation_id="hookop_test",
            parent_operation_id=None,
            operation=operation,
            origin="caller",
            depth=0,
            scope_name=scope_name,
        )


def test_scope_context_cannot_dispatch_hook_callbacks() -> None:
    """Scopes group operations, so they cannot stand in for one."""

    registry = HookRegistry([MemoryHook()])
    block = MemoryBlock(type="note", value="one")
    with registry.scope("agent.turn") as span:
        with pytest.raises(ValueError, match="cannot be dispatched with a scope"):
            registry.before_put(span, block)
        with registry.operation(HookOperation.PUT) as context:
            assert context.is_scope is False
            registry.before_put(context, block)


@pytest.mark.parametrize("name", [None, 7, HookOperation.PUT.value.encode()])
def test_scope_rejects_names_that_are_not_strings(name: object) -> None:
    """A scope name is a caller-defined string."""

    registry = HookRegistry()
    with pytest.raises(TypeError, match="Scope name must be a string"), registry.scope(cast(Any, name)):
        pass


@pytest.mark.parametrize("name", ["", "   "])
def test_scope_rejects_empty_names(name: str) -> None:
    """An unnamed scope would group operations under nothing."""

    registry = HookRegistry()
    with pytest.raises(ValueError, match="Scope name must not be empty"), registry.scope(name):
        pass


def test_scope_context_is_released_after_an_error() -> None:
    """A failing scope leaves later operations at the top level."""

    hook = RecordingContextHook()
    memory = Memory(hooks=[hook])
    with pytest.raises(RuntimeError), memory.hooks.scope("agent.turn", attributes={"run_id": "r1"}):
        memory.put(type="note", value="one")
        raise RuntimeError("the turn failed")
    memory.put(type="note", value="two")

    failed, recovered = hook.contexts
    assert (failed.depth, dict(failed.attributes)) == (1, {"run_id": "r1"})
    assert (recovered.depth, recovered.parent_operation_id) == (0, None)
    assert dict(recovered.attributes) == {}


def test_scopes_are_isolated_across_concurrent_runs() -> None:
    """Concurrent agent runs never observe each other's grouping labels."""

    hook = RecordingContextHook()
    memory = Memory(hooks=[hook])

    def run(index: int) -> None:
        """Perform one labeled operation in its own scope."""

        with memory.hooks.scope("agent.turn", attributes={"run_id": f"r{index}"}):
            memory.put(type="note", value=index)

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(run, range(24)))

    assert len(hook.contexts) == 24
    assert {context.attributes["run_id"] for context in hook.contexts} == {f"r{index}" for index in range(24)}
    assert len({context.parent_operation_id for context in hook.contexts}) == 24


def test_scope_opened_inside_a_hook_respects_the_depth_limit() -> None:
    """Hook-originated scopes are bounded like hook-originated operations."""

    class ScopingHook(MemoryHook):
        """Open a scope from inside a callback."""

        def __init__(self) -> None:
            self.registry = HookRegistry([self], max_depth=0)
            self.errors: list[str] = []

        def after_put(self, context: HookContext, block: MemoryBlock) -> None:
            """Record what happens when the scope exceeds the limit."""

            try:
                with self.registry.scope("audit.batch"):
                    pass
            except HookRecursionError as raised:
                self.errors.append(type(raised).__name__)

    hook = ScopingHook()
    Memory(hooks=hook.registry).put(type="note", value="one")

    assert hook.errors == ["HookRecursionError"]


def test_handle_attributes_distinguish_sandboxes_that_share_a_principal() -> None:
    """The trail records which handle performed the work, not only who did."""

    hook = RecordingContextHook()
    memory = Memory(principal="agent", hooks=[hook], attributes={"service": "genie"})
    first_root = memory.put(type="root", value="first")
    second_root = memory.put(type="root", value="second")
    narrow = memory.get_handle(root=first_root)
    wide = memory.get_handle(root=second_root, allow_up=True, allow_global=True)
    hook.contexts.clear()

    narrow.put(type="note", value="one")
    wide.put(type="note", value="two")
    with pytest.raises(AccessDenied):
        narrow.get(second_root.id)

    written = hook.attributes_for(HookOperation.PUT)
    refused = hook.attributes_for(HookOperation.GET)
    assert {context.principal_id for context in hook.contexts} == {"agent"}
    assert written[0] == {
        "service": "genie",
        "handle.id": narrow.id,
        "handle.capabilities": "",
        "handle.root": first_root.id,
    }
    assert written[1] == {
        "service": "genie",
        "handle.id": wide.id,
        "handle.capabilities": "ancestors,global_search",
        "handle.root": second_root.id,
    }
    assert refused == [written[0]]
    assert narrow.id != wide.id


def test_handle_attributes_describe_unbounded_and_narrowed_handles() -> None:
    """Capabilities and roots are reported as the boundary actually stands."""

    hook = RecordingContextHook()
    memory = Memory(hooks=[hook])
    root = memory.put(type="root", value="root")
    unbounded = memory.get_unbounded_handle(allow_mounts=True, allow_policy_override=True)
    child = memory.get_handle(root=root, allow_up=True, allow_mounts=True).get_handle(
        root=root,
        allow_mounts=True,
    )

    assert dict(unbounded.attributes) == {
        "handle.id": unbounded.id,
        "handle.capabilities": "mounts,policy_override",
    }
    assert dict(child.attributes) == {
        "handle.id": child.id,
        "handle.capabilities": "mounts",
        "handle.root": root.id,
    }


def test_handle_attributes_label_facade_operations_inside_a_scope() -> None:
    """Structure facades report the same span and sandbox as memory operations."""

    hook = RecordingContextHook()
    backend = InMemoryBackend()
    memory = Memory(backend=backend, hooks=[hook])
    root = memory.put(type="root", value="root")
    handle = memory.get_handle(root=root)
    collection = handle.put(type="collection", value="keys")
    keyed = KeyedCollection(handle, backend, collection=collection)
    hook.contexts.clear()

    with memory.hooks.scope("agent.turn", attributes={"run_id": "r1"}):
        note = handle.put(type="note", value="one")
        keyed.bind(MemoryAddress((), "one"), note)

    expected = {"run_id": "r1", **dict(handle.attributes)}
    assert hook.attributes_for(HookOperation.KEY_BIND) == [expected]
    assert hook.attributes_for(HookOperation.PUT) == [expected]
