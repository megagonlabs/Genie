"""Mental models: standing questions with an answer that is already written.

Consolidation finds an observation in the facts. A caller asks for a mental
model on purpose.

- **Standing question:** a caller names a question once, for example "what is
  important before I change this service?". Upstream Hindsight keeps an
  answer to it current in the background. So a read at request time is a
  lookup, not a retrieval followed by a synthesis.
- **Rebuild rule:** Hindsight rebuilds a model only when memories inside its
  scope really change. It keeps the previous versions, so a user can examine
  the drift.
- **Here:** the lamp rebuilds a model with an explicit `refresh` call, not in
  the background.

This is almost exactly `VersionedRecords`:

- **Stable identity:** the standing question.
- **Revision:** each rebuild.
- **`current`:** the fast read.
- **`history`:** the drift.

The lamp adds 2 rules of its own: which memories are in scope, and how the lamp
composes the answer from them. Genie supplies the rest. This includes the
`LineageDAG` edges that show which observations a version was written from.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from examples.lamps.hindsight.bank import HindsightMemoryBank
from examples.lamps.hindsight.model import (
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
from examples.lamps.hindsight.observations import ConsolidationEngine
from genie import MemoryBlock
from genie.structures import MemoryAddress, RevisionItem


class MentalModelStore:
    """Standing questions, with answers that the store rebuilds when the bank learns."""

    def __init__(self, bank: HindsightMemoryBank, consolidation: ConsolidationEngine) -> None:
        """Create a mental-model store over one bank.

        Args:
            bank: Bank that the models read from.
            consolidation: Engine that supplies the observations that the
                models summarize.
        """

        self.bank = bank
        self.consolidation = consolidation

    def create(self, *, name: str, question: str, tags: Sequence[str] = ()) -> MentalModel:
        """Register a standing question, before it has any answer.

        Args:
            name: Stable model name for later reads.
            question: Question that this model keeps an answer for.
            tags: Scope tags. An empty scope reads the whole bank.

        Returns:
            The registered model, with no revision yet.

        Raises:
            ValueError: If the name is already registered, or a text is empty.
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
        """Compose the answer of one model again, and write a revision only if the answer changed.

        - **Rebuild rule:** the method composes the answer from the
          observations in scope. It writes a new revision only if the new
          answer is not empty and is different from the current answer.
        - **No rebuild for more evidence:** more evidence for a known belief
          does not change the answer. So it writes no revision.
        - **Lineage:** a new revision gets a lineage edge from each observation
          in scope.

        Args:
            name: Registered model name.
            at: Timestamp that the method records on a new revision.

        Returns:
            The model. It has a new revision only if the answer changed.

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
        """Return the observations that a model with these tags summarizes.

        Args:
            tags: Model scope tags. An empty scope matches all observations.
                Otherwise, an observation matches if it has at least one of
                the tags.

        Returns:
            Observations with the highest proof count first, then in claim
            order.
        """

        scope = set(tags)
        selected = [
            observation
            for observation in self.consolidation.observations()
            if not scope or scope.intersection(observation.tags)
        ]
        return sorted(selected, key=lambda item: (-item.proof_count, item.claim))

    def current(self, name: str) -> MentalModel | None:
        """Return the current stored answer of a standing question.

        Args:
            name: Registered model name.

        Returns:
            The model, or `None` if it is not registered.
        """

        address = _model_address(name)
        identity = self.bank.structures.mental_models.identity(address)
        if identity is None:
            return None
        current = self.bank.structures.mental_models.current(address)
        return _model(identity, current, sources=self._sources(current))

    def history(self, name: str) -> list[MentalModel]:
        """Return every answer of this question, oldest first.

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
        """Return the ids of the observation revisions that one model version was written from."""

        if revision is None:
            return ()
        return tuple(
            block.id
            for block in self.bank.provenance(revision.block)
            if block.type == OBSERVATION_REVISION_TYPE
        )


def _compose(observations: Sequence[Observation]) -> str:
    """Write one model answer from the observations in its scope.

    - **Content:** the answer is the beliefs themselves, in claim order.
    - **Why:** the answer has no proof counts and no order by evidence. So more
      evidence for a known belief changes the count but not the answer, and the
      store writes no new version. Thus a new version always means a new answer.
    """

    ordered = sorted(observations, key=lambda observation: observation.claim)
    return " ".join(observation.text for observation in ordered)


def _model(
    identity: MemoryBlock,
    revision: RevisionItem | None,
    *,
    sources: tuple[str, ...],
) -> MentalModel:
    """Assemble one mental model from its identity and one revision."""

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
