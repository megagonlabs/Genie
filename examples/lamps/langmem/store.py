"""The namespaced store that holds the addresses of LangMem memories.

LangMem writes through a LangGraph `BaseStore`:

- **Address:** an item is at a namespace plus a key.
- **Retrieval:** an exact address read, or a vector search below a namespace
  prefix.
- **Filter:** a metadata filter can narrow the search.

That is the full storage contract. `KeyedCollection` covers the address part of
it directly.

The important part is the namespace. Upstream, a namespace is only a name.
Here, each segment is also a block that owns the blocks at the addresses below
it:

- **`search`:** it gives Genie a subtree scope. It does not filter the
  candidates itself.
- **`forget`:** one call retires a whole user.
- **Ranking:** it stays in this module, because Genie has no ranking by design.
  The backend selects the candidate blocks, and the application selects the
  best candidate.
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

    The store controls addresses, validation, and retrieval. It never decides
    what a memory means or when a memory changes. A `MemoryManager` decides
    that.
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
            memory: Bounded Genie handle that owns all namespaces and items.
            structures: Facade bundle on the same handle.
            schemas: Exact namespaces, each with the schema for its values. A
                namespace without a registered schema accepts each value that
                the configured Genie codec can encode.
        """

        self.memory = memory
        self.structures = structures
        self._schemas = {namespace(*key): schema for key, schema in (schemas or {}).items()}

    def schema_for(self, value: Sequence[str]) -> ValueSchema | None:
        """Return the schema registered for one exact namespace.

        Args:
            value: Namespace to find.

        Returns:
            The registered schema, or `None` if the namespace has no schema.
        """

        return self._schemas.get(namespace(*value))

    def insert(self, value: Sequence[str], key: str, item_value: Any) -> MemoryItem:
        """Store a new item at an empty address.

        Args:
            value: Resolved namespace for the item.
            key: Key in that namespace.
            item_value: Application value. The store checks it against the
                namespace schema.

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
        """Replace the value at an address, and keep the identity of the item.

        - **Upstream:** LangMem patches a memory in place. So the references to
          the memory stay valid, and its change history stays.
        - **Here:** a Genie update keeps the block id and increases
          `MemoryItem.version`. So the patch is visible, and the lamp keeps no
          audit record.

        Args:
            value: Resolved namespace that holds the item.
            key: Key in that namespace.
            item_value: Replacement value. The store checks it against the
                schema.

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

        The keyed binding goes away with the block. So a later insert can use
        the freed address again.

        Args:
            value: Resolved namespace that holds the item.
            key: Key in that namespace.

        Returns:
            The item before the delete.

        Raises:
            KeyError: If the address holds no item.
        """

        existing = self.require(value, key)
        self.memory.delete(existing.block)
        return existing

    def get(self, value: Sequence[str], key: str) -> MemoryItem | None:
        """Find one item by its exact address.

        Args:
            value: Resolved namespace.
            key: Key in that namespace.

        Returns:
            The item, or `None` if the address is empty.
        """

        address = MemoryAddress(namespace(*value), required_text(key, field="key"))
        block = self.structures.items.get(address)
        return None if block is None else item_from_block(block)

    def require(self, value: Sequence[str], key: str) -> MemoryItem:
        """Find one item, and raise `KeyError` if the address is empty.

        Args:
            value: Resolved namespace.
            key: Key in that namespace.

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
        """Return all items with an address at or below a namespace prefix.

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
        """Search below a namespace prefix. A metadata filter can narrow the search.

        - **Candidates:** Genie selects them. The namespace prefix becomes a
          subtree scope. The item type and the metadata filter, if any, go into
          the query. One search runs for each query term.
        - **Score:** the lamp calculates it. The portable text match of Genie is
          a substring test over the whole block, not a similarity measure. A
          candidate counts a term only if the term is in the value of the
          candidate. The score is the share of terms that the value contains.

        Args:
            prefix: Namespace prefix for the search.
            query: Free text. The store matches its terms against item values.
            filters: Metadata filter that the store gives to Genie, for example
                `{"schema": "semantic_fact"}`.
            limit: Maximum number of hits on the page. The store caps it at
                `SEARCH_LIMIT`.
            offset: Number of hits to skip. A caller uses it to read the
                results page by page.

        Returns:
            Hits in descending score order. The item address breaks ties. The
            list is empty if the namespace does not exist or the query has no
            alphanumeric term.

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
        """Return the block that owns one namespace, if it exists.

        Args:
            value: Namespace to find.

        Returns:
            The namespace block, or `None` if the path does not exist.
        """

        return self.structures.namespace_node(value)

    def forget(self, value: Sequence[str]) -> list[MemoryItem]:
        """Delete a namespace and everything with an address at or below it.

        - **Key-value store:** it has no single operation for "this user left".
          The application finds and removes each key, one at a time.
        - **Here:** each namespace segment owns what is below it. So one call
          does the work. The subtree goes, and the structure bindings go with
          the blocks that they referred to.
        - **Whole subtree:** the subtree holds everything that belongs to the
          user, not only the items. The same namespace block owns the user
          profile and each conversation that is still on the queue for the
          user. So nothing stays that can write to the namespace after the
          lamp retires it.

        Args:
            value: Namespace to retire.

        Returns:
            The removed items, in ascending address order. The list does not
            include the profile and the queued submissions, because they are
            not items. The delete removed them too.

        Raises:
            KeyError: If the namespace block does not exist.
        """

        target = namespace(*value)
        node = self.namespace_node(target)
        if node is None:
            raise KeyError(f"Namespace {namespace_path(target)!r} does not exist.")
        removed = self.items(target)
        self.structures.tiers.delete_subtree(node)
        return removed

    def _check(self, value: Namespace, item_value: Any) -> tuple[Any, str | None]:
        """Validate a value against its namespace schema, if the namespace has a registered schema."""

        schema = self._schemas.get(value)
        if schema is None:
            return deepcopy(item_value), None
        return schema.validate(item_value), schema.name


def _positive(value: object, *, field: str) -> int:
    """Return a positive integer bound, capped at `SEARCH_LIMIT`, the scan limit of the lamp."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be an integer of at least 1.")
    return min(value, SEARCH_LIMIT)


def _offset(value: object) -> int:
    """Return a page offset of 0 or more."""

    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("offset must be an integer of at least 0.")
    return value
