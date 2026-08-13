"""Tests for memory hook dispatch and customization."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from threading import Lock
from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    HookContext,
    HookOperation,
    HookRecursionError,
    HookRegistry,
    InMemoryBackend,
    Memory,
    MemoryBlock,
    MemoryConflict,
    MemoryHook,
    MemoryPolicy,
    MemoryQuery,
    Scope,
    SearchResult,
    StructureAction,
    StructureChange,
)
from genie.structures import (
    EventLog,
    KeyedCollection,
    MemoryAddress,
    TemporalCollection,
    VersionedRecords,
)


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
    with pytest.raises(ValueError):
        with registry.operation("missing"):
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


def test_hook_depth_limit_and_exception_cleanup() -> None:
    """Depth limits fail clearly and context always resets after exceptions."""

    class DeepHook(MemoryHook):
        """Attempt operations beyond the hook-originated depth limit."""

        def __init__(self, registry: HookRegistry) -> None:
            self.registry = registry

        def after_put(self, context: HookContext, block) -> None:
            """Create two nested operation contexts from one callback."""

            with self.registry.operation(HookOperation.GET):
                with self.registry.operation(HookOperation.SEARCH):
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
    assert [context.operation_id for context in hook.before] == [
        context.operation_id for context in hook.after
    ]


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
    assert [context.operation_id for context in hook.added] == [
        context.operation_id for context in hook.added_after
    ]
    assert [context.operation_id for context in hook.updated] == [
        context.operation_id for context in hook.updated_after
    ]


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
    assert [context.operation_id for _, context in hook.before] == [
        context.operation_id for _, context in hook.after
    ]
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
