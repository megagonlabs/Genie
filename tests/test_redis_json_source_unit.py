import pytest

from conftest import FakeRedisHashClient
from genie.exceptions import DataCorruptionError
from genie.infrastructure.redis_json_source import RedisJsonSource
from genie.models.node import Node


class TestRedisJsonSourceUnit:
    @pytest.fixture
    def client(self):
        return FakeRedisHashClient()

    @pytest.fixture
    def source(self, client):
        return RedisJsonSource(client)

    def test_metadata_round_trip(self, source):
        node = Node(
            node_id="root",
            name="Root",
            metadata={"owner": "alice"},
            relationships={"references": ["doc_1"]},
        )

        source.upsert_node(node)
        loaded = source.get_node("root")

        assert loaded is not None
        assert loaded.metadata == {"owner": "alice"}
        assert loaded.relationships == {"references": ["doc_1"]}

    def test_child_ids_drive_leaf_state(self, source):
        source.upsert_node(Node(node_id="root", name="Root"))
        source.upsert_node(Node(node_id="child", name="Child", parent_id="root"))

        root = source.get_node("root")
        child = source.get_node("child")

        assert root is not None
        assert root.child_ids == ["child"]
        assert root.is_leaf is False
        assert child is not None
        assert child.is_leaf is True

    def test_reparenting_removes_old_child_reference(self, source):
        source.upsert_node(Node(node_id="root_a", name="A"))
        source.upsert_node(Node(node_id="root_b", name="B"))
        source.upsert_node(Node(node_id="child", name="Child", parent_id="root_a"))

        source.upsert_node(Node(node_id="child", name="Child", parent_id="root_b"))

        assert source.get_children("root_a") == []
        assert source.get_children("root_b") == ["child"]

    def test_reparenting_rewrites_descendant_paths(self, source):
        source.upsert_node(Node(node_id="root_a", name="A"))
        source.upsert_node(Node(node_id="root_b", name="B"))
        source.upsert_node(Node(node_id="child", name="Child", parent_id="root_a"))
        source.upsert_node(Node(node_id="grandchild", name="Grandchild", parent_id="child"))

        source.upsert_node(Node(node_id="child", name="Child", parent_id="root_b"))

        assert source.get_absolute_path("child") == "/root_b/child"
        assert source.get_absolute_path("grandchild") == "/root_b/child/grandchild"

    def test_rejects_descendant_cycles(self, source):
        source.upsert_node(Node(node_id="root", name="Root"))
        source.upsert_node(Node(node_id="child", name="Child", parent_id="root"))

        with pytest.raises(ValueError, match="would create a cycle"):
            source.upsert_node(Node(node_id="root", name="Root", parent_id="child"))

    def test_get_absolute_path_raises_for_corrupt_cycle(self, source, client):
        source.upsert_node(Node(node_id="root", name="Root"))
        source.upsert_node(Node(node_id="child", name="Child", parent_id="root"))
        client.hset("node:root", "parent_id", "child")

        with pytest.raises(DataCorruptionError, match="Cycle detected"):
            source.get_absolute_path("child")

    def test_get_node_raises_on_malformed_json(self, source, client):
        source.upsert_node(Node(node_id="corrupt", name="Corrupt"))
        client.hset("node:corrupt", "metadata", "{broken_json")

        with pytest.raises(DataCorruptionError, match="malformed JSON"):
            source.get_node("corrupt")

    def test_record_status_updates_without_affecting_node_payload(self, source, client):
        source.upsert_node(Node(node_id="mem_1", name="Memory"), record_status="pending", last_error="")

        source.set_record_status("mem_1", "failed", "embedding timeout")

        assert client.hget("node:mem_1", "record_status") == "failed"
        assert client.hget("node:mem_1", "last_error") == "embedding timeout"
        loaded = source.get_node("mem_1")
        assert loaded is not None
        assert loaded.name == "Memory"
