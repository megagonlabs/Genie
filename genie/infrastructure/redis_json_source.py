from typing import List, Optional
import json
import logging

# Production absolute imports
from genie.interfaces.json_source import IJsonSource
from genie.models.node import Node

logger = logging.getLogger(__name__)


class RedisJsonSource(IJsonSource):
    def __init__(self, redis_client):
        self.client = redis_client

    def upsert_node(self, node: Node) -> Node:
        redis_key = f"node:{node.node_id}"

        # INDUSTRY STANDARD: Prevent ghost references by removing node from old parent if it moved
        existing_parent_bytes = self.client.hget(redis_key, "parent_id")
        if existing_parent_bytes:
            existing_parent = existing_parent_bytes.decode('utf-8')
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
            decoded_data = {k.decode('utf-8'): v.decode('utf-8') for k, v in raw_data.items()}

            return Node(
                node_id=node_id,
                name=decoded_data.get("name", ""),
                parent_id=decoded_data.get("parent_id") or None,
                payload=json.loads(decoded_data.get("payload", "{}")),
                relationships=json.loads(decoded_data.get("relationships", "{}")),
            )
        except json.JSONDecodeError as e:
            logger.error(f"JSON Parsing corruption in node {node_id}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected data corruption in node {node_id}: {e}")
            return None

    def get_children(self, parent_id: str) -> List[str]:
        return [mem.decode() for mem in self.client.smembers(f"node:{parent_id}:children")]

    def delete_node(self, node_id: str) -> bool:
        """
        Deletes the node and recursively deletes all descendants.
        PRODUCTION FIX: Uses an iterative Queue to prevent Python RecursionError crashes.
        """
        try:
            # Iterative Breadth-First Search queue
            nodes_to_process = [node_id]
            nodes_to_delete = []

            while nodes_to_process:
                current_id = nodes_to_process.pop(0)
                nodes_to_delete.append(current_id)
                children = self.get_children(current_id)
                nodes_to_process.extend(children)

            # Delete starting from the bottom up (optional, but clean) or all at once
            for target_id in reversed(nodes_to_delete):
                parent_id_bytes = self.client.hget(f"node:{target_id}", "parent_id")

                # Perform actual deletions
                self.client.delete(f"node:{target_id}")
                self.client.delete(f"node:{target_id}:children")

                # Remove from parent's edge set
                if parent_id_bytes:
                    parent_id = parent_id_bytes.decode('utf-8')
                    if parent_id:
                        self.client.srem(f"node:{parent_id}:children", target_id)

            return True
        except Exception as e:
            logger.error(f"Failed to delete node graph starting at {node_id}: {e}")
            return False

    def get_absolute_path(self, node_id: str) -> str:
        """
        PRODUCTION FIX: Bounded circuit breaker to prevent infinite loops from circular graph references.
        """
        path = [node_id]
        current_id = node_id
        max_depth = 100  # Circuit breaker threshold

        while max_depth > 0:
            parent_id = self.client.hget(f"node:{current_id}", "parent_id")
            if not parent_id:
                break

            current_id = parent_id.decode('utf-8')

            # Additional cycle detection
            if current_id in path:
                logger.warning(f"Circular graph reference detected at node {current_id}. Breaking path construction.")
                break

            path.insert(0, current_id)
            max_depth -= 1

        if max_depth == 0:
            logger.warning(f"Max path depth of 100 exceeded for node {node_id}. Path truncated.")

        return "/" + "/".join(path)
