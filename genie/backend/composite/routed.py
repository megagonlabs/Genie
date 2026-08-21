"""Routed backend composition helpers.

`RoutedBackend` lets one public `MemoryBackend` coordinate multiple internal
backends. Each memory block has exactly one owning route, while search,
traversal, and link interfaces may be route-local or supplied as shared
indexes/stores.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from typing import Literal, overload

from genie.backend.base import MemoryBackend
from genie.backend.capabilities import LinkStore, SearchIndex, TraversalIndex
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict
from genie.memory.links import MemoryLink
from genie.query.filters import matches_filters
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult
from genie.query.scope import Scope

RouteKey = str
RouteSelector = Callable[[MemoryBlock], RouteKey | None]


@dataclass(frozen=True, slots=True)
class RouteInfo:
    """Catalog entry describing which route owns a block.

    Attributes:
        block_id: Memory block id.
        route: Route key for the backend that owns the block.
    """

    block_id: str
    route: RouteKey


class InMemoryLinkStore:
    """Simple standalone link store for routed backends.

    Unlike `InMemoryBackend`, this store does not own memory blocks and does not
    validate endpoint existence. `RoutedBackend` performs endpoint validation
    before writing links to a shared link store.
    """

    def __init__(self) -> None:
        """Create an empty link store."""

        self._links: dict[str, MemoryLink] = {}
        self._lock = RLock()

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a link.

        Args:
            link: Link to store.

        Returns:
            Defensive copy of the stored link.

        Raises:
            MemoryConflict: If the link id already exists.
        """

        with self._lock:
            if link.id in self._links:
                raise MemoryConflict(f"Memory link {link.id!r} already exists.")
            link = _copy_link(link)
            self._links[link.id] = link
            return _copy_link(link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace the metadata of an existing link.

        Args:
            link: Link containing the replacement metadata.

        Returns:
            Defensive copy of the stored replacement link.

        Raises:
            MemoryConflict: If the link is missing or any field other than
                metadata changed.
        """

        with self._lock:
            previous = self._links.get(link.id)
            if previous is None:
                raise MemoryConflict(f"Memory link {link.id!r} does not exist.")
            if not _same_link_identity(previous, link):
                raise MemoryConflict("Link updates can change only metadata.")
            link = _copy_link(link)
            self._links[link.id] = link
            return _copy_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links connected to a block.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Defensive copies of matching links.

        Raises:
            ValueError: If `direction` is invalid.
        """

        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be one of 'out', 'in', or 'both'.")
        with self._lock:
            selected: list[MemoryLink] = []
            for link in self._links.values():
                if relation is not None and link.relation != relation:
                    continue
                if (
                    (direction == "out" and link.source_id == block_id)
                    or (direction == "in" and link.target_id == block_id)
                    or (direction == "both" and block_id in {link.source_id, link.target_id})
                ):
                    selected.append(_copy_link(link))
            return selected

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete links connected to a block.

        Args:
            block_id: Block id whose links should be removed.
        """

        with self._lock:
            for link_id, link in list(self._links.items()):
                if link.source_id == block_id or link.target_id == block_id:
                    self._links.pop(link_id, None)


class RoutedBackend:
    """Route blocks across multiple backend adapters.

    A `RoutedBackend` still satisfies the single `MemoryBackend` interface. Route
    selection is an implementation detail hidden behind the public `Memory`
    object.
    """

    def __init__(
        self,
        routes: Mapping[RouteKey, MemoryBackend],
        *,
        route_selector: RouteSelector | None = None,
        default_route: RouteKey | None = None,
        traversal_index: TraversalIndex | None = None,
        search_index: SearchIndex | None = None,
        link_store: LinkStore | None = None,
        enforce_tree_route: bool = True,
    ) -> None:
        """Create a routed backend.

        Args:
            routes: Mapping of route keys to backend adapters.
            route_selector: Optional selector used for new root blocks. It may
                return `None` to use `default_route` or inherit the parent route.
            default_route: Route used when the selector returns `None`.
            traversal_index: Optional shared traversal index. If omitted,
                traversal is handled by the route that owns the block.
            search_index: Optional shared search index. If omitted, searches are
                run against route backends and merged when needed.
            link_store: Optional shared link store. If omitted, links are stored
                route-locally and cross-route links are rejected.
            enforce_tree_route: Whether children must stay in the same route as
                their parent. This should usually remain enabled.

        Raises:
            ValueError: If no routes are configured or the default route is
                invalid.
        """

        if not routes:
            raise ValueError("RoutedBackend requires at least one route.")
        self.routes = dict(routes)
        self.route_selector = route_selector
        self.default_route = default_route or next(iter(self.routes))
        self.traversal_index = traversal_index
        self.search_index = search_index
        self.link_store = link_store
        self.enforce_tree_route = enforce_tree_route
        self._catalog: dict[str, RouteKey] = {}
        self._lock = RLock()
        if self.default_route not in self.routes:
            raise ValueError(f"Default route {self.default_route!r} is not configured.")

    def register_route(
        self,
        route: RouteKey,
        backend: MemoryBackend,
        *,
        replace: bool = False,
        make_default: bool = False,
    ) -> None:
        """Register a backend route after initialization.

        Runtime route registration is intended for cases where application code
        discovers a symbolic mount or tenant descriptor that requires opening a
        backend adapter that was not known when the routed backend was created.
        Existing `Memory` objects and `MemoryHandle` instances that share this
        routed backend can use the new route immediately.

        Args:
            route: Route key to register.
            backend: Backend adapter for the route.
            replace: Whether an existing route with no cataloged blocks may be
                replaced.
            make_default: Whether future unselected root blocks should use this
                route as the default.

        Raises:
            MemoryConflict: If the route already exists without `replace`, or
                if replacing it would orphan cataloged blocks.
        """

        with self._lock:
            if route in self.routes:
                if not replace:
                    raise MemoryConflict(f"Route {route!r} is already configured.")
                if route in self._catalog.values():
                    raise MemoryConflict(f"Route {route!r} cannot be replaced because it owns cataloged blocks.")
            self.routes[route] = backend
            if make_default:
                self.default_route = route

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Persist a new block to its owning route.

        Args:
            block: Encoded block to store.

        Returns:
            Stored block.

        Raises:
            MemoryConflict: If the id already exists, the route is invalid, or
                the selected route conflicts with the parent route.
        """

        with self._lock:
            if self.get_block(block.id) is not None:
                raise MemoryConflict(f"Memory block {block.id!r} already exists.")
            route = self._route_for_new_block(block)
            stored = self.routes[route].put_block(block)
            self._catalog[stored.id] = route
            return stored

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Fetch a block by id from its owning route.

        Args:
            block_id: Memory block id.

        Returns:
            Stored block, or `None` when no route contains the id.
        """

        route = self._route_for_existing_block(block_id, missing_ok=True)
        if route is None:
            return None
        return self.routes[route].get_block(block_id)

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace a block in its current owning route.

        Updates do not move a block to another route. If a block is reparented,
        the parent must be in the same route when tree-route enforcement is
        enabled.
        """

        with self._lock:
            route = self._route_for_existing_block(block.id)
            if block.parent_id is not None and self.enforce_tree_route:
                parent_route = self._route_for_existing_block(block.parent_id)
                if parent_route != route:
                    raise MemoryConflict("A memory block cannot be reparented across routes.")
            stored = self.routes[route].update_block(
                block,
                expected_version=expected_version,
            )
            self._catalog[stored.id] = route
            return stored

    def delete_block(
        self,
        block_id: str,
        *,
        expected_versions: Mapping[str, int] | None = None,
    ) -> None:
        """Delete a block from its owning route and remove catalog state.

        Args:
            block_id: Memory block id. Missing ids are ignored.
        """

        with self._lock:
            route = self._route_for_existing_block(block_id, missing_ok=True)
            if route is None:
                return
            descendants = self.descendants(block_id, include_self=True)
            self.routes[route].delete_block(
                block_id,
                expected_versions=expected_versions,
            )
            for block in descendants:
                self._catalog.pop(block.id, None)
                if self.link_store is not None:
                    self.link_store.delete_links_for_block(block.id)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return direct children for a block id.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks.
        """

        if self.traversal_index is not None:
            return self.traversal_index.children(block_id)
        route = self._route_for_existing_block(block_id, missing_ok=True)
        if route is None:
            return []
        return self.routes[route].children(block_id)

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return ancestors for a block id.

        Args:
            block_id: Block id to inspect.

        Returns:
            Ancestor blocks in nearest-parent-first order.
        """

        if self.traversal_index is not None:
            return self.traversal_index.ancestors(block_id)
        route = self._route_for_existing_block(block_id, missing_ok=True)
        if route is None:
            return []
        return self.routes[route].ancestors(block_id)

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return descendants for a block id.

        Args:
            block_id: Root block id.
            include_self: Whether to include the root block.

        Returns:
            Descendant blocks in backend-defined traversal order.
        """

        if self.traversal_index is not None:
            return self.traversal_index.descendants(block_id, include_self=include_self)
        route = self._route_for_existing_block(block_id, missing_ok=True)
        if route is None:
            return []
        return self.routes[route].descendants(block_id, include_self=include_self)

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is in `root_id`'s subtree.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is equal to or below the root.
        """

        if self.traversal_index is not None:
            return self.traversal_index.contains(root_id, block_id)
        root_route = self._route_for_existing_block(root_id, missing_ok=True)
        block_route = self._route_for_existing_block(block_id, missing_ok=True)
        if root_route is None or block_route is None or root_route != block_route:
            return False
        return self.routes[root_route].contains(root_id, block_id)

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Persist a link in the shared link store or source route.

        Args:
            link: Link to store.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, either endpoint is
                missing, or a cross-route link is attempted without a shared
                link store.
        """

        with self._lock:
            source_route = self._route_for_existing_block(link.source_id)
            target_route = self._route_for_existing_block(link.target_id)
            if self.link_store is not None:
                return self.link_store.add_link(link)
            if source_route != target_route:
                raise MemoryConflict("Cross-route links require a shared link_store.")
            return self.routes[source_route].add_link(link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace link metadata in the shared store or source route.

        Args:
            link: Existing link containing the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link is missing or any field other than
                metadata changed.
        """

        with self._lock:
            if self.link_store is not None:
                return self.link_store.update_link(link)
            source_route = self._route_for_existing_block(link.source_id)
            return self.routes[source_route].update_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links from the shared link store or owning route.

        Args:
            block_id: Block id to inspect.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.
        """

        if self.link_store is not None:
            return self.link_store.list_links(block_id, relation=relation, direction=direction)
        route = self._route_for_existing_block(block_id, missing_ok=True)
        if route is None:
            return []
        return self.routes[route].list_links(block_id, relation=relation, direction=direction)

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete links connected to a block.

        Args:
            block_id: Block id whose connected links should be removed.
        """

        with self._lock:
            if self.link_store is not None:
                self.link_store.delete_links_for_block(block_id)
                return
            route = self._route_for_existing_block(block_id, missing_ok=True)
            if route is None:
                return
            self.routes[route].delete_links_for_block(block_id)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search through the shared search index or route-local indexes.

        Args:
            query: Normalized memory query.

        Returns:
            Search results from the shared index, scoped route, or merged routes.
        """

        if self.search_index is not None:
            return self.search_index.search(query)
        if query.scope == Scope.LINKS and self.link_store is not None and query.root_id is not None:
            return self._search_linked_blocks(query)
        if query.root_id is not None and query.scope != Scope.GLOBAL:
            route = self._route_for_existing_block(query.root_id, missing_ok=True)
            if route is None:
                return []
            return self.routes[route].search(query)
        results: list[SearchResult] = []
        for backend in self.routes.values():
            results.extend(backend.search(query))
        results.sort(key=lambda result: result.score, reverse=True)
        return results[: query.limit]

    def route_for_block(self, block_id: str) -> RouteKey | None:
        """Return the route that owns a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Route key, or `None` when the block is unknown.
        """

        return self._route_for_existing_block(block_id, missing_ok=True)

    def _route_for_new_block(self, block: MemoryBlock) -> RouteKey:
        """Choose and validate the owning route for a new block."""

        selected = self.route_selector(block) if self.route_selector is not None else None
        if block.parent_id is not None and self.enforce_tree_route:
            parent_route = self._route_for_existing_block(block.parent_id)
            if selected is not None and selected != parent_route:
                raise MemoryConflict(f"Child route {selected!r} does not match parent route {parent_route!r}.")
            return parent_route
        route = selected or self.default_route
        if route not in self.routes:
            raise MemoryConflict(f"Route {route!r} is not configured.")
        return route

    @overload
    def _route_for_existing_block(
        self,
        block_id: str,
        *,
        missing_ok: Literal[False] = False,
    ) -> RouteKey: ...

    @overload
    def _route_for_existing_block(
        self,
        block_id: str,
        *,
        missing_ok: Literal[True],
    ) -> RouteKey | None: ...

    def _route_for_existing_block(
        self,
        block_id: str,
        *,
        missing_ok: bool = False,
    ) -> RouteKey | None:
        """Find the route that owns an existing block."""

        with self._lock:
            route = self._catalog.get(block_id)
            if route is not None:
                return route

            discovered: list[RouteKey] = [route_key for route_key, backend in self.routes.items() if backend.get_block(block_id) is not None]
            if not discovered:
                if missing_ok:
                    return None
                raise MemoryConflict(f"Memory block {block_id!r} does not exist.")
            if len(discovered) > 1:
                raise MemoryConflict(f"Memory block {block_id!r} exists in multiple routes.")
            self._catalog[block_id] = discovered[0]
            return discovered[0]

    def _search_linked_blocks(self, query: MemoryQuery) -> list[SearchResult]:
        """Search blocks connected to a root through the shared link store."""

        root_id = query.root_id
        if root_id is None:
            return []
        linked_ids: list[str] = []
        seen: set[str] = set()
        for link in self.list_links(root_id, direction="both"):
            for block_id in (link.source_id, link.target_id):
                if block_id not in seen:
                    seen.add(block_id)
                    linked_ids.append(block_id)
        blocks = [block for block_id in linked_ids if (block := self.get_block(block_id)) is not None]
        return _search_blocks(query, blocks)


def _search_blocks(query: MemoryQuery, blocks: list[MemoryBlock]) -> list[SearchResult]:
    """Apply portable in-memory matching to a list of blocks."""

    results: list[SearchResult] = []
    text = query.text.casefold() if query.text else None
    for block in blocks:
        if query.types and block.type not in query.types:
            continue
        if query.filters and not matches_filters(block, query.filters):
            continue
        score = 1.0
        reason = "matched filters"
        if text:
            haystack = f"{block.type} {block.value!r} {block.metadata!r}".casefold()
            if text not in haystack:
                continue
            score = float(haystack.count(text) or 1)
            reason = "matched text"
        results.append(SearchResult(block=_copy_block(block), score=score, reason=reason))
    results.sort(key=lambda result: result.score, reverse=True)
    return results[: query.limit]


def _copy_block(block: MemoryBlock) -> MemoryBlock:
    """Return a block copy with isolated mutable payloads."""

    return block.with_updates(
        value=deepcopy(block.value),
        metadata=deepcopy(block.metadata),
    )


def _copy_link(link: MemoryLink) -> MemoryLink:
    """Return a link copy with isolated mutable metadata."""

    return MemoryLink(
        id=link.id,
        source_id=link.source_id,
        target_id=link.target_id,
        relation=link.relation,
        metadata=deepcopy(link.metadata),
        created_at=link.created_at,
    )


def _same_link_identity(left: MemoryLink, right: MemoryLink) -> bool:
    """Return whether two links identify the same relationship."""

    return left.id == right.id and left.source_id == right.source_id and left.target_id == right.target_id and left.relation == right.relation and left.created_at == right.created_at
