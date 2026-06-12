from unittest.mock import MagicMock

import pytest
from redis.exceptions import ResponseError

from genie.exceptions import ConfigurationError
from genie.infrastructure.redis_index_manager import RedisIndexManager


def build_index_info(
    *,
    prefixes=None,
    vector_dim=384,
    distance_metric="COSINE",
    vector_type="FLOAT32",
    vector_algorithm="HNSW",
):
    return {
        "index_definition": {
            "key_type": "HASH",
            "prefixes": prefixes or ["node:"],
        },
        "attributes": [
            {"identifier": "search_text", "attribute": "search_text", "type": "TEXT"},
            {"identifier": "path", "attribute": "path", "type": "TAG"},
            {"identifier": "record_status", "attribute": "record_status", "type": "TAG"},
            {
                "identifier": "embedding",
                "attribute": "embedding",
                "type": "VECTOR",
                "algorithm": vector_algorithm,
                "attributes": {
                    "TYPE": vector_type,
                    "DIM": vector_dim,
                    "DISTANCE_METRIC": distance_metric,
                },
            },
        ],
    }


def build_legacy_index_info_list(
    *,
    prefixes=None,
    vector_dim=384,
    distance_metric="COSINE",
    vector_type="FLOAT32",
    vector_algorithm="HNSW",
):
    return {
        "Index Definition": [
            "key_type",
            "HASH",
            "prefixes",
            prefixes or ["node:"],
        ],
        "Attributes": [
            ["identifier", "search_text", "attribute", "search_text", "type", "TEXT"],
            ["identifier", "path", "attribute", "path", "type", "TAG"],
            ["identifier", "record_status", "attribute", "record_status", "type", "TAG"],
            [
                "identifier",
                "embedding",
                "attribute",
                "embedding",
                "type",
                "VECTOR",
                "algorithm",
                vector_algorithm,
                "attributes",
                [
                    "TYPE",
                    vector_type,
                    "DIM",
                    vector_dim,
                    "DISTANCE_METRIC",
                    distance_metric,
                ],
            ],
        ],
    }


class TestRedisIndexManagerUnit:
    def test_verify_index_accepts_expected_schema(self):
        client = MagicMock()
        client.ft.return_value.info.return_value = build_index_info()
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        manager.verify_index()

        client.ft.return_value.info.assert_called_once()

    def test_verify_index_accepts_legacy_metadata_shape(self):
        client = MagicMock()
        client.ft.return_value.info.return_value = build_legacy_index_info_list()
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        manager.verify_index()

    def test_verify_index_accepts_versioned_name_with_custom_prefix(self):
        client = MagicMock()
        client.ft.return_value.info.return_value = build_index_info(prefixes=["tenant_a:node:"])
        manager = RedisIndexManager(
            client,
            index_name="genie_idx_prod_v1",
            prefix="tenant_a:node:",
        )

        manager.verify_index()

    def test_verify_index_rejects_missing_index(self):
        client = MagicMock()
        client.ft.return_value.info.side_effect = ResponseError("Unknown Index name")
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        with pytest.raises(ConfigurationError, match="does not exist"):
            manager.verify_index()

    def test_verify_index_rejects_prefix_mismatch(self):
        client = MagicMock()
        client.ft.return_value.info.return_value = build_index_info(prefixes=["other:"])
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        with pytest.raises(ConfigurationError, match="configured for prefixes"):
            manager.verify_index()

    def test_verify_index_rejects_vector_dimension_mismatch(self):
        client = MagicMock()
        client.ft.return_value.info.return_value = build_index_info(vector_dim=768)
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:", vector_dimension=384)

        with pytest.raises(ConfigurationError, match="DIM=768, expected 384"):
            manager.verify_index()

    def test_verify_index_rejects_distance_metric_mismatch(self):
        client = MagicMock()
        client.ft.return_value.info.return_value = build_index_info(distance_metric="L2")
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        with pytest.raises(ConfigurationError, match="DISTANCE_METRIC='L2', expected 'COSINE'"):
            manager.verify_index()

    def test_create_index_creates_when_missing(self):
        client = MagicMock()
        ft = client.ft.return_value
        ft.info.side_effect = [ResponseError("Unknown Index name"), build_index_info()]
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        manager.create_index()

        ft.create_index.assert_called_once()
        assert ft.info.call_count == 2

    def test_create_index_does_not_mutate_existing_incompatible_schema(self):
        client = MagicMock()
        ft = client.ft.return_value
        ft.info.return_value = build_index_info(vector_dim=768)
        manager = RedisIndexManager(client, index_name="genie_idx", prefix="node:")

        with pytest.raises(ConfigurationError, match="DIM=768, expected 384"):
            manager.create_index()

        ft.create_index.assert_not_called()
