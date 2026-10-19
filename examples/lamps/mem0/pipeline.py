"""The 2 phases of each Mem0 write.

Mem0 does not store its input directly. A call to `add` gives it messages. Mem0
sends them through 2 model passes before it writes anything:

1. **Extraction:** reads the exchange and makes candidate facts.
2. **Update phase:** takes each candidate and gets the stored memories that are
   most similar to it. Then it returns one of 4 operations: `ADD` the candidate
   beside them, `UPDATE` one of them in place, `DELETE` one that the candidate
   contradicts, or `NONE`.

The second pass makes Mem0 a memory system and not only a store, because this
pass decides what to forget.

Upstream, both passes are model calls. The lamp runs with no model, so a
deterministic step replaces each pass:

- **Extraction:** takes the candidates that the caller made already. The other
  lamps take declared changes in the same way.
- **Decision:** uses the same 2 inputs that the model gets: the candidate, and
  the stored memories that are nearest to it. It uses an exact-repeat check on
  the Mem0 digest, a similarity floor, and the statement of the extractor about
  a contradiction of an earlier memory.

This module does not write to Genie. It returns the decisions, and the caller
applies them. Thus a caller can also print the decisions and make assertions
about them.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from examples.lamps.mem0.model import (
    Decision,
    Fact,
    Message,
    StoredMemory,
    content_hash,
    overlap,
    tokenize,
)


@dataclass(frozen=True, slots=True)
class FactExtractor:
    """Phase one: change one exchange into candidate memories.

    Attributes:
        include_roles: Roles of the turns that the extractor reads. Mem0 skips
            system messages, and this filter is the only filter here.
    """

    include_roles: tuple[str, ...] = ("user", "assistant")

    def extract(self, messages: Sequence[Message], declared: Sequence[Fact] = ()) -> tuple[Fact, ...]:
        """Return the candidate facts for one exchange.

        - **Upstream:** a model reads the turns and writes the facts.
        - **Here:** the caller supplies the facts. This phase removes the
          repeats in the call.
        - **Speaker:** if a fact has no `actor_id` and no `role`, this phase
          gives it the speaker of the last turn that the extractor reads. Thus
          the history rows tell who a memory came from.

        Args:
            messages: Turns in the exchange.
            declared: Candidates, as a model would make them.

        Returns:
            The candidates in the supplied order, without repeats.
        """

        turns = self._turns(messages)
        speaker = turns[-1] if turns else None
        found: list[Fact] = []
        seen: set[str] = set()
        for fact in declared:
            if fact.text in seen:
                continue
            seen.add(fact.text)
            if fact.actor_id is None and fact.role is None and speaker is not None:
                fact = Fact(
                    text=fact.text,
                    retracts=fact.retracts,
                    actor_id=speaker.name or speaker.role,
                    role=speaker.role,
                )
            found.append(fact)
        return tuple(found)

    def verbatim(self, messages: Sequence[Message]) -> tuple[Fact, ...]:
        """Return one candidate for each turn, and keep the exchange in its original words.

        This is the Mem0 `infer=False` path. No extraction runs. Thus each turn
        that the extractor reads becomes a memory in its own words, with its
        speaker as the actor. The extractor reads a turn if its role is in
        `include_roles`.

        Args:
            messages: Turns in the exchange.

        Returns:
            One candidate for each turn that the extractor reads, in turn order.
        """

        return tuple(
            Fact(text=message.content, actor_id=message.name or message.role, role=message.role)
            for message in self._turns(messages)
        )

    def _turns(self, messages: Sequence[Message]) -> tuple[Message, ...]:
        """Return the turns that this extractor reads."""

        return tuple(message for message in messages if message.role in self.include_roles)


@dataclass(frozen=True, slots=True)
class MemoryUpdater:
    """Phase two: decide the effect of one candidate on the stored memories.

    Attributes:
        threshold: Minimum similarity between a candidate and a stored memory.
            At or above this value, the updater treats the candidate as a
            different wording of the stored memory, not as a new memory.
            Upstream, the model makes this decision.
        neighbors: Number of nearest stored memories that the client gives to
            the updater. Upstream gives the top matches to the update prompt in
            the same way.
    """

    threshold: float = 0.5
    neighbors: int = 5

    def __post_init__(self) -> None:
        """Validate the decision bounds.

        Raises:
            TypeError: If a bound has the wrong runtime type.
            ValueError: If the threshold is not greater than zero and at most
                one, or if `neighbors` is less than one.
        """

        if isinstance(self.threshold, bool) or not isinstance(self.threshold, int | float):
            raise TypeError("threshold must be a number.")
        if not 0.0 < float(self.threshold) <= 1.0:
            raise ValueError("threshold must be greater than zero and at most one.")
        if isinstance(self.neighbors, bool) or not isinstance(self.neighbors, int):
            raise TypeError("neighbors must be an integer.")
        if self.neighbors < 1:
            raise ValueError("neighbors must be greater than zero.")

    def decide(self, fact: Fact, neighbors: Sequence[tuple[StoredMemory, float]]) -> Decision:
        """Return the operation that one candidate fact causes.

        The outcomes are the 4 events that the Mem0 update prompt selects from.
        The method checks them in the same order as that prompt:

        - **`NONE` for a repeat:** a neighbor has the same text, word for word.
          A repeat is not a change.
        - **`UPDATE`:** the similarity of the nearest neighbor is at or above
          `threshold`. The candidate replaces the wording of that neighbor.
        - **`DELETE`:** the nearest neighbor is at or above `threshold`, and the
          extractor says that the candidate contradicts it.
        - **`NONE` for a retraction with no target:** the candidate retracts,
          but no neighbor is at or above `threshold`.
        - **`ADD`:** all other cases.

        Args:
            fact: Candidate from the extraction phase.
            neighbors: Stored memories that are nearest to the candidate, the
                most similar first, as `MemoryStore.neighbors` returns them.

        Returns:
            The decision, with the memory that it applies to and the reason.
        """

        digest = content_hash(fact.text)
        repeat = next((memory for memory, _ in neighbors if memory.hash == digest), None)
        if repeat is not None:
            return Decision(
                fact=fact,
                event="NONE",
                target_id=repeat.memory_id,
                score=1.0,
                reason="the memory is already stored word for word",
            )
        nearest, score = neighbors[0] if neighbors else (None, 0.0)
        if nearest is not None and score >= self.threshold:
            if fact.retracts:
                return Decision(
                    fact=fact,
                    event="DELETE",
                    target_id=nearest.memory_id,
                    score=score,
                    reason="the candidate says the stored memory no longer holds",
                )
            return Decision(
                fact=fact,
                event="UPDATE",
                target_id=nearest.memory_id,
                score=score,
                reason="the candidate is a newer wording of the stored memory",
            )
        if fact.retracts:
            return Decision(
                fact=fact,
                event="NONE",
                target_id=None,
                score=score,
                reason="nothing close enough is on file to retract",
            )
        return Decision(
            fact=fact,
            event="ADD",
            target_id=None,
            score=score,
            reason="no stored memory is close enough to fold this into",
        )

    def similarity(self, fact: Fact, memory: StoredMemory) -> float:
        """Return the similarity of one candidate and one stored memory.

        Args:
            fact: Candidate from the extraction phase.
            memory: Stored memory for the comparison.

        Returns:
            The overlap of their token sets, from zero to one.
        """

        return overlap(tokenize(fact.text), memory.terms)
