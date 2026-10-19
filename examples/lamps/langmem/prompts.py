"""Procedural memory: the instructions of the agent, learned from feedback.

The other lamps have no equivalent of this LangMem idea.

- **What it changes:** semantic and episodic memory change what an agent knows.
  Procedural memory changes how the agent behaves. LangMem stores it as the
  prompt of the agent.
- **Loop:** the application annotates past exchanges with feedback. An
  optimizer merges that feedback into the prompt. The result controls the next
  run of the agent. So memory and configuration are no longer separate things.

`VersionedRecords` is the structure that fits:

- **Identity:** a prompt is a stable identity. So the agent always reads "the
  current code-review prompt", not a specific text.
- **Revision:** each optimization is a revision. If a revision makes the prompt
  worse, a reader can read the version before it.
- **Lineage:** `LineageDAG` edges go from the annotated exchanges to the
  revision that they produced. They answer a question that a versioned prompt
  alone cannot answer: not only what changed, but which piece of feedback
  changed it.

Upstream, a language model rewrites the prompt from the feedback with one of 3
strategies. The lamp keeps the shape of the strategy with the lowest cost,
`prompt_memory`. That strategy takes successful patterns from past
interactions into the next prompt:

- **Keyed rules:** feedback names a keyed rule. A rule replaces the earlier rule
  with the same key, so rules with the same key do not accumulate.
- **Prompt text:** the base text plus its rules, in acquisition order.
- **New revision:** the optimizer writes a revision only if the rule set really
  changes. So feedback that repeats the prompt adds no version.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, cast

from examples.lamps.langmem.model import (
    PROMPT_NAMESPACE,
    PROMPT_REVISION_TYPE,
    PROMPT_TYPE,
    SEARCH_LIMIT,
    TRAJECTORY_TYPE,
    AnnotatedTrajectory,
    PromptRule,
    PromptVersion,
    compose_prompt,
    format_timestamp,
    normalize_timestamp,
    required_text,
)
from examples.lamps.langmem.structures import LangMemStructures
from genie import MemoryBlock, MemoryHandle
from genie.structures import MemoryAddress, RevisionItem


class PromptOptimizer:
    """The prompts that an agent uses, and the feedback that revised them."""

    def __init__(self, memory: MemoryHandle, *, structures: LangMemStructures) -> None:
        """Create an optimizer over one bounded handle.

        Args:
            memory: Bounded Genie handle that owns the prompt and trajectory
                tiers.
            structures: Facade bundle on the same handle.
        """

        self.memory = memory
        self.structures = structures

    def register(self, name: str, *, prompt: str, when_to_update: str = "") -> PromptVersion:
        """Register a prompt before feedback changes it.

        Args:
            name: Stable prompt name. The agent reads the prompt by this name.
            prompt: Prompt text that a person wrote.
            when_to_update: Condition for the optimizer. It tells when this
                prompt changes. The store keeps it for a model to read. The
                lamp itself accepts a change by the rule key, not by this
                condition.

        Returns:
            The prompt at revision 0, which is only the base text.

        Raises:
            ValueError: If the name is empty or already registered, or the text
                is empty.
        """

        prompt_name = required_text(name, field="name")
        address = _address(prompt_name)
        if self.structures.prompts.identity(address) is not None:
            raise ValueError(f"Prompt {prompt_name!r} already exists.")
        identity = self.structures.tiers.create_child(
            parent=self.structures.prompt_root,
            type=PROMPT_TYPE,
            value={
                "name": prompt_name,
                "base": required_text(prompt, field="prompt"),
                "when_to_update": when_to_update.strip(),
            },
            metadata={"name": prompt_name},
        )
        self.structures.prompts.register(address, identity)
        return _version(identity, None, sources=())

    def optimize(
        self,
        name: str,
        trajectories: Sequence[AnnotatedTrajectory],
        *,
        at: datetime,
    ) -> PromptVersion:
        """Merge annotated exchanges into a prompt, if they change its rules.

        - **Record:** the optimizer stores each exchange, or reuses the record
          with the same name.
        - **Credit:** only the exchanges that changed the rule set get a
          lineage edge to the new revision.

        Args:
            name: Registered prompt name.
            trajectories: Past exchanges and the feedback to learn from.
            at: Timestamp for the revision, if the optimizer writes one.

        Returns:
            The prompt after the optimization. Its `revision` does not change
            if the feedback asked for nothing new.

        Raises:
            ValueError: If the prompt is not registered.
        """

        optimized_at = normalize_timestamp(at, field="at")
        address = _address(name)
        identity = self.structures.prompts.identity(address)
        if identity is None:
            raise ValueError(f"Unknown prompt {name!r}.")
        current = self.structures.prompts.current(address)
        rules = list(_rules(current))
        recorded = [self._record(trajectory) for trajectory in trajectories]
        contributors = [
            block
            for trajectory, block in zip(trajectories, recorded, strict=True)
            if _fold(rules, trajectory.rules)
        ]
        if not contributors:
            return self.current(name) or _version(identity, None, sources=())
        revision_block = self.structures.tiers.create_child(
            parent=identity,
            type=PROMPT_REVISION_TYPE,
            value={
                "name": _field(identity, "name"),
                "base": _field(identity, "base"),
                "text": compose_prompt(_field(identity, "base"), rules),
                "rules": [{"key": rule.key, "text": rule.text} for rule in rules],
                "optimized_at": format_timestamp(optimized_at),
            },
            metadata={"name": _field(identity, "name")},
        )
        self.structures.prompts.append(address, revision_block)
        for block in contributors:
            self.structures.lineage.add(block, revision_block, metadata={"role": "feedback"})
        optimized = self.current(name)
        if optimized is None:
            raise ValueError(f"Prompt {name!r} lost its revision during optimization.")
        return optimized

    def current(self, name: str) -> PromptVersion | None:
        """Return the prompt version that the agent uses now.

        Args:
            name: Registered prompt name.

        Returns:
            The prompt, or `None` if it is not registered.
        """

        address = _address(name)
        identity = self.structures.prompts.identity(address)
        if identity is None:
            return None
        revision = self.structures.prompts.current(address)
        return _version(identity, revision, sources=self._sources(revision))

    def history(self, name: str) -> list[PromptVersion]:
        """Return all versions of a prompt, oldest first.

        Args:
            name: Registered prompt name.

        Returns:
            Versions in ascending revision order. The list is empty if the
            prompt is not registered.
        """

        address = _address(name)
        identity = self.structures.prompts.identity(address)
        if identity is None:
            return []
        return [
            _version(identity, revision, sources=self._sources(revision))
            for revision in self.structures.prompts.history(address, limit=SEARCH_LIMIT)
        ]

    def prompts(self) -> list[PromptVersion]:
        """Return the current version of each registered prompt.

        Returns:
            Prompts in ascending name order.
        """

        found = [
            self.current(entry.binding.address.key)
            for entry in self.structures.prompts.identities(namespace=PROMPT_NAMESPACE, limit=SEARCH_LIMIT)
        ]
        return sorted((prompt for prompt in found if prompt is not None), key=lambda prompt: prompt.name)

    def trajectory(self, name: str) -> MemoryBlock | None:
        """Return the stored record of one annotated exchange.

        Args:
            name: Trajectory name.

        Returns:
            The trajectory block, or `None` if the optimizer never recorded it.
        """

        target = required_text(name, field="name")
        return next(
            (
                child
                for child in self.structures.tiers.children(self.structures.trajectories)
                if child.type == TRAJECTORY_TYPE and child.metadata.get("name") == target
            ),
            None,
        )

    def _record(self, trajectory: AnnotatedTrajectory) -> MemoryBlock:
        """Store one annotated exchange, or return the record with the same name if it exists."""

        name = required_text(trajectory.name, field="name")
        existing = self.trajectory(name)
        if existing is not None:
            return existing
        return self.structures.tiers.create_child(
            parent=self.structures.trajectories,
            type=TRAJECTORY_TYPE,
            value={
                "name": name,
                "messages": [message.as_json() for message in trajectory.messages],
                "feedback": trajectory.feedback,
                "rules": [{"key": rule.key, "text": rule.text} for rule in trajectory.rules],
            },
            metadata={"name": name},
        )

    def _sources(self, revision: RevisionItem | None) -> tuple[str, ...]:
        """Return the names of the exchanges that produced one version, in name order."""

        if revision is None:
            return ()
        names = [
            required_text(neighbor.block.value.get("name"), field="name")
            for neighbor in self.structures.lineage.predecessors(revision.block)
            if neighbor.block.type == TRAJECTORY_TYPE and isinstance(neighbor.block.value, dict)
        ]
        return tuple(sorted(names))


def _fold(rules: list[PromptRule], proposed: Sequence[PromptRule]) -> bool:
    """Merge the rules of one exchange into the rule set of a prompt, in place.

    Returns:
        `True` if the rule set changed. This value decides if the optimizer
        writes a new version, and if it credits this exchange for the version.
    """

    changed = False
    for rule in proposed:
        position = next((index for index, existing in enumerate(rules) if existing.key == rule.key), None)
        if position is None:
            rules.append(rule)
            changed = True
        elif rules[position].text != rule.text:
            rules[position] = rule
            changed = True
    return changed


def _rules(revision: RevisionItem | None) -> tuple[PromptRule, ...]:
    """Return the rules of one prompt version."""

    if revision is None or not isinstance(revision.block.value, dict):
        return ()
    stored = cast(dict[str, Any], revision.block.value).get("rules", ())
    return tuple(
        PromptRule(
            key=required_text(entry.get("key"), field="rule key"),
            text=required_text(entry.get("text"), field="rule text"),
        )
        for entry in stored
    )


def _version(
    identity: MemoryBlock,
    revision: RevisionItem | None,
    *,
    sources: tuple[str, ...],
) -> PromptVersion:
    """Assemble one prompt version from its identity and a revision."""

    base = _field(identity, "base")
    rules = _rules(revision)
    return PromptVersion(
        identity=identity,
        block=None if revision is None else revision.block,
        name=_field(identity, "name"),
        base=base,
        text=compose_prompt(base, rules),
        rules=rules,
        revision=0 if revision is None else revision.entry.revision,
        when_to_update=cast(str, _payload(identity).get("when_to_update", "")),
        sources=sources,
    )


def _address(name: str) -> MemoryAddress:
    """Return the versioned-record address of one prompt."""

    return MemoryAddress(PROMPT_NAMESPACE, required_text(name, field="name"))


def _field(block: MemoryBlock, name: str) -> str:
    """Read one required string field from the stored value of a block."""

    return required_text(_payload(block).get(name), field=name)


def _payload(block: MemoryBlock) -> dict[str, Any]:
    """Return the dictionary value of a prompt block."""

    if not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} does not hold a prompt value.")
    return cast(dict[str, Any], block.value)
