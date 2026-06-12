"""Redis RediSearch index provisioning and verification utilities for Genie."""

from typing import Any, cast

from redis import Redis
from redis.exceptions import ResponseError
from redis.commands.search.field import TextField, TagField, VectorField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from genie.exceptions import ConfigurationError
from genie.interfaces.index_manager import IIndexManager


class RedisIndexManager(IIndexManager):
    """Manage the RediSearch index used by Genie.

    Parameters:
    redis_client:
        Connected Redis client.
    index_name:
        RediSearch index name to verify or create.
    prefix:
        Hash key prefix that the index should cover.
    vector_dimension:
        Expected embedding dimension for the indexed vector field.
    """

    def __init__(self, redis_client: Redis, index_name: str = "genie_idx", prefix: str = "node:", vector_dimension: int = 384):
        self.connection = redis_client
        self.index_name = index_name
        self.prefix = prefix
        self.vector_dimension = vector_dimension
        self.schema = [
            TextField("search_text"),
            TagField("path", separator="|"),
            TagField("record_status", separator="|"),
            VectorField("embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": self.vector_dimension, "DISTANCE_METRIC": "COSINE"}),
        ]

    def create_index(self) -> None:
        """Create the RediSearch index when it does not yet exist.

        Existing indexes are verified rather than mutated. If an index already
        exists but is incompatible with Genie, a configuration error is raised
        instead of altering the live schema implicitly.
        """
        try:
            self.verify_index()
        except ConfigurationError as exc:
            if "does not exist" not in str(exc):
                raise
            self._create_index()
            self.verify_index()

    def verify_index(self) -> None:
        """Verify that the configured RediSearch index exists and matches Genie."""
        info = self._get_index_info()
        self._verify_index_definition(info)
        self._verify_schema(info)

    def _create_index(self) -> None:
        """Issue the RediSearch create-index command for Genie's schema."""
        definition = IndexDefinition(prefix=[self.prefix], index_type=IndexType.HASH)
        self.connection.ft(self.index_name).create_index(fields=self.schema, definition=definition)

    def _get_index_info(self) -> dict[str, Any]:
        """Fetch and normalize ``FT.INFO`` metadata for the configured index."""
        try:
            raw_info = self.connection.ft(self.index_name).info()
        except ResponseError as exc:
            error_msg = str(exc).lower()
            if "unknown index" in error_msg or "no such index" in error_msg:
                raise ConfigurationError(
                    f"Redis index '{self.index_name}' does not exist. Provision it explicitly before using Genie."
                ) from exc
            raise
        decoded_info = self._decode_nested(raw_info)
        if not isinstance(decoded_info, dict):
            raise ConfigurationError(
                f"Redis index '{self.index_name}' returned malformed metadata."
            )
        # RediSearch response keys vary by version, so normalize them once up front.
        return self._normalize_mapping_keys(cast(dict[str, Any], decoded_info))

    def _verify_index_definition(self, info: dict[str, Any]) -> None:
        """Validate the index definition metadata such as prefix and key type."""
        definition = self._coerce_mapping(info.get("index_definition") or {})
        if definition is None:
            raise ConfigurationError(
                f"Redis index '{self.index_name}' returned malformed index definition metadata."
            )
        definition = self._normalize_mapping_keys(definition)
        prefixes = definition.get("prefixes", [])
        if isinstance(prefixes, tuple):
            prefixes = list(prefixes)
        if not isinstance(prefixes, list):
            raise ConfigurationError(
                f"Redis index '{self.index_name}' returned malformed prefix metadata."
            )
        if self.prefix not in prefixes:
            raise ConfigurationError(
                f"Redis index '{self.index_name}' is configured for prefixes {prefixes}, expected '{self.prefix}'."
            )

        index_type = str(definition.get("key_type", "")).upper()
        if index_type and index_type != "HASH":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' uses key type '{index_type}', expected 'HASH'."
            )

    def _verify_schema(self, info: dict[str, Any]) -> None:
        """Validate that the live RediSearch schema matches Genie expectations."""
        attributes = info.get("attributes", [])
        if not isinstance(attributes, list):
            raise ConfigurationError(
                f"Redis index '{self.index_name}' returned malformed schema metadata."
            )
        normalized_fields = {
            field["identifier"]: field for field in (self._normalize_attribute(attr) for attr in attributes)
        }

        self._verify_tag_field(normalized_fields, "path")
        self._verify_tag_field(normalized_fields, "record_status")
        self._verify_text_field(normalized_fields, "search_text")
        self._verify_vector_field(normalized_fields, "embedding")

    def _verify_text_field(self, fields: dict[str, dict[str, Any]], field_name: str) -> None:
        """Validate a required RediSearch text field."""
        field = fields.get(field_name)
        if field is None:
            raise ConfigurationError(f"Redis index '{self.index_name}' is missing required field '{field_name}'.")
        if field.get("type") != "TEXT":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' has type '{field.get('type')}', expected 'TEXT'."
            )

    def _verify_tag_field(self, fields: dict[str, dict[str, Any]], field_name: str) -> None:
        """Validate a required RediSearch tag field."""
        field = fields.get(field_name)
        if field is None:
            raise ConfigurationError(f"Redis index '{self.index_name}' is missing required field '{field_name}'.")
        if field.get("type") != "TAG":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' has type '{field.get('type')}', expected 'TAG'."
            )

    def _verify_vector_field(self, fields: dict[str, dict[str, Any]], field_name: str) -> None:
        """Validate the vector field configuration used for semantic search."""
        field = fields.get(field_name)
        if field is None:
            raise ConfigurationError(f"Redis index '{self.index_name}' is missing required field '{field_name}'.")
        if field.get("type") != "VECTOR":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' has type '{field.get('type')}', expected 'VECTOR'."
            )

        algorithm = str(field.get("algorithm", "")).upper()
        if algorithm and algorithm != "HNSW":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' uses algorithm '{algorithm}', expected 'HNSW'."
            )

        attributes = field.get("attributes", {})
        if not isinstance(attributes, dict):
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' returned malformed vector attributes."
            )
        dim = self._safe_int(attributes.get("DIM"))
        if dim != self.vector_dimension:
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' has DIM={dim}, expected {self.vector_dimension}."
            )

        dtype = str(attributes.get("TYPE", "")).upper()
        if dtype and dtype != "FLOAT32":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' has TYPE='{dtype}', expected 'FLOAT32'."
            )

        distance = str(attributes.get("DISTANCE_METRIC", "")).upper()
        if distance and distance != "COSINE":
            raise ConfigurationError(
                f"Redis index '{self.index_name}' field '{field_name}' has DISTANCE_METRIC='{distance}', expected 'COSINE'."
            )

    def _normalize_attribute(self, attribute: Any) -> dict[str, Any]:
        """Normalize a RediSearch field descriptor into a predictable mapping."""
        decoded_attribute = self._decode_nested(attribute)
        normalized = self._coerce_mapping(decoded_attribute) or {}
        normalized = self._normalize_mapping_keys(normalized)

        identifier = normalized.get("identifier", normalized.get("attribute"))
        field_type = str(normalized.get("type", "")).upper()
        algorithm = str(normalized.get("algorithm", normalized.get("algo", ""))).upper()
        nested_attributes = normalized.get("attributes")

        if nested_attributes is None and isinstance(attribute, list):
            nested_attributes = self._extract_vector_attributes(attribute)

        if isinstance(nested_attributes, list):
            decoded_nested_attributes = self._decode_nested(nested_attributes)
            nested_attributes = self._pairs_to_dict(
                decoded_nested_attributes if isinstance(decoded_nested_attributes, list) else []
            )
        elif not isinstance(nested_attributes, dict):
            nested_attributes = {}
        else:
            nested_attributes = self._normalize_mapping_keys(nested_attributes)

        return {
            "identifier": identifier,
            "type": field_type,
            "algorithm": algorithm,
            "attributes": {str(key).upper(): value for key, value in nested_attributes.items()},
        }

    def _extract_vector_attributes(self, attribute: list[Any]) -> dict[str, Any]:
        """Extract nested vector attributes from legacy list-based metadata."""
        decoded = self._decode_nested(attribute)
        if not isinstance(decoded, list):
            return {}
        if "VECTOR" not in decoded:
            return {}
        vector_index = decoded.index("VECTOR")
        if len(decoded) <= vector_index + 2:
            return {}
        start_index = vector_index + 3
        if isinstance(decoded[vector_index + 2], int):
            start_index = vector_index + 3
        return self._pairs_to_dict(decoded[start_index:])

    def _pairs_to_dict(self, values: list[Any]) -> dict[str, Any]:
        """Convert alternating key/value lists into a dictionary."""
        result: dict[str, Any] = {}
        for index in range(0, len(values) - 1, 2):
            key = values[index]
            value = values[index + 1]
            result[str(key)] = value
        return result

    def _coerce_mapping(self, value: Any) -> dict[str, Any] | None:
        """Convert supported Redis metadata shapes into a dictionary."""
        if isinstance(value, dict):
            return cast(dict[str, Any], value)
        if isinstance(value, list):
            return self._pairs_to_dict(value)
        if isinstance(value, tuple):
            return self._pairs_to_dict(list(value))
        return None

    def _normalize_mapping_keys(self, mapping: dict[str, Any]) -> dict[str, Any]:
        """Normalize mapping keys to lowercase snake_case strings."""
        normalized: dict[str, Any] = {}
        for key, value in mapping.items():
            normalized_key = self._normalize_key(str(key))
            normalized[normalized_key] = value
        return normalized

    def _normalize_key(self, key: str) -> str:
        """Normalize a metadata key to a canonical snake_case representation."""
        return key.strip().lower().replace(" ", "_").replace("-", "_")

    def _decode_nested(self, value: Any) -> Any:
        """Recursively decode Redis byte values into native Python strings."""
        if isinstance(value, bytes):
            return value.decode("utf-8")
        if isinstance(value, list):
            return [self._decode_nested(item) for item in value]
        if isinstance(value, tuple):
            return [self._decode_nested(item) for item in value]
        if isinstance(value, dict):
            return {self._decode_nested(key): self._decode_nested(item) for key, item in value.items()}
        return value

    def _safe_int(self, value: Any) -> Any:
        """Convert a value to ``int`` when possible, otherwise return it unchanged."""
        try:
            return int(value)
        except (TypeError, ValueError):
            return value
