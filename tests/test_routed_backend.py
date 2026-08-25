"""Tests for routed backend composition."""

from __future__ import annotations

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
    RoutedBackend,
    SearchResult,
    UnsupportedOperation,
)
from genie.backend.base import MemoryBackend
from genie.backend.structure_capabilities import (
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalIndex,
    TemporalStore,
)
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict
from genie.structures import EventLog, KeyedCollection, MemoryAddress


def route_by_metadata(block: MemoryBlock) -> str | None:
    """Choose a route from block metadata when provided."""

    route = block.metadata.get("route")
    return route if isinstance(route, str) else None


@dataclass(frozen=True)
class Preference:
    """Custom value used to prove routed backends work with codecs."""

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
        """Create an empty recording index."""

        self.queries: list[MemoryQuery] = []
        self.results: list[SearchResult] = []

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Record the query and return configured results."""

        self.queries.append(query)
        return list(self.results)


class RecordingLinkStore:
    """Shared link store that records cleanup calls."""

    def __init__(self) -> None:
        """Create an empty recording link store."""

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
        """Record and clean links connected to a block."""

        self.deleted_blocks.append(block_id)
        self.inner.delete_links_for_block(block_id)


class RecordingTraversalIndex:
    """Shared traversal index with explicit cross-route relationships."""

    def __init__(self) -> None:
        """Create an empty recording traversal index."""

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
    """The shared reference link store preserves link identity on updates."""

    store = InMemoryLinkStore()
    link = store.add_link(MemoryLink(source_id="source", target_id="target", relation="related"))

    updated = store.update_link(replace(link, metadata={"confidence": 0.8}))

    assert store.list_links("source") == [updated]
    with pytest.raises(MemoryConflict):
        store.update_link(replace(link, target_id="other"))
    with pytest.raises(MemoryConflict):
        store.add_link(link)


def test_standalone_link_store_validates_missing_updates_and_list_filters() -> None:
    """Standalone link reads validate direction and honor relation and endpoint filters."""

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
    """The shared reference store atomically claims each link identity."""

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
    """Independent root blocks can live in different backend stores."""

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
    """Concurrent creates cannot place one block id in multiple routes."""

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
    """Child blocks inherit the parent route to preserve subtree operations."""

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
    """Block updates stay in their owning route and cannot cross-route reparent."""

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
    """Global search merges routes while subtree search stays on the root route."""

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
    """A configured shared search index owns routed search execution."""

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
    """Shared traversal indexes can define parent/child ownership across routes."""

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


def test_routed_backend_updates_route_local_links() -> None:
    """Route-local links delegate metadata updates to their owning backend."""

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
    """Cross-route mounts do not turn the target into an owned descendant."""

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
    """A newly registered route can resolve a previously pending mount target."""

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
    """A runtime route can become the default for future root blocks."""

    backend = RoutedBackend({"us": InMemoryBackend()}, default_route="us")
    memory = Memory(backend=backend)

    backend.register_route("eu", InMemoryBackend(), make_default=True)
    block = memory.put(type="note", value="uses new default")

    assert backend.route_for_block(block.id) == "eu"


def test_routed_backend_register_route_rejects_unsafe_replacement() -> None:
    """Route registration rejects duplicates and cataloged-route replacement."""

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


def test_routed_backend_rejects_invalid_routes_and_duplicate_ids() -> None:
    """Route validation keeps each block owned by one backend route."""

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

    with pytest.raises(MemoryConflict):
        memory.put(id="bad-route", type="note", value="bad", metadata={"route": "missing"})
    with pytest.raises(MemoryConflict):
        memory.put(id="same-id", type="note", value="second", metadata={"route": "b"})


def test_routed_backend_missing_read_paths_are_empty_and_mutations_are_safe_noops() -> None:
    """Unknown ids stay empty on reads and harmless cleanup operations."""

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
    """A block discovered in multiple routes is rejected instead of chosen arbitrarily."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    route_a.put_block(MemoryBlock(id="duplicate", type="note", value="a"))
    route_b.put_block(MemoryBlock(id="duplicate", type="note", value="b"))
    backend = RoutedBackend({"a": route_a, "b": route_b}, default_route="a")

    with pytest.raises(MemoryConflict, match="exists in multiple routes"):
        backend.route_for_block("duplicate")
    with pytest.raises(MemoryConflict, match="does not exist"):
        backend.update_block(
            MemoryBlock(id="missing", type="note", value="missing", version=2)
        )


def test_shared_link_search_applies_type_and_filter_constraints() -> None:
    """Portable linked search applies every query constraint before ranking."""

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
    """The catalog can discover blocks that already exist inside a route."""

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


def test_routed_backend_delete_cleans_catalog_and_shared_links() -> None:
    """Recursive delete removes catalog state and shared links for descendants."""

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
    """Facades work over a routed backend when the owning route is the store."""

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

    # The access object stays the composite-backed Memory; only the structure
    # records live in the route that owns the container block.
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

    with pytest.raises(UnsupportedOperation):
        KeyedCollection(memory, backend, collection=catalog_root)

    catalog = KeyedCollection(memory, route_a, collection=catalog_root)

    with pytest.raises(MemoryConflict):
        catalog.bind(MemoryAddress(("service",), "timeout"), far_block)


def test_backend_for_block_resolves_the_owning_route() -> None:
    """The routing primitive returns the adapter that owns a block."""

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
    # A tree never spans routes, so a child resolves to its parent's adapter.
    assert backend.backend_for_block(child.id) is route_b
    assert backend.backend_for_block("missing") is None


def test_structure_facade_binds_through_backend_for_block() -> None:
    """Applications reach a route's optional capabilities without the route table."""

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
    events = EventLog(memory, log_route, log=log_root)
    events.append(event)

    assert [item.block.id for item in events.read()] == [event.id]


def test_routed_backend_does_not_claim_structure_capabilities() -> None:
    """The composite satisfies the core contract and none of the optional ones."""

    backend = RoutedBackend({"a": InMemoryBackend()})

    assert isinstance(backend, MemoryBackend)
    for capability in (AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore, TemporalIndex):
        assert not isinstance(backend, capability)


def test_a_tree_cannot_span_routes() -> None:
    """Cross-route parenting is rejected by the composite, on create and on update."""

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

    # The invariant is what makes route-local traversal exact.
    assert backend.children(parent.id) == []
    assert backend.contains(parent.id, orphan.id) is False
