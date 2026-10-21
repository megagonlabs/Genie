"""Tests for the composition of backends in `RoutedBackend`."""

from __future__ import annotations

from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from threading import Barrier
from typing import Any, Literal, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    CodecRegistry,
    EncodedValue,
    HookContext,
    InMemoryBackend,
    InMemoryLinkStore,
    JsonCodec,
    LinkRelations,
    Memory,
    MemoryCodec,
    MemoryHook,
    MemoryLink,
    MemoryPolicy,
    MemoryQuery,
    RouteCatalog,
    RoutedBackend,
    RouteInfo,
    SearchResult,
    SQLiteBackend,
)
from genie.backend.base import BlockRouter, MemoryBackend
from genie.backend.structure_capabilities import (
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
)
from genie.memory.block import MemoryBlock
from genie.memory.errors import BackendCorruption, BackendUnavailable, MemoryConflict, MemoryNotFound, UnsupportedOperation
from genie.structures import EventLog, KeyedCollection, LineageDAG, MemoryAddress
from tests.backends.test_backend_contracts import (
    AcyclicLinkStoreContract,
    CoreOnlyBackend,
    LinkStoreContract,
)


def route_by_metadata(block: MemoryBlock) -> str | None:
    """Return the `route` name from the block metadata, or `None` if the metadata has no string `route`."""

    route = block.metadata.get("route")
    return route if isinstance(route, str) else None


@dataclass(frozen=True)
class Preference:
    """Custom value type that shows that `RoutedBackend` works with codecs."""

    topic: str
    value: str


class PreferenceCodec(MemoryCodec):
    """Codec that stores `Preference` values as dictionaries."""

    content_type = "application/vnd.genie.preference+json"

    def can_encode(self, value: Any) -> bool:
        """Return whether this codec can encode the value."""

        return isinstance(value, Preference)

    def encode(self, value: Any) -> EncodedValue:
        """Encode a preference value."""

        return EncodedValue(self.content_type, {"topic": value.topic, "value": value.value})

    def decode(self, value: EncodedValue) -> Any:
        """Decode a preference value."""

        return Preference(**value.data)


class AuditHook(MemoryHook):
    """Hook that records created block ids."""

    def __init__(self) -> None:
        """Create an empty hook."""

        self.created: list[str] = []

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Record a stored block id."""

        self.created.append(block.id)


class RecordingSearchIndex:
    """Shared search index that records queries and returns configured results."""

    def __init__(self) -> None:
        """Create an empty `RecordingSearchIndex`."""

        self.queries: list[MemoryQuery] = []
        self.results: list[SearchResult] = []

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Record the query and return configured results."""

        self.queries.append(query)
        return list(self.results)


class RecordingLinkStore:
    """Shared link store that records cleanup calls."""

    def __init__(self) -> None:
        """Create an empty `RecordingLinkStore`."""

        self.inner = InMemoryLinkStore()
        self.deleted_blocks: list[str] = []

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a link in the inner link store."""

        return self.inner.add_link(link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Update a link in the inner link store."""

        return self.inner.update_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links from the inner link store."""

        return self.inner.list_links(block_id, relation=relation, direction=direction)

    def delete_links_for_block(self, block_id: str) -> None:
        """Record the block id and delete the links connected to the block."""

        self.deleted_blocks.append(block_id)
        self.inner.delete_links_for_block(block_id)


class AcyclicRecordingLinkStore(InMemoryLinkStore):
    """Shared link store that claims `AcyclicLinkStore` and records its calls.

    It does not check for cycles, and it only shows that the composite sends
    acyclic inserts to a shared store and not to a route.
    """

    def __init__(self) -> None:
        """Create an empty `AcyclicRecordingLinkStore`."""

        super().__init__()
        self.acyclic_link_ids: list[str] = []

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Record the call and store the link."""

        self.acyclic_link_ids.append(link.id)
        return self.add_link(link)


class RecordingTraversalIndex:
    """Shared traversal index with explicit cross-route relationships."""

    def __init__(self) -> None:
        """Create an empty `RecordingTraversalIndex`."""

        self.children_by_root: dict[str, list[MemoryBlock]] = {}
        self.ancestors_by_block: dict[str, list[MemoryBlock]] = {}
        self.descendants_by_root: dict[tuple[str, bool], list[MemoryBlock]] = {}
        self.contains_pairs: set[tuple[str, str]] = set()
        self.calls: list[tuple[str, str]] = []

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return configured children for `block_id`."""

        self.calls.append(("children", block_id))
        return list(self.children_by_root.get(block_id, ()))

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return configured ancestors for `block_id`."""

        self.calls.append(("ancestors", block_id))
        return list(self.ancestors_by_block.get(block_id, ()))

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return configured descendants for `block_id`."""

        self.calls.append(("descendants", block_id))
        return list(self.descendants_by_root.get((block_id, include_self), ()))

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return configured ownership for a root/candidate pair."""

        self.calls.append(("contains", root_id))
        return root_id == block_id or (root_id, block_id) in self.contains_pairs


def test_standalone_link_store_updates_only_metadata() -> None:
    """`InMemoryLinkStore` keeps the link identity on updates and rejects a changed target."""

    store = InMemoryLinkStore()
    link = store.add_link(MemoryLink(source_id="source", target_id="target", relation="related"))

    updated = store.update_link(replace(link, metadata={"confidence": 0.8}))

    assert store.list_links("source") == [updated]
    with pytest.raises(MemoryConflict):
        store.update_link(replace(link, target_id="other"))
    with pytest.raises(MemoryConflict):
        store.add_link(link)


def test_standalone_link_store_validates_missing_updates_and_list_filters() -> None:
    """`InMemoryLinkStore.list_links` validates `direction` and applies the relation and endpoint filters, and `update_link` rejects an absent link."""

    store = InMemoryLinkStore()
    related = store.add_link(MemoryLink("source", "target", "related"))
    store.add_link(MemoryLink("other", "source", "different"))

    assert store.list_links("target", direction="in") == [related]
    assert store.list_links("source", relation="missing", direction="both") == []
    with pytest.raises(ValueError, match="direction must be one of"):
        store.list_links("source", direction=cast(Literal["out", "in", "both"], "sideways"))
    with pytest.raises(MemoryConflict, match="does not exist"):
        store.update_link(MemoryLink("source", "target", "related"))


def test_standalone_link_store_rejects_concurrent_duplicate_ids() -> None:
    """Of 48 concurrent adds of one link id, `InMemoryLinkStore` accepts exactly one."""

    store = InMemoryLinkStore()
    link = MemoryLink(
        source_id="source",
        target_id="target",
        relation="related",
        id="shared-link",
    )

    def add(_: int) -> bool:
        try:
            store.add_link(link)
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=12) as executor:
        outcomes = list(executor.map(add, range(48)))

    assert outcomes.count(True) == 1
    assert store.list_links("source") == [link]


def test_routed_backend_routes_root_blocks_to_owning_stores() -> None:
    """`RoutedBackend` can store independent root blocks in different route backends."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)

    block_a = memory.put(type="note", value="alpha", metadata={"route": "a"})
    block_b = memory.put(type="note", value="beta", metadata={"route": "b"})

    assert backend.route_for_block(block_a.id) == "a"
    assert backend.route_for_block(block_b.id) == "b"
    assert route_a.get_block(block_a.id) is not None
    assert route_a.get_block(block_b.id) is None
    assert route_b.get_block(block_b.id) is not None
    assert route_b.get_block(block_a.id) is None


def test_routed_backend_atomically_assigns_one_owning_route() -> None:
    """If two writers create the same block id at the same time, only one route gets the block."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    barrier = Barrier(2)
    candidates = [
        MemoryBlock(id="shared", type="note", value="a", metadata={"route": "a"}),
        MemoryBlock(id="shared", type="note", value="b", metadata={"route": "b"}),
    ]

    def put(block: MemoryBlock) -> bool:
        barrier.wait()
        try:
            backend.put_block(block)
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(put, candidates))

    assert outcomes.count(True) == 1
    assert sum(
        route.get_block("shared") is not None
        for route in (route_a, route_b)
    ) == 1


def test_routed_backend_children_inherit_parent_route() -> None:
    """A child block uses the route of its parent so that subtree operations work, and the backend rejects a child with a different route."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)

    parent = memory.put(type="project", value="tenant b", metadata={"route": "b"})
    child = memory.put(parent=parent, type="note", value="child")

    assert backend.route_for_block(parent.id) == "b"
    assert backend.route_for_block(child.id) == "b"
    assert [block.id for block in memory.children(parent)] == [child.id]
    assert [block.id for block in backend.ancestors(child.id)] == [parent.id]

    with pytest.raises(MemoryConflict):
        memory.put(parent=parent, type="note", value="wrong route", metadata={"route": "a"})


def test_routed_backend_updates_blocks_without_changing_routes() -> None:
    """A block update stays in the route that owns the block, and the backend rejects a move to a parent in another route."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)
    parent_a = memory.put(type="root", value="a", metadata={"route": "a"})
    parent_b = memory.put(type="root", value="b", metadata={"route": "b"})
    child = memory.put(parent=parent_b, type="note", value={"status": "new"})

    updated = memory.update(child, value={"status": "done"})

    assert backend.route_for_block(updated.id) == "b"
    assert route_a.get_block(updated.id) is None
    stored = route_b.get_block(updated.id)
    assert stored is not None
    assert stored.version == 2
    route_b.put_block(
        MemoryBlock(
            id=parent_a.id,
            type="shadow",
            value="route-local duplicate used to expose an invalid move",
        )
    )

    with pytest.raises(MemoryConflict, match="cannot be reparented across routes"):
        backend.update_block(
            replace(
                stored,
                parent_id=parent_a.id,
                version=stored.version + 1,
            ),
            expected_version=stored.version,
        )

    unchanged = route_b.get_block(updated.id)
    assert unchanged is not None
    assert unchanged.parent_id == parent_b.id


def test_routed_backend_merges_global_search_and_routes_scoped_search() -> None:
    """A global search merges the results of all routes, and a subtree search uses only the route of the root."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)
    root_a = memory.put(type="root", value="alpha root", metadata={"route": "a"})
    root_b = memory.put(type="root", value="beta root", metadata={"route": "b"})
    note_a = memory.put(parent=root_a, type="note", value="needle alpha")
    note_b = memory.put(parent=root_b, type="note", value="needle beta")

    global_results = memory.search("needle", scope="global")
    scoped_results = memory.search("needle", root=root_a, scope="subtree")

    assert {result.block.id for result in global_results} == {note_a.id, note_b.id}
    assert [result.block.id for result in scoped_results] == [note_a.id]


def test_routed_backend_uses_shared_search_index() -> None:
    """If the composite has a shared search index, it sends every search to that index."""

    search_index = RecordingSearchIndex()
    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        search_index=search_index,
    )
    memory = Memory(backend=backend)
    block = memory.put(type="note", value="shared index", metadata={"route": "b"})
    search_index.results = [SearchResult(block=block, score=7.0, reason="shared")]

    results = memory.search("ignored", scope="global")

    assert [result.block.id for result in results] == [block.id]
    assert results[0].score == 7.0
    assert len(search_index.queries) == 1
    assert search_index.queries[0].text == "ignored"


def test_routed_backend_uses_shared_traversal_index() -> None:
    """A shared traversal index can define parent and child ownership across routes."""

    traversal_index = RecordingTraversalIndex()
    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        traversal_index=traversal_index,
    )
    memory = Memory(backend=backend)
    root = memory.put(type="root", value="root", metadata={"route": "a"})
    cross_route_child = memory.put(type="note", value="child", metadata={"route": "b"})
    traversal_index.children_by_root[root.id] = [cross_route_child]
    traversal_index.ancestors_by_block[cross_route_child.id] = [root]
    traversal_index.descendants_by_root[(root.id, True)] = [root, cross_route_child]
    traversal_index.contains_pairs.add((root.id, cross_route_child.id))

    scope = memory.get_handle(root=root)

    assert [block.id for block in memory.children(root)] == [cross_route_child.id]
    assert [block.id for block in memory.ancestors(cross_route_child)] == [root.id]
    assert [block.id for block in backend.descendants(root.id, include_self=True)] == [
        root.id,
        cross_route_child.id,
    ]
    assert memory.contains(root, cross_route_child) is True
    assert scope.get(cross_route_child) is not None
    assert ("children", root.id) in traversal_index.calls
    assert ("contains", root.id) in traversal_index.calls


def test_routed_backend_supports_cross_route_links_with_shared_link_store() -> None:
    """A shared link store can represent relationships across routes."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        link_store=InMemoryLinkStore(),
    )
    memory = Memory(backend=backend)
    source = memory.put(type="requirement", value="source", metadata={"route": "a"})
    target = memory.put(type="note", value="target needle", metadata={"route": "b"})

    link = memory.add_link(source, target, "references")
    updated = memory.update_link(link, metadata={"confidence": 0.8})
    linked_results = memory.search("needle", root=source, scope="links")

    assert memory.list_links(source) == [updated]
    assert [result.block.id for result in linked_results] == [target.id]

    backend.delete_links_for_block(source.id)
    assert memory.list_links(source) == []


def test_routed_backend_links_search_with_shared_link_store_includes_the_root() -> None:
    """With a shared link store, a `links` search finds the root, also when the root has no links."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        link_store=InMemoryLinkStore(),
    )
    memory = Memory(backend=backend)
    root = memory.put(type="note", value="root needle", metadata={"route": "a"})
    target = memory.put(type="note", value="target needle", metadata={"route": "b"})

    assert [result.block.id for result in memory.search("needle", root=root, scope="links")] == [root.id]

    memory.add_link(root, target, "references")

    assert {result.block.id for result in memory.search("needle", root=root, scope="links")} == {root.id, target.id}


def test_routed_backend_updates_route_local_links() -> None:
    """For a route-local link, `RoutedBackend` sends the metadata update to the backend that owns the link."""

    backend = RoutedBackend({"a": InMemoryBackend()}, default_route="a")
    memory = Memory(backend=backend)
    source = memory.put(type="requirement", value="source")
    target = memory.put(type="note", value="target")
    link = memory.add_link(source, target, "references", metadata={"status": "new"})

    updated = memory.update_link(link, metadata={"status": "reviewed"})

    assert memory.list_links(source) == [updated]
    assert updated.metadata == {"status": "reviewed"}

    backend.delete_links_for_block(source.id)
    assert memory.list_links(source) == []


def test_routed_backend_supports_cross_route_mounts_as_symbolic_links() -> None:
    """A cross-route mount does not make the target an owned descendant, and a delete of the source tree keeps the target tree."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        link_store=InMemoryLinkStore(),
    )
    memory = Memory(backend=backend)
    local_root = memory.put(type="project", value="local", metadata={"route": "a"})
    mount_source = memory.put(parent=local_root, type="reference", value="remote")
    remote_root = memory.put(type="project", value="remote", metadata={"route": "b"})
    remote_child = memory.put(parent=remote_root, type="note", value="remote child")

    mount = memory.add_mount(mount_source, remote_root)

    assert memory.list_mounts(mount_source) == [mount]
    assert memory.discover_mounts(local_root) == [mount]
    assert [block.id for block in memory.children(local_root)] == [mount_source.id]
    assert remote_root.id not in {block.id for block in backend.descendants(local_root.id)}

    memory.delete(local_root)

    assert memory.get(local_root) is None
    assert memory.get(mount_source) is None
    assert memory.get(remote_root) is not None
    assert memory.get(remote_child) is not None


def test_routed_backend_can_register_route_for_discovered_pending_mount() -> None:
    """`resolve_mount` returns `None` for a pending mount until `register_route` adds the route that holds the target."""

    link_store = InMemoryLinkStore()
    backend = RoutedBackend(
        {"us": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="us",
        link_store=link_store,
    )
    memory = Memory(backend=backend)
    us_project = memory.put(type="project", value="US launch", metadata={"route": "us"})

    eu_store = InMemoryBackend()
    eu_memory = Memory(backend=eu_store)
    eu_project = eu_memory.put(type="project", value="EU rollout")
    eu_note = eu_memory.put(parent=eu_project, type="note", value="EU data residency")
    pending_mount = link_store.add_link(
        MemoryLink(
            source_id=us_project.id,
            target_id=eu_project.id,
            relation=LinkRelations.MOUNT,
            metadata={"route": "eu", "backend": "in-memory"},
        )
    )
    updated_mount = memory.update_link(pending_mount, metadata={"status": "discovered"})

    assert updated_mount.metadata == {
        "route": "eu",
        "backend": "in-memory",
        "status": "discovered",
    }
    assert memory.list_mounts(us_project) == [updated_mount]
    assert memory.discover_mounts(us_project) == [updated_mount]
    assert memory.resolve_mount(pending_mount) is None

    backend.register_route("eu", eu_store)
    resolved = memory.resolve_mount(pending_mount)
    eu_results = memory.search("data residency", root=eu_project, scope="subtree")

    assert resolved is not None
    assert resolved.id == eu_project.id
    assert backend.route_for_block(eu_project.id) == "eu"
    assert [result.block.id for result in eu_results] == [eu_note.id]


def test_routed_backend_register_route_controls_default_for_new_roots() -> None:
    """`register_route` with `make_default=True` makes the new route the default for new root blocks."""

    backend = RoutedBackend({"us": InMemoryBackend()}, default_route="us")
    memory = Memory(backend=backend)

    backend.register_route("eu", InMemoryBackend(), make_default=True)
    block = memory.put(type="note", value="uses new default")

    assert backend.route_for_block(block.id) == "eu"


def test_routed_backend_register_route_rejects_unsafe_replacement() -> None:
    """`register_route` rejects a duplicate name and the replacement of a route that holds blocks, but it can replace an empty route."""

    backend = RoutedBackend({"us": InMemoryBackend()}, default_route="us")
    memory = Memory(backend=backend)
    memory.put(type="note", value="cataloged")

    with pytest.raises(MemoryConflict):
        backend.register_route("us", InMemoryBackend())
    with pytest.raises(MemoryConflict):
        backend.register_route("us", InMemoryBackend(), replace=True)

    replaceable = RoutedBackend({"old": InMemoryBackend()}, default_route="old")
    new_backend = InMemoryBackend()

    replaceable.register_route("old", new_backend, replace=True)

    assert replaceable.routes["old"] is new_backend


def test_routed_backend_rejects_cross_route_links_without_shared_link_store() -> None:
    """Route-local link stores cannot represent cross-route relationships."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)
    source = memory.put(type="requirement", value="source", metadata={"route": "a"})
    target = memory.put(type="note", value="target", metadata={"route": "b"})

    with pytest.raises(MemoryConflict):
        memory.add_link(source, target, "references")


def test_lineage_dag_checks_cycles_in_the_route_that_owns_the_links() -> None:
    """Each route checks its own links, because route-local links never cross routes."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)
    lineage = LineageDAG(memory, relation="derived_from")
    raw = memory.put(type="dataset", value="raw", metadata={"route": "a"})
    clean = memory.put(type="dataset", value="clean", metadata={"route": "a"})
    model = memory.put(type="model", value="model", metadata={"route": "b"})
    report = memory.put(type="report", value="report", metadata={"route": "b"})

    data_edge = lineage.add(raw, clean)
    model_edge = lineage.add(model, report)

    with pytest.raises(MemoryConflict, match="cycle"):
        lineage.add(clean, raw)
    with pytest.raises(MemoryConflict, match="Cross-route"):
        lineage.add(clean, model)
    assert route_a.list_links(raw.id, relation="derived_from") == [data_edge]
    assert route_b.list_links(model.id, relation="derived_from") == [model_edge]


def test_routed_acyclic_links_need_a_capable_route() -> None:
    """If the route does not support `AcyclicLinkStore`, the composite raises `UnsupportedOperation` and writes no link."""

    memory = Memory(backend=RoutedBackend({"plain": CoreOnlyBackend()}))
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")

    with pytest.raises(UnsupportedOperation, match="Route 'plain' does not support atomic acyclic links"):
        LineageDAG(memory, relation="derived_from").add(first, second)
    assert memory.list_links(first) == []


def test_routed_acyclic_links_use_a_capable_shared_link_store() -> None:
    """A shared store that claims `AcyclicLinkStore` checks links across routes."""

    link_store = AcyclicRecordingLinkStore()
    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        link_store=link_store,
    )
    memory = Memory(backend=backend)
    source = memory.put(type="dataset", value="raw", metadata={"route": "a"})
    target = memory.put(type="model", value="model", metadata={"route": "b"})

    edge = LineageDAG(memory, relation="derived_from").add(source, target)

    assert link_store.acyclic_link_ids == [edge.id]
    assert memory.list_links(source) == [edge]


def test_routed_acyclic_links_refuse_a_shared_store_without_the_capability() -> None:
    """The links are in the shared store, so the routes cannot check cycles for it, and the composite raises `UnsupportedOperation`."""

    link_store = InMemoryLinkStore()
    memory = Memory(backend=RoutedBackend({"a": InMemoryBackend()}, link_store=link_store))
    first = memory.put(type="artifact", value="first")
    second = memory.put(type="artifact", value="second")

    with pytest.raises(UnsupportedOperation, match="shared link store does not support atomic acyclic links"):
        LineageDAG(memory, relation="derived_from").add(first, second)
    assert link_store.list_links(first.id, direction="both") == []


def test_routed_backend_rejects_invalid_routes_and_duplicate_ids() -> None:
    """`RoutedBackend` rejects an empty route table, an unknown route name, and a block id that another route already owns.

    An unknown route from the route selector raises `UnsupportedOperation`,
    the same error as an unknown route in a route catalog.
    """

    with pytest.raises(ValueError, match="requires at least one route"):
        RoutedBackend({})
    with pytest.raises(ValueError):
        RoutedBackend({"a": InMemoryBackend()}, default_route="missing")

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend)
    memory.put(id="same-id", type="note", value="first", metadata={"route": "a"})

    with pytest.raises(UnsupportedOperation, match="route 'missing', which this RoutedBackend does not have") as raised:
        memory.put(id="bad-route", type="note", value="bad", metadata={"route": "missing"})
    assert not isinstance(raised.value, MemoryConflict)
    with pytest.raises(MemoryConflict):
        memory.put(id="same-id", type="note", value="second", metadata={"route": "b"})


def test_routed_backend_missing_read_paths_are_empty_and_mutations_are_safe_noops() -> None:
    """For an unknown id, reads return empty results, and `delete_block` and `delete_links_for_block` do nothing."""

    backend = RoutedBackend({"a": InMemoryBackend()}, default_route="a")

    assert backend.get_block("missing") is None
    assert backend.children("missing") == []
    assert backend.ancestors("missing") == []
    assert backend.descendants("missing", include_self=True) == []
    assert backend.contains("missing", "also-missing") is False
    assert backend.list_links("missing", direction="both") == []
    assert backend.search(
        MemoryQuery(root_id="missing", scope="subtree")
    ) == []
    backend.delete_block("missing")
    backend.delete_links_for_block("missing")


def test_routed_backend_detects_ambiguous_preexisting_ownership() -> None:
    """If a block id exists in more than one route, each lookup raises `BackendCorruption` and does not choose a route at random.

    The error is not a `MemoryConflict`, because a retry with fresh state
    cannot repair stored data that breaks the one-route rule.
    """

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    route_a.put_block(MemoryBlock(id="duplicate", type="note", value="a", created_by="system"))
    route_b.put_block(MemoryBlock(id="duplicate", type="note", value="b", created_by="system"))
    backend = RoutedBackend({"a": route_a, "b": route_b}, default_route="a")
    memory = Memory(backend=backend)

    with pytest.raises(BackendCorruption, match=r"exists in multiple routes: \['a', 'b'\]") as raised:
        backend.route_for_block("duplicate")
    assert not isinstance(raised.value, MemoryConflict)
    with pytest.raises(BackendCorruption):
        backend.get_block("duplicate")
    with pytest.raises(BackendCorruption):
        memory.get("duplicate")
    with pytest.raises(BackendCorruption):
        memory.put(id="duplicate", type="note", value="c")
    with pytest.raises(BackendCorruption):
        backend.delete_block("duplicate")
    assert route_a.get_block("duplicate") is not None
    assert route_b.get_block("duplicate") is not None
    with pytest.raises(MemoryConflict, match="does not exist"):
        backend.update_block(
            MemoryBlock(id="missing", type="note", value="missing", version=2)
        )


def test_shared_link_search_applies_type_and_filter_constraints() -> None:
    """A portable search in the `links` scope applies the type filter and metadata filters before it ranks the results."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        link_store=InMemoryLinkStore(),
    )
    memory = Memory(backend=backend)
    source = memory.put(type="root", value="source", metadata={"route": "a"})
    selected = memory.put(
        type="note",
        value="needle selected",
        metadata={"route": "b", "priority": 2},
    )
    wrong_type = memory.put(
        type="task",
        value="needle task",
        metadata={"route": "b", "priority": 2},
    )
    wrong_filter = memory.put(
        type="note",
        value="needle low",
        metadata={"route": "b", "priority": 1},
    )
    for target in (selected, wrong_type, wrong_filter):
        memory.add_link(source, target, "related")

    results = memory.search(
        "needle",
        root=source,
        scope="links",
        types=["note"],
        filters={"priority": {">=": 2}},
    )

    assert [result.block.id for result in results] == [selected.id]


def test_routed_backend_discovers_preexisting_route_blocks() -> None:
    """The route cache can discover blocks that already exist inside a route."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    existing = route_b.put_block(
        MemoryBlock(id="existing", type="note", value="stored", created_by="system")
    )
    backend = RoutedBackend({"a": route_a, "b": route_b}, default_route="a")
    memory = Memory(backend=backend)

    fetched = memory.get(existing.id)

    assert fetched is not None
    assert fetched.id == existing.id
    assert backend.route_for_block(existing.id) == "b"


def test_routed_backend_delete_cleans_route_cache_and_shared_links() -> None:
    """A recursive delete removes the route cache entries and the shared links of the root and its descendants."""

    link_store = RecordingLinkStore()
    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
        link_store=link_store,
    )
    memory = Memory(backend=backend)
    outside = memory.put(type="note", value="outside", metadata={"route": "a"})
    root = memory.put(type="project", value="remove me", metadata={"route": "b"})
    child = memory.put(parent=root, type="note", value="child")
    memory.add_link(outside, child, "references")

    memory.delete(root)

    assert memory.get(root.id) is None
    assert memory.get(child.id) is None
    assert backend.route_for_block(root.id) is None
    assert backend.route_for_block(child.id) is None
    assert link_store.deleted_blocks == [root.id, child.id]
    assert memory.list_links(outside) == []


def test_routed_backend_works_with_memory_layer_extensions() -> None:
    """Hooks, codecs, and policies continue to work through routed storage."""

    hook = AuditHook()
    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    owner = Memory(
        backend=backend,
        principal="owner",
        hooks=[hook],
        codecs=CodecRegistry([JsonCodec(), PreferenceCodec()]),
    )
    private = owner.put(
        type="preference",
        value=Preference("tone", "warm"),
        metadata={"route": "b"},
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    reader = Memory(
        backend=backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    retrieved = owner.get(private)

    assert retrieved is not None
    assert backend.route_for_block(private.id) == "b"
    assert hook.created == [private.id]
    assert retrieved.value == Preference("tone", "warm")
    with pytest.raises(AccessDenied):
        reader.get(private)


def test_structure_facades_use_the_route_that_owns_the_container() -> None:
    """A structure facade works over `RoutedBackend` if the facade uses the route that owns the container as its store."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend, principal="app:demo")
    catalog_root = memory.put(type="collection", value="catalog", metadata={"route": "a"})
    log_root = memory.put(type="log", value="events", metadata={"route": "a"})
    config = memory.put(type="config", value="retry budget is three", metadata={"route": "a"})
    event = memory.put(type="event", value="retry budget changed", metadata={"route": "a"})

    # The access object is still the `Memory` over the composite backend. Only
    # the structure records are in the route that owns the container block.
    catalog = KeyedCollection(memory, route_a, collection=catalog_root)
    events = EventLog(memory, route_a, log=log_root)
    address = MemoryAddress(("service",), "retry")
    catalog.bind(address, config)
    events.append(event)

    stored = catalog.get(address)

    assert stored is not None
    assert stored.id == config.id
    assert [item.block.id for item in catalog.items()] == [config.id]
    assert [item.block.id for item in events.read()] == [event.id]
    assert backend.route_for_block(config.id) == "a"


def test_structure_facade_membership_stays_inside_one_route() -> None:
    """The composite is not a structure store, and membership cannot span routes."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend, principal="app:demo")
    catalog_root = memory.put(type="collection", value="catalog", metadata={"route": "a"})
    far_block = memory.put(type="config", value="timeout budget is ten", metadata={"route": "b"})

    # The composite is not a structure store, but it sends calls to the route that is one.
    catalog = KeyedCollection(memory, backend, collection=catalog_root)

    with pytest.raises(MemoryConflict):
        catalog.bind(MemoryAddress(("service",), "timeout"), far_block)


def test_backend_for_block_resolves_the_owning_route() -> None:
    """`backend_for_block` returns the adapter that owns a block, or `None` for an unknown id."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend, principal="app:demo")
    here = memory.put(type="note", value="local", metadata={"route": "a"})
    there = memory.put(type="note", value="remote", metadata={"route": "b"})
    child = memory.put(type="note", value="child", parent=there, metadata={"route": "b"})

    assert backend.backend_for_block(here.id) is route_a
    assert backend.backend_for_block(there.id) is route_b
    # A tree is always in one route, so a child resolves to the adapter of its parent.
    assert backend.backend_for_block(child.id) is route_b
    assert backend.backend_for_block("missing") is None


def test_structure_facade_binds_through_backend_for_block() -> None:
    """An application can use the optional capabilities of a route through `backend_for_block`, without the route table."""

    route_a = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend, principal="app:demo")
    log_root = memory.put(type="log", value="events", metadata={"route": "a"})
    event = memory.put(type="event", value="retry budget changed", metadata={"route": "a"})

    log_route = backend.backend_for_block(log_root.id)
    assert log_route is route_a
    # `MemoryBackend` does not include the optional structure protocols. So the
    # test checks that a route it resolves by hand has the capability before the
    # test uses that route as a store.
    assert isinstance(log_route, SequenceStore)
    events = EventLog(memory, log_route, log=log_root)
    events.append(event)

    assert [item.block.id for item in events.read()] == [event.id]


def test_routed_backend_claims_only_the_link_capability() -> None:
    """The composite routes links, so it claims `AcyclicLinkStore` and no record store."""

    backend = RoutedBackend({"a": InMemoryBackend()})

    assert isinstance(backend, MemoryBackend)
    assert isinstance(backend, AcyclicLinkStore)
    for capability in (KeyedStore, SequenceStore, TemporalStore, TemporalIndex):
        assert not isinstance(backend, capability)


def test_a_tree_cannot_span_routes() -> None:
    """The composite rejects a parent in a different route on create and on update."""

    backend = RoutedBackend(
        {"a": InMemoryBackend(), "b": InMemoryBackend()},
        route_selector=route_by_metadata,
        default_route="a",
    )
    parent = backend.put_block(MemoryBlock(type="folder", value="parent", id="p1", metadata={"route": "a"}))
    orphan = backend.put_block(MemoryBlock(type="note", value="orphan", id="n1", metadata={"route": "b"}))

    with pytest.raises(MemoryConflict, match="does not match parent route"):
        backend.put_block(MemoryBlock(type="note", value="child", id="n2", parent_id=parent.id, metadata={"route": "b"}))

    with pytest.raises(MemoryConflict, match="cannot be reparented across routes"):
        backend.update_block(orphan.with_updates(parent_id=parent.id))

    # This invariant keeps route-local traversal exact.
    assert backend.children(parent.id) == []
    assert backend.contains(parent.id, orphan.id) is False


def test_facade_accepts_the_composite_and_routes_to_the_owning_adapter() -> None:
    """A facade that gets the composite binds to the route that holds the records."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    backend = RoutedBackend(
        {"a": route_a, "b": route_b},
        route_selector=route_by_metadata,
        default_route="a",
    )
    memory = Memory(backend=backend, principal="app:demo")
    log_root = memory.put(type="log", value="events", metadata={"route": "a"})
    event = memory.put(type="event", value="deploy started", metadata={"route": "a"})

    # The test does not resolve the route by hand. The facade takes the composite directly.
    EventLog(memory, backend, log=log_root).append(event)

    # The entry is in route "a". A facade bound directly to that adapter reads it.
    assert [item.block.id for item in EventLog(memory, route_a, log=log_root).read()] == [event.id]
    assert EventLog(memory, route_b, log=log_root).read() == []


def test_facade_reports_an_unknown_container_rather_than_a_missing_capability() -> None:
    """If no route owns the container, the facade raises `MemoryNotFound`, not an error about an unsupported capability."""

    backend = RoutedBackend({"a": InMemoryBackend()})
    memory = Memory(backend=backend, principal="app:demo")
    log_root = memory.put(type="log", value="events")
    memory.delete(log_root.id)

    with pytest.raises(MemoryNotFound):
        EventLog(memory, backend, log=log_root)


def test_composite_still_advertises_no_structure_capability() -> None:
    """The composite routes for facades but does not claim a storage capability such as `SequenceStore` or `KeyedStore`."""

    backend = RoutedBackend({"a": InMemoryBackend()})

    assert isinstance(backend, BlockRouter)
    assert not isinstance(backend, SequenceStore)
    assert not isinstance(backend, KeyedStore)


def test_routes_mapping_is_read_only() -> None:
    """Only `register_route` replaces a route, and a direct assignment to `routes` raises `TypeError`."""

    backend = RoutedBackend({"a": InMemoryBackend()})

    assert "a" in backend.routes
    with pytest.raises(TypeError):
        backend.routes["a"] = InMemoryBackend()  # type: ignore[index]

    # `routes` is a live view, not a copy.
    backend.register_route("c", InMemoryBackend())
    assert sorted(backend.routes) == ["a", "c"]


def test_route_replacement_probes_the_backend_when_the_route_cache_is_cold(tmp_path: Any) -> None:
    """`register_route` does not replace a route that still holds blocks, even in a newly opened composite.

    The route cache fills only on demand, so a new composite has no cache
    entries, and the replacement check asks the route backend directly.
    """

    database = tmp_path / "genie.db"
    first = RoutedBackend({"a": SQLiteBackend(database), "b": InMemoryBackend()}, default_route="a")
    stored = Memory(backend=first, principal="app:demo").put(type="note", value="persisted")

    # Open the same store again, as after a restart.
    second = RoutedBackend({"a": SQLiteBackend(database), "b": InMemoryBackend()}, default_route="a")

    with pytest.raises(MemoryConflict, match="still holds blocks"):
        second.register_route("a", InMemoryBackend(), replace=True)

    assert Memory(backend=second, principal="app:demo").get(stored.id) is not None


def test_route_replacement_allows_a_genuinely_empty_route(tmp_path: Any) -> None:
    """`register_route` can replace a route with an empty backend, so the probe does not reject every replacement."""

    database = tmp_path / "empty.db"
    backend = RoutedBackend({"a": SQLiteBackend(database), "b": InMemoryBackend()}, default_route="b")
    replacement = InMemoryBackend()

    backend.register_route("a", replacement, replace=True)

    assert backend.routes["a"] is replacement


class RejectingBackend(InMemoryBackend):
    """Route backend that refuses each new block, as a route does for a value it cannot store."""

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Refuse the block and store nothing."""

        raise ValueError("This route refuses the block.")


class LostReplyBackend(InMemoryBackend):
    """Route backend that stores each new block and then loses the reply, as after a dropped connection."""

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store the block, then raise as if the commit reply did not arrive."""

        super().put_block(block)
        raise BackendUnavailable("The reply of the commit did not arrive.")


class FailingReleaseCatalog(InMemoryBackend):
    """Route catalog that records claims but cannot remove them."""

    def release_routes(self, block_ids: Sequence[str]) -> None:
        """Fail as if the catalog store is not available."""

        raise BackendUnavailable("The route catalog is not available.")


def open_routed(
    us: MemoryBackend,
    eu: MemoryBackend,
    *,
    route_catalog: RouteCatalog | None = None,
) -> RoutedBackend:
    """Return a composite over the routes `us` and `eu`, as one worker process opens it."""

    return RoutedBackend(
        {"us": us, "eu": eu},
        route_selector=route_by_metadata,
        default_route="us",
        route_catalog=route_catalog,
    )


def test_a_stale_route_cache_entry_does_not_hide_a_moved_block() -> None:
    """A composite checks a cached route before it uses it, so it finds a block that another instance stored again in another route.

    Without a route catalog. `second` deletes `customer-7` and stores the id
    again in route `eu`, while `first` still has route `us` in its route cache.
    """

    us = InMemoryBackend()
    eu = InMemoryBackend()
    first = Memory(backend=open_routed(us, eu))
    second = Memory(backend=open_routed(us, eu))
    first.put(id="customer-7", type="record", value="first")
    assert first.get("customer-7") is not None
    second.delete("customer-7")
    second.put(id="customer-7", type="record", value="second", metadata={"route": "eu"})

    moved = first.get("customer-7")
    assert moved is not None
    assert moved.value == "second"
    with pytest.raises(MemoryConflict, match="already exists"):
        first.put(id="customer-7", type="record", value="third")
    assert us.get_block("customer-7") is None
    first.delete("customer-7")
    assert eu.get_block("customer-7") is None


def test_routed_backends_that_share_a_route_catalog_store_one_id_in_one_route() -> None:
    """Of 8 composite instances that put one id at the same time, each into a route of its own choice, only one stores it."""

    us = InMemoryBackend()
    eu = InMemoryBackend()
    route_catalog = InMemoryBackend()
    backends = [open_routed(us, eu, route_catalog=route_catalog) for _ in range(8)]
    candidates = [
        MemoryBlock(id="shared", type="note", value=index, metadata={"route": "us" if index % 2 else "eu"})
        for index in range(len(backends))
    ]
    barrier = Barrier(len(backends))

    def put(backend: RoutedBackend, block: MemoryBlock) -> bool:
        barrier.wait()
        try:
            backend.put_block(block)
        except MemoryConflict:
            return False
        return True

    with ThreadPoolExecutor(max_workers=len(backends)) as executor:
        outcomes = list(executor.map(put, backends, candidates))

    assert outcomes.count(True) == 1
    holders = [name for name, route in (("us", us), ("eu", eu)) if route.get_block("shared") is not None]
    assert holders == [cast(str, candidates[outcomes.index(True)].metadata["route"])]
    assert route_catalog.get_route("shared") == RouteInfo("shared", holders[0])


def test_routed_backends_over_sqlite_files_keep_ids_unique_with_a_route_catalog(tmp_path: Any) -> None:
    """Composites with their own SQLite connections to the same route and catalog files keep each id in one route.

    Each composite stands for one worker process. The test covers both
    failures of GENIE-17: 2 concurrent puts of one id into different routes,
    and a put from an instance whose route cache is stale.
    """

    opened: list[SQLiteBackend] = []

    def open_worker() -> RoutedBackend:
        us, eu, route_catalog = (SQLiteBackend(tmp_path / name) for name in ("us.db", "eu.db", "routes.db"))
        opened.extend((us, eu, route_catalog))
        return open_routed(us, eu, route_catalog=route_catalog)

    try:
        workers = [open_worker() for _ in range(2)]
        barrier = Barrier(len(workers))

        def put(backend: RoutedBackend, route: str) -> bool:
            barrier.wait()
            try:
                backend.put_block(MemoryBlock(id="raced", type="note", value=route, metadata={"route": route}))
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=len(workers)) as executor:
            outcomes = list(executor.map(put, workers, ["us", "eu"]))
        assert outcomes.count(True) == 1

        first = Memory(backend=workers[0])
        second = Memory(backend=workers[1])
        first.put(id="customer-7", type="record", value="first")
        assert first.get("customer-7") is not None
        second.delete("customer-7")
        second.put(id="customer-7", type="record", value="second", metadata={"route": "eu"})
        with pytest.raises(MemoryConflict):
            first.put(id="customer-7", type="record", value="third")

        fresh_backend = open_worker()
        stored = Memory(backend=fresh_backend).get("customer-7")
        assert stored is not None
        assert stored.value == "second"
        assert fresh_backend.get_block("raced") is not None
    finally:
        for backend in opened:
            backend.close()


def test_route_catalog_entries_follow_puts_and_deletes() -> None:
    """A put claims an entry for the block and for each child, and a recursive delete releases all of them, so the ids can move to another route."""

    us = InMemoryBackend()
    eu = InMemoryBackend()
    route_catalog = InMemoryBackend()
    memory = Memory(backend=open_routed(us, eu, route_catalog=route_catalog))
    root = memory.put(id="project", type="project", value="root", metadata={"route": "eu"})
    child = memory.put(id="task", parent=root, type="task", value="child")

    assert route_catalog.get_route(root.id) == RouteInfo(root.id, "eu")
    assert route_catalog.get_route(child.id) == RouteInfo(child.id, "eu")

    memory.delete(root)

    assert route_catalog.get_route(root.id) is None
    assert route_catalog.get_route(child.id) is None
    other = Memory(backend=open_routed(us, eu, route_catalog=route_catalog))
    other.put(id="project", type="project", value="moved")
    assert route_catalog.get_route("project") == RouteInfo("project", "us")
    assert us.get_block("project") is not None


def test_a_failed_route_write_releases_its_claim() -> None:
    """If the selected route refuses the block, the composite releases the claim, so a later put of the id can succeed."""

    route_catalog = InMemoryBackend()
    backend = open_routed(RejectingBackend(), InMemoryBackend(), route_catalog=route_catalog)

    with pytest.raises(ValueError, match="refuses"):
        backend.put_block(MemoryBlock(id="refused", type="note", value="us"))

    assert route_catalog.get_route("refused") is None
    backend.put_block(MemoryBlock(id="refused", type="note", value="eu", metadata={"route": "eu"}))
    assert route_catalog.get_route("refused") == RouteInfo("refused", "eu")


def test_a_claim_stays_when_the_failed_write_reached_the_route() -> None:
    """A write that stored the block but lost its reply keeps the claim, so the id cannot get a second route."""

    us = LostReplyBackend()
    route_catalog = InMemoryBackend()
    backend = open_routed(us, InMemoryBackend(), route_catalog=route_catalog)

    with pytest.raises(BackendUnavailable):
        backend.put_block(MemoryBlock(id="landed", type="note", value="us"))

    assert route_catalog.get_route("landed") == RouteInfo("landed", "us")
    assert backend.get_block("landed") is not None
    with pytest.raises(MemoryConflict):
        backend.put_block(MemoryBlock(id="landed", type="note", value="eu", metadata={"route": "eu"}))


def test_a_catalog_entry_with_no_block_holds_the_id_until_it_is_released() -> None:
    """A claim that a failure left behind reads as no block, refuses a new put of the id, and `release_routes` clears it."""

    route_catalog = InMemoryBackend()
    route_catalog.claim_route(RouteInfo("orphan", "eu"))
    backend = open_routed(InMemoryBackend(), InMemoryBackend(), route_catalog=route_catalog)

    assert backend.get_block("orphan") is None
    assert backend.route_for_block("orphan") is None
    with pytest.raises(MemoryConflict, match="route catalog entry"):
        backend.put_block(MemoryBlock(id="orphan", type="note", value="us"))

    route_catalog.release_routes(["orphan"])
    backend.put_block(MemoryBlock(id="orphan", type="note", value="us"))
    assert backend.route_for_block("orphan") == "us"


def test_route_catalog_still_finds_blocks_stored_without_it() -> None:
    """A block that a route already held before the catalog was in use is still found, and its id cannot go to a second route."""

    eu = InMemoryBackend()
    eu.put_block(MemoryBlock(id="existing", type="note", value="eu"))
    route_catalog = InMemoryBackend()
    backend = open_routed(InMemoryBackend(), eu, route_catalog=route_catalog)

    assert backend.route_for_block("existing") == "eu"
    with pytest.raises(MemoryConflict, match="already exists"):
        backend.put_block(MemoryBlock(id="existing", type="note", value="us"))
    assert route_catalog.get_route("existing") is None


def test_route_catalog_entry_for_an_unconfigured_route_raises() -> None:
    """An entry that names a route this composite does not have raises `UnsupportedOperation`, and does not read as a missing block.

    Another instance registered route `apac` and stored the block there. This
    instance reads the block after it registers the same route.
    """

    apac = InMemoryBackend()
    stored = apac.put_block(MemoryBlock(id="elsewhere", type="note", value="apac", created_by="system"))
    route_catalog = InMemoryBackend()
    route_catalog.claim_route(RouteInfo(stored.id, "apac"))
    backend = open_routed(InMemoryBackend(), InMemoryBackend(), route_catalog=route_catalog)
    memory = Memory(backend=backend)

    with pytest.raises(UnsupportedOperation, match="route 'apac', which this RoutedBackend does not have") as raised:
        backend.get_block(stored.id)
    assert not isinstance(raised.value, MemoryConflict)
    with pytest.raises(UnsupportedOperation):
        memory.get(stored.id)
    with pytest.raises(UnsupportedOperation):
        memory.put(id=stored.id, type="note", value="us")

    backend.register_route("apac", apac)

    assert backend.get_block(stored.id) == stored
    assert backend.route_for_block(stored.id) == "apac"


def test_a_put_to_an_unconfigured_route_raises_before_it_claims_the_id() -> None:
    """A put that the route selector sends to an unknown route raises `UnsupportedOperation`, and leaves no catalog entry.

    The put succeeds after the instance registers the route.
    """

    route_catalog = InMemoryBackend()
    backend = open_routed(InMemoryBackend(), InMemoryBackend(), route_catalog=route_catalog)
    block = MemoryBlock(id="tenant-note", type="note", value="apac", metadata={"route": "apac"})

    with pytest.raises(UnsupportedOperation, match="route 'apac', which this RoutedBackend does not have"):
        backend.put_block(block)
    assert route_catalog.get_route(block.id) is None

    backend.register_route("apac", InMemoryBackend())
    backend.put_block(block)
    assert route_catalog.get_route(block.id) == RouteInfo(block.id, "apac")


def test_a_route_backend_can_hold_the_route_catalog() -> None:
    """Route entries do not share the id space of blocks, so one route backend can store blocks and the catalog together."""

    us = InMemoryBackend()
    eu = InMemoryBackend()
    memory = Memory(backend=open_routed(us, eu, route_catalog=us))
    local = memory.put(id="local", type="note", value="us")
    remote = memory.put(id="remote", type="note", value="eu", metadata={"route": "eu"})

    assert us.get_route(local.id) == RouteInfo(local.id, "us")
    assert us.get_route(remote.id) == RouteInfo(remote.id, "eu")
    assert us.get_block(remote.id) is None
    assert memory.get(remote.id) == remote
    memory.delete(remote)
    assert us.get_route(remote.id) is None


def test_routed_backend_rejects_a_route_catalog_without_the_capability() -> None:
    """A `route_catalog` that does not implement `RouteCatalog` raises `TypeError` at construction."""

    with pytest.raises(TypeError, match="RouteCatalog"):
        open_routed(InMemoryBackend(), InMemoryBackend(), route_catalog=cast(RouteCatalog, CoreOnlyBackend()))


def test_a_failed_catalog_release_does_not_undo_a_committed_delete(caplog: pytest.LogCaptureFixture) -> None:
    """If the catalog cannot release the entries, the delete still succeeds, a warning is logged, and the entries stay."""

    route_catalog = FailingReleaseCatalog()
    memory = Memory(backend=open_routed(InMemoryBackend(), InMemoryBackend(), route_catalog=route_catalog))
    root = memory.put(id="root", type="project", value="root")
    memory.put(id="leaf", parent=root, type="task", value="leaf")

    with caplog.at_level("WARNING", logger="genie.backend.composite.routed"):
        memory.delete(root)

    assert memory.get(root.id) is None
    assert route_catalog.get_route("root") == RouteInfo("root", "us")
    assert route_catalog.get_route("leaf") == RouteInfo("leaf", "us")
    assert any("stay" in record.getMessage() for record in caplog.records)
    with pytest.raises(MemoryConflict, match="route catalog entry"):
        memory.put(id="root", type="project", value="again")


class TestRoutedStructureBackendContracts(LinkStoreContract, AcyclicLinkStoreContract):
    """Run the link contracts against a composite with route-local links."""

    def make_backend(self) -> RoutedBackend:
        """Return a composite with two routes, where new root blocks go to route `a`."""

        return RoutedBackend({"a": InMemoryBackend(), "b": InMemoryBackend()}, default_route="a")


class TestRoutedStructureBackendContractsWithRouteCatalog(TestRoutedStructureBackendContracts):
    """Run the link contracts against a composite that claims each block id in a shared route catalog."""

    def make_backend(self) -> RoutedBackend:
        """Return a composite with two routes and an in-memory route catalog, where new root blocks go to route `a`."""

        return RoutedBackend(
            {"a": InMemoryBackend(), "b": InMemoryBackend()},
            default_route="a",
            route_catalog=InMemoryBackend(),
        )
