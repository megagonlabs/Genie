"""Share one route catalog between composite backends over the same routes.

Run from the repository root:

    python examples/basic/routed_catalog.py
"""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

if __package__ in {None, ""}:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import Memory, MemoryBlock, MemoryConflict, RoutedBackend, SQLiteBackend


def route_by_region(block: MemoryBlock) -> str | None:
    """Choose a backend route from block metadata.

    Args:
        block: The block that the backend stores.

    Returns:
        The route key if the block metadata has a string `region`. Otherwise
        `None`.
    """

    region = block.metadata.get("region")
    return region if isinstance(region, str) else None


class Worker:
    """One application worker, such as one process of a web server.

    Each worker opens its own connections to the same route files and to the
    same route catalog file.
    """

    def __init__(self, directory: Path) -> None:
        """Open the routes and the route catalog in a directory.

        Args:
            directory: Directory that holds the SQLite files.
        """

        self.stores = [SQLiteBackend(directory / name) for name in ("us.db", "eu.db", "routes.db")]
        us_store, eu_store, route_catalog = self.stores
        self.backend = RoutedBackend(
            {"us": us_store, "eu": eu_store},
            route_selector=route_by_region,
            default_route="us",
            route_catalog=route_catalog,
        )
        self.memory = Memory(backend=self.backend, principal="service:crm")

    def close(self) -> None:
        """Close the SQLite connections of this worker."""

        for store in self.stores:
            store.close()


def main() -> None:
    """Show that workers that share a route catalog keep each id in one route."""

    print("== Shared route catalog ==")
    with TemporaryDirectory(prefix="genie-example-") as directory:
        first = Worker(Path(directory))
        second = Worker(Path(directory))

        first.memory.put(id="customer-7", type="customer", value={"name": "Ana"})
        print(f"first worker stores customer-7 in: {first.backend.route_for_block('customer-7')}")

        # The customer moves to the EU. A route never changes on update, so the
        # second worker deletes the block and stores the id again in route `eu`.
        # The first worker still has route `us` in its route cache.
        second.memory.delete("customer-7")
        second.memory.put(
            id="customer-7",
            type="customer",
            value={"name": "Ana"},
            metadata={"region": "eu"},
        )

        # The first worker checks its cached route before it uses it, so it
        # finds the block in route `eu`.
        print(f"first worker now finds customer-7 in: {first.backend.route_for_block('customer-7')}")

        # The route catalog already has an entry for the id. So the first
        # worker cannot store a second customer-7 in route `us`.
        try:
            first.memory.put(id="customer-7", type="customer", value={"name": "Ana"})
        except MemoryConflict:
            print("first worker put of customer-7 in us: refused, the id already has a route")
        else:
            raise AssertionError("The route catalog did not refuse a second route for the id.")

        fresh = Worker(Path(directory))
        stored = fresh.memory.get("customer-7")
        print(f"new worker reads customer-7 from: {fresh.backend.route_for_block('customer-7')}")

        assert stored is not None
        assert stored.metadata == {"region": "eu"}
        assert fresh.backend.route_for_block("customer-7") == "eu"
        us_store, eu_store, route_catalog = fresh.stores
        assert us_store.get_block("customer-7") is None
        assert eu_store.get_block("customer-7") is not None
        assert route_catalog.get_route("customer-7") is not None

        for worker in (first, second, fresh):
            worker.close()


if __name__ == "__main__":
    main()
