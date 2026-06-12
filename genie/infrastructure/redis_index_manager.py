from redis import Redis
from redis.exceptions import ResponseError
from redis.commands.search.field import TextField, TagField, VectorField
from redis.commands.search.indexDefinition import IndexDefinition, IndexType
from genie.interfaces.index_manager import IIndexManager


class RedisIndexManager(IIndexManager):

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

    def initialize_index(self):
        try:
            self.connection.ft(self.index_name).info()
            self._ensure_schema_completeness()
        except ResponseError as e:
            error_msg = str(e).lower()
            if "unknown index" in error_msg or "no such index" in error_msg:
                self._create_index()
            else:
                raise e

    def _create_index(self):
        definition = IndexDefinition(prefix=[self.prefix], index_type=IndexType.HASH)
        self.connection.ft(self.index_name).create_index(fields=self.schema, definition=definition)

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
                try:
                    cmd = ["FT.ALTER", self.index_name, "SCHEMA", "ADD"] + field_obj.args
                    self.connection.execute_command(*cmd)
                except Exception as e:
                    raise e
