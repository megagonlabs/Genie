"""LangMem-inspired namespace/key/value storage over Genie primitives.

The store deliberately has no fixed memory value schema. Applications supply
schemas per namespace, while Genie provides owned blocks, scoped access, and
typed links.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import json
import re
from typing import Any, Literal, Protocol, cast

from genie import MemoryBlock, MemoryHandle, MemoryLink
from genie.backend import KeyedStore, TemporalStore
from genie.structures import (
    KeyedCollection,
    MemoryAddress,
    PropertyGraph,
    TemporalCollection,
)

ITEM_TYPE = "langmem.item"
ITEM_RELATION = "langmem.item_relation"

Namespace = tuple[str, ...]
GraphDirection = Literal["out", "in"]

_SEARCH_LIMIT = 1_000
_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")


class ValueSchema:
    """Small runtime schema supplied for values in one namespace.

    This is intentionally not a universal memory schema. Each namespace can
    use a different instance, and namespaces without a registered schema can
    store any JSON-compatible value supported by Genie's configured codec.
    """

    __slots__ = ("name", "_fields")

    def __init__(self, name: str, fields: Mapping[str, type]) -> None:
        """Create an exact dictionary schema.

        Args:
            name: Human-readable schema name retained in block metadata.
            fields: Required field names and their expected Python types.
        """

        self.name = _required_text(name, field="schema name")
        if not fields:
            raise ValueError("A value schema requires at least one field.")
        normalized: dict[str, type] = {}
        for field_name, expected_type in fields.items():
            key = _required_text(field_name, field="schema field")
            if not isinstance(expected_type, type):
                raise TypeError(f"Schema field {key!r} must use a Python type.")
            normalized[key] = expected_type
        self._fields = normalized

    @property
    def fields(self) -> dict[str, type]:
        """Return an isolated copy of the schema's field mapping."""

        return dict(self._fields)

    def validate(self, value: object) -> dict[str, Any]:
        """Validate and copy one exact dictionary value."""

        if not isinstance(value, dict):
            raise TypeError(f"Schema {self.name!r} requires a dictionary value.")
        missing = self._fields.keys() - value.keys()
        extra = value.keys() - self._fields.keys()
        if missing:
            raise ValueError(f"Schema {self.name!r} is missing fields: {', '.join(sorted(missing))}.")
        if extra:
            raise ValueError(f"Schema {self.name!r} does not define fields: {', '.join(sorted(extra))}.")
        for field_name, expected_type in self._fields.items():
            field_value = value[field_name]
            if not isinstance(field_value, expected_type):
                raise TypeError(
                    f"Schema {self.name!r} field {field_name!r} must be "
                    f"{expected_type.__name__}."
                )
        return deepcopy(value)


@dataclass(frozen=True, slots=True)
class LangMemItem:
    """Resolved view of one `(namespace, key, value)` item."""

    block: MemoryBlock
    namespace: Namespace
    key: str
    value: Any
    schema_name: str | None
    entities: tuple[str, ...]
    occurred_at: datetime | None

    @property
    def created_at(self) -> datetime:
        """Return the Genie block creation timestamp."""

        return self.block.created_at

    @property
    def updated_at(self) -> datetime:
        """Return the Genie block update timestamp."""

        return self.block.updated_at

    def canonical_value(self) -> dict[str, Any]:
        """Return the exact namespace/key/value wrapper stored in Genie."""

        return deepcopy(cast(dict[str, Any], self.block.value))


@dataclass(frozen=True, slots=True)
class SearchResult:
    """One item ranked by the lamp's portable lexical retrieval."""

    item: LangMemItem
    score: float
    reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphResult:
    """One item reached through an application-defined graph relation."""

    item: LangMemItem
    relation: str
    direction: GraphDirection
    distance: int


class LangMemStructureStore(KeyedStore, TemporalStore, Protocol):
    """Backend capabilities required by the LangMem structure bundle."""


@dataclass(frozen=True, slots=True)
class LangMemStructures:
    """Composable structural views used by the LangMem application service."""

    items: KeyedCollection
    timeline: TemporalCollection
    graph: PropertyGraph

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: LangMemStructureStore,
    ) -> LangMemStructures:
        """Build the facade bundle around one bounded store root."""

        if memory.root_id is None:
            raise ValueError("LangMemStructures requires a bounded MemoryHandle.")
        return cls(
            items=KeyedCollection(memory, store, collection=memory.root_id),
            timeline=TemporalCollection(memory, store, collection=memory.root_id),
            graph=PropertyGraph(memory),
        )


class LangMemStore:
    """Schema-flexible namespaced item store backed by a bounded Genie handle."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: LangMemStructures,
        schemas: Mapping[Namespace, ValueSchema] | None = None,
    ) -> None:
        """Create a store with optional per-namespace value schemas.

        Args:
            memory: Bounded Genie handle owning all items.
            structures: Keyed, temporal, and graph views over the same handle.
            schemas: Exact namespaces mapped to application-supplied schemas.
        """

        self.memory = memory
        self.structures = structures
        self._schemas = {
            _normalize_namespace(namespace): schema
            for namespace, schema in (schemas or {}).items()
        }

    def create(
        self,
        namespace: Sequence[str],
        key: str,
        value: Any,
        *,
        occurred_at: datetime | None = None,
        entities: Sequence[str] = (),
    ) -> LangMemItem:
        """Create one schema-validated namespaced item.

        Raises:
            ValueError: If the namespace/key already exists or input is invalid.
            TypeError: If the value does not conform to its supplied schema.
        """

        normalized_namespace = _normalize_namespace(namespace)
        normalized_key = _required_text(key, field="key")
        address = MemoryAddress(normalized_namespace, normalized_key)
        if self.structures.items.get(address) is not None:
            raise ValueError(
                f"Item {normalized_key!r} already exists in namespace {normalized_namespace!r}."
            )
        normalized_value, schema_name = self._validate_value(normalized_namespace, value)
        normalized_occurred_at = (
            _normalize_timestamp(occurred_at, field="occurred_at")
            if occurred_at is not None
            else None
        )
        normalized_entities = _normalized_strings(entities, field="entities")
        payload = {
            "namespace": list(normalized_namespace),
            "key": normalized_key,
            "value": normalized_value,
        }
        block = self.memory.put(
            type=ITEM_TYPE,
            value=payload,
            metadata=_item_index(
                normalized_namespace,
                normalized_key,
                schema_name=schema_name,
                occurred_at=normalized_occurred_at,
                entities=normalized_entities,
            ),
        )
        try:
            self.structures.items.bind(address, block)
            if normalized_occurred_at is not None:
                self.structures.timeline.add(block, occurred_at=normalized_occurred_at)
        except Exception:
            self.memory.delete(block)
            raise
        return _item_from_block(block)

    def update(
        self,
        namespace: Sequence[str],
        key: str,
        value: Any,
        *,
        occurred_at: datetime | None = None,
        entities: Sequence[str] | None = None,
    ) -> LangMemItem:
        """Replace an item's value while preserving its namespace, key, and id.

        `occurred_at=None` and `entities=None` preserve existing annotations.
        """

        normalized_namespace = _normalize_namespace(namespace)
        normalized_key = _required_text(key, field="key")
        existing = self.get(normalized_namespace, normalized_key)
        if existing is None:
            raise KeyError(
                f"Item {normalized_key!r} was not found in namespace {normalized_namespace!r}."
            )
        normalized_value, schema_name = self._validate_value(normalized_namespace, value)
        normalized_occurred_at = (
            existing.occurred_at
            if occurred_at is None
            else _normalize_timestamp(occurred_at, field="occurred_at")
        )
        normalized_entities = (
            existing.entities
            if entities is None
            else _normalized_strings(entities, field="entities")
        )
        block = self.memory.update(
            existing.block,
            value={"value": normalized_value},
            metadata=_item_index(
                normalized_namespace,
                normalized_key,
                schema_name=schema_name,
                occurred_at=normalized_occurred_at,
                entities=normalized_entities,
            ),
        )
        if occurred_at is not None:
            temporal = self.structures.timeline.get(block)
            if temporal is None:
                self.structures.timeline.add(block, occurred_at=normalized_occurred_at)
            else:
                self.structures.timeline.replace(
                    block,
                    occurred_at=normalized_occurred_at,
                )
        return _item_from_block(block)

    def get(self, namespace: Sequence[str], key: str) -> LangMemItem | None:
        """Retrieve an item directly by its exact namespace and key."""

        normalized_namespace = _normalize_namespace(namespace)
        normalized_key = _required_text(key, field="key")
        block = self.structures.items.get(
            MemoryAddress(normalized_namespace, normalized_key)
        )
        return _item_from_block(block) if block is not None else None

    def search(
        self,
        namespace_prefix: Sequence[str],
        query: str,
        *,
        collections: Sequence[str] = (),
        limit: int = 5,
    ) -> list[SearchResult]:
        """Search items below a namespace prefix with transparent lexical scoring."""

        prefix = _normalize_namespace(namespace_prefix)
        normalized_query = _required_text(query, field="query")
        normalized_collections = frozenset(
            _required_text(value, field="collection") for value in collections
        )
        normalized_limit = _validate_limit(limit)
        query_tokens = _tokens(normalized_query)
        results: list[SearchResult] = []
        for item in self._items_below(prefix):
            if normalized_collections and item.namespace[-1] not in normalized_collections:
                continue
            value_tokens = _tokens(_searchable_text(item.value))
            overlap = query_tokens.intersection(value_tokens)
            if not overlap:
                continue
            score = len(overlap) / len(query_tokens)
            results.append(
                SearchResult(
                    item=item,
                    score=round(score, 4),
                    reasons=(f"value: {', '.join(sorted(overlap))}",),
                )
            )
        results.sort(
            key=lambda result: (
                -result.score,
                result.item.namespace,
                result.item.key,
            )
        )
        return results[:normalized_limit]

    def search_temporal(
        self,
        namespace_prefix: Sequence[str],
        start: datetime,
        end: datetime,
        *,
        collections: Sequence[str] = (),
        limit: int = 20,
    ) -> list[LangMemItem]:
        """Return items whose optional event annotation falls in an interval."""

        prefix = _normalize_namespace(namespace_prefix)
        normalized_start = _normalize_timestamp(start, field="start")
        normalized_end = _normalize_timestamp(end, field="end")
        if normalized_start > normalized_end:
            raise ValueError("start must be earlier than or equal to end.")
        normalized_collections = frozenset(
            _required_text(value, field="collection") for value in collections
        )
        normalized_limit = _validate_limit(limit)
        try:
            exclusive_end = normalized_end + timedelta(microseconds=1)
        except OverflowError:
            exclusive_end = None
        items = []
        for temporal in self.structures.timeline.events(
            start=normalized_start,
            stop=exclusive_end,
            limit=_SEARCH_LIMIT,
        ):
            item = _item_from_block(temporal.block)
            occurred_at = temporal.entry.occurred_at
            if occurred_at is None or occurred_at > normalized_end:
                continue
            if not _is_below(item.namespace, prefix):
                continue
            if normalized_collections and item.namespace[-1] not in normalized_collections:
                continue
            items.append(item)
        items.sort(key=lambda item: (item.occurred_at, item.namespace, item.key))
        return items[:normalized_limit]

    def link(
        self,
        source_namespace: Sequence[str],
        source_key: str,
        target_namespace: Sequence[str],
        target_key: str,
        *,
        relation: str,
    ) -> MemoryLink:
        """Connect two namespaced items with an application-defined relation."""

        source = self._require_item(source_namespace, source_key)
        target = self._require_item(target_namespace, target_key)
        normalized_relation = _required_text(relation, field="relation")
        for existing in self.structures.graph.edges(
            source.block,
            relation=ITEM_RELATION,
            direction="out",
        ):
            if (
                existing.target_id == target.block.id
                and existing.metadata.get("relation") == normalized_relation
            ):
                return existing
        return self.structures.graph.add_edge(
            source.block,
            target.block,
            ITEM_RELATION,
            metadata={"relation": normalized_relation},
        )

    def search_graph(
        self,
        namespace_prefix: Sequence[str],
        anchor_namespace: Sequence[str],
        anchor_key: str,
        *,
        max_depth: int = 1,
        limit: int = 20,
    ) -> list[GraphResult]:
        """Traverse application-defined item relations inside a namespace prefix."""

        prefix = _normalize_namespace(namespace_prefix)
        anchor = self._require_item(anchor_namespace, anchor_key)
        if not _is_below(anchor.namespace, prefix):
            raise ValueError("The graph anchor is outside the requested namespace prefix.")
        if not isinstance(max_depth, int) or isinstance(max_depth, bool) or max_depth < 1:
            raise ValueError("max_depth must be an integer of at least 1.")
        normalized_limit = _validate_limit(limit)
        queue: deque[tuple[LangMemItem, int]] = deque([(anchor, 0)])
        visited = {anchor.block.id}
        results: list[GraphResult] = []
        while queue and len(results) < normalized_limit:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            connected: list[tuple[LangMemItem, str, GraphDirection]] = []
            for neighbor_record in self.structures.graph.neighbors(
                current.block,
                relation=ITEM_RELATION,
                direction="both",
            ):
                link = neighbor_record.link
                neighbor_id = neighbor_record.block.id
                direction: GraphDirection = neighbor_record.direction
                if neighbor_id in visited:
                    continue
                neighbor = self._item_by_block_id(neighbor_id)
                if neighbor is None or not _is_below(neighbor.namespace, prefix):
                    continue
                relation = cast(str, link.metadata.get("relation", "related"))
                connected.append((neighbor, relation, direction))
            connected.sort(key=lambda value: (value[0].namespace, value[0].key, value[1]))
            for neighbor, relation, direction in connected:
                if neighbor.block.id in visited:
                    continue
                visited.add(neighbor.block.id)
                next_depth = depth + 1
                results.append(GraphResult(neighbor, relation, direction, next_depth))
                if len(results) >= normalized_limit:
                    break
                queue.append((neighbor, next_depth))
        return results

    def _validate_value(self, namespace: Namespace, value: Any) -> tuple[Any, str | None]:
        schema = self._schemas.get(namespace)
        if schema is None:
            return deepcopy(value), None
        return schema.validate(value), schema.name

    def _all_items(self) -> list[LangMemItem]:
        return [
            _item_from_block(item.block)
            for item in self.structures.items.items(
                recursive=True,
                limit=_SEARCH_LIMIT,
            )
        ]

    def _items_below(self, prefix: Namespace) -> list[LangMemItem]:
        return [
            _item_from_block(item.block)
            for item in self.structures.items.items(
                namespace=prefix,
                recursive=True,
                limit=_SEARCH_LIMIT,
            )
        ]

    def _item_by_block_id(self, block_id: str) -> LangMemItem | None:
        block = self.memory.get(block_id)
        if block is None or block.type != ITEM_TYPE:
            return None
        return _item_from_block(block)

    def _require_item(self, namespace: Sequence[str], key: str) -> LangMemItem:
        item = self.get(namespace, key)
        if item is None:
            raise KeyError(f"Item {key!r} was not found in namespace {tuple(namespace)!r}.")
        return item


def _item_from_block(block: MemoryBlock) -> LangMemItem:
    """Decode one canonical item wrapper."""

    if block.type != ITEM_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a LangMem item.")
    payload = cast(dict[str, Any], block.value)
    if set(payload) != {"namespace", "key", "value"}:
        raise TypeError(f"Block {block.id!r} does not contain a namespace/key/value item.")
    namespace_value = payload["namespace"]
    if not isinstance(namespace_value, list):
        raise TypeError(f"Block {block.id!r} has an invalid namespace.")
    occurred_at_value = block.metadata.get("occurred_at")
    return LangMemItem(
        block=block,
        namespace=_normalize_namespace(cast(list[str], namespace_value)),
        key=_required_text(payload["key"], field="key"),
        value=deepcopy(payload["value"]),
        schema_name=cast(str | None, block.metadata.get("schema")),
        entities=tuple(cast(list[str], block.metadata.get("entities", []))),
        occurred_at=(
            _parse_timestamp(cast(str, occurred_at_value), field="occurred_at")
            if occurred_at_value is not None
            else None
        ),
    )


def _item_index(
    namespace: Namespace,
    key: str,
    *,
    schema_name: str | None,
    occurred_at: datetime | None,
    entities: Sequence[str],
) -> dict[str, Any]:
    """Return portable index metadata outside the schema-controlled value."""

    return {
        "namespace": list(namespace),
        "namespace_path": "/".join(namespace),
        "key": key,
        "schema": schema_name,
        "occurred_at": _format_timestamp(occurred_at) if occurred_at is not None else None,
        "entities": list(entities),
    }


def _normalize_namespace(value: Sequence[str]) -> Namespace:
    """Validate a non-empty hierarchical namespace."""

    if isinstance(value, str) or not value:
        raise ValueError("namespace must be a non-empty sequence of strings.")
    return tuple(_required_text(segment, field="namespace segment") for segment in value)


def _is_below(namespace: Namespace, prefix: Namespace) -> bool:
    return namespace[: len(prefix)] == prefix


def _normalized_strings(values: Sequence[str], *, field: str) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = _required_text(value, field=field)
        marker = item.casefold()
        if marker not in seen:
            seen.add(marker)
            normalized.append(item)
    return tuple(normalized)


def _required_text(value: object, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string.")
    return value.strip()


def _normalize_timestamp(value: datetime, *, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValueError(f"{field} must be a timezone-aware datetime.")
    return value.astimezone(UTC)


def _parse_timestamp(value: str, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO 8601 timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from exc
    return _normalize_timestamp(parsed, field=field)


def _format_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _searchable_text(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, default=str)


def _tokens(value: str) -> set[str]:
    return set(_TOKEN_PATTERN.findall(value.casefold()))


def _validate_limit(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError("limit must be an integer of at least 1.")
    return min(value, _SEARCH_LIMIT)
