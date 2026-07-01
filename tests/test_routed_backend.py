"""Tests for routed backend composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    CodecRegistry,
    EncodedValue,
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
)
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict


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

    def after_put(self, block: MemoryBlock) -> None:
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


def test_routed_backend_routes_root_blocks_to_authoritative_stores() -> None:
    """Independent root blocks can live in different authoritative stores."""

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

    with pytest.raises(MemoryConflict):
        memory.put(parent=parent, type="note", value="wrong route", metadata={"route": "a"})


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


def test_routed_backend_delegates_to_shared_search_index() -> None:
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
    linked_results = memory.search("needle", root=source, scope="links")

    assert memory.list_links(source) == [link]
    assert [result.block.id for result in linked_results] == [target.id]


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

    assert memory.list_mounts(us_project) == [pending_mount]
    assert memory.discover_mounts(us_project) == [pending_mount]
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
    """Route validation protects the one-authority-per-block invariant."""

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


def test_routed_backend_discovers_preexisting_route_blocks() -> None:
    """The catalog can discover blocks that already exist inside a route."""

    route_a = InMemoryBackend()
    route_b = InMemoryBackend()
    existing = route_b.put_block(MemoryBlock(id="existing", type="note", value="stored"))
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
