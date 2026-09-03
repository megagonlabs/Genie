"""The operations the benchmarks measure.

Each case is one logical operation. A case declares the layers it can be
measured at:

- **`backend`** is one call on a backend capability protocol. No policy check,
  no hook dispatch, no codec, no block copying.
- **`memory`** is the matching call on the public `Memory` or `MemoryHandle`
  API, which is what an application actually pays.
- **`core`** is one core-library helper on its own, with no store behind it.
  These cases explain where the gap between the other two layers went.

A `prepare` function builds everything a case needs and returns the one call
the runner times. Preparation runs outside the measured region, so a timed call
covers the operation and nothing else.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from benchmarks import dataset
from benchmarks.dataset import HUB_ID, ROOT_ID, BlockSpec, Corpus, payload
from benchmarks.settings import BACKEND, CORE, MEMORY, BenchmarkSettings
from genie import HookOperation, Memory, MemoryLink, MemoryQuery, Operation, Scope

GROUPS = ("write", "read", "link", "search", "overhead")
"""Case groups, following the memory lifecycle and then the core helpers."""

SUBTREE_CHILDREN = 8
"""Children given to each throwaway subtree the `delete_subtree` case removes."""

RELATION = "bench.related"
"""Relation used by every link case."""

WRITTEN_TYPE = "bench.written"
"""Block type the write cases create."""


@dataclass(frozen=True, slots=True)
class CaseContext:
    """Everything a case may build on.

    The corpus is already written to the store when a case is prepared.

    Attributes:
        settings: Run settings.
        corpus: Corpus specs the store was filled from.
        backend: Backend under measurement.
        memory: Memory wrapping that backend.
        principal_id: Owner of every block in the corpus.
    """

    settings: BenchmarkSettings
    corpus: Corpus
    backend: Any
    memory: Memory
    principal_id: str


@dataclass(frozen=True, slots=True)
class PreparedCase:
    """One case ready to be timed.

    Attributes:
        call: Makes one call. Receives the zero-based call index, which lets a
            case walk over its own targets instead of reusing one.
        describe: Turns the last result into notes saying what the call did.
    """

    call: Callable[[int], Any]
    describe: Callable[[Any], Mapping[str, Any]] | None = None


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One logical operation, measurable at one or more layers.

    Attributes:
        name: Case name used in reports and on the command line.
        group: Lifecycle group this case belongs to.
        description: What the timed call does.
        backend: Prepares the raw backend protocol call, when there is one.
        memory: Prepares the public `Memory` call, when there is one.
        core: Prepares the core-only helper call, when there is one.
        needs_corpus: Whether the case needs the full corpus written first.
            The `core` cases do not, so they skip that setup entirely.
    """

    name: str
    group: str
    description: str
    backend: Callable[[CaseContext], PreparedCase] | None = None
    memory: Callable[[CaseContext], PreparedCase] | None = None
    core: Callable[[CaseContext], PreparedCase] | None = None
    needs_corpus: bool = True

    @property
    def layers(self) -> tuple[str, ...]:
        """Layers this case can be measured at, in report order."""

        return tuple(
            layer
            for layer, prepare in ((BACKEND, self.backend), (MEMORY, self.memory), (CORE, self.core))
            if prepare is not None
        )

    def prepare_for(self, layer: str) -> Callable[[CaseContext], PreparedCase] | None:
        """Return the prepare function for one layer.

        Args:
            layer: Layer name.

        Returns:
            The prepare function, or `None` when this case has no such layer.
        """

        return {BACKEND: self.backend, MEMORY: self.memory, CORE: self.core}.get(layer)


def _count(result: Any) -> Mapping[str, Any]:
    """Note how many items a call returned."""

    return {"returned": len(result)}


def _found(result: Any) -> Mapping[str, Any]:
    """Note whether a point read found anything."""

    return {"found": result is not None}


def _truth(result: Any) -> Mapping[str, Any]:
    """Note the answer a boolean call gave."""

    return {"result": bool(result)}


def _spec(context: CaseContext, prefix: str, index: int, parent_id: str | None) -> BlockSpec:
    """Build one throwaway block spec for a write case."""

    return BlockSpec(
        id=f"{prefix}_{index:06d}",
        type=WRITTEN_TYPE,
        value=payload(index, value_size=context.settings.value_size),
        parent_id=parent_id,
        metadata={"role": "written", "index": index, "bucket": index % 10},
    )


def _encoded(context: CaseContext, spec: BlockSpec) -> Any:
    """Encode a spec the way `Memory` would before a backend write."""

    return dataset.make_block(
        spec,
        principal_id=context.principal_id,
        policy=context.memory.default_policy,
        codecs=context.memory.codecs,
    )


def _section_id(context: CaseContext, index: int) -> str:
    """Return a section id, walking the chain so writes spread out."""

    sections = context.corpus.sections
    return sections[index % len(sections)].id


def _leaf_id(context: CaseContext, index: int) -> str:
    """Return a leaf id, walking the corpus so reads spread out."""

    leaves = context.corpus.leaves
    return leaves[index % len(leaves)].id


# --------------------------------------------------------------------------
# write
# --------------------------------------------------------------------------


def _backend_put_root(context: CaseContext) -> PreparedCase:
    blocks = [
        _encoded(context, _spec(context, "bench_put_root", index, None))
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(call=lambda index: context.backend.put_block(blocks[index]))


def _memory_put_root(context: CaseContext) -> PreparedCase:
    specs = [
        _spec(context, "bench_put_root", index, None)
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(
        call=lambda index: context.memory.put(
            type=specs[index].type,
            value=specs[index].value,
            metadata=specs[index].metadata,
            id=specs[index].id,
        )
    )


def _backend_put_child(context: CaseContext) -> PreparedCase:
    blocks = [
        _encoded(context, _spec(context, "bench_put_child", index, _section_id(context, index)))
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(call=lambda index: context.backend.put_block(blocks[index]))


def _memory_put_child(context: CaseContext) -> PreparedCase:
    specs = [
        _spec(context, "bench_put_child", index, _section_id(context, index))
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(
        call=lambda index: context.memory.put(
            type=specs[index].type,
            value=specs[index].value,
            parent=specs[index].parent_id,
            metadata=specs[index].metadata,
            id=specs[index].id,
        )
    )


def _backend_set_value(context: CaseContext) -> PreparedCase:
    # The version chain is known in advance, because nothing else writes to this
    # block. Building every replacement up front keeps the timed region to the
    # single compare-and-swap call.
    spec = _spec(context, "bench_set_target", 0, _section_id(context, 0))
    stored = context.backend.put_block(_encoded(context, spec))
    now = datetime.now(UTC)
    replacements = [
        stored.with_updates(
            value=context.memory.codecs.encode(
                payload(index, value_size=context.settings.value_size)
            ).data,
            updated_at=now,
            version=index + 2,
        )
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(
        call=lambda index: context.backend.update_block(
            replacements[index],
            expected_version=index + 1,
        )
    )


def _memory_set_value(context: CaseContext) -> PreparedCase:
    spec = _spec(context, "bench_set_target", 0, _section_id(context, 0))
    context.memory.put(
        type=spec.type,
        value=spec.value,
        parent=spec.parent_id,
        metadata=spec.metadata,
        id=spec.id,
    )
    values = [
        payload(index, value_size=context.settings.value_size)
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(call=lambda index: context.memory.set(spec.id, values[index]))


def _memory_merge_value(context: CaseContext) -> PreparedCase:
    spec = _spec(context, "bench_merge_target", 0, _section_id(context, 0))
    context.memory.put(
        type=spec.type,
        value=spec.value,
        parent=spec.parent_id,
        metadata=spec.metadata,
        id=spec.id,
    )
    merges = [{"counter": index} for index in range(context.settings.total_calls)]
    return PreparedCase(call=lambda index: context.memory.update(spec.id, value=merges[index]))


def _backend_delete_leaf(context: CaseContext) -> PreparedCase:
    ids = []
    for index in range(context.settings.total_calls):
        spec = _spec(context, "bench_delete_leaf", index, _section_id(context, index))
        context.backend.put_block(_encoded(context, spec))
        ids.append(spec.id)
    return PreparedCase(call=lambda index: context.backend.delete_block(ids[index]))


def _memory_delete_leaf(context: CaseContext) -> PreparedCase:
    ids = []
    for index in range(context.settings.total_calls):
        spec = _spec(context, "bench_delete_leaf", index, _section_id(context, index))
        context.memory.put(
            type=spec.type,
            value=spec.value,
            parent=spec.parent_id,
            metadata=spec.metadata,
            id=spec.id,
        )
        ids.append(spec.id)
    return PreparedCase(call=lambda index: context.memory.delete(ids[index]))


def _backend_delete_subtree(context: CaseContext) -> PreparedCase:
    ids = []
    for index in range(context.settings.total_calls):
        parent = _spec(context, "bench_delete_subtree", index, _section_id(context, index))
        context.backend.put_block(_encoded(context, parent))
        for child in range(SUBTREE_CHILDREN):
            spec = _spec(context, f"bench_subtree_child_{index:06d}", child, parent.id)
            context.backend.put_block(_encoded(context, spec))
        ids.append(parent.id)
    return PreparedCase(call=lambda index: context.backend.delete_block(ids[index]))


def _memory_delete_subtree(context: CaseContext) -> PreparedCase:
    ids = []
    for index in range(context.settings.total_calls):
        parent = _spec(context, "bench_delete_subtree", index, _section_id(context, index))
        context.memory.put(
            type=parent.type,
            value=parent.value,
            parent=parent.parent_id,
            metadata=parent.metadata,
            id=parent.id,
        )
        for child in range(SUBTREE_CHILDREN):
            spec = _spec(context, f"bench_subtree_child_{index:06d}", child, parent.id)
            context.memory.put(
                type=spec.type,
                value=spec.value,
                parent=spec.parent_id,
                metadata=spec.metadata,
                id=spec.id,
            )
        ids.append(parent.id)
    return PreparedCase(call=lambda index: context.memory.delete(ids[index]))


# --------------------------------------------------------------------------
# read
# --------------------------------------------------------------------------


def _backend_get(context: CaseContext) -> PreparedCase:
    ids = [leaf.id for leaf in context.corpus.leaves]
    return PreparedCase(
        call=lambda index: context.backend.get_block(ids[index % len(ids)]),
        describe=_found,
    )


def _memory_get(context: CaseContext) -> PreparedCase:
    ids = [leaf.id for leaf in context.corpus.leaves]
    return PreparedCase(
        call=lambda index: context.memory.get(ids[index % len(ids)]),
        describe=_found,
    )


def _memory_handle_get(context: CaseContext) -> PreparedCase:
    handle = context.memory.get_handle(root=ROOT_ID)
    ids = [leaf.id for leaf in context.corpus.leaves]
    return PreparedCase(
        call=lambda index: handle.get(ids[index % len(ids)]),
        describe=_found,
    )


def _backend_children(context: CaseContext) -> PreparedCase:
    return PreparedCase(
        call=lambda index: context.backend.children(_section_id(context, index)),
        describe=_count,
    )


def _memory_children(context: CaseContext) -> PreparedCase:
    return PreparedCase(
        call=lambda index: context.memory.children(_section_id(context, index)),
        describe=_count,
    )


def _backend_ancestors(context: CaseContext) -> PreparedCase:
    leaf_id = context.corpus.deepest_leaf_id
    return PreparedCase(call=lambda index: context.backend.ancestors(leaf_id), describe=_count)


def _memory_ancestors(context: CaseContext) -> PreparedCase:
    leaf_id = context.corpus.deepest_leaf_id
    return PreparedCase(call=lambda index: context.memory.ancestors(leaf_id), describe=_count)


def _backend_descendants(context: CaseContext) -> PreparedCase:
    return PreparedCase(
        call=lambda index: context.backend.descendants(ROOT_ID),
        describe=_count,
    )


def _backend_contains(context: CaseContext) -> PreparedCase:
    leaf_id = context.corpus.deepest_leaf_id
    return PreparedCase(
        call=lambda index: context.backend.contains(ROOT_ID, leaf_id),
        describe=_truth,
    )


def _memory_contains(context: CaseContext) -> PreparedCase:
    leaf_id = context.corpus.deepest_leaf_id
    return PreparedCase(
        call=lambda index: context.memory.contains(ROOT_ID, leaf_id),
        describe=_truth,
    )


# --------------------------------------------------------------------------
# link
# --------------------------------------------------------------------------


def _backend_add_link(context: CaseContext) -> PreparedCase:
    links = [
        MemoryLink(
            source_id=_leaf_id(context, index),
            target_id=ROOT_ID,
            relation=RELATION,
            id=f"bench_link_{index:06d}",
        )
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(call=lambda index: context.backend.add_link(links[index]))


def _memory_add_link(context: CaseContext) -> PreparedCase:
    sources = [_leaf_id(context, index) for index in range(context.settings.total_calls)]
    return PreparedCase(
        call=lambda index: context.memory.add_link(sources[index], ROOT_ID, RELATION)
    )


def _backend_list_links(context: CaseContext) -> PreparedCase:
    targets = context.corpus.link_target_ids(context.settings.links_per_block)
    for index, target_id in enumerate(targets):
        context.backend.add_link(
            MemoryLink(
                source_id=HUB_ID,
                target_id=target_id,
                relation=RELATION,
                id=f"bench_hub_link_{index:06d}",
            )
        )
    return PreparedCase(
        call=lambda index: context.backend.list_links(HUB_ID, direction="out"),
        describe=_count,
    )


def _memory_list_links(context: CaseContext) -> PreparedCase:
    for target_id in context.corpus.link_target_ids(context.settings.links_per_block):
        context.memory.add_link(HUB_ID, target_id, RELATION)
    return PreparedCase(
        call=lambda index: context.memory.list_links(HUB_ID, direction="out"),
        describe=_count,
    )


# --------------------------------------------------------------------------
# search
# --------------------------------------------------------------------------


def _subtree_text_query(settings: BenchmarkSettings) -> MemoryQuery:
    """Build the scoped text query both search layers run."""

    return MemoryQuery(
        text=dataset.TEXT_NEEDLE,
        root_id=ROOT_ID,
        scope=Scope.SUBTREE,
        limit=settings.search_limit,
    )


def _global_filter_query(settings: BenchmarkSettings) -> MemoryQuery:
    """Build the unscoped metadata filter query both search layers run."""

    return MemoryQuery(
        filters={dataset.FILTER_FIELD: dataset.FILTER_VALUE},
        scope=Scope.GLOBAL,
        limit=settings.search_limit,
    )


def _search_notes(settings: BenchmarkSettings) -> Callable[[Any], Mapping[str, Any]]:
    """Note what a search returned next to the budget it was given."""

    def describe(result: Any) -> Mapping[str, Any]:
        return {"returned": len(result), "candidate_budget": settings.search_limit}

    return describe


def _backend_search_subtree_text(context: CaseContext) -> PreparedCase:
    query = _subtree_text_query(context.settings)
    return PreparedCase(
        call=lambda index: context.backend.search(query),
        describe=_search_notes(context.settings),
    )


def _memory_search_subtree_text(context: CaseContext) -> PreparedCase:
    query = _subtree_text_query(context.settings)
    return PreparedCase(
        call=lambda index: context.memory.search(query),
        describe=_search_notes(context.settings),
    )


def _backend_search_global_filter(context: CaseContext) -> PreparedCase:
    query = _global_filter_query(context.settings)
    return PreparedCase(
        call=lambda index: context.backend.search(query),
        describe=_search_notes(context.settings),
    )


def _memory_search_global_filter(context: CaseContext) -> PreparedCase:
    query = _global_filter_query(context.settings)
    return PreparedCase(
        call=lambda index: context.memory.search(query),
        describe=_search_notes(context.settings),
    )


# --------------------------------------------------------------------------
# overhead
# --------------------------------------------------------------------------


def _core_codec_round_trip(context: CaseContext) -> PreparedCase:
    codecs = context.memory.codecs
    value = payload(0, value_size=context.settings.value_size)
    return PreparedCase(call=lambda index: codecs.decode(codecs.encode(value)))


def _core_policy_check(context: CaseContext) -> PreparedCase:
    engine = context.memory.policy_engine
    principal = context.memory.principal
    block = _encoded(context, _spec(context, "bench_policy_target", 0, None))
    return PreparedCase(
        call=lambda index: engine.allows(Operation.READ, block, principal),
        describe=_truth,
    )


def _core_hook_dispatch(context: CaseContext) -> PreparedCase:
    hooks = context.memory.hooks
    principal_id = context.principal_id
    block = _encoded(context, _spec(context, "bench_hook_target", 0, None))

    def call(index: int) -> Any:
        with hooks.operation(HookOperation.PUT, principal_id=principal_id) as hook_context:
            result = hooks.before_put(hook_context, block)
            hooks.after_put(hook_context, result)
        return result

    return PreparedCase(call=call)


CASES: tuple[BenchmarkCase, ...] = (
    BenchmarkCase(
        name="put_root",
        group="write",
        description="Create a block with no parent",
        backend=_backend_put_root,
        memory=_memory_put_root,
    ),
    BenchmarkCase(
        name="put_child",
        group="write",
        description="Create a block under an existing section",
        backend=_backend_put_child,
        memory=_memory_put_child,
    ),
    BenchmarkCase(
        name="set_value",
        group="write",
        description="Replace a block's whole value with a compare-and-swap write",
        backend=_backend_set_value,
        memory=_memory_set_value,
    ),
    BenchmarkCase(
        name="merge_value",
        group="write",
        description="Merge one field into a block's value",
        memory=_memory_merge_value,
    ),
    BenchmarkCase(
        name="delete_leaf",
        group="write",
        description="Delete one childless block",
        backend=_backend_delete_leaf,
        memory=_memory_delete_leaf,
    ),
    BenchmarkCase(
        name="delete_subtree",
        group="write",
        description=f"Delete one block and the {SUBTREE_CHILDREN} children it owns",
        backend=_backend_delete_subtree,
        memory=_memory_delete_subtree,
    ),
    BenchmarkCase(
        name="get",
        group="read",
        description="Read one block by id",
        backend=_backend_get,
        memory=_memory_get,
    ),
    BenchmarkCase(
        name="handle_get",
        group="read",
        description="Read one block by id through a bounded MemoryHandle",
        memory=_memory_handle_get,
    ),
    BenchmarkCase(
        name="children",
        group="read",
        description="List the direct children of one section",
        backend=_backend_children,
        memory=_memory_children,
    ),
    BenchmarkCase(
        name="ancestors",
        group="read",
        description="Walk up from the deepest leaf to the corpus root",
        backend=_backend_ancestors,
        memory=_memory_ancestors,
    ),
    BenchmarkCase(
        name="descendants",
        group="read",
        description="List every block the corpus root owns",
        backend=_backend_descendants,
    ),
    BenchmarkCase(
        name="contains",
        group="read",
        description="Test subtree membership for the deepest leaf",
        backend=_backend_contains,
        memory=_memory_contains,
    ),
    BenchmarkCase(
        name="add_link",
        group="link",
        description="Create one typed link from a leaf to the corpus root",
        backend=_backend_add_link,
        memory=_memory_add_link,
    ),
    BenchmarkCase(
        name="list_links",
        group="link",
        description="List the outgoing links of the hub block",
        backend=_backend_list_links,
        memory=_memory_list_links,
    ),
    BenchmarkCase(
        name="search_subtree_text",
        group="search",
        description="Text search at Scope.SUBTREE from the corpus root",
        backend=_backend_search_subtree_text,
        memory=_memory_search_subtree_text,
    ),
    BenchmarkCase(
        name="search_global_filter",
        group="search",
        description="Metadata filter search at Scope.GLOBAL",
        backend=_backend_search_global_filter,
        memory=_memory_search_global_filter,
    ),
    BenchmarkCase(
        name="codec_round_trip",
        group="overhead",
        description="CodecRegistry encode then decode of one value",
        core=_core_codec_round_trip,
        needs_corpus=False,
    ),
    BenchmarkCase(
        name="policy_check",
        group="overhead",
        description="PolicyEngine read check against one block policy",
        core=_core_policy_check,
        needs_corpus=False,
    ),
    BenchmarkCase(
        name="hook_dispatch",
        group="overhead",
        description="One hook operation context with no hooks registered",
        core=_core_hook_dispatch,
        needs_corpus=False,
    ),
)
"""Every case a default run measures, in report order."""


def default_cases() -> tuple[BenchmarkCase, ...]:
    """Return every case a default run measures."""

    return CASES


def select(
    cases: tuple[BenchmarkCase, ...],
    *,
    groups: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    layers: tuple[str, ...] = (),
) -> tuple[BenchmarkCase, ...]:
    """Filter cases by group, name text, and layer.

    Args:
        cases: Cases to filter.
        groups: Keep only these groups. Empty keeps every group.
        names: Keep cases whose name contains any of these strings. Empty keeps
            every name.
        layers: Keep cases measurable at any of these layers. Empty keeps every
            case.

    Returns:
        The matching cases, in their original order.
    """

    selected = []
    for case in cases:
        if groups and case.group not in groups:
            continue
        if names and not any(name in case.name for name in names):
            continue
        if layers and not any(layer in case.layers for layer in layers):
            continue
        selected.append(case)
    return tuple(selected)
