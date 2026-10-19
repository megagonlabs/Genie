"""The Hindsight-inspired memory bank: documents in, resolved facts out.

`retain` is the Hindsight write path. A caller gives the bank a source payload,
for example a chat turn, a document, or an agent run. Then the bank:

- **Keeps the payload** as ground truth.
- **Stores the facts** extracted from the payload.
- **Resolves names:** it resolves the names that those facts mention to
  canonical entities.
- **Records provenance:** it records where every fact came from.

Upstream, extraction is the one step that really needs a language model. So the
lamp accepts `FactDraft` values that are already extracted. The Graphiti lamp
does the same with triplets that are already extracted.

The Genie side of that path:

- **Provenance is a `LineageDAG`:** so "what is this belief based on?" is a
  walk, not a stored list.
- **Entity resolution ends in a `KeyedCollection` bind:** every surface form
  that resolved to an entity becomes an address of the block of that entity. So
  the second lookup of a name is a keyed read.
- **Retention order is an `EventLog`:** its sequence numbers are also the
  cursor that consolidation reads from.
- **One `TemporalCollection` entry for each fact holds both Hindsight time
  axes:** the event time is when the bank learned the fact, and the validity is
  the period that the fact describes.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any

from examples.lamps.hindsight.model import (
    ASSOCIATIVE_RELATIONS,
    DOCUMENT_TYPE,
    ENTITY_NAMESPACE,
    ENTITY_TYPE,
    MEMORY_TYPE,
    MENTIONS_RELATION,
    SEARCH_LIMIT,
    Document,
    Entity,
    FactDraft,
    MemoryFact,
    RetainResult,
    document_from_block,
    entity_from_block,
    fact_from_block,
    format_timestamp,
    normalize_name,
    normalize_timestamp,
    required_text,
    unique_strings,
)
from examples.lamps.hindsight.structures import HindsightStructures
from genie import AccessDenied, MemoryBlock, MemoryHandle, MemoryLink
from genie.structures import LogItem, MemoryAddress, TemporalItem, ValidityInterval

DEFAULT_ENTITY_KIND = "concept"


class HindsightMemoryBank:
    """One isolated bank of documents, facts, and canonical entities.

    The bank owns the Hindsight taxonomy and resolution rules. Genie owns the
    blocks that those rules write, the ownership boundary that keeps one bank
    out of another, and the structures that make the results queryable.
    """

    def __init__(self, memory: MemoryHandle, *, structures: HindsightStructures) -> None:
        """Create a bank over one bounded Genie memory handle.

        Args:
            memory: Handle whose root owns every block in the bank.
            structures: Structure bundle built over the same root.
        """

        self.memory = memory
        self.structures = structures

    def retain(
        self,
        *,
        source: str,
        content: str,
        retained_at: datetime,
        context: str = "",
        facts: Sequence[FactDraft] = (),
    ) -> RetainResult:
        """Store one source payload and the facts extracted from it.

        If a valid fact with the same normalized text was on file before this
        call, the bank does not store the draft again. The bank links the new
        document to that stored fact instead. So a repeated fact adds evidence
        for a belief and does not make the bank larger.

        Args:
            source: Where the payload came from.
            content: Raw payload text that the bank keeps as ground truth.
            retained_at: When the bank learned this payload.
            context: Optional text that describes the situation.
            facts: Facts that are already extracted from the payload.

        Returns:
            The stored document, one fact for each draft in draft order, and
            the subset of those facts that were already on file.

        Raises:
            ValueError: If required text is empty or a draft is not valid.
        """

        learned_at = normalize_timestamp(retained_at, field="retained_at")
        document_block = self.structures.tiers.create_child(
            parent=self.structures.documents,
            type=DOCUMENT_TYPE,
            value={
                "source": required_text(source, field="source"),
                "content": required_text(content, field="content"),
                "context": context.strip(),
                "retained_at": format_timestamp(learned_at),
            },
            metadata={"source": source.strip(), "retained_at": format_timestamp(learned_at)},
        )
        document = document_from_block(document_block)
        stored: list[MemoryFact] = []
        duplicates: list[MemoryFact] = []
        seen: set[str] = set()
        for draft in facts:
            text = required_text(draft.text, field="fact text")
            text_key = normalize_name(text)
            existing = None if text_key in seen else self._fact_with_text(text_key)
            seen.add(text_key)
            if existing is not None:
                self.structures.lineage.add(
                    document_block,
                    existing.block,
                    metadata={"role": "re-attested"},
                )
                stored.append(existing)
                duplicates.append(existing)
                continue
            stored.append(self._store_fact(draft, document=document, learned_at=learned_at))
        return RetainResult(
            document=document,
            facts=tuple(stored),
            duplicates=tuple(duplicates),
        )

    def register_entity(
        self,
        name: str,
        *,
        kind: str = DEFAULT_ENTITY_KIND,
        aliases: Sequence[str] = (),
    ) -> Entity:
        """Declare a canonical entity and its surface forms (other written names).

        Args:
            name: Canonical display name.
            kind: Application-defined entity kind.
            aliases: Extra surface forms to bind to the same entity.

        Returns:
            The canonical entity, new or with the added aliases.

        Raises:
            ValueError: If the name is empty, or the kind of a new entity is
                empty.
        """

        canonical = required_text(name, field="entity name")
        entity = self.entity(canonical)
        if entity is None:
            entity = self._create_entity(canonical, kind=required_text(kind, field="entity kind"))
        return self._bind_aliases(entity, unique_strings(aliases, field="aliases"))

    def resolve_entity(self, name: str, *, kind: str = DEFAULT_ENTITY_KIND) -> Entity:
        """Return the entity that a written name refers to, and resolve surface forms.

        - **Known name:** a name that is already bound to an entity is a keyed
          read.
        - **New name:** the bank compares a new name with the canonical names
          on file. A canonical name matches if each word of the new name starts
          a different word of the canonical name, in the same order. So `Alice`
          and `Alice C.` both match `Alice Chen`, but `Alice Cooper` does not.
        - **1 match:** the bank binds the new name as an alias of that entity.
        - **No match or an ambiguous name:** the bank creates a new entity and
          does not guess.

        Args:
            name: Entity name as written in a fact.
            kind: Entity kind for a new entity.

        Returns:
            The resolved canonical entity.

        Raises:
            ValueError: If the name is empty.
        """

        written = required_text(name, field="entity name")
        existing = self.entity(written)
        if existing is not None:
            return existing
        candidates = [
            entity
            for entity in self.entities()
            if _subsumes(normalize_name(written), entity.name)
        ]
        if len(candidates) == 1:
            return self._bind_aliases(candidates[0], (written,))
        return self._create_entity(written, kind=kind)

    def entity(self, name: str) -> Entity | None:
        """Return the entity bound to one written name, if any.

        Args:
            name: Canonical name or a known alias.

        Returns:
            The entity, or `None` if the name never resolved.
        """

        block = self.structures.entities.get(_entity_address(name))
        return entity_from_block(block) if block is not None else None

    def entities(self) -> list[Entity]:
        """Return every canonical entity one time.

        Returns:
            Entities in ascending canonical-name order.
        """

        seen: dict[str, Entity] = {}
        for item in self.structures.entities.items(
            namespace=ENTITY_NAMESPACE,
            limit=SEARCH_LIMIT,
        ):
            if item.block.id not in seen and item.block.type == ENTITY_TYPE:
                seen[item.block.id] = entity_from_block(item.block)
        return sorted(seen.values(), key=lambda entity: entity.name)

    def mentioned_by(self, entity: str | Entity) -> list[MemoryFact]:
        """Return the valid facts that mention one entity.

        Args:
            entity: Entity, or a surface form that is already bound to an
                entity.

        Returns:
            Facts in retention order.

        Raises:
            ValueError: If the name never resolved to an entity.
        """

        resolved = entity if isinstance(entity, Entity) else self.entity(entity)
        if resolved is None:
            raise ValueError(f"Unknown entity {entity!r}.")
        reachable = {
            neighbor.block.id
            for neighbor in self.structures.graph.neighbors(
                resolved.block,
                relation=MENTIONS_RELATION,
                direction="in",
            )
        }
        return [fact for fact in self.facts() if fact.id in reachable]

    def facts(self, *, include_invalidated: bool = False) -> list[MemoryFact]:
        """Return the stored facts in the order that the bank learned them.

        Args:
            include_invalidated: If `True`, the result also includes
                invalidated facts.

        Returns:
            Facts in retention order.
        """

        return [
            fact
            for item in self.retention(include_invalidated=include_invalidated)
            if (fact := fact_from_block(item.block)).state == "valid" or include_invalidated
        ]

    def retention(self, *, start: int | None = None, include_invalidated: bool = True) -> list[LogItem]:
        """Read the retention log, optionally from a cursor.

        Consolidation uses this method to find the facts that it did not merge
        into an observation yet. For that reason, each item holds both the log
        entry and the block.

        Args:
            start: Optional inclusive log sequence to start from.
            include_invalidated: If `True`, the result also includes
                invalidated facts.

        Returns:
            Log items in ascending sequence order.
        """

        items = self.structures.retention.read(start=start, limit=SEARCH_LIMIT)
        if include_invalidated:
            return items
        return [item for item in items if fact_from_block(item.block).state == "valid"]

    def fact(self, block: str | MemoryBlock | MemoryFact) -> MemoryFact | None:
        """Return one stored fact by id.

        Args:
            block: Fact, block, or block id.

        Returns:
            The fact, or `None` if the id is not a fact in this bank. An id
            from outside the root of the bank reads as not found and does not
            raise an error. So a lookup does not show if another bank holds
            that id.
        """

        try:
            stored = self.memory.get(_block_id(block))
        except AccessDenied:
            return None
        if stored is None or stored.type != MEMORY_TYPE:
            return None
        return fact_from_block(stored)

    def require_fact(self, block: str | MemoryBlock | MemoryFact) -> MemoryFact:
        """Return one stored fact, or raise `ValueError`.

        Args:
            block: Fact, block, or block id.

        Returns:
            The stored fact.

        Raises:
            ValueError: If the id is not a fact in this bank.
        """

        stored = self.fact(block)
        if stored is None:
            raise ValueError(f"Unknown memory {_block_id(block)!r}.")
        return stored

    def invalidate(self, block: str | MemoryBlock | MemoryFact) -> MemoryFact:
        """Mark one fact as `invalidated`, but do not delete it.

        Hindsight keeps invalidated facts readable, so a correction stays
        auditable. Recall and consolidation do not use them. The block, its
        provenance, and its links all stay.

        Args:
            block: Fact to retire.

        Returns:
            The updated fact.

        Raises:
            ValueError: If the id is not a fact in this bank.
        """

        stored = self.require_fact(block)
        updated = self.memory.update(
            stored.block,
            value={"state": "invalidated"},
            metadata={"state": "invalidated"},
        )
        return fact_from_block(updated)

    def connect(
        self,
        source: str | MemoryBlock | MemoryFact,
        target: str | MemoryBlock | MemoryFact,
        relation: str,
        *,
        metadata: dict[str, Any] | None = None,
    ) -> MemoryLink:
        """Add one associative link between two facts.

        Hindsight connects memories in 4 ways. The mention edges already hold
        the entity connections. So this method adds the other 3: temporal,
        semantic, and causal.

        Args:
            source: Fact where the directed link starts.
            target: Fact where the directed link ends.
            relation: One of the associative relations of the lamp.
            metadata: Optional link properties.

        Returns:
            The stored link.

        Raises:
            ValueError: If the relation is not associative or an id is unknown.
        """

        if relation not in ASSOCIATIVE_RELATIONS:
            raise ValueError(f"Unsupported Hindsight relation {relation!r}.")
        return self.structures.graph.add_edge(
            self.require_fact(source).block,
            self.require_fact(target).block,
            relation,
            metadata=deepcopy(metadata or {}),
        )

    def learned_between(
        self,
        start: datetime | None = None,
        stop: datetime | None = None,
    ) -> list[MemoryFact]:
        """Return the valid facts that the bank learned inside a half-open range.

        Args:
            start: Optional inclusive lower bound on learned time.
            stop: Optional exclusive upper bound on learned time.

        Returns:
            Facts in ascending learned-at order.
        """

        items = self.structures.timeline.events(
            start=None if start is None else normalize_timestamp(start, field="start"),
            stop=None if stop is None else normalize_timestamp(stop, field="stop"),
            limit=SEARCH_LIMIT,
        )
        return _valid_facts(items, key=_event_time)

    def describing(self, interval: ValidityInterval) -> list[MemoryFact]:
        """Return the valid facts whose described period overlaps an interval.

        Args:
            interval: Period to compare with the described period of each fact.

        Returns:
            Facts in ascending order of the start of their described period.
        """

        items = self.structures.timeline.overlapping(interval, limit=SEARCH_LIMIT)
        return _valid_facts(items, key=_validity_start)

    def provenance(self, block: str | MemoryBlock) -> tuple[MemoryBlock, ...]:
        """Return the blocks that one block was directly derived from.

        Args:
            block: Fact, observation revision, or mental-model revision.

        Returns:
            Direct lineage predecessors in ascending id order.
        """

        neighbors = self.structures.lineage.predecessors(_block_id(block))
        return tuple(sorted((neighbor.block for neighbor in neighbors), key=lambda item: item.id))

    def _store_fact(
        self,
        draft: FactDraft,
        *,
        document: Document,
        learned_at: datetime,
    ) -> MemoryFact:
        """Store one extracted fact with its entities, times, and provenance.

        If a structure write fails, the method deletes the new block and raises
        the error again.
        """

        if draft.fact_type not in {"world", "experience"}:
            raise ValueError("fact_type must be 'world' or 'experience'.")
        text = required_text(draft.text, field="fact text")
        occurred = _occurred_interval(draft)
        entities = [
            self.resolve_entity(name)
            for name in unique_strings(draft.entities, field="entities")
        ]
        block = self.structures.tiers.create_child(
            parent=self.structures.memories,
            type=MEMORY_TYPE,
            value={
                "text": text,
                "fact_type": draft.fact_type,
                "state": "valid",
                "context": draft.context.strip(),
                "entities": [entity.name for entity in entities],
                "tags": list(unique_strings(draft.tags, field="tags")),
                "occurred_start": None if occurred is None else format_timestamp(occurred.valid_at),
                "occurred_end": (
                    None
                    if occurred is None or occurred.invalid_at is None
                    else format_timestamp(occurred.invalid_at)
                ),
                "learned_at": format_timestamp(learned_at),
                "document_id": document.id,
                "claim": None if draft.claim is None else draft.claim.as_value(),
            },
            metadata={
                "fact_type": draft.fact_type,
                "state": "valid",
                "tags": list(unique_strings(draft.tags, field="tags")),
                "claim": "" if draft.claim is None else draft.claim.key,
                "text_key": normalize_name(text),
            },
        )
        try:
            self.structures.retention.append(block)
            self.structures.timeline.add(block, occurred_at=learned_at, validity=occurred)
            for entity in entities:
                self.structures.graph.add_edge(block, entity.block, MENTIONS_RELATION)
            self.structures.lineage.add(document.block, block, metadata={"role": "extracted"})
        except Exception:
            self.memory.delete(block)
            raise
        return fact_from_block(block)

    def _fact_with_text(self, text_key: str) -> MemoryFact | None:
        """Return the earliest valid fact that already stores this normalized text.

        The lookup is a `MemoryHandle.search` with a metadata filter, not a
        scan. So deduplication uses the same portable query layer that an
        adapter can run in its own store.
        """

        results = self.memory.search(
            types=[MEMORY_TYPE],
            filters={"text_key": text_key, "state": "valid"},
            limit=SEARCH_LIMIT,
        )
        facts = sorted((fact_from_block(result.block) for result in results), key=lambda fact: fact.learned_at)
        return facts[0] if facts else None

    def _create_entity(self, name: str, *, kind: str) -> Entity:
        """Create a canonical entity and bind its own name to it."""

        block = self.structures.tiers.create_child(
            parent=self.structures.entity_root,
            type=ENTITY_TYPE,
            value={"name": name, "kind": kind, "aliases": [name]},
            metadata={"name": name, "kind": kind},
        )
        try:
            self.structures.entities.bind(_entity_address(name), block)
        except Exception:
            self.memory.delete(block)
            raise
        return entity_from_block(block)

    def _bind_aliases(self, entity: Entity, aliases: Sequence[str]) -> Entity:
        """Bind extra surface forms to an entity and add them to its aliases.

        The method skips a surface form that is already bound to any entity.
        """

        added = [alias for alias in aliases if self.entity(alias) is None]
        if not added:
            return entity
        for alias in added:
            self.structures.entities.bind(_entity_address(alias), entity.block)
        updated = self.memory.update(
            entity.block,
            value={"aliases": [*entity.aliases, *added]},
        )
        return entity_from_block(updated)


def _entity_address(name: str) -> MemoryAddress:
    """Return the keyed address of one written entity name."""

    return MemoryAddress(ENTITY_NAMESPACE, normalize_name(name))


def _occurred_interval(draft: FactDraft) -> ValidityInterval | None:
    """Return the period that a draft describes, or `None` for an ongoing fact."""

    if draft.occurred_at is None:
        if draft.occurred_until is not None:
            raise ValueError("occurred_until requires occurred_at.")
        return None
    return ValidityInterval(
        valid_at=normalize_timestamp(draft.occurred_at, field="occurred_at"),
        invalid_at=(
            None
            if draft.occurred_until is None
            else normalize_timestamp(draft.occurred_until, field="occurred_until")
        ),
    )


def _valid_facts(
    items: Sequence[TemporalItem],
    *,
    key: Callable[[TemporalItem], datetime],
) -> list[MemoryFact]:
    """Decode temporal items into valid facts in a stable time order (ties by fact text)."""

    selected = [
        (key(item), fact)
        for item in items
        if item.block.type == MEMORY_TYPE and (fact := fact_from_block(item.block)).state == "valid"
    ]
    selected.sort(key=lambda pair: (pair[0], pair[1].text))
    return [fact for _, fact in selected]


def _event_time(item: TemporalItem) -> datetime:
    """Return the learned-at event time of a temporal item."""

    if item.entry.occurred_at is None:
        raise ValueError(f"Temporal entry for {item.block.id!r} has no event time.")
    return item.entry.occurred_at


def _validity_start(item: TemporalItem) -> datetime:
    """Return the start of the period a temporal item describes."""

    if item.entry.validity is None:
        raise ValueError(f"Temporal entry for {item.block.id!r} has no described period.")
    return item.entry.validity.valid_at


def _subsumes(written: str, canonical: str) -> bool:
    """Return `True` if a written name is an abbreviation of a canonical name."""

    written_words = written.split()
    canonical_words = normalize_name(canonical).split()
    if not written_words or len(written_words) > len(canonical_words):
        return False
    index = 0
    for word in written_words:
        while index < len(canonical_words) and not canonical_words[index].startswith(word):
            index += 1
        if index == len(canonical_words):
            return False
        index += 1
    return True


def _block_id(block: str | MemoryBlock | MemoryFact) -> str:
    """Return a block id from a fact, block, or id."""

    if isinstance(block, MemoryFact):
        return block.id
    if isinstance(block, MemoryBlock):
        return block.id
    return block
