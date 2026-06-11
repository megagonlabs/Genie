import pytest
from unittest.mock import MagicMock
from genie.core.orchestrator import MemoryOrchestrator


class TestMemoryOrchestrator:
    @pytest.fixture
    def mock_json_source(self):
        source = MagicMock()
        source.get_absolute_path.return_value = "/mock_workspace/mock_node"
        return source

    @pytest.fixture
    def mock_vector_source(self):
        return MagicMock()

    @pytest.fixture
    def mock_index_manager(self):
        return MagicMock()

    @pytest.fixture
    def orchestrator(self, mock_json_source, mock_vector_source, mock_index_manager):
        mock_embedder = MagicMock()
        mock_embedder.generate_vector.return_value = [0.05] * 384
        return MemoryOrchestrator(json_source=mock_json_source, vector_source=mock_vector_source, index_manager=mock_index_manager, embedder=mock_embedder)

    def test_setup_initializes_index(self, orchestrator, mock_index_manager):
        orchestrator.setup()
        mock_index_manager.initialize_index.assert_called_once()

    def test_create_workspace(self, orchestrator, mock_json_source, mock_vector_source):
        node = orchestrator.create_workspace(name="Strategic Plans", parent_id="org_root")
        assert node is not None
        assert node.name == "Strategic Plans"
        assert node.parent_id == "org_root"
        assert node.node_id.startswith("workspace_")
        mock_json_source.upsert_node.assert_called_once()
        mock_vector_source.upsert_document.assert_not_called()

    def test_create_workspace_rejects_missing_parent(self, orchestrator, mock_json_source):
        mock_json_source.get_node.return_value = None
        with pytest.raises(ValueError, match="parent_id 'Missing Workspace' does not exist"):
            orchestrator.create_workspace(name="Child Workspace", parent_id="Missing Workspace")
        mock_json_source.upsert_node.assert_not_called()

    def test_save_memory_success_and_uuid_format(self, orchestrator, mock_json_source, mock_vector_source):
        workspace_id = "workspace_alpha"
        content = "Agent discovered a new paradigm in neural networks."
        node = orchestrator.save_memory(workspace_id, content)
        assert node is not None
        assert node.payload["text"] == content
        assert node.parent_id == workspace_id
        assert node.node_id.startswith("node_")
        assert len(node.node_id) == 37
        mock_json_source.upsert_node.assert_called_once()
        mock_vector_source.upsert_document.assert_called_once()

    def test_save_memory_rejects_missing_workspace(self, orchestrator, mock_json_source, mock_vector_source):
        mock_json_source.get_node.return_value = None
        with pytest.raises(ValueError, match="workspace_node_id 'Workspace Name' does not exist"):
            orchestrator.save_memory("Workspace Name", "Humidity affects stomatal conductance.")
        mock_json_source.upsert_node.assert_not_called()
        mock_vector_source.upsert_document.assert_not_called()

    def test_save_memory_empty_content_skips_vector(self, orchestrator, mock_json_source, mock_vector_source):
        workspace_id = "workspace_beta"
        node = orchestrator.save_memory(workspace_id, content_text="")
        assert node is not None
        assert node.payload["text"] == ""
        mock_json_source.upsert_node.assert_called_once()
        mock_vector_source.upsert_document.assert_not_called()

    def test_save_memory_rollback_on_vector_failure(self, orchestrator, mock_json_source, mock_vector_source):
        workspace_id = "workspace_gamma"
        content = "This text will cause a simulated vector database timeout."
        mock_vector_source.upsert_document.side_effect = Exception("Vector DB Connection Timeout")
        with pytest.raises(Exception, match="Vector DB Connection Timeout"):
            orchestrator.save_memory(workspace_id, content)
        mock_json_source.upsert_node.assert_called_once()
        mock_json_source.delete_node.assert_called_once()

    def test_search_memory_encapsulation(self, orchestrator, mock_json_source, mock_vector_source):
        mock_vector_source.hybrid_search.return_value = [{"node_id": "node_xyz", "score": 0.01, "text": "found target memory"}]
        results = orchestrator.search_memory(query_text="Find GPU info", scope_node_id="workspace_alpha", top_k=3)
        assert len(results) == 1
        assert results[0]["node_id"] == "node_xyz"
        mock_json_source.get_absolute_path.assert_called_once_with("workspace_alpha")
        mock_vector_source.hybrid_search.assert_called_once_with(query_text="Find GPU info", query_vector=[0.05] * 384, path_filter="/mock_workspace/mock_node", top_k=3)

    def test_search_memory_rejects_missing_scope(self, orchestrator, mock_json_source, mock_vector_source):
        mock_json_source.get_node.return_value = None
        with pytest.raises(ValueError, match="scope_node_id 'Workspace Name' does not exist"):
            orchestrator.search_memory(query_text="Find GPU info", scope_node_id="Workspace Name", top_k=3)
        mock_vector_source.hybrid_search.assert_not_called()

    def test_get_memory_forwarding(self, orchestrator, mock_json_source):
        orchestrator.get_memory("node_123")
        mock_json_source.get_node.assert_called_once_with("node_123")

    def test_delete_memory_forwarding(self, orchestrator, mock_json_source):
        orchestrator.delete_memory("node_456")
        mock_json_source.delete_node.assert_called_once_with("node_456")
