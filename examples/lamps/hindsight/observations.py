"""Consolidation: turning retained facts into beliefs that survive new evidence.

Observations are the layer that makes Hindsight more than a store of sentences.
Related facts are folded into one deduplicated belief that carries a proof
count, gets stronger when more evidence arrives, gets extended when the evidence
adds nuance, and records the whole journey rather than being overwritten when
the evidence reverses.

Three Genie structures carry that behavior:

- `VersionedRecords` gives each belief a stable identity and an ordered history,
  so "what did we used to think?" is a read rather than an audit table the lamp
  has to keep.
- `LineageDAG` holds the evidence edges, so the proof count is counted off the
  graph instead of stored and kept in sync.
- The retention `EventLog` supplies the cursor. Consolidation processes the
  facts appended since it last ran, which is also how an observation knows it
  has gone stale.

What the lamp supplies is the judgment Hindsight asks a language model for. Each
extracted fact names the belief it speaks to and whether it supports or
contradicts it, so grouping and reconciliation stay deterministic.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import cast

from examples.lamps.hindsight.bank import HindsightMemoryBank
from examples.lamps.hindsight.model import (
    CLAIM_NAMESPACE,
    MEMORY_TYPE,
    OBSERVATION_REVISION_TYPE,
    OBSERVATION_TYPE,
    SEARCH_LIMIT,
    MemoryFact,
    Observation,
    Stance,
    fact_from_block,
    format_timestamp,
    normalize_timestamp,
    parse_timestamp,
    required_text,
)
from genie import MemoryBlock
from genie.structures import MemoryAddress, RevisionItem

CURSOR_FIELD = "consolidated_through"


class ConsolidationEngine:
    """Folds retained facts into versioned, evidence-backed observations."""

    def __init__(self, bank: HindsightMemoryBank) -> None:
        """Create a consolidation engine over one memory bank.

        Args:
            bank: Bank whose retained facts are consolidated.
        """

        self.bank = bank

    def consolidate(self, *, at: datetime) -> list[Observation]:
        """Fold every fact retained since the last run into observations.

        Upstream this runs in the background after each `retain`. Here it is an
        explicit call so the example stays deterministic, but the unit of work
        is the same: the facts appended to the retention log after the stored
        cursor.

        The cursor moves before the beliefs are written, so an observation
        refreshed by this run does not see its own evidence as unconsolidated
        and report itself stale.

        Args:
            at: Timestamp recorded on the revisions this run writes.

        Returns:
            The observations created or refreshed, in claim order.
        """

        consolidated_at = normalize_timestamp(at, field="at")
        cursor = self.cursor()
        grouped: dict[str, list[MemoryFact]] = {}
        for item in self.bank.retention(start=cursor):
            cursor = item.entry.sequence + 1
            fact = fact_from_block(item.block)
            if fact.state != "valid" or fact.claim is None:
                continue
            grouped.setdefault(fact.claim.key, []).append(fact)
        self._set_cursor(cursor)
        return [
            self._apply(claim, facts, consolidated_at=consolidated_at)
            for claim, facts in sorted(grouped.items())
        ]

    def cursor(self) -> int:
        """Return the retention sequence consolidation will resume from.

        Returns:
            The next unconsolidated log sequence.
        """

        anchor = self.bank.memory.get(self.bank.structures.observation_root.id)
        if anchor is None or not isinstance(anchor.value, dict):
            return 0
        stored = anchor.value.get(CURSOR_FIELD, 0)
        return stored if isinstance(stored, int) else 0

    def pending(self) -> list[MemoryFact]:
        """Return valid facts that carry a claim and are not consolidated yet.

        Returns:
            Facts in retention order.
        """

        return [
            fact
            for item in self.bank.retention(start=self.cursor())
            if (fact := fact_from_block(item.block)).state == "valid" and fact.claim is not None
        ]

    def observation(self, claim: str) -> Observation | None:
        """Return the current wording of one belief.

        Args:
            claim: Belief key.

        Returns:
            The current observation, or `None` when the belief is unknown.
        """

        address = _claim_address(claim)
        identity = self.bank.structures.observations.identity(address)
        current = self.bank.structures.observations.current(address)
        if identity is None or current is None:
            return None
        return self._observation(identity, current, stale=self._is_stale(claim))

    def observations(self) -> list[Observation]:
        """Return the current wording of every belief on file.

        Returns:
            Observations in ascending claim order.
        """

        stale = {fact.claim.key for fact in self.pending() if fact.claim is not None}
        results: list[Observation] = []
        for item in self.bank.structures.observations.identities(
            namespace=CLAIM_NAMESPACE,
            limit=SEARCH_LIMIT,
        ):
            current = self.bank.structures.observations.current(item.binding.address)
            if current is None:
                continue
            claim = item.binding.address.key
            results.append(self._observation(item.block, current, stale=claim in stale))
        return sorted(results, key=lambda observation: observation.claim)

    def history(self, claim: str) -> list[Observation]:
        """Return every wording this belief has had, oldest first.

        Args:
            claim: Belief key.

        Returns:
            Observations in ascending revision order.
        """

        address = _claim_address(claim)
        identity = self.bank.structures.observations.identity(address)
        if identity is None:
            return []
        stale = self._is_stale(claim)
        return [
            self._observation(identity, revision, stale=stale)
            for revision in self.bank.structures.observations.history(address, limit=SEARCH_LIMIT)
        ]

    def _apply(
        self,
        claim: str,
        facts: Sequence[MemoryFact],
        *,
        consolidated_at: datetime,
    ) -> Observation:
        """Create or refine one belief from the facts that speak to it."""

        address = _claim_address(claim)
        identity = self.bank.structures.observations.identity(address)
        if identity is None:
            identity = self.bank.structures.tiers.create_child(
                parent=self.bank.structures.observation_root,
                type=OBSERVATION_TYPE,
                value={"claim": claim},
                metadata={"claim": claim},
            )
            self.bank.structures.observations.register(address, identity)
        current = self.bank.structures.observations.current(address)
        previous = _revision_text(current) if current is not None else ""
        text, stance = _reconcile(previous, facts)
        tags = _merged_tags(current, facts)
        if current is not None and text == previous and tags == _revision_tags(current):
            self._attach(current.block, facts)
            return self._observation(identity, current, stale=self._is_stale(claim))
        revision_block = self.bank.structures.tiers.create_child(
            parent=identity,
            type=OBSERVATION_REVISION_TYPE,
            value={
                "claim": claim,
                "text": text,
                "stance": stance,
                "tags": list(tags),
                "consolidated_at": format_timestamp(consolidated_at),
            },
            metadata={"claim": claim, "stance": stance, "tags": list(tags)},
        )
        carried = [] if current is None else list(self.bank.provenance(current.block))
        self.bank.structures.observations.append(address, revision_block)
        self._attach(revision_block, facts, carried=carried)
        refreshed = self.bank.structures.observations.current(address)
        if refreshed is None:
            raise ValueError(f"Observation {claim!r} lost its revision during consolidation.")
        return self._observation(identity, refreshed, stale=self._is_stale(claim))

    def _attach(
        self,
        revision: MemoryBlock,
        facts: Sequence[MemoryFact],
        *,
        carried: Sequence[MemoryBlock] = (),
    ) -> None:
        """Link supporting facts to a revision as its evidence."""

        attached = {block.id for block in self.bank.provenance(revision)}
        for block in (*carried, *(fact.block for fact in facts)):
            if block.id in attached:
                continue
            attached.add(block.id)
            self.bank.structures.lineage.add(block, revision, metadata={"role": "evidence"})

    def _observation(
        self,
        identity: MemoryBlock,
        revision: RevisionItem,
        *,
        stale: bool,
    ) -> Observation:
        """Assemble one observation from its identity, revision, and evidence."""

        sources = tuple(
            block.id
            for block in self.bank.provenance(revision.block)
            if block.type == MEMORY_TYPE
        )
        value = revision.block.value
        return Observation(
            identity=identity,
            block=revision.block,
            claim=_field(value, "claim"),
            text=_field(value, "text"),
            revision=revision.entry.revision,
            stance=_stance(_field(value, "stance")),
            tags=tuple(value.get("tags", ())),
            consolidated_at=parse_timestamp(_field(value, "consolidated_at")),
            proof_count=len(sources),
            sources=sources,
            stale=stale,
        )

    def _is_stale(self, claim: str) -> bool:
        """Return whether unconsolidated facts still speak to this belief."""

        return any(fact.claim is not None and fact.claim.key == claim for fact in self.pending())

    def _set_cursor(self, sequence: int) -> None:
        """Record how far consolidation has read the retention log."""

        self.bank.memory.update(
            self.bank.structures.observation_root,
            value={CURSOR_FIELD: sequence},
        )


def _reconcile(previous: str, facts: Sequence[MemoryFact]) -> tuple[str, Stance]:
    """Fold new evidence into a belief and report how it changed it.

    Repeating a belief strengthens it without rewording it, a supporting fact
    with something new to say extends it, and a contradicting fact leads with
    the new belief while keeping the old one visible.
    """

    text = previous
    stance: Stance = "supports"
    for fact in facts:
        if fact.claim is None:
            continue
        belief = fact.claim.belief
        if not text:
            text = belief
        elif belief in text:
            continue
        elif fact.claim.stance == "contradicts":
            text = f"{belief} Previously: {text}"
            stance = "contradicts"
        else:
            text = f"{text} Also: {belief}"
    return text, stance


def _merged_tags(current: RevisionItem | None, facts: Sequence[MemoryFact]) -> tuple[str, ...]:
    """Return the union of a revision's tags and its new evidence's tags."""

    tags = list(_revision_tags(current)) if current is not None else []
    for fact in facts:
        for tag in fact.tags:
            if tag not in tags:
                tags.append(tag)
    return tuple(tags)


def _revision_text(revision: RevisionItem) -> str:
    """Return the belief text stored on a revision."""

    return _field(revision.block.value, "text")


def _revision_tags(revision: RevisionItem) -> tuple[str, ...]:
    """Return the tags stored on a revision."""

    value = revision.block.value
    if not isinstance(value, dict):
        raise TypeError("Observation revision value must be a dictionary.")
    return tuple(value.get("tags", ()))


def _claim_address(claim: str) -> MemoryAddress:
    """Return the versioned-record address of one belief."""

    return MemoryAddress(CLAIM_NAMESPACE, required_text(claim, field="claim"))


def _stance(value: str) -> Stance:
    """Validate and narrow a stored stance."""

    if value not in {"supports", "contradicts"}:
        raise ValueError("stance must be 'supports' or 'contradicts'.")
    return cast(Stance, value)


def _field(value: object, name: str) -> str:
    """Read one required string field from a stored revision value."""

    if not isinstance(value, dict):
        raise TypeError("Observation revision value must be a dictionary.")
    stored = value.get(name)
    if not isinstance(stored, str):
        raise TypeError(f"Stored field {name!r} must be a string.")
    return stored
