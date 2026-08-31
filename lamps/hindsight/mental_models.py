"""Mental models: standing questions with an answer already written down.

An observation is discovered. A mental model is commissioned.

- A caller names a question once - "what should I know before changing this
  service?" - and the bank keeps an answer to it current in the background. So
  reading it at request time is a lookup instead of a retrieval and a synthesis.
- Hindsight rebuilds a model only when memories inside its scope actually
  change, and it keeps the previous versions so the drift is inspectable.

This is `VersionedRecords` almost verbatim: the standing question is the stable
identity, each rebuild is a revision, `current` is the fast read, and `history`
is the drift.

The lamp adds two rules of its own - which memories are in scope, and how the
answer is composed from them. Genie supplies the rest, including the
`LineageDAG` edges that say which observations a given version was written from.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from genie import MemoryBlock
from genie.structures import MemoryAddress, RevisionItem
from lamps.hindsight.bank import HindsightMemoryBank
from lamps.hindsight.model import (
    MENTAL_MODEL_REVISION_TYPE,
    MENTAL_MODEL_TYPE,
    MODEL_NAMESPACE,
    OBSERVATION_REVISION_TYPE,
    SEARCH_LIMIT,
    MentalModel,
    Observation,
    format_timestamp,
    normalize_timestamp,
    parse_timestamp,
    required_text,
    unique_strings,
)
from lamps.hindsight.observations import ConsolidationEngine


class MentalModelStore:
    """Standing questions whose answers are rebuilt as the bank learns."""

    def __init__(self, bank: HindsightMemoryBank, consolidation: ConsolidationEngine) -> None:
        """Create a mental-model store over one bank.

        Args:
            bank: Bank the models read from.
            consolidation: Engine supplying the observations models summarize.
        """

        self.bank = bank
        self.consolidation = consolidation

    def create(self, *, name: str, question: str, tags: Sequence[str] = ()) -> MentalModel:
        """Register a standing question, before it has any answer.

        Args:
            name: Stable model name used to fetch it later.
            question: Question this model keeps an answer to.
            tags: Scope tags. An empty scope reads the whole bank.

        Returns:
            The registered model, with no revision yet.

        Raises:
            ValueError: If the name is already registered or text is empty.
        """

        model_name = required_text(name, field="name")
        address = _model_address(model_name)
        if self.bank.structures.mental_models.identity(address) is not None:
            raise ValueError(f"Mental model {model_name!r} already exists.")
        scope = unique_strings(tags, field="tags")
        identity = self.bank.structures.tiers.create_child(
            parent=self.bank.structures.model_root,
            type=MENTAL_MODEL_TYPE,
            value={
                "name": model_name,
                "question": required_text(question, field="question"),
                "tags": list(scope),
            },
            metadata={"name": model_name, "tags": list(scope)},
        )
        self.bank.structures.mental_models.register(address, identity)
        return _model(identity, None, sources=())

    def refresh(self, name: str, *, at: datetime) -> MentalModel:
        """Rebuild one model's answer if anything in its scope changed.

        Args:
            name: Registered model name.
            at: Timestamp recorded on the revision, when one is written.

        Returns:
            The model, with a new revision only when the answer changed.

        Raises:
            ValueError: If the model is not registered.
        """

        refreshed_at = normalize_timestamp(at, field="at")
        address = _model_address(name)
        identity = self.bank.structures.mental_models.identity(address)
        if identity is None:
            raise ValueError(f"Unknown mental model {name!r}.")
        scope = _tags(identity)
        sources = self.in_scope(scope)
        content = _compose(sources)
        current = self.bank.structures.mental_models.current(address)
        if not content or (current is not None and content == _field(current.block.value, "content")):
            return self.current(name) or _model(identity, None, sources=())
        revision_block = self.bank.structures.tiers.create_child(
            parent=identity,
            type=MENTAL_MODEL_REVISION_TYPE,
            value={
                "name": _field(identity.value, "name"),
                "content": content,
                "refreshed_at": format_timestamp(refreshed_at),
            },
            metadata={"name": _field(identity.value, "name")},
        )
        self.bank.structures.mental_models.append(address, revision_block)
        for observation in sources:
            self.bank.structures.lineage.add(
                observation.block,
                revision_block,
                metadata={"role": "summarized"},
            )
        refreshed = self.current(name)
        if refreshed is None:
            raise ValueError(f"Mental model {name!r} lost its revision during refresh.")
        return refreshed

    def in_scope(self, tags: Sequence[str]) -> list[Observation]:
        """Return the observations a model with these tags summarizes.

        Args:
            tags: Model scope tags. An empty scope matches everything.

        Returns:
            Observations ordered by evidence, strongest first.

        """

        scope = set(tags)
        selected = [
            observation
            for observation in self.consolidation.observations()
            if not scope or scope.intersection(observation.tags)
        ]
        return sorted(selected, key=lambda item: (-item.proof_count, item.claim))

    def current(self, name: str) -> MentalModel | None:
        """Return the answer currently on file for a standing question.

        Args:
            name: Registered model name.

        Returns:
            The model, or `None` when it is not registered.
        """

        address = _model_address(name)
        identity = self.bank.structures.mental_models.identity(address)
        if identity is None:
            return None
        current = self.bank.structures.mental_models.current(address)
        return _model(identity, current, sources=self._sources(current))

    def history(self, name: str) -> list[MentalModel]:
        """Return every answer this question has had, oldest first.

        Args:
            name: Registered model name.

        Returns:
            Model versions in ascending revision order.
        """

        address = _model_address(name)
        identity = self.bank.structures.mental_models.identity(address)
        if identity is None:
            return []
        return [
            _model(identity, revision, sources=self._sources(revision))
            for revision in self.bank.structures.mental_models.history(address, limit=SEARCH_LIMIT)
        ]

    def models(self) -> list[MentalModel]:
        """Return every registered standing question.

        Returns:
            Models in ascending name order.
        """

        results = [
            self.current(item.binding.address.key)
            for item in self.bank.structures.mental_models.identities(
                namespace=MODEL_NAMESPACE,
                limit=SEARCH_LIMIT,
            )
        ]
        return sorted((model for model in results if model is not None), key=lambda item: item.name)

    def _sources(self, revision: RevisionItem | None) -> tuple[str, ...]:
        """Return the observation revisions one model version was written from."""

        if revision is None:
            return ()
        return tuple(
            block.id
            for block in self.bank.provenance(revision.block)
            if block.type == OBSERVATION_REVISION_TYPE
        )


def _compose(observations: Sequence[Observation]) -> str:
    """Write one model answer from the observations in its scope.

    The answer is the beliefs themselves in claim order. Leaving out both the
    proof counts and any evidence-based ordering is what makes a rebuild mean
    something: more evidence for a belief already on file changes the count but
    not the answer, so no new version is written.
    """

    ordered = sorted(observations, key=lambda observation: observation.claim)
    return " ".join(observation.text for observation in ordered)


def _model(
    identity: MemoryBlock,
    revision: RevisionItem | None,
    *,
    sources: tuple[str, ...],
) -> MentalModel:
    """Assemble one mental model from its identity and current revision."""

    value = identity.value
    if not isinstance(value, dict):
        raise TypeError("Mental model value must be a dictionary.")
    return MentalModel(
        identity=identity,
        block=None if revision is None else revision.block,
        name=_field(value, "name"),
        question=_field(value, "question"),
        content="" if revision is None else _field(revision.block.value, "content"),
        revision=0 if revision is None else revision.entry.revision,
        tags=_tags(identity),
        refreshed_at=(
            None
            if revision is None
            else parse_timestamp(_field(revision.block.value, "refreshed_at"))
        ),
        sources=sources,
    )


def _tags(identity: MemoryBlock) -> tuple[str, ...]:
    """Return the scope tags declared on a model identity."""

    value = identity.value
    if not isinstance(value, dict):
        raise TypeError("Mental model value must be a dictionary.")
    return tuple(value.get("tags", ()))


def _model_address(name: str) -> MemoryAddress:
    """Return the versioned-record address of one standing question."""

    return MemoryAddress(MODEL_NAMESPACE, required_text(name, field="name"))


def _field(value: object, name: str) -> str:
    """Read one required string field from a stored value."""

    if not isinstance(value, dict):
        raise TypeError("Mental model value must be a dictionary.")
    stored = value.get(name)
    if not isinstance(stored, str):
        raise TypeError(f"Stored field {name!r} must be a string.")
    return stored
