"""The two phases every Mem0 write goes through.

Mem0 does not store what you tell it. A call to `add` hands it messages, and it
runs them through two model passes before anything is written:

1. Extraction reads the exchange and emits candidate facts.
2. The update phase takes each candidate, fetches the stored memories most
   similar to it, and returns one of four operations: `ADD` it beside them,
   `UPDATE` one of them in place, `DELETE` one they contradict, or `NONE`.

The second pass is what makes Mem0 a memory system rather than a store, because
it is the part that decides what to forget.

Both passes are model calls upstream, and the lamp runs with no model, so both
are replaced by something deterministic:

- Extraction takes candidates the caller already formed, the way the other lamps
  take declared changes.
- The decision is computed from the same two inputs the model gets - the
  candidate and the stored memories nearest to it - using an exact-repeat check
  on Mem0's own digest, a similarity floor, and the extractor's statement of
  whether the candidate contradicts what came before.

Nothing here writes to Genie. The decisions are returned so the caller applies
them, which is also why they can be printed and asserted on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from lamps.mem0.model import (
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
    """Phase one: turn one exchange into candidate memories.

    Attributes:
        include_roles: Roles whose turns are read. Mem0 skips system messages,
            which is the whole of the filtering here.
    """

    include_roles: tuple[str, ...] = ("user", "assistant")

    def extract(self, messages: Sequence[Message], declared: Sequence[Fact] = ()) -> tuple[Fact, ...]:
        """Return the candidate facts for one exchange.

        Upstream a model reads the turns and writes the facts. Here the caller
        supplies them, and this phase does the surrounding work: it drops
        repeats within the call and credits a fact that names no speaker to the
        last turn of the exchange it came from, so the history rows say who a
        memory came from.

        Args:
            messages: Turns in the exchange.
            declared: Candidates a model would have produced.

        Returns:
            The candidates in supplied order, without repeats.
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
        """Return one candidate per turn, storing the exchange as it was said.

        This is Mem0's `infer=False` path. No extraction runs, so each kept turn
        becomes a memory in its own words, credited to whoever said it.

        Args:
            messages: Turns in the exchange.

        Returns:
            One candidate per kept turn, in turn order.
        """

        return tuple(
            Fact(text=message.content, actor_id=message.name or message.role, role=message.role)
            for message in self._turns(messages)
        )

    def _turns(self, messages: Sequence[Message]) -> tuple[Message, ...]:
        """Return the turns this extractor reads."""

        return tuple(message for message in messages if message.role in self.include_roles)


@dataclass(frozen=True, slots=True)
class MemoryUpdater:
    """Phase two: decide what one candidate does to the memories on file.

    Attributes:
        threshold: How similar a candidate and a stored memory must be for the
            candidate to be treated as another wording of it rather than as
            something new. Upstream this judgment is the model's.
        neighbors: How many of the nearest stored memories are considered, the
            way upstream passes the top matches into the update prompt.
    """

    threshold: float = 0.5
    neighbors: int = 5

    def __post_init__(self) -> None:
        """Validate the decision bounds.

        Raises:
            TypeError: If a bound has the wrong runtime type.
            ValueError: If the threshold is outside zero to one, or fewer than
                one neighbor would be read.
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
        """Return the operation one candidate fact calls for.

        The four outcomes are the ones Mem0's update prompt chooses between,
        decided in the same order it describes:

        - `NONE` when the candidate is already on file word for word, because a
          repeat is not a change.
        - `UPDATE` when it is close enough to a stored memory to replace its
          wording.
        - `DELETE` when it is that close and the extractor says it contradicts
          it.
        - `ADD` for anything else.

        Args:
            fact: Candidate from the extraction phase.
            neighbors: Stored memories nearest to it, most similar first, as
                `MemoryStore.neighbors` returns them.

        Returns:
            The decision, naming the memory it applies to and why.
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
        """Return how similar one candidate and one stored memory are.

        Args:
            fact: Candidate from the extraction phase.
            memory: Stored memory to compare against.

        Returns:
            The overlap between their token sets, between zero and one.
        """

        return overlap(tokenize(fact.text), memory.terms)
