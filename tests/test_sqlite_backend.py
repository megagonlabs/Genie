"""Tests for the persistent SQLite backend."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from threading import Barrier, Event
from typing import Any

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
    SQLiteBackend,
    UnsupportedOperation,
)
from genie.structures import KeyBinding, MemoryAddress


def test_sqlite_backend_implements_the_core_backend_protocol() -> None:
    """SQLite exposes every method required by ``MemoryBackend``."""

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
    """Connection and durability options fail before opening a database."""

    with pytest.raises(error, match=message):
        SQLiteBackend(**kwargs)


def test_sqlite_backend_persists_core_memory_state(tmp_path: Path) -> None:
    """Blocks, policies, ownership, links, and search survive a restart."""

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


def test_sqlite_backend_rejects_conflicts_without_partial_changes() -> None:
    """Failed writes roll back and preserve the previous ownership tree."""

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
    """Link updates preserve topology and reads cannot mutate stored JSON."""

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
    """SQLite persistence does not silently change custom encoded value types."""

    with SQLiteBackend() as backend:
        with pytest.raises(TypeError, match="strict JSON"):
            backend.put_block(MemoryBlock(type="tuple", value=("not", "json")))


@pytest.mark.parametrize(
    ("column", "stored_value"),
    [
        ("value_json", "{not-json"),
        ("value_json", "NaN"),
        ("created_at", "not-a-timestamp"),
    ],
)
def test_sqlite_backend_reports_invalid_stored_block_data_as_corruption(
    column: str,
    stored_value: str,
) -> None:
    """Malformed rows fail through the documented backend error boundary."""

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
    """Truthy strings in stored policies cannot silently widen access."""

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
    """Invalid optional-capability records use the same corruption boundary."""

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


def test_sqlite_backend_context_manager_closes_connection() -> None:
    """The backend owns and closes its SQLite connection."""

    with SQLiteBackend() as backend:
        backend.put_block(MemoryBlock(type="note", value={}))

    with pytest.raises(RuntimeError, match="closed"):
        backend.get_block("missing")


def test_sqlite_backend_rejects_newer_schema(tmp_path: Path) -> None:
    """Opening an unknown future schema fails instead of corrupting it."""

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
    """The public Memory workflow passes its read version into the write."""

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
    """A policy-checked subtree cannot gain an unchecked child before deletion."""

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
    """Leaf-first deletion does not depend on SQLite cascade recursion depth."""

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
    """Fresh databases receive Genie identity and incompatible schemas fail."""

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
    """An unrelated schema is rejected before any Genie migration runs."""

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
    """A validated pre-identity schema is adopted without changing its data."""

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
    """Constructor failures also use stable backend error types."""

    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a SQLite database")

    with pytest.raises(BackendCorruption) as captured:
        SQLiteBackend(path)

    assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sqlite_backend_maps_unopenable_path_to_unavailable(tmp_path: Path) -> None:
    """An unusable database path raises a stable availability error."""

    with pytest.raises(BackendUnavailable) as captured:
        SQLiteBackend(tmp_path)

    assert isinstance(captured.value.__cause__, sqlite3.Error)


def test_sqlite_backend_maps_lock_timeout_to_retryable_busy_error(tmp_path: Path) -> None:
    """Lock timeouts expose a stable retryable Genie error with driver cause."""

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
    """SQLite's real full-database result maps to a stable capacity error."""

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
    """A driver read-only failure is exposed through the public backend API."""

    class ReadOnlyConnection(sqlite3.Connection):
        """Connection that can simulate storage becoming read-only."""

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
