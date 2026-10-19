"""Consolidation of retained facts into beliefs that survive new evidence.

Observations make Hindsight more than a store of sentences. Consolidation merges
related facts into one deduplicated belief with a proof count:

- **Strengthen:** more evidence makes the belief stronger.
- **Extend:** evidence with new detail adds to the wording of the belief.
- **Reconcile:** when the evidence reverses, the belief records the full
  history and the lamp does not overwrite it.

The behavior uses 3 Genie structures:

- **`VersionedRecords`:** gives each belief a stable identity and an ordered
  history. So "what did we think before?" is a read, not an audit table that the
  lamp keeps.
- **`LineageDAG`:** holds the evidence edges. So the lamp gets the proof count
  from the graph. It does not store the count and keep it in sync.
- **Retention `EventLog`:** its sequence numbers are the cursor positions.
  Consolidation reads the facts appended since its last run. The same cursor
  also tells an observation that it is stale.

The lamp supplies the decision that Hindsight asks a language model for. Each
extracted fact names its belief and says if it supports or contradicts that
belief. So the groups of facts and the reconciliation stay deterministic.
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
    """Merges retained facts into versioned observations with evidence."""

    def __init__(self, bank: HindsightMemoryBank) -> None:
        """Create a consolidation engine over one memory bank.

        Args:
            bank: Bank with the retained facts to consolidate.
        """

        self.bank = bank

    def consolidate(self, *, at: datetime) -> list[Observation]:
        """Merge every fact retained since the last run into observations.

        - **Upstream:** this work runs in the background after each `retain`.
        - **Here:** it is an explicit call, so the example stays deterministic.
          The unit of work is the same: the facts in the retention log at or
          after the stored cursor.
        - **Skipped facts:** invalidated facts and facts without a claim change
          no observation, but the cursor moves past them.
        - **Cursor first:** the method moves the cursor before it writes the
          beliefs. So an observation that this run refreshes does not see its
          own evidence as unconsolidated, and does not report itself as stale.

        Args:
            at: Timestamp that the method records on the revisions of this run.

        Returns:
            The observations that this run created or refreshed, in claim order.
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
        """Return the retention sequence where the next consolidation starts.

        Returns:
            The next unconsolidated log sequence.
        """

        anchor = self.bank.memory.get(self.bank.structures.observation_root.id)
        if anchor is None or not isinstance(anchor.value, dict):
            return 0
        stored = anchor.value.get(CURSOR_FIELD, 0)
        return stored if isinstance(stored, int) else 0

    def pending(self) -> list[MemoryFact]:
        """Return the valid facts that have a claim and are not consolidated yet.

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
            The current observation, or `None` if the belief is unknown.
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
        """Return every wording of this belief, oldest first.

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
        """Create or refine one belief from the facts about it.

        If the text and the tags do not change, the method adds only the
        evidence edges and writes no revision.
        """

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
        """Link the facts that support the belief, and the evidence carried from the previous revision, to a revision."""

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
        """Return `True` if valid facts that are not consolidated yet still name this belief."""

        return any(fact.claim is not None and fact.claim.key == claim for fact in self.pending())

    def _set_cursor(self, sequence: int) -> None:
        """Record the retention sequence where the next consolidation starts."""

        self.bank.memory.update(
            self.bank.structures.observation_root,
            value={CURSOR_FIELD: sequence},
        )


def _reconcile(previous: str, facts: Sequence[MemoryFact]) -> tuple[str, Stance]:
    """Merge new evidence into a belief, and return the new text and stance.

    - **New belief:** if the belief has no wording yet, the first fact sets it.
    - **Strengthen:** a fact whose belief text is already in the wording does
      not change the wording.
    - **Extend:** a fact that supports the belief and has new content adds
      `Also: <belief>` to the wording.
    - **Reconcile:** a fact that contradicts the belief puts the new belief
      first, then `Previously:` and the old wording. So the old wording stays
      visible, and the stance becomes `contradicts`.
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
    """Return the tags of a revision plus the tags of its new evidence, without duplicates."""

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
