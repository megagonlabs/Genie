"""Routed backend composition helpers.

- **One public backend:** `RoutedBackend` lets one public `MemoryBackend`
  coordinate several internal backends.
- **One owner:** each memory block has exactly one owning route.
- **Local or shared:** search, traversal, and link interfaces can be local to
  a route, or a caller can give shared indexes and stores.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from threading import RLock
from types import MappingProxyType
from typing import Literal, overload

from genie.backend.base import MemoryBackend
from genie.backend.capabilities import LinkStore, SearchIndex, TraversalIndex
from genie.backend.structure_capabilities import AcyclicLinkStore
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict, UnsupportedOperation
from genie.memory.links import MemoryLink
from genie.query.filters import matches_filters
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult
from genie.query.scope import Scope

RouteKey = str
RouteSelector = Callable[[MemoryBlock], RouteKey | None]


@dataclass(frozen=True, slots=True)
class RouteInfo:
    """Catalog entry that tells which route owns a block.

    Attributes:
        block_id: Memory block id.
        route: Route key for the backend that owns the block.
    """

    block_id: str
    route: RouteKey


class InMemoryLinkStore:
    """Simple standalone link store for routed backends.

    - **No blocks:** unlike `InMemoryBackend`, this store does not own memory
      blocks, and it does not check that the endpoints exist.
    - **Endpoint check:** `RoutedBackend` checks the endpoints before it writes
      links to a shared link store.
    - **No acyclic inserts:** this store does not implement `AcyclicLinkStore`.
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
            link: Link with the replacement metadata.

        Returns:
            Defensive copy of the stored replacement link.

        Raises:
            MemoryConflict: If the link does not exist, or a field other than
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
            block_id: Block id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Defensive copies of matching links.

        Raises:
            ValueError: If `direction` is not valid.
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
            block_id: Block id whose links to remove.
        """

        with self._lock:
            for link_id, link in list(self._links.items()):
                if link.source_id == block_id or link.target_id == block_id:
                    self._links.pop(link_id, None)


class RoutedBackend:
    """Route blocks across multiple backend adapters.

    - **One interface:** a `RoutedBackend` still satisfies the single
      `MemoryBackend` interface. The route selection is an implementation
      detail behind the public `Memory` object.
    - **One route for each tree:** a tree never spans routes. A child is always
      stored in the route of its parent, and a block cannot move to a parent in
      another route. Because of this invariant, route-local traversal and link
      delegation are exact, not approximate.
    - **Capabilities:** the composite implements the core `MemoryBackend`
      contract and `AcyclicLinkStore`. Links are part of the core contract, so
      the composite already routes them, and it routes acyclic inserts the
      same way.
    - **No structural state:** it does not implement the other optional
      structure capability protocols in `genie.backend.structure_capabilities`.
    - **`BlockRouter`:** it implements `BlockRouter`. So as the store of a
      structure facade, it resolves to the route that holds the records.
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
    ) -> None:
        """Create a routed backend.

        Args:
            routes: Mapping of route keys to backend adapters.
            route_selector: Optional selector for new blocks. It can return
                `None`. Then a root block uses `default_route`, and a child uses
                the route of its parent. For a child, a selected route that
                is not the parent route raises `MemoryConflict`. For a root
                block, a route that is not configured raises `MemoryConflict`.
            default_route: Route for a new root block when there is no selector,
                or the selector returns `None`. Defaults to the first route.
            traversal_index: Optional shared traversal index. Without one, the
                route that owns the block does the traversal.
            search_index: Optional shared search index. Without one, searches
                run against the route backends, and the results merge when
                needed.
            link_store: Optional shared link store. Without one, links stay in
                their route, and cross-route links are rejected.

        Raises:
            ValueError: If no routes are configured, or the default route is
                not one of them.
        """

        if not routes:
            raise ValueError("RoutedBackend requires at least one route.")
        self._routes: dict[RouteKey, MemoryBackend] = dict(routes)
        self.route_selector = route_selector
        self.default_route = default_route or next(iter(self._routes))
        self.traversal_index = traversal_index
        self.search_index = search_index
        self.link_store = link_store
        self._catalog: dict[str, RouteKey] = {}
        self._lock = RLock()
        if self.default_route not in self._routes:
            raise ValueError(f"Default route {self.default_route!r} is not configured.")

    @property
    def routes(self) -> Mapping[RouteKey, MemoryBackend]:
        """Return a read-only view of the configured routes.

        - **Live view:** the mapping shows later `register_route` calls.
        - **Read-only:** a route replacement goes only through
          `register_route`. It refuses to leave behind blocks that the old
          backend still holds.

        Returns:
            Read-only mapping of route keys to backend adapters.
        """

        return MappingProxyType(self._routes)

    def register_route(
        self,
        route: RouteKey,
        backend: MemoryBackend,
        *,
        replace: bool = False,
        make_default: bool = False,
    ) -> None:
        """Register a backend route after initialization.

        - **Use:** application code finds a symbolic mount or a tenant
          descriptor, and it needs a backend adapter that was not known when
          the routed backend was created.
        - **Immediate effect:** existing `Memory` objects and `MemoryHandle`
          instances that share this routed backend can use the new route at
          once.

        Args:
            route: Route key to register.
            backend: Backend adapter for the route.
            replace: Whether an existing route can be replaced. A replacement
                is possible only when the backend of the route holds no blocks.
            make_default: If `True`, later root blocks with no selected route
                use this route as the default.

        Raises:
            MemoryConflict: If the route already exists and `replace` is
                `False`, or if a replacement leaves behind blocks that the route
                still holds.
        """

        with self._lock:
            if route in self._routes:
                if not replace:
                    raise MemoryConflict(f"Route {route!r} is already configured.")
                if self._route_holds_blocks(route):
                    raise MemoryConflict(f"Route {route!r} cannot be replaced because it still holds blocks.")
            self._routes[route] = backend
            if make_default:
                self.default_route = route

    def put_block(self, block: MemoryBlock) -> MemoryBlock:
        """Store a new block in its owning route.

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
            stored = self._routes[route].put_block(block)
            self._catalog[stored.id] = route
            return stored

    def get_block(self, block_id: str) -> MemoryBlock | None:
        """Get a block by id from its owning route.

        Args:
            block_id: Memory block id.

        Returns:
            Stored block, or `None` when no route contains the id.
        """

        backend = self.backend_for_block(block_id)
        return None if backend is None else backend.get_block(block_id)

    def update_block(
        self,
        block: MemoryBlock,
        *,
        expected_version: int | None = None,
    ) -> MemoryBlock:
        """Replace a block in its current owning route.

        An update does not move a block to another route. A block with a new
        parent stays in the same route, so the new parent is also in that
        route.

        Args:
            block: Encoded replacement block.
            expected_version: Expected current version. With a value, the
                owning route applies the compare-and-swap.

        Returns:
            Stored replacement block.

        Raises:
            MemoryConflict: If the block does not exist, its version changed,
                or the new parent is in another route.
        """

        with self._lock:
            route = self._route_for_existing_block(block.id)
            if block.parent_id is not None:
                parent_route = self._route_for_existing_block(block.parent_id)
                if parent_route != route:
                    raise MemoryConflict("A memory block cannot be reparented across routes.")
            stored = self._routes[route].update_block(
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

        The backend removes the catalog entries and the shared link-store
        links of the block and of each descendant that the owning route deleted
        with it.

        Args:
            block_id: Memory block id. An unknown id is ignored.
            expected_versions: Exact id-to-version snapshot of the subtree to
                delete. With a value, the owning route applies the
                compare-and-swap.

        Raises:
            MemoryConflict: If `expected_versions` no longer describes the
                complete subtree.
        """

        with self._lock:
            backend = self.backend_for_block(block_id)
            if backend is None:
                return
            descendants = self.descendants(block_id, include_self=True)
            backend.delete_block(
                block_id,
                expected_versions=expected_versions,
            )
            for block in descendants:
                self._catalog.pop(block.id, None)
                if self.link_store is not None:
                    self.link_store.delete_links_for_block(block.id)

    def children(self, block_id: str) -> list[MemoryBlock]:
        """Return the direct children of a block id.

        Args:
            block_id: Parent block id.

        Returns:
            Direct child blocks.
        """

        if self.traversal_index is not None:
            return self.traversal_index.children(block_id)
        backend = self.backend_for_block(block_id)
        return [] if backend is None else backend.children(block_id)

    def ancestors(self, block_id: str) -> list[MemoryBlock]:
        """Return the ancestors of a block id.

        Args:
            block_id: Block id to examine.

        Returns:
            Ancestor blocks, nearest parent first.
        """

        if self.traversal_index is not None:
            return self.traversal_index.ancestors(block_id)
        backend = self.backend_for_block(block_id)
        return [] if backend is None else backend.ancestors(block_id)

    def descendants(self, block_id: str, *, include_self: bool = False) -> list[MemoryBlock]:
        """Return the descendants of a block id.

        Args:
            block_id: Root block id.
            include_self: If `True`, the result includes the root block.

        Returns:
            Descendant blocks in backend-defined traversal order.
        """

        if self.traversal_index is not None:
            return self.traversal_index.descendants(block_id, include_self=include_self)
        backend = self.backend_for_block(block_id)
        return [] if backend is None else backend.descendants(block_id, include_self=include_self)

    def contains(self, root_id: str, block_id: str) -> bool:
        """Return whether `block_id` is in the subtree of `root_id`.

        Args:
            root_id: Potential subtree root id.
            block_id: Candidate block id.

        Returns:
            `True` when the candidate is the root or is below it.
        """

        if self.traversal_index is not None:
            return self.traversal_index.contains(root_id, block_id)
        # A tree never spans routes, so blocks in different routes can never
        # share a subtree. The early `False` is exact, not a fallback.
        root_route = self._route_for_existing_block(root_id, missing_ok=True)
        block_route = self._route_for_existing_block(block_id, missing_ok=True)
        if root_route is None or block_route is None or root_route != block_route:
            return False
        return self._routes[root_route].contains(root_id, block_id)

    def add_link(self, link: MemoryLink) -> MemoryLink:
        """Store a link in the shared link store or in the source route.

        Args:
            link: Link to store.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, or a cross-route link has no shared link store.
        """

        with self._lock:
            source_route = self._route_for_existing_block(link.source_id)
            target_route = self._route_for_existing_block(link.target_id)
            if self.link_store is not None:
                return self.link_store.add_link(link)
            if source_route != target_route:
                raise MemoryConflict("Cross-route links require a shared link_store.")
            return self._routes[source_route].add_link(link)

    def add_acyclic_link(self, link: MemoryLink) -> MemoryLink:
        """Store a link only when its relation stays acyclic.

        The cycle check runs in the store that holds the links:

        - **Shared link store:** that store checks the relation across all
          routes.
        - **No shared link store:** the route that owns both endpoints checks
          it. Links never cross routes in this mode. So a cycle can form only
          inside one route, and the route-local check is exact.
        - **Protection for each route:** without a shared link store, each route
          protects a relation alone. A route rejects ordinary `add_link` calls
          for the relation only after its own first acyclic insert, not after
          one in another route.

        Args:
            link: Link to store. Its relation identifies the protected graph.

        Returns:
            Stored link.

        Raises:
            MemoryConflict: If the link id already exists, an endpoint does not
                exist, the link closes a cycle, or a cross-route link has no
                shared link store.
            UnsupportedOperation: If the store for the link does not support
                atomic acyclic links.
        """

        with self._lock:
            source_route = self._route_for_existing_block(link.source_id)
            target_route = self._route_for_existing_block(link.target_id)
            if self.link_store is not None:
                if not isinstance(self.link_store, AcyclicLinkStore):
                    raise UnsupportedOperation("The shared link store does not support atomic acyclic links.")
                return self.link_store.add_acyclic_link(link)
            if source_route != target_route:
                raise MemoryConflict("Cross-route links require a shared link_store.")
            backend = self._routes[source_route]
            if not isinstance(backend, AcyclicLinkStore):
                raise UnsupportedOperation(f"Route {source_route!r} does not support atomic acyclic links.")
            return backend.add_acyclic_link(link)

    def update_link(self, link: MemoryLink) -> MemoryLink:
        """Replace link metadata in the shared store or in the source route.

        Args:
            link: Existing link with the replacement metadata.

        Returns:
            Stored replacement link.

        Raises:
            MemoryConflict: If the link does not exist, or a field other than
                metadata changed.
        """

        with self._lock:
            if self.link_store is not None:
                return self.link_store.update_link(link)
            source_route = self._route_for_existing_block(link.source_id)
            return self._routes[source_route].update_link(link)

    def list_links(
        self,
        block_id: str,
        *,
        relation: str | None = None,
        direction: Literal["out", "in", "both"] = "out",
    ) -> list[MemoryLink]:
        """List links from the shared link store or from the owning route.

        Args:
            block_id: Block id to examine.
            relation: Optional relation type filter.
            direction: Link direction relative to `block_id`.

        Returns:
            Matching links.
        """

        if self.link_store is not None:
            return self.link_store.list_links(block_id, relation=relation, direction=direction)
        backend = self.backend_for_block(block_id)
        return [] if backend is None else backend.list_links(block_id, relation=relation, direction=direction)

    def delete_links_for_block(self, block_id: str) -> None:
        """Delete links connected to a block.

        Args:
            block_id: Block id whose connected links to remove.
        """

        with self._lock:
            if self.link_store is not None:
                self.link_store.delete_links_for_block(block_id)
                return
            backend = self.backend_for_block(block_id)
            if backend is not None:
                backend.delete_links_for_block(block_id)

    def search(self, query: MemoryQuery) -> list[SearchResult]:
        """Search through the shared search index or the route-local indexes.

        - **Shared index:** with a shared search index, it answers all searches.
        - **`links` scope:** with a shared link store, the composite matches
          the blocks that the root links to.
        - **Other scoped searches:** the route of the root answers.
        - **Global search:** each route returns up to `limit` results. The
          composite merges them by score and keeps the first `limit`.

        Args:
            query: Normalized memory query.

        Returns:
            Search results from the shared index, the scoped route, or the
            merged routes.
        """

        if self.search_index is not None:
            return self.search_index.search(query)
        if query.scope == Scope.LINKS and self.link_store is not None and query.root_id is not None:
            return self._search_linked_blocks(query)
        if query.root_id is not None and query.scope != Scope.GLOBAL:
            backend = self.backend_for_block(query.root_id)
            return [] if backend is None else backend.search(query)
        results: list[SearchResult] = []
        for backend in self._routes.values():
            results.extend(backend.search(query))
        results.sort(key=lambda result: result.score, reverse=True)
        return results[: query.limit]

    def backend_for_block(self, block_id: str) -> MemoryBackend | None:
        """Return the backend that owns a block id.

        - **Routing primitive:** each route-local operation resolves its target
          backend through this method, not through `routes`.
        - **Ownership report:** a structure facade does not need it. As the
          store of a facade, the composite resolves the owning route through
          `BlockRouter`.
        - **Manual resolution:** it is only for direct use of the optional
          capabilities of a route. `MemoryBackend` does not include the
          optional structure protocols, so a caller checks the capability
          first.
        - **Subtrees:** a tree never spans routes. So the backend for a
          container block also owns each block in the subtree of that
          container.

        Args:
            block_id: Memory block id.

        Returns:
            Owning backend, or `None` when no route contains the id.
        """

        route = self._route_for_existing_block(block_id, missing_ok=True)
        return None if route is None else self._routes[route]

    def route_for_block(self, block_id: str) -> RouteKey | None:
        """Return the route that owns a block id.

        Args:
            block_id: Memory block id.

        Returns:
            Route key, or `None` when the block is unknown.
        """

        return self._route_for_existing_block(block_id, missing_ok=True)

    def _route_holds_blocks(self, route: RouteKey) -> bool:
        """Return whether the backend of a route still holds a block.

        - **Catalog first:** a route that the catalog already knows counts as
          populated, with no probe.
        - **Backend probe:** otherwise the method asks the backend directly. The
          catalog is a cache that fills lazily when blocks are routed. So a
          composite opened over a store that already has data starts with an
          empty catalog.
        - **Probe form:** the probe is a bounded global search, which each
          `MemoryBackend` supports. A backend whose search index is stale or
          managed from outside can still report too few blocks.
        - **No search support:** if the search raises `UnsupportedOperation`,
          the route counts as empty.

        Args:
            route: Route key to examine.

        Returns:
            `True` when the catalog knows the route or the probe finds a block.
        """

        if route in self._catalog.values():
            return True
        try:
            return bool(self._routes[route].search(MemoryQuery(scope=Scope.GLOBAL, limit=1)))
        except UnsupportedOperation:
            return False

    def _route_for_new_block(self, block: MemoryBlock) -> RouteKey:
        """Choose and check the owning route for a new block."""

        selected = self.route_selector(block) if self.route_selector is not None else None
        if block.parent_id is not None:
            parent_route = self._route_for_existing_block(block.parent_id)
            if selected is not None and selected != parent_route:
                raise MemoryConflict(f"Child route {selected!r} does not match parent route {parent_route!r}.")
            return parent_route
        route = selected or self.default_route
        if route not in self._routes:
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
        """Find the route that owns an existing block, and record it in the catalog."""

        with self._lock:
            route = self._catalog.get(block_id)
            if route is not None:
                return route

            discovered: list[RouteKey] = [route_key for route_key, backend in self._routes.items() if backend.get_block(block_id) is not None]
            if not discovered:
                if missing_ok:
                    return None
                raise MemoryConflict(f"Memory block {block_id!r} does not exist.")
            if len(discovered) > 1:
                raise MemoryConflict(f"Memory block {block_id!r} exists in multiple routes.")
            self._catalog[block_id] = discovered[0]
            return discovered[0]

    def _search_linked_blocks(self, query: MemoryQuery) -> list[SearchResult]:
        """Search the blocks that the shared link store connects to a root."""

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
    """Apply the portable in-memory matcher to a list of blocks."""

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
    """Return a block copy whose mutable payloads are separate copies."""

    return block.with_updates(
        value=deepcopy(block.value),
        metadata=deepcopy(block.metadata),
    )


def _copy_link(link: MemoryLink) -> MemoryLink:
    """Return a link copy whose mutable metadata is a separate copy."""

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
