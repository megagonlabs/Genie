import logging
from typing import Dict, Tuple
from redis import Redis
from redis.exceptions import ResponseError
from redis.commands.search.field import TextField, TagField, VectorField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from genie.interfaces.index_manager import IIndexManager

logger = logging.getLogger(__name__)


class RedisIndexManager(IIndexManager):
    """Handles the graceful creation and schema evolution of the RediSearch index."""

    def __init__(self, redis_client: Redis, index_name: str = "genie_idx", prefix: str = "node:"):
        self.connection = redis_client
        self.index_name = index_name
        self.prefix = prefix

        # INDUSTRY STANDARD: Set separator to '|' so paths with commas aren't accidentally split
        self.schema = [TextField("search_text"), TagField("path", separator="|"), VectorField("embedding", "HNSW", {"TYPE": "FLOAT32", "DIM": 384, "DISTANCE_METRIC": "COSINE"})]

    def initialize_index(self):
        try:
            self.connection.ft(self.index_name).info()
            logger.info(f"Index '{self.index_name}' already exists. Checking schema for missing fields...")
            self._ensure_schema_completeness()
        except ResponseError as e:
            error_msg = str(e).lower()
            if "unknown index" in error_msg or "no such index" in error_msg:
                logger.info(f"Index '{self.index_name}' not found. Creating new index...")
                self._create_index()
            else:
                logger.error(f"Unexpected Redis Response: {e}")
                raise e

    def _create_index(self):
        definition = IndexDefinition(prefix=[self.prefix], index_type=IndexType.HASH)
        self.connection.ft(self.index_name).create_index(fields=self.schema, definition=definition)
        logger.info(f"Successfully created index '{self.index_name}'.")

    def _ensure_schema_completeness(self):
        info = self.connection.ft(self.index_name).info()
        attributes = info.get('attributes', [])
        existing_fields = set()

        for attr in attributes:
            if isinstance(attr, dict):
                identifier = attr.get('identifier', attr.get('attribute', ''))
                if isinstance(identifier, bytes):
                    identifier = identifier.decode('utf-8')
                existing_fields.add(identifier)
            elif isinstance(attr, list):
                attr_strs = [a.decode('utf-8') if isinstance(a, bytes) else str(a) for a in attr]
                if 'identifier' in attr_strs:
                    existing_fields.add(attr_strs[attr_strs.index('identifier') + 1])
                else:
                    existing_fields.add(attr_strs[0])

        for field_obj in self.schema:
            field_name = field_obj.name
            if field_name not in existing_fields:
                logger.info(f"Field '{field_name}' missing in index. Altering schema...")
                try:
                    cmd = ["FT.ALTER", self.index_name, "SCHEMA", "ADD"] + field_obj.args
                    self.connection.execute_command(*cmd)
                except Exception as e:
                    logger.warning(f"Could not add field '{field_name}': {e}")
