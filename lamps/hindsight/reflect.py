"""Reflection: answering from the highest layer that already knows.

Hindsight's `reflect` runs an agentic loop with a fixed preference order. It
looks for a mental model first, because a fresh one is a written answer and
costs a single read. Failing that it reads observations, which are already
consolidated. Only then does it fall back to raw facts, which it also uses to
verify an observation that has gone stale.

The lamp keeps that hierarchy and drops the model call: the tiers are consulted
in the same order, the highest tier with something to say writes the answer, and
everything consulted is reported as evidence. Because provenance is a
`LineageDAG`, each piece of evidence can name what it was derived from without
the reflection storing anything: a fact points back at its source document, an
observation at its supporting facts, a mental-model version at the observations
it summarized.
"""

from __future__ import annotations

from collections.abc import Sequence

from lamps.hindsight.bank import HindsightMemoryBank
from lamps.hindsight.mental_models import MentalModelStore
from lamps.hindsight.model import (
    Evidence,
    Reflection,
    required_text,
)
from lamps.hindsight.recall import DEFAULT_MAX_TOKENS, HindsightRecall, RecallWindow

TIER_ORDER = ("search_mental_models", "search_observations", "recall")


class Reflector:
    """Answers a question from mental models, observations, then raw facts."""

    def __init__(
        self,
        bank: HindsightMemoryBank,
        models: MentalModelStore,
        recall: HindsightRecall,
    ) -> None:
        """Create a reflector over one bank's retrieval layers.

        Args:
            bank: Bank holding facts and provenance.
            models: Store holding standing answers and the observations they
                summarize.
            recall: Four-way retrieval used for the raw-fact tier.
        """

        self.bank = bank
        self.models = models
        self.recall = recall

    def reflect(
        self,
        question: str,
        *,
        model_name: str | None = None,
        query: str | None = None,
        entity: str | None = None,
        tags: Sequence[str] = (),
        window: RecallWindow | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> Reflection:
        """Answer a question from the highest layer that can answer it.

        Args:
            question: Question to reflect on.
            model_name: Optional standing question to check first.
            query: Optional retrieval text, defaulting to the question.
            entity: Optional entity anchoring graph retrieval.
            tags: Scope tags, defaulting to the mental model's own scope.
            window: Optional time range for the temporal retriever.
            max_tokens: Budget for the raw-fact tier.

        Returns:
            The composed answer, the evidence behind it, and the tiers used.

        Raises:
            ValueError: If the question is empty.
        """

        asked = required_text(question, field="question")
        model = self.models.current(model_name) if model_name is not None else None
        scope = tuple(tags) if tags else (model.tags if model is not None else ())
        evidence: list[Evidence] = []
        answer = ""

        if model is not None and model.revision > 0:
            evidence.append(
                Evidence(
                    kind="mental_model",
                    block_id=model.id,
                    text=f"{model.name} r{model.revision}",
                    sources=model.sources,
                )
            )
            answer = model.content

        observations = self.models.in_scope(scope)
        for observation in observations:
            evidence.append(
                Evidence(
                    kind="observation",
                    block_id=observation.id,
                    text=observation.text,
                    sources=observation.sources,
                )
            )
        if not answer and observations:
            answer = " ".join(observation.text for observation in observations)

        response = self.recall.recall(
            query if query is not None else asked,
            entity=entity,
            types=("world", "experience"),
            tags=scope,
            window=window,
            max_tokens=max_tokens,
        )
        for hit in response.hits:
            evidence.append(
                Evidence(
                    kind=hit.kind,
                    block_id=hit.id,
                    text=hit.text,
                    sources=tuple(block.id for block in self.bank.provenance(hit.block)),
                )
            )
        if not answer and response.hits:
            answer = " ".join(hit.text for hit in response.hits[:2])

        stale = [observation.claim for observation in observations if observation.stale]
        if stale:
            answer = f"{answer} Unverified: {', '.join(sorted(stale))} has newer unconsolidated evidence."
        return Reflection(
            question=asked,
            text=answer or "Nothing in this bank answers that yet.",
            based_on=tuple(evidence),
            trace=TIER_ORDER,
        )
