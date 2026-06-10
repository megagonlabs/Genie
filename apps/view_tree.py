import streamlit as st
import redis
import json
from typing import Dict, List, Any, Optional, cast
from redis.exceptions import ResponseError

# Set page configuration
st.set_page_config(page_title="Genie Memory Visualizer", layout="wide", page_icon="🧞")

st.title("🧞 Genie Hierarchical Memory Tree")
st.caption("Visual map of your Agent's structured long-term memory.")


# 1. Connect to your Redis backend
@st.cache_resource
def get_redis_client() -> redis.Redis:  # <-- Explicit return type hint
    return redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)


try:
    r = get_redis_client()
    r.ping()
    st.success("Connected to Redis Stack successfully!", icon="✅")
except Exception as e:
    st.error(f"Could not connect to Redis: {e}")
    st.stop()


# 2. Fetch and reconstruct the graph hierarchy
def decode_redis_value(value: Any) -> str:
    """Normalizes Redis bytes/strings into display-safe text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def node_id_from_key(key: str) -> Optional[str]:
    """Extracts the Genie node id from a Redis key like node:<node_id>."""
    if not key.startswith("node:") or key.endswith(":children"):
        return None
    return key.removeprefix("node:")


def parse_json_field(value: Any, fallback: Any) -> Any:
    if not isinstance(value, str) or not value:
        return fallback

    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return fallback


def load_memory_graph() -> Dict[str, Any]:
    """Scans Redis for genie nodes and builds an adjacency list/tree."""
    redis_client = cast(Any, r)
    all_keys = [decode_redis_value(key) for key in redis_client.scan_iter("node:*")]

    nodes: Dict[str, Dict[str, Any]] = {}
    child_edges: Dict[str, List[str]] = {}
    root_nodes: List[str] = []

    for key in all_keys:
        if key.endswith(":children"):
            parent_ref = key.removeprefix("node:").removesuffix(":children")
            child_edges[parent_ref] = [
                decode_redis_value(child_id)
                for child_id in redis_client.smembers(key)
            ]
            continue

        node_id = node_id_from_key(key)
        if not node_id:
            continue

        try:
            key_type = decode_redis_value(redis_client.type(key))
            raw_node = None

            if key_type == "string":
                data = redis_client.get(key)
                if data:
                    raw_node = json.loads(cast(str, data))

            elif key_type in {"json", "ReJSON-RL"}:
                raw_node = redis_client.json().get(key)

            elif key_type == "hash":
                hash_data: Dict[str, Any] = {}

                for field in redis_client.hkeys(key):
                    field_str = decode_redis_value(field)
                    if field_str == "embedding":
                        continue
                    value = redis_client.hget(key, field_str)
                    if value is None:
                        continue
                    hash_data[field_str] = decode_redis_value(value)

                hash_data["payload"] = parse_json_field(hash_data.get("payload"), {})
                hash_data["relationships"] = parse_json_field(hash_data.get("relationships"), {})
                raw_node = hash_data

            if raw_node:
                node_dict = cast(Dict[str, Any], raw_node)
                resolved_node_id = str(node_dict.get("node_id") or node_id)
                payload = node_dict.get("payload", {})

                if not isinstance(payload, dict):
                    payload = {"text": str(payload)}
                elif "text" not in payload and node_dict.get("search_text"):
                    payload = {**payload, "text": node_dict["search_text"]}

                nodes[resolved_node_id] = {
                    "node_id": resolved_node_id,
                    "name": node_dict.get("name") or resolved_node_id,
                    "parent_id": node_dict.get("parent_id") or None,
                    "payload": payload,
                    "children": [],
                }

        except ResponseError:
            continue
        except (json.JSONDecodeError, TypeError):
            continue

    name_to_node_id = {
        node["name"]: node_id
        for node_id, node in nodes.items()
        if node.get("name")
    }

    def resolve_node_ref(node_ref: Any) -> Optional[str]:
        if not node_ref:
            return None

        node_ref_str = str(node_ref)
        if node_ref_str in nodes:
            return node_ref_str

        return name_to_node_id.get(node_ref_str)

    attached_nodes = set()

    for parent_ref, child_ids in child_edges.items():
        parent_id = resolve_node_ref(parent_ref)
        if not parent_id:
            continue

        for child_ref in child_ids:
            child_id = resolve_node_ref(child_ref)
            if not child_id or child_id == parent_id:
                continue
            if child_id not in attached_nodes:
                nodes[parent_id]["children"].append(nodes[child_id])
                attached_nodes.add(child_id)

    for node_id, node in nodes.items():
        if node_id in attached_nodes:
            continue

        parent_id = resolve_node_ref(node["parent_id"])
        if parent_id and parent_id != node_id:
            nodes[parent_id]["children"].append(node)
            attached_nodes.add(node_id)
        else:
            root_nodes.append(node_id)

    return {node_id: nodes[node_id] for node_id in root_nodes}


# 3. Recursive UI Renderer
def render_tree_node(node: Dict[str, Any]):
    """Recursively renders nodes as interactive Streamlit components."""
    node_id = node["node_id"]
    name = node["name"]
    children = node["children"]
    payload = node["payload"]

    # Differentiate Workspaces vs Data Nodes visually
    is_workspace = node_id.startswith("workspace_") or len(children) > 0
    icon = "📁" if is_workspace else "📄"
    label = f"{icon} {name} ({node_id})"

    if is_workspace:
        # Workspaces get expandable folders
        with st.expander(label, expanded=True):
            if payload and "text" in payload and payload["text"]:
                st.info(payload["text"])

            if children:
                for child in children:
                    render_tree_node(child)
            else:
                st.text("  (Empty workspace)")
    else:
        # Leaf memory nodes get clean text displays
        with st.chat_message("assistant"):
            st.markdown(f"**{name}**")
            st.write(payload.get("text", "No text content."))


# 4. Main UI Execution
roots = load_memory_graph()

if not roots:
    st.info("No memory nodes found in the database yet. Run your agent loop to populate memories!")
else:
    col1, col2 = st.columns([3, 1])

    with col1:
        st.subheader("Memory Hierarchy Map")
        for root_id, root_node in roots.items():
            render_tree_node(root_node)

    with col2:
        st.subheader("Database Metrics")

        redis_client = cast(Any, r)
        all_keys = list(redis_client.scan_iter("*"))
        st.metric(label="Total Tracked Nodes", value=len(all_keys))

        if st.button("Refresh Tree View"):
            st.rerun()
