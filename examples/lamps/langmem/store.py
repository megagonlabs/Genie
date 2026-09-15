"""The namespaced store LangMem's memories are addressed in.

LangMem writes through a LangGraph `BaseStore`:

- An item lives at a namespace plus a key.
- Retrieval is either an exact address lookup or a vector search below a
  namespace prefix.
- A metadata filter can narrow the search.

That is the whole storage contract, and `KeyedCollection` covers the addressing
part of it directly.

The part worth reading is what happens to the namespace. A namespace is a bare
name upstream. Here each segment is also a block that owns what is addressed
below it:

- `search` hands Genie a subtree scope instead of filtering candidates itself.
- `forget` retires a whole user with one call.
- Ranking stays in this module, because Genie deliberately has no ranking. The
  backend decides which blocks are candidates, and the application decides which
  candidate is best.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from examples.lamps.langmem.model import (
    ITEM_TYPE,
    SEARCH_LIMIT,
    MemoryItem,
    Namespace,
    SearchHit,
    ValueSchema,
    is_below,
    item_from_block,
    item_metadata,
    namespace,
    namespace_path,
    required_text,
    searchable_text,
    tokenize,
)
from examples.lamps.langmem.structures import LangMemStructures
from genie import MemoryBlock, MemoryHandle, Scope
from genie.structures import MemoryAddress


class MemoryStore:
    """Namespaced item storage with schema-checked values.

    The store owns addressing, validation, and retrieval. It never decides what
    a memory means or when one should change; a `MemoryManager` does that.
    """

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: LangMemStructures,
        schemas: Mapping[Namespace, ValueSchema] | None = None,
    ) -> None:
        """Create a store over one bounded handle.

        Args:
            memory: Bounded Genie handle owning every namespace and item.
            structures: Facade bundle built over the same handle.
            schemas: Exact namespaces mapped to the schema their values follow.
                A namespace with no registered schema accepts any value the
                configured Genie codec encodes.
        """

        self.memory = memory
        self.structures = structures
        self._schemas = {namespace(*key): schema for key, schema in (schemas or {}).items()}

    def schema_for(self, value: Sequence[str]) -> ValueSchema | None:
        """Return the schema registered for one exact namespace.

        Args:
            value: Namespace to look up.

        Returns:
            The registered schema, or `None` when the namespace has none.
        """

        return self._schemas.get(namespace(*value))

    def insert(self, value: Sequence[str], key: str, item_value: Any) -> MemoryItem:
        """Store a new item at an address that is currently empty.

        Args:
            value: Resolved namespace to store under.
            key: Key inside that namespace.
            item_value: Application value, checked against the namespace schema.

        Returns:
            The stored item.

        Raises:
            TypeError: If the value does not match the namespace schema.
            ValueError: If the address already holds an item, or the schema
                rejects the value.
        """

        target = namespace(*value)
        item_key = required_text(key, field="key")
        address = MemoryAddress(target, item_key)
        if self.structures.items.get(address) is not None:
            raise ValueError(f"Item {item_key!r} already exists in namespace {namespace_path(target)!r}.")
        checked, schema_name = self._check(target, item_value)
        block = self.structures.tiers.create_child(
            parent=self.structures.ensure_namespace(target),
            type=ITEM_TYPE,
            value={"namespace": list(target), "key": item_key, "value": checked},
            metadata=item_metadata(target, item_key, schema_name=schema_name),
        )
        try:
            self.structures.items.bind(address, block)
        except Exception:
            self.memory.delete(block)
            raise
        return item_from_block(block)

    def update(self, value: Sequence[str], key: str, item_value: Any) -> MemoryItem:
        """Replace the value at an address while keeping the item's identity.

        LangMem patches a memory in place so that references to it stay valid
        and its change history is preserved. A Genie update keeps the block id
        and raises `MemoryItem.version`, so the patch is visible without the
        lamp keeping an audit record.

        Args:
            value: Resolved namespace holding the item.
            key: Key inside that namespace.
            item_value: Replacement value, checked against the schema.

        Returns:
            The updated item, with the same id as before.

        Raises:
            KeyError: If the address holds no item.
            TypeError: If the value does not match the namespace schema.
            ValueError: If the schema rejects the value.
        """

        existing = self.require(value, key)
        checked, schema_name = self._check(existing.namespace, item_value)
        block = self.memory.update(
            existing.block,
            value={"value": checked},
            metadata=item_metadata(existing.namespace, existing.key, schema_name=schema_name),
        )
        return item_from_block(block)

    def delete(self, value: Sequence[str], key: str) -> MemoryItem:
        """Remove one item and return its last stored state.

        The keyed binding goes with the block, so an address freed this way can
        be inserted into again.

        Args:
            value: Resolved namespace holding the item.
            key: Key inside that namespace.

        Returns:
            The item as it was before deletion.

        Raises:
            KeyError: If the address holds no item.
        """

        existing = self.require(value, key)
        self.memory.delete(existing.block)
        return existing

    def get(self, value: Sequence[str], key: str) -> MemoryItem | None:
        """Look one item up by its exact address.

        Args:
            value: Resolved namespace.
            key: Key inside that namespace.

        Returns:
            The item, or `None` when the address is empty.
        """

        address = MemoryAddress(namespace(*value), required_text(key, field="key"))
        block = self.structures.items.get(address)
        return None if block is None else item_from_block(block)

    def require(self, value: Sequence[str], key: str) -> MemoryItem:
        """Look one item up and fail when the address is empty.

        Args:
            value: Resolved namespace.
            key: Key inside that namespace.

        Returns:
            The stored item.

        Raises:
            KeyError: If the address holds no item.
        """

        item = self.get(value, key)
        if item is None:
            raise KeyError(f"Item {key!r} was not found in namespace {namespace_path(namespace(*value))!r}.")
        return item

    def items(self, prefix: Sequence[str]) -> list[MemoryItem]:
        """Return every item addressed at or below a namespace prefix.

        Args:
            prefix: Namespace prefix.

        Returns:
            Items in ascending address order.
        """

        target = namespace(*prefix)
        found = [
            item_from_block(entry.block)
            for entry in self.structures.items.items(
                namespace=target,
                recursive=True,
                limit=SEARCH_LIMIT,
            )
        ]
        return sorted(found, key=lambda item: item.address)

    def search(
        self,
        prefix: Sequence[str],
        query: str,
        *,
        filters: Mapping[str, Any] | None = None,
        limit: int = 10,
        offset: int = 0,
    ) -> list[SearchHit]:
        """Search below a namespace prefix, optionally narrowed by metadata.

        - Genie selects the candidates. The namespace prefix becomes a subtree
          scope, the item type and any metadata filter go into the query, and
          one search runs per query term.
        - Scoring is the lamp's, because Genie's portable text match is a
          substring test over the whole block rather than a similarity measure.
          A candidate only counts a term that appears in its own value, and the
          score is the share of terms that do.

        Args:
            prefix: Namespace prefix to search below.
            query: Free text whose terms are matched against item values.
            filters: Metadata filter passed through to Genie, such as
                `{"schema": "semantic_fact"}`.
            limit: Maximum hits to return after paging.
            offset: Hits to skip, so a caller can page through results.

        Returns:
            Hits in descending score order, ties broken on the item address.

        Raises:
            ValueError: If the query is empty, or limit or offset is invalid.
        """

        target = namespace(*prefix)
        terms = tokenize(required_text(query, field="query"))
        page_size = _positive(limit, field="limit")
        skip = _offset(offset)
        node = self.namespace_node(target)
        if node is None or not terms:
            return []
        candidates: dict[str, MemoryBlock] = {}
        for term in terms:
            for result in self.memory.search(
                text=term,
                root=node,
                scope=Scope.SUBTREE,
                types=[ITEM_TYPE],
                filters=dict(filters) if filters else None,
                limit=SEARCH_LIMIT,
            ):
                candidates.setdefault(result.block.id, result.block)
        hits: list[SearchHit] = []
        for block in candidates.values():
            item = item_from_block(block)
            if not is_below(item.namespace, target):
                continue
            present = tokenize(searchable_text(item.value))
            matched = tuple(term for term in terms if term in present)
            if not matched:
                continue
            hits.append(
                SearchHit(
                    item=item,
                    score=round(len(matched) / len(terms), 4),
                    matched=matched,
                )
            )
        hits.sort(key=lambda hit: (-hit.score, hit.item.address))
        return hits[skip : skip + page_size]

    def namespace_node(self, value: Sequence[str]) -> MemoryBlock | None:
        """Return the block that owns one namespace, when it exists.

        Args:
            value: Namespace to look up.

        Returns:
            The namespace block, or `None` when the path was never created.
        """

        return self.structures.namespace_node(value)

    def forget(self, value: Sequence[str]) -> list[MemoryItem]:
        """Delete a namespace and everything addressed at or below it.

        A key-value store has no answer for "this user left"; every key has to
        be found and removed one at a time. Because each namespace segment owns
        what is stored below it, ownership answers it in one call: the subtree
        goes, and the structure bindings go with the blocks they pointed at.

        The subtree is everything that belongs to the user, not only their
        items. Their profile and any conversation still queued for them are
        owned by the same namespace block, so nothing is left behind that could
        write to the namespace after it is retired.

        Args:
            value: Namespace to retire.

        Returns:
            The items that were removed, in ascending address order. The
            profile and queued submissions that went with them are not listed,
            because they are not items.

        Raises:
            KeyError: If the namespace holds nothing.
        """

        target = namespace(*value)
        node = self.namespace_node(target)
        if node is None:
            raise KeyError(f"Namespace {namespace_path(target)!r} does not exist.")
        removed = self.items(target)
        self.structures.tiers.delete_subtree(node)
        return removed

    def _check(self, value: Namespace, item_value: Any) -> tuple[Any, str | None]:
        """Validate a value against its namespace schema, if one is registered."""

        schema = self._schemas.get(value)
        if schema is None:
            return deepcopy(item_value), None
        return schema.validate(item_value), schema.name


def _positive(value: object, *, field: str) -> int:
    """Return a positive integer bound, capped at the lamp's scan limit."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be an integer of at least 1.")
    return min(value, SEARCH_LIMIT)


def _offset(value: object) -> int:
    """Return a non-negative paging offset."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be an integer of at least 0.")
    return value
