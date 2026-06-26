"""Simple Streamlit viewer for Genie hierarchy data stored in Redis."""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set, cast

import redis
import streamlit as st


DEFAULT_REDIS_URL = "redis://localhost:6379/0"
DEFAULT_KEY_PREFIX = "node:"


def iter_sync_keys(client: redis.Redis, pattern: str) -> Iterable[Any]:
    """Return a synchronously iterable Redis key stream."""
    return cast(Iterable[Any], client.scan_iter(match=pattern))


def read_sync_hash(client: redis.Redis, redis_key: str) -> Mapping[Any, Any]:
    """Return a synchronously readable Redis hash."""
    return cast(Mapping[Any, Any], client.hgetall(redis_key))


def read_sync_set_members(client: redis.Redis, redis_key: str) -> Iterable[Any]:
    """Return synchronously iterable Redis set members."""
    return cast(Iterable[Any], client.smembers(redis_key))


def get_redis_client(redis_url: str) -> redis.Redis:
    """Create the Redis client used by the viewer.

    Parameters:
    redis_url:
        Redis connection string entered by the user.

    Returns:
    redis.Redis
        Connected Redis client instance.
    """
    return redis.Redis.from_url(redis_url)


def decode_value(value: Any) -> str:
    """Decode Redis values into displayable text."""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def parse_json_field(raw_value: str) -> Any:
    """Parse JSON when possible, otherwise return the raw string."""
    if not raw_value:
        return {}
    try:
        return json.loads(raw_value)
    except json.JSONDecodeError:
        return raw_value


def infer_node_kind(node_id: str) -> str:
    """Infer a coarse Genie node kind from the identifier prefix."""
    if node_id.startswith("workspace_"):
        return "workspace"
    if node_id.startswith("node_"):
        return "memory"
    return "node"


def extract_metadata_value(node: Mapping[str, Any], key: str) -> str:
    """Read one metadata value when the field is a dictionary."""
    metadata = node.get("metadata", {})
    if isinstance(metadata, dict):
        value = metadata.get(key, "")
        return value if isinstance(value, str) else str(value)
    return ""


def parse_node_hash(redis_key: str, raw_hash: Mapping[Any, Any]) -> Dict[str, Any]:
    """Normalize one Redis node hash into a viewer-friendly dictionary."""
    node_id = redis_key.split(":", 1)[1]
    node: Dict[str, Any] = {
        "node_id": node_id,
        "redis_key": redis_key,
        "node_kind": infer_node_kind(node_id),
        "payload": {},
        "metadata": {},
        "relationships": {},
        "children": [],
        "child_ids": [],
        "is_leaf": True,
        "has_embedding": False,
        "search_text": "",
    }

    for field_name_raw, field_value_raw in raw_hash.items():
        field_name = decode_value(field_name_raw)
        if field_name == "embedding":
            node["has_embedding"] = True
            continue

        field_value = decode_value(field_value_raw)
        if field_name in {"payload", "metadata", "relationships"}:
            node[field_name] = parse_json_field(field_value)
        else:
            node[field_name] = field_value

    node["parent_id"] = node.get("parent_id") or None
    node["name"] = node.get("name") or node_id
    node["stage"] = extract_metadata_value(node, "stage")
    node["run_id"] = extract_metadata_value(node, "run_id")
    return node


def load_nodes(client: redis.Redis, key_prefix: str) -> Dict[str, Dict[str, Any]]:
    """Load Genie nodes and their child relationships from Redis."""
    nodes: Dict[str, Dict[str, Any]] = {}
    child_sets: Dict[str, List[str]] = {}

    for redis_key_raw in sorted(iter_sync_keys(client, f"{key_prefix}*")):
        redis_key = decode_value(redis_key_raw)
        key_type = decode_value(client.type(redis_key))

        if redis_key.endswith(":children"):
            members = sorted(decode_value(member) for member in read_sync_set_members(client, redis_key))
            parent_key = redis_key[: -len(":children")]
            parent_node_id = parent_key[len(key_prefix) :] if parent_key.startswith(key_prefix) else parent_key
            child_sets[parent_node_id] = members
            continue

        if key_type != "hash":
            continue

        nodes[redis_key[len(key_prefix) :]] = parse_node_hash(
            redis_key,
            read_sync_hash(client, redis_key),
        )

    for node_id, node in nodes.items():
        child_ids = child_sets.get(node_id, [])
        node["children"] = child_ids
        node["child_ids"] = child_ids
        node["is_leaf"] = not bool(child_ids)

    for parent_node_id, child_ids in child_sets.items():
        if parent_node_id in nodes:
            continue
        nodes[parent_node_id] = {
            "node_id": parent_node_id,
            "name": parent_node_id,
            "node_kind": infer_node_kind(parent_node_id),
            "parent_id": None,
            "path": "",
            "record_status": "unknown",
            "stage": "",
            "run_id": "",
            "payload": {},
            "metadata": {},
            "relationships": {},
            "children": child_ids,
            "child_ids": child_ids,
            "is_leaf": not bool(child_ids),
            "has_embedding": False,
            "search_text": "",
            "redis_key": f"{key_prefix}{parent_node_id}",
        }

    return nodes


def find_root_ids(nodes: Mapping[str, Dict[str, Any]]) -> List[str]:
    """Identify root nodes in the loaded hierarchy."""
    roots: List[str] = []
    for node_id, node in nodes.items():
        parent_id = node.get("parent_id")
        if not parent_id or parent_id not in nodes:
            roots.append(node_id)
    return sorted(roots)


def group_roots_by_run(nodes: Mapping[str, Dict[str, Any]], root_ids: Iterable[str]) -> Dict[str, List[str]]:
    """Group tree roots by run id to make navigation easier."""
    grouped: Dict[str, List[str]] = defaultdict(list)
    for root_id in root_ids:
        run_id = nodes[root_id].get("run_id") or "unscoped"
        grouped[str(run_id)].append(root_id)
    return {run_id: sorted(group) for run_id, group in sorted(grouped.items())}


def build_search_blob(node: Mapping[str, Any]) -> str:
    """Build a lowercased blob used for simple node filtering."""
    parts = [
        str(node.get("node_id", "")),
        str(node.get("redis_key", "")),
        str(node.get("name", "")),
        str(node.get("path", "")),
        str(node.get("stage", "")),
        str(node.get("record_status", "")),
        str(node.get("run_id", "")),
    ]
    return " ".join(part for part in parts if part).lower()


def descendant_matches(
    nodes: Mapping[str, Dict[str, Any]],
    node_id: str,
    *,
    search_text: str,
    active_path: Optional[Set[str]] = None,
) -> bool:
    """Return whether a node or descendant matches the current search text."""
    active_path = set() if active_path is None else set(active_path)
    if node_id in active_path or node_id not in nodes:
        return False

    active_path.add(node_id)
    node = nodes[node_id]
    if not search_text or search_text in build_search_blob(node):
        return True

    return any(
        descendant_matches(nodes, child_id, search_text=search_text, active_path=active_path)
        for child_id in node.get("children", [])
    )


def node_label(node: Mapping[str, Any]) -> str:
    """Build a compact, readable tree label."""
    label = str(node.get("name") or node.get("node_id"))
    stage = str(node.get("stage") or "")
    if stage:
        return f"{label} · {stage}"
    return label


def collect_visible_node_ids(
    nodes: Mapping[str, Dict[str, Any]],
    node_id: str,
    *,
    search_text: str,
    active_path: Optional[Set[str]] = None,
) -> List[str]:
    """Collect the visible node ids for the current filtered subtree."""
    active_path = set() if active_path is None else set(active_path)
    if node_id in active_path or node_id not in nodes:
        return []

    active_path.add(node_id)
    if not descendant_matches(nodes, node_id, search_text=search_text):
        return []

    visible_node_ids = [node_id]
    for child_id in nodes[node_id].get("children", []):
        visible_node_ids.extend(
            collect_visible_node_ids(
                nodes,
                child_id,
                search_text=search_text,
                active_path=active_path,
            )
        )
    return visible_node_ids


def set_selected_node(node_id: str) -> None:
    """Persist the currently selected node in Streamlit session state.

    Parameters:
    node_id:
        Identifier of the node selected from the hierarchy.
    """
    st.session_state["selected_node_id"] = node_id


def render_hierarchy_list(
    nodes: Mapping[str, Dict[str, Any]],
    root_ids: Iterable[str],
    *,
    search_text: str,
    selected_node_id: Optional[str],
) -> None:
    """Render the hierarchy as a simple nested list of selectable nodes.

    Parameters:
    nodes:
        Loaded Genie nodes keyed by node id.
    root_ids:
        Root nodes to render for the current filter scope.
    search_text:
        Lowercased search text used to filter the visible hierarchy.
    selected_node_id:
        Node currently selected in the detail panel.
    """

    def render_subtree(node_id: str, depth: int, active_path: Optional[Set[str]] = None) -> None:
        active_path = set() if active_path is None else set(active_path)
        if node_id in active_path or node_id not in nodes:
            return
        if not descendant_matches(nodes, node_id, search_text=search_text):
            return

        active_path.add(node_id)
        node = nodes[node_id]
        is_selected = node_id == selected_node_id
        child_count = len(node.get("children", []))
        button_label = f"{node_label(node)} ({child_count})"
        button_type = "primary" if is_selected else "secondary"
        spacer_width = min(0.08 * depth, 0.42)
        marker_col, button_col = st.columns([0.06, 0.94], gap="small")

        with marker_col:
            if is_selected:
                st.markdown(
                    (
                        '<div style="height: 2.35rem; display:flex; align-items:center; '
                        'justify-content:center; color:#1f8f4e; font-size:1.1rem; font-weight:700;">'
                        "✓"
                        "</div>"
                    ),
                    unsafe_allow_html=True,
                )
            else:
                st.markdown(
                    '<div style="height: 2.35rem;"></div>',
                    unsafe_allow_html=True,
                )

        with button_col:
            if depth > 0:
                _, content_col = st.columns([spacer_width, 1.0 - spacer_width])
            else:
                content_col = button_col

            with content_col:
                # A unique key per rendered node keeps Streamlit selection behavior stable.
                if st.button(
                    button_label,
                    key=f"hierarchy_{node_id}",
                    use_container_width=True,
                    type=button_type,
                    on_click=set_selected_node,
                    args=(node_id,),
                ):
                    pass

        for child_id in node.get("children", []):
            render_subtree(child_id, depth + 1, active_path)

    for root_id in root_ids:
        render_subtree(root_id, 0)


def build_node_options(
    nodes: Mapping[str, Dict[str, Any]],
    root_ids: Iterable[str],
    *,
    search_text: str,
) -> List[str]:
    """Build the ordered list of selectable visible node ids."""
    ordered_node_ids: List[str] = []
    seen: Set[str] = set()
    for root_id in root_ids:
        for node_id in collect_visible_node_ids(
            nodes,
            root_id,
            search_text=search_text,
        ):
            if node_id not in seen:
                seen.add(node_id)
                ordered_node_ids.append(node_id)
    return ordered_node_ids


def render_node_details(node: Optional[Dict[str, Any]]) -> None:
    """Render a lightweight detail panel for the selected node."""
    if node is None:
        st.info("Select a node to inspect it.")
        return

    st.subheader(node.get("name", node["node_id"]))
    st.caption(node["node_id"])

    summary = {
        "kind": node.get("node_kind", ""),
        "status": node.get("record_status", ""),
        "stage": node.get("stage", ""),
        "run_id": node.get("run_id", ""),
        "parent_id": node.get("parent_id", ""),
        "path": node.get("path", ""),
        "children": len(node.get("children", [])),
    }
    st.json(summary)

    payload = node.get("payload", {})
    if isinstance(payload, dict) and payload.get("text"):
        st.markdown("**Text**")
        st.text_area(
            "payload_text",
            value=str(payload.get("text", "")),
            height=260,
            disabled=True,
            label_visibility="collapsed",
        )
    else:
        st.markdown("**Payload**")
        st.json(payload)

    metadata = node.get("metadata", {})
    if metadata:
        st.markdown("**Metadata**")
        st.json(metadata)

    relationships = node.get("relationships", {})
    if relationships:
        st.markdown("**Relationships**")
        st.json(relationships)


def main() -> None:
    """Run the simplified Streamlit hierarchy viewer."""
    st.set_page_config(page_title="Genie Tree", layout="wide")
    st.title("Genie Tree")
    st.caption("A simple view of the hierarchy stored in Redis.")

    with st.sidebar:
        redis_url = st.text_input("Redis URL", value=DEFAULT_REDIS_URL)
        key_prefix = st.text_input("Key Prefix", value=DEFAULT_KEY_PREFIX)
        search_text = st.text_input("Search", value="").strip().lower()
        refresh = st.button("Refresh", use_container_width=True)

    if refresh:
        st.session_state.pop("selected_node_id", None)

    try:
        client = get_redis_client(redis_url)
        client.ping()
        nodes = load_nodes(client, key_prefix)
    except redis.RedisError as exc:
        st.error(f"Unable to connect to Redis: {exc}")
        return

    if not nodes:
        st.warning("No nodes were found for the configured prefix.")
        return

    root_ids = find_root_ids(nodes)
    run_groups = group_roots_by_run(nodes, root_ids)
    run_options = ["all", *run_groups.keys()]

    with st.sidebar:
        selected_run = st.selectbox("Run", run_options)

    visible_roots = root_ids if selected_run == "all" else run_groups.get(selected_run, [])
    visible_node_ids = build_node_options(nodes, visible_roots, search_text=search_text)
    if visible_node_ids and st.session_state.get("selected_node_id") not in visible_node_ids:
        st.session_state["selected_node_id"] = visible_node_ids[0]

    left_col, right_col = st.columns([1.15, 0.85], gap="large")

    with left_col:
        st.subheader("Hierarchy")
        st.caption(f"{len(visible_roots)} root node(s), {len(visible_node_ids)} visible node(s)")
        if visible_node_ids:
            render_hierarchy_list(
                nodes,
                visible_roots,
                search_text=search_text,
                selected_node_id=st.session_state.get("selected_node_id"),
            )
        else:
            st.info("No nodes match the current search and run filters.")

    with right_col:
        st.subheader("Details")
        render_node_details(nodes.get(st.session_state.get("selected_node_id")))


if __name__ == "__main__":
    main()
