"""Streamlit viewer for inspecting Genie's hierarchical Redis data."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional, Set

import redis
import streamlit as st


DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_KEY_PREFIX = "node:"


def get_redis_client(redis_url: str) -> redis.Redis:
    """Create a Redis client for the Streamlit session.

    Parameters:
    redis_url:
        Redis connection URL entered by the user.

    Returns:
    redis.Redis
        Connected Redis client instance.
    """
    return redis.Redis.from_url(redis_url)


def decode_value(value: Any) -> str:
    """Decode Redis bytes into readable text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_json_field(raw_value: str) -> Any:
    """Parse a JSON field when possible, otherwise return the original text."""
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def load_tree_data(client: redis.Redis, key_prefix: str) -> Dict[str, Dict[str, Any]]:
    """Load Genie nodes and rebuild the hierarchical view from Redis.

    Parameters:
    client:
        Connected Redis client.
    key_prefix:
        Prefix used by Genie for node hashes.

    Returns:
    dict[str, dict[str, Any]]
        Mapping from node_id to normalized node data for the viewer.
    """
    nodes: Dict[str, Dict[str, Any]] = {}

    for redis_key in sorted(client.scan_iter(match=f"{key_prefix}*")):
        key_text = decode_value(redis_key)
        if key_text.endswith(":children"):
            continue
        if decode_value(client.type(redis_key)) != "hash":
            continue

        raw_hash = client.hgetall(redis_key)
        node_id = key_text[len(key_prefix):]
        node_data: Dict[str, Any] = {
            "node_id": node_id,
            "redis_key": key_text,
            "children": sorted(
                decode_value(child_id)
                for child_id in client.smembers(f"{key_text}:children")
            ),
        }

        for field_name_raw, field_value_raw in raw_hash.items():
            field_name = decode_value(field_name_raw)
            if field_name == "embedding":
                continue
            field_value = decode_value(field_value_raw)
            if field_name in {"payload", "metadata", "relationships"}:
                node_data[field_name] = parse_json_field(field_value)
            else:
                node_data[field_name] = field_value

        node_data["parent_id"] = node_data.get("parent_id") or None
        nodes[node_id] = node_data

    return nodes


def find_root_ids(nodes: Dict[str, Dict[str, Any]]) -> List[str]:
    """Identify root nodes from the loaded hierarchy."""
    root_ids: List[str] = []
    for node_id, node in nodes.items():
        parent_id = node.get("parent_id")
        if not parent_id or parent_id not in nodes:
            root_ids.append(node_id)
    return sorted(root_ids)


def get_node_label(node: Dict[str, Any]) -> str:
    """Build a concise label for a node in the tree."""
    status = node.get("record_status", "unknown")
    display_name = node.get("name") or node["node_id"]
    return f"{display_name} [{status}]"


def render_node_tree(
    nodes: Dict[str, Dict[str, Any]],
    node_id: str,
    *,
    depth: int = 0,
    search_text: str = "",
    active_path: Optional[Set[str]] = None,
) -> None:
    """Render the hierarchy recursively with lightweight selection controls."""
    active_path = set() if active_path is None else set(active_path)
    if node_id in active_path:
        st.warning(f"Cycle detected while rendering node '{node_id}'.")
        return

    active_path.add(node_id)
    node = nodes[node_id]
    visible_text = get_node_label(node)
    normalized_search = search_text.lower().strip()

    # Filter on either node title or node identifier without hiding descendants entirely.
    if (
        normalized_search
        and normalized_search not in visible_text.lower()
        and normalized_search not in node_id.lower()
    ):
        descendant_matches = any(
            has_matching_descendant(nodes, child_id, normalized_search)
            for child_id in node.get("children", [])
        )
        if not descendant_matches:
            return

    button_key = f"select-{node_id}"
    spacer_width = min(0.08 * depth, 0.45)
    content_width = 1.0 - spacer_width
    if depth > 0:
        _, node_col = st.columns([spacer_width, content_width])
        with node_col:
            if st.button(visible_text, key=button_key, use_container_width=True):
                st.session_state["selected_node_id"] = node_id
    else:
        if st.button(visible_text, key=button_key, use_container_width=True):
            st.session_state["selected_node_id"] = node_id

    for child_id in node.get("children", []):
        if child_id not in nodes:
            spacer_width = min(0.08 * (depth + 1), 0.45)
            content_width = 1.0 - spacer_width
            _, warning_col = st.columns([spacer_width, content_width])
            with warning_col:
                st.caption(f"Missing child node: {child_id}")
            continue
        render_node_tree(
            nodes,
            child_id,
            depth=depth + 1,
            search_text=search_text,
            active_path=active_path,
        )


def has_matching_descendant(
    nodes: Dict[str, Dict[str, Any]],
    node_id: str,
    normalized_search: str,
    active_path: Optional[Set[str]] = None,
) -> bool:
    """Return whether a node or any descendant matches the filter text."""
    active_path = set() if active_path is None else set(active_path)
    if node_id in active_path:
        return False

    active_path.add(node_id)
    node = nodes.get(node_id)
    if node is None:
        return False

    label = node.get("name") or node_id
    if normalized_search in label.lower() or normalized_search in node_id.lower():
        return True

    return any(
        has_matching_descendant(nodes, child_id, normalized_search, active_path)
        for child_id in node.get("children", [])
        if child_id in nodes
    )


def render_node_details(node: Optional[Dict[str, Any]]) -> None:
    """Render the detail panel for the currently selected node."""
    if node is None:
        st.info("Select a node from the tree to inspect its contents.")
        return

    st.subheader(node.get("name") or node["node_id"])
    st.caption(node["node_id"])

    summary_rows = {
        "Record Status": node.get("record_status", ""),
        "Parent ID": node.get("parent_id", ""),
        "Path": node.get("path", ""),
        "Redis Key": node.get("redis_key", ""),
        "Children": len(node.get("children", [])),
        "Last Error": node.get("last_error", ""),
    }
    st.json(summary_rows)

    st.markdown("**Payload**")
    st.json(node.get("payload", {}))

    st.markdown("**Metadata**")
    st.json(node.get("metadata", {}))

    st.markdown("**Relationships**")
    st.json(node.get("relationships", {}))

    st.markdown("**Children**")
    st.json(node.get("children", []))

    additional_fields = {
        key: value
        for key, value in node.items()
        if key
        not in {
            "node_id",
            "name",
            "parent_id",
            "payload",
            "metadata",
            "relationships",
            "children",
            "record_status",
            "path",
            "redis_key",
            "last_error",
        }
    }
    if additional_fields:
        st.markdown("**Additional Fields**")
        st.json(additional_fields)


def summarize_tree(nodes: Dict[str, Dict[str, Any]]) -> Dict[str, int]:
    """Compute lightweight integrity metrics for the loaded hierarchy."""
    root_count = 0
    leaf_count = 0
    missing_parent_count = 0
    missing_child_reference_count = 0

    for node in nodes.values():
        parent_id = node.get("parent_id")
        if not parent_id:
            root_count += 1
        elif parent_id not in nodes:
            root_count += 1
            missing_parent_count += 1

        children = node.get("children", [])
        if not children:
            leaf_count += 1
        missing_child_reference_count += sum(1 for child_id in children if child_id not in nodes)

    return {
        "root_count": root_count,
        "leaf_count": leaf_count,
        "missing_parent_count": missing_parent_count,
        "missing_child_reference_count": missing_child_reference_count,
    }


def main() -> None:
    """Run the Streamlit app."""
    st.set_page_config(page_title="Genie Tree Viewer", layout="wide")
    st.title("Genie Hierarchical Memory Viewer")
    st.caption("Inspect workspaces, saved memories, and indexing state directly from Redis.")

    with st.sidebar:
        st.header("Connection")
        redis_url = st.text_input("Redis URL", value=DEFAULT_REDIS_URL)
        key_prefix = st.text_input("Node key prefix", value=DEFAULT_KEY_PREFIX)
        refresh = st.button("Refresh tree", use_container_width=True)
        search_text = st.text_input("Filter nodes", value="")

    if refresh:
        st.session_state.pop("selected_node_id", None)

    try:
        client = get_redis_client(redis_url)
        client.ping()
        nodes = load_tree_data(client, key_prefix)
    except redis.RedisError as exc:
        st.error(f"Unable to connect to Redis: {exc}")
        return

    if not nodes:
        st.warning("No Genie node hashes were found for the configured prefix.")
        return

    root_ids = find_root_ids(nodes)
    tree_summary = summarize_tree(nodes)
    fallback_to_all_nodes = not root_ids
    if fallback_to_all_nodes:
        root_ids = sorted(nodes)
    selected_node_id = st.session_state.get("selected_node_id")
    if not selected_node_id and root_ids:
        selected_node_id = root_ids[0]
        st.session_state["selected_node_id"] = selected_node_id
    selected_node = nodes.get(selected_node_id) if selected_node_id else None

    metric_col_1, metric_col_2, metric_col_3, metric_col_4 = st.columns(4)
    metric_col_1.metric("Nodes", len(nodes))
    metric_col_2.metric("Roots", tree_summary["root_count"])
    metric_col_3.metric("Leaves", tree_summary["leaf_count"])
    metric_col_4.metric(
        "Missing Links",
        tree_summary["missing_parent_count"] + tree_summary["missing_child_reference_count"],
    )

    if tree_summary["missing_parent_count"] or tree_summary["missing_child_reference_count"]:
        st.warning(
            "The hierarchy contains incomplete references. "
            f"Missing parents: {tree_summary['missing_parent_count']}, "
            f"missing child links: {tree_summary['missing_child_reference_count']}."
        )
    elif fallback_to_all_nodes:
        st.info("No structural roots were detected, so the viewer is rendering all nodes as entry points.")

    left_col, right_col = st.columns([1.15, 1], gap="large")

    with left_col:
        st.subheader("Hierarchy")
        st.caption(
            f"{len(nodes)} node(s), {len(root_ids)} root node(s), "
            f"filter: {search_text or 'none'}"
        )
        for root_id in root_ids:
            render_node_tree(nodes, root_id, search_text=search_text)

    with right_col:
        render_node_details(selected_node)


if __name__ == "__main__":
    main()
