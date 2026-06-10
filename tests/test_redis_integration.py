import pytest
import redis
import json
from genie.models.node import Node
from genie.infrastructure.redis_index_manager import RedisIndexManager
from genie.infrastructure.redis_json_source import RedisJsonSource
from genie.infrastructure.redis_vector_source import RedisVectorSource

REDIS_URL = "redis://localhost:6379/0"


@pytest.fixture(scope="module")
def redis_client():
    client = redis.Redis.from_url(REDIS_URL)
    try:
        client.ping()
    except redis.ConnectionError:
        pytest.skip("Local Redis Stack instance is not running. Skipping integration tests.")

    # Pre-flight wipe to guarantee determinism
    client.flushdb()

    yield client

    # Post-flight wipe commented out so you can inspect the DB manually
    client.flushdb()


@pytest.fixture
def json_source(redis_client):
    return RedisJsonSource(redis_client)


@pytest.fixture
def vector_source(redis_client):
    return RedisVectorSource(redis_client, index_name="genie_idx")


class TestRedisIntegration:
    def test_index_manager_initialization(self, redis_client):
        """Tests graceful index creation and schema validation."""
        manager = RedisIndexManager(redis_client, index_name="genie_idx")
        manager.initialize_index()

        info = redis_client.ft("genie_idx").info()
        assert info.get("index_name") in [b"genie_idx", "genie_idx"]

    def test_deep_tree_creation_and_pathing(self, json_source):
        """Tests creating a complex 4-level deep hierarchy and validates absolute path resolution."""
        json_source.upsert_node(Node(node_id="org_root", name="Organization"))
        json_source.upsert_node(Node(node_id="proj_x", name="Project X", parent_id="org_root"))
        json_source.upsert_node(Node(node_id="session_42", name="Session 42", parent_id="proj_x"))
        json_source.upsert_node(Node(node_id="mem_99", name="Final Thought", parent_id="session_42", payload={"status": "complete"}))

        path = json_source.get_absolute_path("mem_99")
        assert path == "/org_root/proj_x/session_42/mem_99"

        leaf = json_source.get_node("mem_99")
        assert leaf.name == "Final Thought"
        assert leaf.payload["status"] == "complete"
        assert leaf.is_leaf is True

    def test_bfs_cascading_deletion(self, json_source):
        """Tests that deleting a high-level folder cleanly wipes all deeply nested children."""
        assert json_source.get_node("session_42") is not None
        assert json_source.get_node("mem_99") is not None

        deletion_success = json_source.delete_node("proj_x")

        assert deletion_success is True
        assert json_source.get_node("proj_x") is None
        assert json_source.get_node("session_42") is None
        assert json_source.get_node("mem_99") is None
        assert json_source.get_node("org_root") is not None

    def test_circular_reference_circuit_breaker(self, json_source, redis_client):
        """Tests that the system survives malicious or accidental circular graphs."""
        json_source.upsert_node(Node(node_id="node_a", name="A"))
        json_source.upsert_node(Node(node_id="node_b", name="B", parent_id="node_a"))

        redis_client.hset("node:node_a", "parent_id", "node_b")

        path = json_source.get_absolute_path("node_b")
        assert "node_a" in path
        assert "node_b" in path

    def test_malformed_json_recovery(self, json_source, redis_client):
        """Tests that corrupted database records do not crash the application during hydration."""
        json_source.upsert_node(Node(node_id="corrupt_node", name="Will Corrupt"))
        redis_client.hset("node:corrupt_node", "payload", "{broken_json: True, missing_quotes}")

        node = json_source.get_node("corrupt_node")
        assert node is None

    def test_strict_boundary_semantic_filtering(self, redis_client, vector_source):
        """
        Tests that semantic vector searches are strictly locked to their hierarchical boundaries.
        Even if a document is a 100% semantic match, it MUST NOT return if it is outside the path filter.
        """
        # FORCE DROP THE STALE INDEX GHOST
        try:
            redis_client.ft("genie_idx").dropindex(dd=True)
        except Exception:
            pass

        RedisIndexManager(redis_client, index_name="genie_idx").initialize_index()

        vector_gpu = [0.1] * 384
        vector_cpu = [-0.1] * 384

        vector_source.upsert_document("doc_alpha_1", "Nvidia tensor cores are fast", vector_gpu, "/workspace_alpha/doc_alpha_1")
        vector_source.upsert_document("doc_alpha_2", "AMD CPUs are efficient", vector_cpu, "/workspace_alpha/doc_alpha_2")
        vector_source.upsert_document("doc_beta_1", "Nvidia tensor cores are fast", vector_gpu, "/workspace_beta/doc_beta_1")

        results_alpha = vector_source.hybrid_search(query_text="tensor cores", query_vector=vector_gpu, path_filter="/workspace_alpha", top_k=1)

        assert len(results_alpha) == 1
        assert results_alpha[0]["node_id"] == "doc_alpha_1"

        results_root = vector_source.hybrid_search(query_text="tensor cores", query_vector=vector_gpu, path_filter="/", top_k=2)

        assert len(results_root) == 2
        returned_ids = [res["node_id"] for res in results_root]
        assert "doc_alpha_1" in returned_ids
        assert "doc_beta_1" in returned_ids
