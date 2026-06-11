from typing import List, Optional
import json
from genie.interfaces.json_source import IJsonSource
from genie.models.node import Node


class RedisJsonSource(IJsonSource):
    def __init__(self, redis_client):
        self.client = redis_client

    @staticmethod
    def _decode(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def upsert_node(self, node: Node) -> Node:
        redis_key = f"node:{node.node_id}"
        if node.parent_id == node.node_id:
            raise ValueError(f"Node '{node.node_id}' cannot be its own parent.")
        if node.parent_id and not self.client.exists(f"node:{node.parent_id}"):
            raise ValueError(f"parent_id '{node.parent_id}' does not exist. Create the parent node before attaching children.")
        existing_parent_bytes = self.client.hget(redis_key, "parent_id")
        if existing_parent_bytes:
            existing_parent = self._decode(existing_parent_bytes)
            if existing_parent and existing_parent != node.parent_id:
                self.client.srem(f"node:{existing_parent}:children", node.node_id)
        node_data = {"name": node.name, "parent_id": node.parent_id or "", "payload": json.dumps(node.payload), "relationships": json.dumps(node.relationships)}
        self.client.hset(redis_key, mapping=node_data)
        if node.parent_id:
            self.client.sadd(f"node:{node.parent_id}:children", node.node_id)
        return node

    def get_node(self, node_id: str) -> Optional[Node]:
        redis_key = f"node:{node_id}"
        raw_data = self.client.hgetall(redis_key)
        if not raw_data:
            return None
        try:
            decoded_data = {}
            for key, value in raw_data.items():
                field_name = self._decode(key)
                if field_name == "embedding":
                    continue
                decoded_data[field_name] = self._decode(value)
            return Node(
                node_id=node_id,
                name=decoded_data.get("name", ""),
                parent_id=decoded_data.get("parent_id") or None,
                payload=json.loads(decoded_data.get("payload", "{}")),
                relationships=json.loads(decoded_data.get("relationships", "{}")),
            )
        except json.JSONDecodeError as e:
            return None
        except Exception as e:
            return None

    def get_children(self, parent_id: str) -> List[str]:
        return [self._decode(mem) for mem in self.client.smembers(f"node:{parent_id}:children")]

    def delete_node(self, node_id: str) -> bool:
        try:
            nodes_to_process = [node_id]
            nodes_to_delete = []
            while nodes_to_process:
                current_id = nodes_to_process.pop(0)
                nodes_to_delete.append(current_id)
                children = self.get_children(current_id)
                nodes_to_process.extend(children)
            for target_id in reversed(nodes_to_delete):
                parent_id_bytes = self.client.hget(f"node:{target_id}", "parent_id")
                self.client.delete(f"node:{target_id}")
                self.client.delete(f"node:{target_id}:children")
                if parent_id_bytes:
                    parent_id = self._decode(parent_id_bytes)
                    if parent_id:
                        self.client.srem(f"node:{parent_id}:children", target_id)
            return True
        except Exception as e:
            return False

    def get_absolute_path(self, node_id: str) -> str:
        path = [node_id]
        current_id = node_id
        max_depth = 100
        while max_depth > 0:
            parent_id = self.client.hget(f"node:{current_id}", "parent_id")
            if not parent_id:
                break
            current_id = self._decode(parent_id)
            if current_id in path:
                break
            path.insert(0, current_id)
            max_depth -= 1
        return "/" + "/".join(path)
