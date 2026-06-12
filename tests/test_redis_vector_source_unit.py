from types import SimpleNamespace
from unittest.mock import MagicMock

from genie.infrastructure.redis_vector_source import RedisVectorSource


class TestRedisVectorSourceUnit:
    def test_hybrid_search_uses_rrf_and_merges_duplicate_hits(self):
        client = MagicMock()
        ft = client.ft.return_value
        ft.search.side_effect = [
            SimpleNamespace(
                docs=[
                    SimpleNamespace(id="node:alpha", search_text="alpha text"),
                    SimpleNamespace(id="node:beta", search_text="beta text"),
                ]
            ),
            SimpleNamespace(
                docs=[
                    SimpleNamespace(id="node:beta", search_text="beta text", vector_score="0.01"),
                    SimpleNamespace(id="node:gamma", search_text="gamma text", vector_score="0.02"),
                ]
            ),
        ]

        source = RedisVectorSource(client)
        results = source.hybrid_search(
            query_text="alpha beta",
            query_vector=[0.1, 0.2, 0.3],
            path_filter="/workspace_alpha",
            top_k=3,
        )

        assert [result["node_id"] for result in results] == ["beta", "alpha", "gamma"]
        assert results[0]["text"] == "beta text"
        assert ft.search.call_count == 2

        text_query = ft.search.call_args_list[0].args[0]
        vector_query = ft.search.call_args_list[1].args[0]
        assert text_query.query_string() == "(@path:{\\/workspace_alpha*} @record_status:{ready} alpha beta)"
        assert vector_query.query_string() == "(@path:{\\/workspace_alpha*} @record_status:{ready})=>[KNN 10 @embedding $query_vec AS vector_score]"

    def test_hybrid_search_root_scope_omits_path_filter(self):
        client = MagicMock()
        ft = client.ft.return_value
        ft.search.side_effect = [
            SimpleNamespace(docs=[SimpleNamespace(id="node:alpha", search_text="alpha text")]),
            SimpleNamespace(docs=[SimpleNamespace(id="node:alpha", search_text="alpha text", vector_score="0.01")]),
        ]

        source = RedisVectorSource(client)
        source.hybrid_search(
            query_text="alpha",
            query_vector=[0.1, 0.2, 0.3],
            path_filter="/",
            top_k=1,
        )

        text_query = ft.search.call_args_list[0].args[0]
        vector_query = ft.search.call_args_list[1].args[0]
        assert text_query.query_string() == "(@record_status:{ready} alpha)"
        assert vector_query.query_string() == "(@record_status:{ready})=>[KNN 10 @embedding $query_vec AS vector_score]"

    def test_upsert_document_marks_record_ready(self):
        client = MagicMock()
        source = RedisVectorSource(client)

        source.upsert_document(
            node_id="mem_1",
            raw_text="alpha beta",
            dense_vector=[0.1, 0.2],
            path_string="/workspace/mem_1",
            record_status="ready",
            last_error="",
        )

        client.hset.assert_called_once()
        mapping = client.hset.call_args.kwargs["mapping"]
        assert mapping["record_status"] == "ready"
        assert mapping["last_error"] == ""

    def test_upsert_document_uses_configured_key_prefix(self):
        client = MagicMock()
        source = RedisVectorSource(client, index_name="genie_idx_prod_v1", key_prefix="tenant_a:node:")

        source.upsert_document(
            node_id="mem_1",
            raw_text="alpha beta",
            dense_vector=[0.1, 0.2],
            path_string="/workspace/mem_1",
        )

        assert client.hset.call_args.args[0] == "tenant_a:node:mem_1"

    def test_extract_node_id_removes_full_configured_prefix(self):
        client = MagicMock()
        source = RedisVectorSource(client, index_name="genie_idx_prod_v1", key_prefix="tenant_a:node:")

        assert source._extract_node_id("tenant_a:node:mem_1") == "mem_1"
