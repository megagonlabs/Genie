from collections import deque
from typing import List, Optional
import json
from genie.exceptions import DataCorruptionError, StorageError
from genie.interfaces.json_source import IJsonSource
from genie.models.node import Node


class RedisJsonSource(IJsonSource):
    UPSERT_NODE_SCRIPT = """
local node_id = ARGV[1]
local name = ARGV[2]
local parent_id = ARGV[3]
local payload = ARGV[4]
local metadata = ARGV[5]
local relationships = ARGV[6]
local status_value = ARGV[7]
local status_provided = ARGV[8]
local error_value = ARGV[9]
local error_provided = ARGV[10]
local redis_key = "node:" .. node_id

local function resolve_path(target_id)
  local key = "node:" .. target_id
  local cached_path = redis.call("HGET", key, "path")
  if cached_path and cached_path ~= "" then
    return cached_path
  end

  local segments = {target_id}
  local current_id = target_id
  local visited = {}

  while true do
    if visited[current_id] then
      error("Cycle detected while resolving path for node '" .. target_id .. "'.")
    end
    visited[current_id] = true
    local current_parent = redis.call("HGET", "node:" .. current_id, "parent_id")
    if not current_parent or current_parent == "" then
      break
    end
    if redis.call("EXISTS", "node:" .. current_parent) == 0 then
      error("Node '" .. target_id .. "' references missing ancestor '" .. current_parent .. "'.")
    end
    table.insert(segments, 1, current_parent)
    current_id = current_parent
  end

  return "/" .. table.concat(segments, "/")
end

if parent_id == node_id then
  error("Node '" .. node_id .. "' cannot be its own parent.")
end

if parent_id ~= "" and redis.call("EXISTS", "node:" .. parent_id) == 0 then
  error("parent_id '" .. parent_id .. "' does not exist. Create the parent node before attaching children.")
end

if parent_id ~= "" then
  local current_id = parent_id
  local visited = {}
  visited[node_id] = true
  while current_id and current_id ~= "" do
    if visited[current_id] then
      error("parent_id '" .. parent_id .. "' would create a cycle for node '" .. node_id .. "'.")
    end
    visited[current_id] = true
    local next_parent = redis.call("HGET", "node:" .. current_id, "parent_id")
    if not next_parent or next_parent == "" then
      break
    end
    current_id = next_parent
  end
end

local existing_parent = redis.call("HGET", redis_key, "parent_id") or ""
local existing_status = redis.call("HGET", redis_key, "record_status") or "ready"
local existing_error = redis.call("HGET", redis_key, "last_error") or ""
local final_status = existing_status
local final_error = existing_error

if status_provided == "1" then
  final_status = status_value
end

if error_provided == "1" then
  final_error = error_value
end

local new_path
if parent_id == "" then
  new_path = "/" .. node_id
else
  new_path = resolve_path(parent_id) .. "/" .. node_id
end

redis.call(
  "HSET",
  redis_key,
  "name", name,
  "parent_id", parent_id,
  "payload", payload,
  "metadata", metadata,
  "relationships", relationships,
  "record_status", final_status,
  "last_error", final_error,
  "path", new_path
)

if existing_parent ~= "" and existing_parent ~= parent_id then
  redis.call("SREM", "node:" .. existing_parent .. ":children", node_id)
end

if parent_id ~= "" then
  redis.call("SADD", "node:" .. parent_id .. ":children", node_id)
end

local queue = {node_id}
local queue_paths = {[node_id] = new_path}
local head = 1
while head <= #queue do
  local current_id = queue[head]
  local current_path = queue_paths[current_id]
  head = head + 1
  local children = redis.call("SMEMBERS", "node:" .. current_id .. ":children")
  for _, child_id in ipairs(children) do
    local child_path = current_path .. "/" .. child_id
    redis.call("HSET", "node:" .. child_id, "path", child_path)
    queue[#queue + 1] = child_id
    queue_paths[child_id] = child_path
  end
end

return new_path
"""

    SET_RECORD_STATUS_SCRIPT = """
local node_id = ARGV[1]
local record_status = ARGV[2]
local last_error = ARGV[3]
local redis_key = "node:" .. node_id

if redis.call("EXISTS", redis_key) == 0 then
  return 0
end

redis.call("HSET", redis_key, "record_status", record_status, "last_error", last_error)
return 1
"""

    DELETE_SUBTREE_SCRIPT = """
local node_id = ARGV[1]
local root_key = "node:" .. node_id

if redis.call("EXISTS", root_key) == 0 then
  return 0
end

local queue = {node_id}
local nodes = {}
local visited = {}
local head = 1

while head <= #queue do
  local current_id = queue[head]
  head = head + 1
  if not visited[current_id] then
    visited[current_id] = true
    nodes[#nodes + 1] = current_id
    local children = redis.call("SMEMBERS", "node:" .. current_id .. ":children")
    for _, child_id in ipairs(children) do
      queue[#queue + 1] = child_id
    end
  end
end

for index = #nodes, 1, -1 do
  local target_id = nodes[index]
  local target_key = "node:" .. target_id
  local parent_id = redis.call("HGET", target_key, "parent_id")
  redis.call("DEL", target_key)
  redis.call("DEL", target_key .. ":children")
  if parent_id and parent_id ~= "" then
    redis.call("SREM", "node:" .. parent_id .. ":children", target_id)
  end
end

return 1
"""

    def __init__(self, redis_client):
        self.client = redis_client

    @staticmethod
    def _decode(value) -> str:
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def upsert_node(self, node: Node, record_status: Optional[str] = None, last_error: Optional[str] = None) -> Node:
        try:
            if self._supports_eval():
                self.client.eval(
                    self.UPSERT_NODE_SCRIPT,
                    0,
                    node.node_id,
                    node.name,
                    node.parent_id or "",
                    json.dumps(node.payload),
                    json.dumps(node.metadata),
                    json.dumps(node.relationships),
                    record_status or "",
                    "1" if record_status is not None else "0",
                    last_error or "",
                    "1" if last_error is not None else "0",
                )
            else:
                self._upsert_node_fallback(node, record_status=record_status, last_error=last_error)
        except Exception as exc:
            self._raise_domain_error(exc)
        return node

    def get_node(self, node_id: str) -> Optional[Node]:
        redis_key = f"node:{node_id}"
        try:
            raw_data = self.client.hgetall(redis_key)
        except Exception as exc:
            raise StorageError(f"Failed to read node '{node_id}'.") from exc
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
                metadata=json.loads(decoded_data.get("metadata", "{}")),
                relationships=json.loads(decoded_data.get("relationships", "{}")),
                child_ids=self.get_children(node_id),
            )
        except json.JSONDecodeError as exc:
            raise DataCorruptionError(f"Node '{node_id}' contains malformed JSON fields.") from exc
        except Exception as exc:
            raise StorageError(f"Failed to deserialize node '{node_id}'.") from exc

    def get_children(self, parent_id: str) -> List[str]:
        try:
            return sorted(self._decode(mem) for mem in self.client.smembers(f"node:{parent_id}:children"))
        except Exception as exc:
            raise StorageError(f"Failed to read children for node '{parent_id}'.") from exc

    def delete_node(self, node_id: str) -> bool:
        try:
            if self._supports_eval():
                return bool(self.client.eval(self.DELETE_SUBTREE_SCRIPT, 0, node_id))
            return self._delete_node_fallback(node_id)
        except Exception as exc:
            raise StorageError(f"Failed to delete node '{node_id}'.") from exc

    def get_absolute_path(self, node_id: str) -> str:
        try:
            if not self.client.exists(f"node:{node_id}"):
                raise ValueError(f"node_id '{node_id}' does not exist.")
            path = [node_id]
            current_id = node_id
            visited = {node_id}
            while True:
                parent_id = self.client.hget(f"node:{current_id}", "parent_id")
                if not parent_id:
                    break
                current_id = self._decode(parent_id)
                if current_id in visited:
                    raise DataCorruptionError(f"Cycle detected while resolving path for node '{node_id}'.")
                if not self.client.exists(f"node:{current_id}"):
                    raise DataCorruptionError(
                        f"Node '{node_id}' references missing ancestor '{current_id}'."
                    )
                visited.add(current_id)
                path.insert(0, current_id)
        except DataCorruptionError:
            raise
        except Exception as exc:
            raise StorageError(f"Failed to resolve path for node '{node_id}'.") from exc
        return "/" + "/".join(path)

    def set_record_status(self, node_id: str, record_status: str, last_error: Optional[str] = None) -> None:
        try:
            if self._supports_eval():
                updated = self.client.eval(
                    self.SET_RECORD_STATUS_SCRIPT,
                    0,
                    node_id,
                    record_status,
                    last_error or "",
                )
                if not updated:
                    raise ValueError(f"node_id '{node_id}' does not exist.")
            else:
                self._set_record_status_fallback(node_id, record_status, last_error=last_error)
        except Exception as exc:
            self._raise_domain_error(exc)

    def _ensure_acyclic_parentage(self, node_id: str, parent_id: str) -> None:
        current_id = parent_id
        visited = {node_id}
        try:
            while current_id:
                if current_id in visited:
                    raise ValueError(
                        f"parent_id '{parent_id}' would create a cycle for node '{node_id}'."
                    )
                visited.add(current_id)
                parent_bytes = self.client.hget(f"node:{current_id}", "parent_id")
                if not parent_bytes:
                    return
                current_id = self._decode(parent_bytes)
        except ValueError:
            raise
        except Exception as exc:
            raise StorageError(
                f"Failed to validate parentage for node '{node_id}'."
            ) from exc

    def _supports_eval(self) -> bool:
        return callable(getattr(self.client, "eval", None))

    def _upsert_node_fallback(self, node: Node, record_status: Optional[str], last_error: Optional[str]) -> None:
        redis_key = f"node:{node.node_id}"
        if node.parent_id == node.node_id:
            raise ValueError(f"Node '{node.node_id}' cannot be its own parent.")
        if node.parent_id:
            if not self.client.exists(f"node:{node.parent_id}"):
                raise ValueError(f"parent_id '{node.parent_id}' does not exist. Create the parent node before attaching children.")
            self._ensure_acyclic_parentage(node.node_id, node.parent_id)

        existing_parent_bytes = self.client.hget(redis_key, "parent_id")
        existing_parent = self._decode(existing_parent_bytes) if existing_parent_bytes else ""
        existing_status_bytes = self.client.hget(redis_key, "record_status")
        existing_status = self._decode(existing_status_bytes) if existing_status_bytes else "ready"
        existing_error_bytes = self.client.hget(redis_key, "last_error")
        existing_error = self._decode(existing_error_bytes) if existing_error_bytes else ""
        final_status = record_status if record_status is not None else existing_status
        final_error = last_error if last_error is not None else existing_error
        new_path = f"/{node.node_id}" if not node.parent_id else f"{self.get_absolute_path(node.parent_id)}/{node.node_id}"

        node_data = {
            "name": node.name,
            "parent_id": node.parent_id or "",
            "payload": json.dumps(node.payload),
            "metadata": json.dumps(node.metadata),
            "relationships": json.dumps(node.relationships),
            "record_status": final_status,
            "last_error": final_error,
            "path": new_path,
        }
        self.client.hset(redis_key, mapping=node_data)
        if existing_parent and existing_parent != node.parent_id:
            self.client.srem(f"node:{existing_parent}:children", node.node_id)
        if node.parent_id:
            self.client.sadd(f"node:{node.parent_id}:children", node.node_id)
        self._rewrite_subtree_paths(node.node_id, new_path)

    def _rewrite_subtree_paths(self, root_node_id: str, root_path: str) -> None:
        nodes_to_process = deque([(root_node_id, root_path)])
        visited = set()
        while nodes_to_process:
            current_id, current_path = nodes_to_process.popleft()
            if current_id in visited:
                continue
            visited.add(current_id)
            self.client.hset(f"node:{current_id}", "path", current_path)
            for child_id in self.get_children(current_id):
                nodes_to_process.append((child_id, f"{current_path}/{child_id}"))

    def _set_record_status_fallback(self, node_id: str, record_status: str, last_error: Optional[str]) -> None:
        redis_key = f"node:{node_id}"
        if not self.client.exists(redis_key):
            raise ValueError(f"node_id '{node_id}' does not exist.")
        self.client.hset(
            redis_key,
            mapping={
                "record_status": record_status,
                "last_error": last_error or "",
            },
        )

    def _delete_node_fallback(self, node_id: str) -> bool:
        if not self.client.exists(f"node:{node_id}"):
            return False
        nodes_to_process = deque([node_id])
        nodes_to_delete = []
        visited = set()
        while nodes_to_process:
            current_id = nodes_to_process.popleft()
            if current_id in visited:
                continue
            visited.add(current_id)
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

    def _raise_domain_error(self, exc: Exception) -> None:
        error_text = str(exc)
        if "cannot be its own parent" in error_text or "does not exist" in error_text or "would create a cycle" in error_text:
            raise ValueError(error_text) from exc
        if "Cycle detected while resolving path" in error_text or "references missing ancestor" in error_text:
            raise DataCorruptionError(error_text) from exc
        raise StorageError(error_text) from exc
