"""Tests for the persistent SQLite backend."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Event
from typing import Any, cast

import pytest

from genie import (
    AccessRule,
    BackendBusy,
    BackendCapacityExceeded,
    BackendCorruption,
    BackendError,
    BackendReadOnly,
    BackendUnavailable,
    Memory,
    MemoryBackend,
    MemoryBlock,
    MemoryConflict,
    MemoryLink,
    MemoryPolicy,
    MemoryQuery,
    SQLiteBackend,
    UnsupportedOperation,
)
from genie.backend.relational import sqlite as sqlite_module
from genie.structures import KeyBinding, MemoryAddress, TemporalEntry


def test_sqlite_backend_implements_the_core_backend_protocol() -> None:
    """`SQLiteBackend` has every method that `MemoryBackend` requires."""

    with SQLiteBackend() as backend:
        assert isinstance(backend, MemoryBackend)


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        ({"timeout": True}, TypeError, "timeout must be a number"),
        ({"timeout": -1}, ValueError, "timeout must be a finite number"),
        ({"timeout": float("inf")}, ValueError, "timeout must be a finite number"),
        ({"durability": 1}, TypeError, "durability must be a string"),
        ({"durability": "fast"}, ValueError, "durability must be either"),
    ],
)
def test_sqlite_backend_rejects_invalid_configuration(
    kwargs: dict[str, Any],
    error: type[Exception],
    message: str,
) -> None:
    """`SQLiteBackend` rejects invalid `timeout` and `durability` values before it opens a database."""

    with pytest.raises(error, match=message):
        SQLiteBackend(**kwargs)


def test_sqlite_backend_persists_core_memory_state(tmp_path: Path) -> None:
    """Blocks, policies, ownership, links, and search results stay the same after a restart."""

    path = tmp_path / "memory.db"
    policy = MemoryPolicy.owner_or(roles={"reviewer"})
    first = SQLiteBackend(path)
    memory = Memory(backend=first, principal="owner")
    root = memory.put(type="project", value={"name": "search"}, policy=policy)
    child = memory.put(
        parent=root,
        type="experiment",
        value={"result": "reranker improved precision"},
        metadata={"status": "complete"},
        policy=policy,
    )
    updated = memory.update(
        child,
        value={"sample_count": 20},
        metadata={"reviewed": True},
    )
    link = memory.add_link(
        child,
        root,
        "supports",
        metadata={"evidence": ["run-1"]},
    )
    first.close()

    second = SQLiteBackend(path)
    reopened = Memory(backend=second, principal="owner")
    stored_root = reopened.get(root.id)
    stored_child = reopened.get(child.id)

    assert stored_root is not None
    assert stored_root.policy == policy
    assert stored_child == updated
    assert second.children(root.id) == [stored_child]
    assert second.ancestors(child.id) == [stored_root]
    assert second.descendants(root.id, include_self=True) == [
        stored_root,
        stored_child,
    ]
    assert second.contains(root.id, child.id)
    assert reopened.list_links(child) == [link]
    assert [
        result.block.id
        for result in reopened.search(
            "reranker",
            root=root,
            scope="subtree",
            filters={"metadata.status": "complete"},
        )
    ] == [child.id]

    reopened.delete(root)
    assert reopened.get(root.id) is None
    assert reopened.get(child.id) is None
    assert second.list_links(root.id, direction="both") == []
    second.close()


def test_sqlite_backend_searches_every_core_scope() -> None:
    """`SQLiteBackend.search` selects the correct candidates for the global, self, subtree, ancestors, and links scopes."""

    with SQLiteBackend() as backend:
        root = backend.put_block(MemoryBlock(id="root", type="node", value="root"))
        child = backend.put_block(
            MemoryBlock(id="child", parent_id=root.id, type="node", value="child")
        )
        grandchild = backend.put_block(
            MemoryBlock(
                id="grandchild",
                parent_id=child.id,
                type="node",
                value="grandchild",
            )
        )
        outside = backend.put_block(
            MemoryBlock(id="outside", type="node", value="outside")
        )
        backend.add_link(MemoryLink(child.id, outside.id, "related"))

        def ids(scope: str, root_id: str | None) -> set[str]:
            return {
                result.block.id
                for result in backend.search(
                    MemoryQuery(scope=scope, root_id=root_id)
                )
            }

        assert ids("global", None) == {root.id, child.id, grandchild.id, outside.id}
        assert ids("self", child.id) == {child.id}
        assert ids("subtree", root.id) == {root.id, child.id, grandchild.id}
        assert ids("ancestors", grandchild.id) == {
            root.id,
            child.id,
            grandchild.id,
        }
        assert ids("links", child.id) == {child.id, outside.id}
        assert ids("subtree", "missing") == set()


def test_sqlite_backend_rejects_conflicts_without_partial_changes() -> None:
    """The backend rejects a duplicate block and a parent cycle without a partial change to the ownership tree."""

    with SQLiteBackend() as backend:
        root = backend.put_block(MemoryBlock(id="root", type="root", value={}))
        child = backend.put_block(
            MemoryBlock(id="child", parent_id=root.id, type="child", value={})
        )

        with pytest.raises(MemoryConflict):
            backend.put_block(root)
        with pytest.raises(MemoryConflict):
            backend.update_block(replace(root, parent_id=child.id))

        assert backend.get_block(root.id) == root
        assert backend.children(root.id) == [child]
        assert backend.ancestors(child.id) == [root]


def test_sqlite_backend_preserves_link_identity_and_isolates_values() -> None:
    """`update_link` keeps the link endpoints and relation, and a change to a returned value does not change the stored JSON."""

    with SQLiteBackend() as backend:
        source = backend.put_block(
            MemoryBlock(id="source", type="note", value={"items": ["source"]})
        )
        target = backend.put_block(
            MemoryBlock(id="target", type="note", value={"items": ["target"]})
        )
        link = backend.add_link(
            MemoryLink(
                id="link",
                source_id=source.id,
                target_id=target.id,
                relation="references",
                metadata={"tags": ["initial"]},
            )
        )

        fetched = backend.get_block(source.id)
        assert fetched is not None
        fetched.value["items"].append("changed")
        listed = backend.list_links(source.id)
        listed[0].metadata["tags"].append("changed")

        assert backend.get_block(source.id) == source
        assert backend.list_links(source.id) == [link]
        updated = backend.update_link(
            replace(link, metadata={"tags": ["updated"]})
        )
        assert updated.metadata == {"tags": ["updated"]}
        with pytest.raises(MemoryConflict):
            backend.update_link(replace(updated, relation="different"))


def test_sqlite_backend_coordinates_separate_connections(tmp_path: Path) -> None:
    """Database transactions protect logs and DAGs across backend instances."""

    path = tmp_path / "shared.db"
    first = SQLiteBackend(path)
    second = SQLiteBackend(path)
    try:
        log = first.put_block(MemoryBlock(id="log", type="log", value={}))
        target = first.put_block(MemoryBlock(id="target", type="event", value={}))

        with ThreadPoolExecutor(max_workers=8) as executor:
            entries = list(
                executor.map(
                    lambda index: (first if index % 2 == 0 else second).append_log_entry(
                        log.id,
                        target.id,
                    ),
                    range(40),
                )
            )
        assert sorted(entry.sequence for entry in entries) == list(range(40))

        left = first.put_block(MemoryBlock(id="left", type="node", value={}))
        right = first.put_block(MemoryBlock(id="right", type="node", value={}))
        barrier = Barrier(2)

        def add_edge(backend: SQLiteBackend, link: MemoryLink) -> bool:
            barrier.wait()
            try:
                backend.add_acyclic_link(link)
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda item: add_edge(*item),
                    [
                        (
                            first,
                            MemoryLink(left.id, right.id, "derived_into"),
                        ),
                        (
                            second,
                            MemoryLink(right.id, left.id, "derived_into"),
                        ),
                    ],
                )
            )
        assert outcomes.count(True) == 1
    finally:
        first.close()
        second.close()


def test_sqlite_backend_rejects_non_json_storage_values() -> None:
    """`SQLiteBackend` rejects a value that is not strict JSON and an invalid `created_at`, and does not change the value type."""

    with SQLiteBackend() as backend:
        with pytest.raises(TypeError, match="strict JSON"):
            backend.put_block(MemoryBlock(type="tuple", value=("not", "json")))
        with pytest.raises(TypeError, match="created_at must be a datetime"):
            backend.put_block(
                MemoryBlock(
                    type="bad-time",
                    value={},
                    created_at=cast(datetime, "bad"),
                )
            )
        with pytest.raises(ValueError, match="created_at must be timezone-aware"):
            backend.put_block(
                MemoryBlock(
                    type="naive-time",
                    value={},
                    created_at=datetime(2026, 1, 1),  # noqa: DTZ001 - invalid input
                )
            )


@pytest.mark.parametrize(
    ("column", "stored_value"),
    [
        ("value_json", "{not-json"),
        ("value_json", "NaN"),
        ("value_json", sqlite3.Binary(b"1")),
        ("created_at", "not-a-timestamp"),
        ("created_at", 1),
        ("created_at", "2026-01-01T00:00:00"),
    ],
)
def test_sqlite_backend_reports_invalid_stored_block_data_as_corruption(
    column: str,
    stored_value: object,
) -> None:
    """A malformed stored block row raises `BackendCorruption`, the documented backend error."""

    with SQLiteBackend() as backend:
        block = backend.put_block(MemoryBlock(id="note", type="note", value={}))
        connection = backend._require_connection()
        connection.execute(
            f"UPDATE blocks SET {column} = ? WHERE id = ?",
            (stored_value, block.id),
        )

        with pytest.raises(BackendCorruption):
            backend.get_block(block.id)


def test_sqlite_backend_rejects_corrupt_policy_boolean_values() -> None:
    """A truthy string in a stored policy raises `BackendCorruption` and does not widen access."""

    with SQLiteBackend() as backend:
        block = backend.put_block(
            MemoryBlock(
                id="note",
                type="note",
                value={},
                policy=MemoryPolicy.public(),
            )
        )
        connection = backend._require_connection()
        policy_json = connection.execute(
            "SELECT policy_json FROM blocks WHERE id = ?",
            (block.id,),
        ).fetchone()[0]
        payload = json.loads(policy_json)
        payload["read"]["allow_all"] = "false"
        connection.execute(
            "UPDATE blocks SET policy_json = ? WHERE id = ?",
            (json.dumps(payload), block.id),
        )

        with pytest.raises(BackendCorruption, match="memory block"):
            backend.get_block(block.id)


def test_sqlite_backend_reports_corrupt_structure_records() -> None:
    """An invalid record of an optional capability also raises `BackendCorruption`."""

    with SQLiteBackend() as backend:
        collection = backend.put_block(
            MemoryBlock(id="collection", type="collection", value={})
        )
        target = backend.put_block(MemoryBlock(id="target", type="note", value={}))
        address = MemoryAddress(("notes",), "target")
        backend.bind_key(KeyBinding(collection.id, address, target.id))
        backend._require_connection().execute(
            "UPDATE key_bindings SET namespace_json = '{}' WHERE collection_id = ?",
            (collection.id,),
        )

        with pytest.raises(BackendCorruption, match="namespace"):
            backend.list_keys(collection.id)


def test_sqlite_backend_reports_corrupt_link_log_and_temporal_records() -> None:
    """A malformed stored link, log entry, or temporal entry raises `BackendCorruption`."""

    with SQLiteBackend() as backend:
        container = backend.put_block(
            MemoryBlock(id="container", type="container", value={})
        )
        target = backend.put_block(MemoryBlock(id="target", type="note", value={}))
        link = backend.add_link(MemoryLink(container.id, target.id, "related"))
        log_entry = backend.append_log_entry(container.id, target.id)
        temporal_entry = backend.add_temporal_entry(
            TemporalEntry(
                container.id,
                target.id,
                occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
            )
        )
        connection = backend._require_connection()
        connection.execute(
            "UPDATE links SET created_at = 'invalid' WHERE id = ?",
            (link.id,),
        )
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute(
            "UPDATE log_entries SET sequence = -1 WHERE log_id = ? AND sequence = ?",
            (log_entry.log_id, log_entry.sequence),
        )
        connection.execute(
            "UPDATE temporal_entries SET occurred_at = 'invalid' WHERE collection_id = ? AND block_id = ?",
            (temporal_entry.collection_id, temporal_entry.block_id),
        )

        with pytest.raises(BackendCorruption):
            backend.list_links(container.id)
        with pytest.raises(BackendCorruption, match="log entry"):
            backend.list_log_entries(container.id)
        with pytest.raises(BackendCorruption):
            backend.get_temporal_entry(container.id, target.id)


def test_sqlite_backend_context_manager_closes_connection() -> None:
    """The context manager closes the SQLite connection, a later read raises `RuntimeError`, and a second `close` is safe."""

    with SQLiteBackend() as backend:
        backend.put_block(MemoryBlock(type="note", value={}))

    with pytest.raises(RuntimeError, match="closed"):
        backend.get_block("missing")
    backend.close()


def test_sqlite_backend_rejects_newer_schema(tmp_path: Path) -> None:
    """`SQLiteBackend` raises `UnsupportedOperation` for a newer schema version and does not corrupt the file."""

    path = tmp_path / "future.db"
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA user_version = 999")
    connection.close()

    with pytest.raises(UnsupportedOperation, match="newer"):
        SQLiteBackend(path)


def test_sqlite_backend_rejects_stale_block_replacement(tmp_path: Path) -> None:
    """Version-checked replacement prevents lost updates across connections."""

    path = tmp_path / "compare-and-swap.db"
    first = SQLiteBackend(path)
    second = SQLiteBackend(path)
    try:
        original = first.put_block(MemoryBlock(id="note", type="note", value={"base": True}))
        first_snapshot = first.get_block(original.id)
        second_snapshot = second.get_block(original.id)
        assert first_snapshot is not None
        assert second_snapshot is not None

        first.update_block(
            replace(first_snapshot, value={"left": True}, version=2),
            expected_version=1,
        )
        with pytest.raises(MemoryConflict, match="changed"):
            second.update_block(
                replace(second_snapshot, value={"right": True}, version=2),
                expected_version=1,
            )

        stored = first.get_block(original.id)
        assert stored is not None
        assert stored.value == {"left": True}
        assert stored.version == 2
    finally:
        first.close()
        second.close()


def test_memory_updates_do_not_silently_lose_concurrent_changes(tmp_path: Path) -> None:
    """`Memory.update` passes the version that it read into the write, so one of two concurrent updates fails."""

    snapshots_read = Barrier(2)

    class PausingSQLiteBackend(SQLiteBackend):
        def get_block(self, block_id: str) -> MemoryBlock | None:
            block = super().get_block(block_id)
            if block_id == "note":
                snapshots_read.wait(timeout=5)
            return block

    path = tmp_path / "memory-compare-and-swap.db"
    with SQLiteBackend(path) as setup:
        Memory(backend=setup).put(
            id="note",
            type="note",
            value={"base": True},
            policy=MemoryPolicy.public(),
        )
    first_backend = PausingSQLiteBackend(path)
    second_backend = PausingSQLiteBackend(path)
    first = Memory(backend=first_backend)
    second = Memory(backend=second_backend)
    try:
        def update(memory: Memory, field: str) -> bool:
            try:
                memory.update("note", value={field: True})
            except MemoryConflict:
                return False
            return True

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(
                executor.map(
                    lambda item: update(*item),
                    [(first, "left"), (second, "right")],
                )
            )

        assert outcomes.count(True) == 1
        with SQLiteBackend(path) as verifier:
            stored = verifier.get_block("note")
            assert stored is not None
            assert stored.version == 2
    finally:
        first_backend.close()
        second_backend.close()


def test_memory_delete_rejects_a_concurrently_changed_subtree(tmp_path: Path) -> None:
    """`Memory.delete` raises `MemoryConflict` if another writer adds an unchecked child after the policy check, and deletes nothing."""

    descendants_read = Event()
    continue_delete = Event()

    class PausingSQLiteBackend(SQLiteBackend):
        def descendants(
            self,
            block_id: str,
            *,
            include_self: bool = False,
        ) -> list[MemoryBlock]:
            blocks = super().descendants(block_id, include_self=include_self)
            if block_id == "root" and include_self:
                descendants_read.set()
                assert continue_delete.wait(timeout=5)
            return blocks

    path = tmp_path / "conditional-delete.db"
    deleting_backend = PausingSQLiteBackend(path)
    writing_backend = SQLiteBackend(path)
    deleting_memory = Memory(backend=deleting_backend, principal="owner")
    writing_memory = Memory(backend=writing_backend, principal="owner")
    try:
        deleting_memory.put(id="root", type="root", value={})
        with ThreadPoolExecutor(max_workers=1) as executor:
            deletion = executor.submit(deleting_memory.delete, "root")
            assert descendants_read.wait(timeout=5)
            child = writing_memory.put(
                parent="root",
                type="protected",
                value={},
                policy=MemoryPolicy(write=AccessRule.deny()),
            )
            continue_delete.set()
            with pytest.raises(MemoryConflict, match="changed"):
                deletion.result(timeout=5)

        assert deleting_backend.get_block("root") is not None
        assert deleting_backend.get_block(child.id) is not None
    finally:
        continue_delete.set()
        deleting_backend.close()
        writing_backend.close()


def test_sqlite_backend_deletes_trees_deeper_than_trigger_limit() -> None:
    """The backend deletes leaves first, so the delete of a deep tree does not depend on the SQLite cascade recursion depth."""

    with SQLiteBackend() as backend:
        parent_id: str | None = None
        for index in range(1_101):
            block = backend.put_block(
                MemoryBlock(
                    id=f"node-{index}",
                    parent_id=parent_id,
                    type="node",
                    value={},
                )
            )
            parent_id = block.id

        backend.delete_block("node-0")

        assert backend.get_block("node-0") is None
        assert backend.get_block("node-1100") is None


def test_sqlite_backend_uses_full_durability_by_default(tmp_path: Path) -> None:
    """Production defaults enable WAL and FULL synchronous durability."""

    path = tmp_path / "durable.db"
    with SQLiteBackend(path) as backend:
        connection = backend._require_connection()
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1

    with SQLiteBackend(path, durability="normal") as backend:
        connection = backend._require_connection()
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 1


def test_sqlite_backend_identifies_and_validates_its_schema(tmp_path: Path) -> None:
    """A new database gets the Genie `application_id`, and the backend rejects a schema with incompatible columns."""

    path = tmp_path / "identified.db"
    with SQLiteBackend(path):
        pass
    connection = sqlite3.connect(path)
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    assert application_id != 0
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    connection.execute("ALTER TABLE blocks ADD COLUMN unexpected TEXT")
    connection.close()

    with pytest.raises(UnsupportedOperation, match="incompatible columns"):
        SQLiteBackend(path)


def test_sqlite_backend_rejects_an_unowned_schema_without_changing_it(
    tmp_path: Path,
) -> None:
    """The backend rejects a schema that Genie does not own before a Genie migration can change it."""

    path = tmp_path / "other-application.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE blocks (x TEXT)")
    connection.commit()
    connection.close()

    with pytest.raises(UnsupportedOperation, match="incompatible or unowned"):
        SQLiteBackend(path)

    connection = sqlite3.connect(path)
    assert [row[1] for row in connection.execute("PRAGMA table_info(blocks)")] == ["x"]
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute("PRAGMA application_id").fetchone()[0] == 0
    connection.close()


def test_sqlite_backend_adopts_a_valid_legacy_schema(tmp_path: Path) -> None:
    """The backend adopts a valid schema from before the identity pragma and keeps its data."""

    path = tmp_path / "legacy.db"
    with SQLiteBackend(path) as backend:
        backend.put_block(MemoryBlock(id="kept", type="note", value={"kept": True}))
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA application_id = 0")
    connection.close()

    with SQLiteBackend(path) as backend:
        kept = backend.get_block("kept")
        assert kept is not None
        assert kept.value == {"kept": True}

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA application_id").fetchone()[0] != 0
    connection.close()


def test_sqlite_backend_runs_ordered_migrations_from_version_zero(tmp_path: Path) -> None:
    """A recognized empty database migrates atomically to the current schema."""

    path = tmp_path / "version-zero.db"
    with SQLiteBackend(path):
        pass

    connection = sqlite3.connect(path)
    application_id = connection.execute("PRAGMA application_id").fetchone()[0]
    assert application_id != 0
    tables = connection.execute(
        "SELECT name FROM sqlite_master "
        "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for (table,) in tables:
        connection.execute(f'DROP TABLE "{table}"')
    connection.execute("PRAGMA user_version = 0")
    connection.close()

    with SQLiteBackend(path):
        pass

    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    migrated_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert "blocks" in migrated_tables
    assert "temporal_entries" in migrated_tables
    connection.close()


def test_sqlite_schema_migration_rolls_back_as_one_transaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed migration leaves no partial tables, identity, or version."""

    path = tmp_path / "failed-migration.db"
    real_connect = sqlite3.connect

    def connect_with_denied_table(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        connection = real_connect(*args, **kwargs)

        def authorize(
            action: int,
            first: str | None,
            second: str | None,
            database: str | None,
            source: str | None,
        ) -> int:
            del second, database, source
            if action == sqlite3.SQLITE_CREATE_TABLE and first == "links":
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorize)
        return connection

    monkeypatch.setattr(sqlite3, "connect", connect_with_denied_table)
    with pytest.raises(BackendError):
        SQLiteBackend(path)

    connection = real_connect(path)
    user_tables = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    assert user_tables == []
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
    assert connection.execute("PRAGMA application_id").fetchone()[0] == 0
    connection.close()


def test_sqlite_backend_maps_invalid_database_file_to_corruption(tmp_path: Path) -> None:
    """A file that is not a SQLite database raises `BackendCorruption` from the constructor, with the driver error as the cause."""

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a SQLite database")

    with pytest.raises(BackendCorruption) as captured:
        SQLiteBackend(path)

    assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sqlite_backend_maps_unopenable_path_to_unavailable(tmp_path: Path) -> None:
    """A database path that SQLite cannot open raises `BackendUnavailable`, with the driver error as the cause."""

    with pytest.raises(BackendUnavailable) as captured:
        SQLiteBackend(tmp_path)

    assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sqlite_backend_maps_lock_timeout_to_retryable_busy_error(tmp_path: Path) -> None:
    """A lock timeout raises `BackendBusy`, which is retryable and has the driver error as its cause."""

    path = tmp_path / "busy.db"
    with SQLiteBackend(path):
        pass
    first_connection = sqlite3.connect(path, isolation_level=None)
    second = SQLiteBackend(path, timeout=0.01)
    try:
        first_connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(BackendBusy) as captured:
            second.put_block(MemoryBlock(type="note", value={}))
        assert captured.value.retryable is True
        assert isinstance(captured.value.__cause__, sqlite3.Error)
    finally:
        first_connection.rollback()
        first_connection.close()
        second.close()


def test_sqlite_backend_maps_full_database_to_capacity_error(tmp_path: Path) -> None:
    """A real full database in SQLite raises `BackendCapacityExceeded`, with the driver error as the cause."""

    path = tmp_path / "full.db"
    with SQLiteBackend(path) as backend:
        connection = backend._require_connection()
        page_count = connection.execute("PRAGMA page_count").fetchone()[0]
        connection.execute(f"PRAGMA max_page_count = {page_count}")

        with pytest.raises(BackendCapacityExceeded) as captured:
            backend.put_block(MemoryBlock(type="large", value="x" * 1_000_000))

        assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sqlite_backend_maps_read_only_writes_to_public_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-only failure from the driver raises `BackendReadOnly`, with the driver error as the cause."""

    class ReadOnlyConnection(sqlite3.Connection):
        """Connection that can simulate storage that changes to read-only."""

        deny_writes = False

        def execute(
            self,
            sql: str,
            parameters: Any = (),
        ) -> sqlite3.Cursor:
            """Reject data writes after normal backend initialization."""

            if self.deny_writes and sql.lstrip().upper().startswith(
                ("INSERT", "UPDATE", "DELETE", "REPLACE")
            ):
                raise sqlite3.OperationalError("attempt to write a readonly database")
            return super().execute(sql, parameters)

    real_connect = sqlite3.connect

    def connect_with_read_only_control(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        kwargs["factory"] = ReadOnlyConnection
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect_with_read_only_control)
    with SQLiteBackend(tmp_path / "readonly.db") as backend:
        ReadOnlyConnection.deny_writes = True
        with pytest.raises(BackendReadOnly) as captured:
            backend.put_block(MemoryBlock(type="note", value={}))

        assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sqlite_backend_reads_its_schema_identity_in_one_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A constructor that runs at the same time as another constructor on an empty file still succeeds.

    - **The race.** A second constructor commits its tables and both identity
      pragmas in one transaction. In this test, that commit occurs after the
      first constructor reads the version and the application id, and before
      it lists the schema objects.
    - **Why one snapshot.** With separate reads, the first constructor sees
      version 0 next to a complete schema and rejects the file as unowned.
      One read transaction sees the state from before the commit.
    - **Safe repeat.** The migration that follows is idempotent.
    """

    path = tmp_path / "raced.db"
    original = sqlite_module._user_schema_objects
    competing_commits = 0

    def list_objects_after_a_competing_commit(connection: sqlite3.Connection) -> set[str]:
        nonlocal competing_commits
        if competing_commits == 0:
            competing_commits += 1
            with SQLiteBackend(path):
                pass
        return original(connection)

    monkeypatch.setattr(sqlite_module, "_user_schema_objects", list_objects_after_a_competing_commit)

    with SQLiteBackend(path) as backend:
        backend.put_block(MemoryBlock(id="note", type="note", value={}))

    assert competing_commits == 1
    connection = sqlite3.connect(path)
    assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM blocks").fetchone()[0] == 1
    connection.close()


def test_sqlite_backend_waits_for_the_wal_switch_while_another_connection_holds_a_lock(
    tmp_path: Path,
) -> None:
    """If another connection holds a lock, the constructor waits on a new file and does not fail.

    - **SQLite behavior.** While another connection holds a lock, SQLite
      answers `PRAGMA journal_mode = WAL` with "database is locked" at once.
      This statement does not get the busy wait that every other statement
      gets.
    - **Backend behavior.** The backend tries the switch again until `timeout`
      ends.
    """

    path = tmp_path / "first-open.db"
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            opening = executor.submit(SQLiteBackend, path, timeout=5.0)
            Event().wait(0.1)
            holder.execute("COMMIT")
            backend = opening.result(timeout=10)
    finally:
        holder.close()

    try:
        assert backend.get_block("missing") is None
    finally:
        backend.close()
