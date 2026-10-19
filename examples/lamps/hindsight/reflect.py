"""Reflection: an answer from the highest layer that already knows.

Upstream, the Hindsight `reflect` operation runs an agent loop with a fixed
order of preference:

1. A mental model. A fresh mental model is a written answer, and it costs a
   single read.
2. Observations, because they are already consolidated.
3. Raw facts. Upstream also uses them to verify an observation that is stale.

The lamp keeps that order and removes the model call:

- **Same order:** the lamp consults the tiers in the same order.
- **Answer:** the highest tier with content writes the answer.
- **Evidence:** the reflection reports every item that it consulted as
  evidence.
- **Stale note:** the lamp does not verify a stale observation. It adds an
  `Unverified:` note to the answer.

Provenance is a `LineageDAG`. So each piece of evidence names its sources, and
the reflection stores nothing. A fact refers back to its source document, an
observation to the facts that support it, and a mental-model version to the
observations that it summarized.
"""

from __future__ import annotations

from collections.abc import Sequence

from examples.lamps.hindsight.bank import HindsightMemoryBank
from examples.lamps.hindsight.mental_models import MentalModelStore
from examples.lamps.hindsight.model import (
    Evidence,
    Reflection,
    required_text,
)
from examples.lamps.hindsight.recall import DEFAULT_MAX_TOKENS, HindsightRecall, RecallWindow

TIER_ORDER = ("search_mental_models", "search_observations", "recall")


class Reflector:
    """Answers a question from mental models first, then observations, then raw facts."""

    def __init__(
        self,
        bank: HindsightMemoryBank,
        models: MentalModelStore,
        recall: HindsightRecall,
    ) -> None:
        """Create a reflector over the retrieval layers of one bank.

        Args:
            bank: Bank that holds the facts and the provenance.
            models: Store that holds the standing answers and the observations
                that they summarize.
            recall: 4-way retrieval for the raw-fact tier.
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
            query: Optional retrieval text. The default is the question.
            entity: Optional entity where graph retrieval starts.
            tags: Scope tags. The default is the scope of the mental model.
            window: Optional time range for the temporal retriever.
            max_tokens: Token budget for the raw-fact tier.

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
