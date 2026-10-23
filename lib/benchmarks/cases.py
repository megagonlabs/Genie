"""The operations that the benchmarks measure.

Each case is one logical operation. A case declares the layers at which the
harness can measure it:

- **`backend`** is one call on a backend capability protocol. No policy check,
  no hook dispatch, no codec, and no block copy.
- **`memory`** is the equivalent call on the public `Memory` or `MemoryHandle`
  API. This is the cost that an application pays.
- **`core`** is one core-library helper alone, with no store behind it. These
  cases explain the parts of the gap between the other 2 layers.

A `prepare` function builds all that a case needs, and returns the one call
that the runner times. Preparation runs outside the measured region. So a
timed call measures only the operation.

2 rules keep the result of a case correct if more than one worker runs it:

- **Each worker writes its own ids.** An ordinary case creates temporary
  blocks, links, or subtrees. Their ids contain the worker index, so no 2
  workers can write the same id. `_spec` and `_worker_suffix` do this for each
  write case.
- **Shared setup runs one time.** Some preparation changes corpus state that
  all workers read, for example the hub links that `list_links` reads. This
  preparation runs only for worker `0`. The `contention` cases use this rule:
  worker `0` creates their target one time, and then all workers use it.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from benchmarks import dataset
from benchmarks.dataset import HUB_ID, ROOT_ID, BlockSpec, Corpus, payload
from benchmarks.settings import BACKEND, CORE, MEMORY, BenchmarkSettings
from genie import HookOperation, Memory, MemoryConflict, MemoryLink, MemoryQuery, Operation, Scope

GROUPS = ("write", "read", "link", "search", "contention", "overhead")
"""The case groups. First the groups of the memory lifecycle, then contention,
then the core helpers."""

SUBTREE_CHILDREN = 8
"""The children of each temporary subtree that the `delete_subtree` case
removes."""

RELATION = "bench.related"
"""The relation of all link cases."""

WRITTEN_TYPE = "bench.written"
"""The block type that the write cases create."""

HOT_BLOCK_ID = "bench_hot_block"
"""The block that all workers of a `contention` read or write case share."""

HOT_PARENT_ID = "bench_hot_parent"
"""The parent under which all workers of `hot_parent_put` create their
children."""

FIRST_WORKER = 0
"""The worker that does the setup that all workers of a variant share."""


@dataclass(frozen=True, slots=True)
class CaseContext:
    """All that a case can use.

    When the runner prepares a case, the corpus is already in the store.

    Attributes:
        settings: The run settings.
        corpus: The corpus specs that the store got.
        backend: The backend under measurement.
        memory: The memory that wraps that backend.
        principal_id: The owner of each block in the corpus.
        worker_index: The zero-based position of the worker for this
            preparation. Setup that all workers share runs only for
            `FIRST_WORKER`.
        worker_count: The workers that run this variant at the same time. One
            means a single caller. A default run measures a single caller.
    """

    settings: BenchmarkSettings
    corpus: Corpus
    backend: Any
    memory: Memory
    principal_id: str
    worker_index: int = FIRST_WORKER
    worker_count: int = 1

    @property
    def is_first_worker(self) -> bool:
        """`True` if this worker does the setup that all workers share."""

        return self.worker_index == FIRST_WORKER


@dataclass(frozen=True, slots=True)
class PreparedCase:
    """One case that the runner can time.

    Attributes:
        call: Makes one call. It gets the zero-based call index. So a case can
            go through its own targets, and does not use one target again.
        describe: Changes the last result into notes about what the call did.
        conflict_errors: The exception types of the races that this case
            expects to lose, for example `MemoryConflict` if another worker
            wrote the same block first. The runner counts them as conflicts,
            not as errors, so they never cause a failed run. All other
            exceptions are errors.
    """

    call: Callable[[int], Any]
    describe: Callable[[Any], Mapping[str, Any]] | None = None
    conflict_errors: tuple[type[BaseException], ...] = ()


@dataclass(frozen=True, slots=True)
class BenchmarkCase:
    """One logical operation that the harness can measure at one or more layers.

    Attributes:
        name: The case name in reports and on the command line.
        group: The lifecycle group of this case.
        description: What the timed call does.
        backend: Prepares the raw backend protocol call, if the case has one.
        memory: Prepares the public `Memory` call, if the case has one.
        core: Prepares the core-only helper call, if the case has one.
        needs_corpus: If `True`, the runner writes the full corpus first. The
            `core` cases do not need it, so they skip all of that setup.
        concurrent_only: If `True`, the harness measures the case only in a run
            that asks for more than one worker. The `contention` cases have
            this value, because a shared target with one caller is not shared.
            In such a run, the harness measures them at each level, also at
            level 1, so `speedup` has a denominator.
    """

    name: str
    group: str
    description: str
    backend: Callable[[CaseContext], PreparedCase] | None = None
    memory: Callable[[CaseContext], PreparedCase] | None = None
    core: Callable[[CaseContext], PreparedCase] | None = None
    needs_corpus: bool = True
    concurrent_only: bool = False

    @property
    def layers(self) -> tuple[str, ...]:
        """The layers at which the harness can measure this case, in report order."""

        return tuple(
            layer
            for layer, prepare in ((BACKEND, self.backend), (MEMORY, self.memory), (CORE, self.core))
            if prepare is not None
        )

    def prepare_for(self, layer: str) -> Callable[[CaseContext], PreparedCase] | None:
        """Return the prepare function for one layer.

        Args:
            layer: The layer name.

        Returns:
            The prepare function, or `None` if this case does not have that
            layer.
        """

        return {BACKEND: self.backend, MEMORY: self.memory, CORE: self.core}.get(layer)


def _count(result: Any) -> Mapping[str, Any]:
    """Record the number of items that a call returned."""

    return {"returned": len(result)}


def _found(result: Any) -> Mapping[str, Any]:
    """Record if a point read found a block."""

    return {"found": result is not None}


def _truth(result: Any) -> Mapping[str, Any]:
    """Record the answer of a boolean call."""

    return {"result": bool(result)}


def _worker_suffix(context: CaseContext) -> str:
    """Return the id part that keeps one worker away from the blocks of a different worker.

    - **Single caller:** the ids do not change.
    - **More than one caller:** the ids get the worker index. So 2 workers can
      never put the same id.

    Args:
        context: The case context that names the worker.

    Returns:
        An empty string for a single caller. If not, `"_w<index>"`.
    """

    if context.worker_count <= 1:
        return ""
    return f"_w{context.worker_index}"


def _read_cursor(context: CaseContext, size: int) -> int:
    """Return the offset where a worker starts its read rotation.

    - **Why:** all workers run the same call indexes. Without an offset, they
      read the same id at the same time, and a spread read measures a hot read.
    - **How:** the workers start at equal distances along the corpus. So the
      spread cases stay spread.
    - **Exception:** the `contention` cases share a target on purpose, and do
      not use this offset.

    Args:
        context: The case context that names the worker.
        size: The number of ids in the rotation of the case.

    Returns:
        The start offset, or `0` for a single caller.
    """

    if context.worker_count <= 1 or size <= 0:
        return 0
    return context.worker_index * size // context.worker_count


def _spec(context: CaseContext, prefix: str, index: int, parent_id: str | None) -> BlockSpec:
    """Build one temporary block spec for a write case."""

    return BlockSpec(
        id=f"{prefix}{_worker_suffix(context)}_{index:06d}",
        type=WRITTEN_TYPE,
        value=payload(index, value_size=context.settings.value_size),
        parent_id=parent_id,
        metadata={"role": "written", "index": index, "bucket": index % 10},
    )


def _encoded(context: CaseContext, spec: BlockSpec) -> Any:
    """Encode a spec as `Memory` does before a backend write."""

    return dataset.make_block(
        spec,
        principal_id=context.principal_id,
        policy=context.memory.default_policy,
        codecs=context.memory.codecs,
    )


def _section_id(context: CaseContext, index: int) -> str:
    """Return a section id. The function goes along the chain, so the writes spread.

    Each worker starts at its own offset. So more than one caller spreads over
    the chain, and the callers do not wait in a queue on one section.
    """

    sections = context.corpus.sections
    return sections[(index + _read_cursor(context, len(sections))) % len(sections)].id


def _leaf_id(context: CaseContext, index: int) -> str:
    """Return a leaf id. The function goes through the corpus, so the reads spread.

    Each worker starts at its own offset, for the same reason as in
    `_section_id`.
    """

    leaves = context.corpus.leaves
    return leaves[(index + _read_cursor(context, len(leaves))) % len(leaves)].id


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
    # The version chain is known before the calls, because nothing else writes
    # to this block. The case builds all replacements first. So the timed
    # region contains only the single compare-and-swap call.
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
    start = _read_cursor(context, len(ids))
    return PreparedCase(
        call=lambda index: context.backend.get_block(ids[(index + start) % len(ids)]),
        describe=_found,
    )


def _memory_get(context: CaseContext) -> PreparedCase:
    ids = [leaf.id for leaf in context.corpus.leaves]
    start = _read_cursor(context, len(ids))
    return PreparedCase(
        call=lambda index: context.memory.get(ids[(index + start) % len(ids)]),
        describe=_found,
    )


def _memory_handle_get(context: CaseContext) -> PreparedCase:
    handle = context.memory.get_handle(root=ROOT_ID)
    ids = [leaf.id for leaf in context.corpus.leaves]
    start = _read_cursor(context, len(ids))
    return PreparedCase(
        call=lambda index: handle.get(ids[(index + start) % len(ids)]),
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
            id=f"bench_link{_worker_suffix(context)}_{index:06d}",
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
    # The hub is corpus state that all workers read. So the case writes its
    # links one time, not one time for each worker.
    if context.is_first_worker:
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
    # The case writes the links one time, for the same reason as the backend
    # variant above.
    if context.is_first_worker:
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
    """Build the scoped text query that both search layers run."""

    return MemoryQuery(
        text=dataset.TEXT_NEEDLE,
        root_id=ROOT_ID,
        scope=Scope.SUBTREE,
        limit=settings.search_limit,
    )


def _global_filter_query(settings: BenchmarkSettings) -> MemoryQuery:
    """Build the unscoped metadata filter query that both search layers run."""

    return MemoryQuery(
        filters={dataset.FILTER_FIELD: dataset.FILTER_VALUE},
        scope=Scope.GLOBAL,
        limit=settings.search_limit,
    )


def _search_notes(settings: BenchmarkSettings) -> Callable[[Any], Mapping[str, Any]]:
    """Record what a search returned, together with its limit."""

    def describe(result: Any) -> Mapping[str, Any]:
        return {"returned": len(result), "limit": settings.search_limit}

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
# contention
# --------------------------------------------------------------------------
#
# All workers of these cases use one target on purpose. The harness measures
# them only in a run that asks for more than one worker. In that run, it
# measures them at each level, so `speedup` has a single-caller denominator.
# Compare each case with the spread case that it mirrors:
#
# - `hot_block_get` with `get`, which goes through the corpus.
# - `hot_parent_put` with `put_root`, which shares nothing.
# - `hot_block_set` with `set_value`, which gives each worker its own block.
# - `hot_block_merge` with `merge_value`, which gives each worker its own
#   block.


def _shared_spec(context: CaseContext, block_id: str, parent_id: str | None) -> BlockSpec:
    """Build the spec of a block that all workers of a case share.

    The id is fixed. It does not come from the worker index. This is the
    purpose: all workers use the same block.

    Args:
        context: The case context.
        block_id: The id that all workers use.
        parent_id: The parent of the block.

    Returns:
        The spec.
    """

    return BlockSpec(
        id=block_id,
        type=WRITTEN_TYPE,
        value=payload(0, value_size=context.settings.value_size),
        parent_id=parent_id,
        metadata={"role": "shared", "index": 0, "bucket": 0},
    )


def _store_shared_block(context: CaseContext, block_id: str) -> None:
    """Create the shared target one time, directly through the backend."""

    if context.is_first_worker:
        context.backend.put_block(_encoded(context, _shared_spec(context, block_id, ROOT_ID)))


def _put_shared_block(context: CaseContext, block_id: str) -> None:
    """Create the shared target one time, through the public `Memory` API."""

    if not context.is_first_worker:
        return
    spec = _shared_spec(context, block_id, ROOT_ID)
    context.memory.put(
        type=spec.type,
        value=spec.value,
        parent=spec.parent_id,
        metadata=spec.metadata,
        id=spec.id,
    )


def _backend_hot_block_get(context: CaseContext) -> PreparedCase:
    _store_shared_block(context, HOT_BLOCK_ID)
    return PreparedCase(call=lambda index: context.backend.get_block(HOT_BLOCK_ID), describe=_found)


def _memory_hot_block_get(context: CaseContext) -> PreparedCase:
    _put_shared_block(context, HOT_BLOCK_ID)
    return PreparedCase(call=lambda index: context.memory.get(HOT_BLOCK_ID), describe=_found)


def _backend_hot_parent_put(context: CaseContext) -> PreparedCase:
    _store_shared_block(context, HOT_PARENT_ID)
    blocks = [
        _encoded(context, _spec(context, "bench_hot_child", index, HOT_PARENT_ID))
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(call=lambda index: context.backend.put_block(blocks[index]))


def _memory_hot_parent_put(context: CaseContext) -> PreparedCase:
    _put_shared_block(context, HOT_PARENT_ID)
    specs = [
        _spec(context, "bench_hot_child", index, HOT_PARENT_ID)
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


def _memory_hot_block_set(context: CaseContext) -> PreparedCase:
    _put_shared_block(context, HOT_BLOCK_ID)
    values = [
        payload(index, value_size=context.settings.value_size)
        for index in range(context.settings.total_calls)
    ]
    return PreparedCase(
        call=lambda index: context.memory.set(HOT_BLOCK_ID, values[index]),
        conflict_errors=(MemoryConflict,),
    )


def _memory_hot_block_merge(context: CaseContext) -> PreparedCase:
    _put_shared_block(context, HOT_BLOCK_ID)
    merges = [{"counter": index} for index in range(context.settings.total_calls)]
    return PreparedCase(
        call=lambda index: context.memory.update(HOT_BLOCK_ID, value=merges[index]),
        conflict_errors=(MemoryConflict,),
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
        name="hot_block_get",
        group="contention",
        description="Read one block every worker shares",
        backend=_backend_hot_block_get,
        memory=_memory_hot_block_get,
        concurrent_only=True,
    ),
    BenchmarkCase(
        name="hot_parent_put",
        group="contention",
        description="Create a child under one parent every worker shares",
        backend=_backend_hot_parent_put,
        memory=_memory_hot_parent_put,
        concurrent_only=True,
    ),
    BenchmarkCase(
        name="hot_block_set",
        group="contention",
        description="Replace the value of one block every worker shares",
        memory=_memory_hot_block_set,
        concurrent_only=True,
    ),
    BenchmarkCase(
        name="hot_block_merge",
        group="contention",
        description="Merge one field into one block every worker shares",
        memory=_memory_hot_block_merge,
        concurrent_only=True,
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
"""All registered cases, in report order. A default run skips the
`concurrent_only` cases."""


def default_cases() -> tuple[BenchmarkCase, ...]:
    """Return all registered cases. A default run skips the `concurrent_only` cases."""

    return CASES


def select(
    cases: tuple[BenchmarkCase, ...],
    *,
    groups: tuple[str, ...] = (),
    names: tuple[str, ...] = (),
    layers: tuple[str, ...] = (),
    concurrent: bool = False,
) -> tuple[BenchmarkCase, ...]:
    """Filter cases by group, name text, layer, and worker count.

    Args:
        cases: The cases to filter.
        groups: Keep only these groups. If empty, keep all groups.
        names: Keep the cases whose name contains one of these strings. If
            empty, keep all names.
        layers: Keep the cases that the harness can measure at one of these
            layers. If empty, keep all cases.
        concurrent: `True` if the run measures more than one worker. If
            `False`, remove the `concurrent_only` cases, because a shared
            target with one caller is not shared.

    Returns:
        The matching cases, in their original order.
    """

    selected = []
    for case in cases:
        if case.concurrent_only and not concurrent:
            continue
        if groups and case.group not in groups:
            continue
        if names and not any(name in case.name for name in names):
            continue
        if layers and not any(layer in case.layers for layer in layers):
            continue
        selected.append(case)
    return tuple(selected)
