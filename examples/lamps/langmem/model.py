"""Value types, constants, and helpers for the LangMem lamp.

LangMem stores everything at an address: a hierarchical namespace plus a key
inside it. The namespace is a template such as `("memories", "{user_id}")`
that is filled in at request time, so one configured tool set serves every
user without the tool arguments ever naming one.

This module holds the four things the rest of the lamp shares: the block types
that keep the four memory kinds apart, the address helpers that resolve and
compare namespaces, the read-only records the stores hand back, and the
timestamp and token helpers. Nothing here writes to Genie, so every other
module can depend on it without a cycle.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal, cast

from genie import MemoryBlock

STORE_TYPE = "langmem.store"
ANCHOR_TYPE = "langmem.anchor"
NAMESPACE_TYPE = "langmem.namespace"
ITEM_TYPE = "langmem.item"
PROFILE_TYPE = "langmem.profile"
PROFILE_REVISION_TYPE = "langmem.profile_revision"
PROMPT_TYPE = "langmem.prompt"
PROMPT_REVISION_TYPE = "langmem.prompt_revision"
TRAJECTORY_TYPE = "langmem.trajectory"
SUBMISSION_TYPE = "langmem.submission"

DERIVED_FROM_RELATION = "langmem.derived_from"

TIERS = ("namespaces", "profiles", "prompts", "trajectories", "queue")

PROMPT_NAMESPACE = ("prompts",)

PatchAction = Literal["insert", "update", "delete"]
PATCH_ACTIONS: frozenset[str] = frozenset({"insert", "update", "delete"})

SubmissionState = Literal["pending", "superseded", "processed"]

Namespace = tuple[str, ...]

SEARCH_LIMIT = 1_000

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+")
_PLACEHOLDER_PATTERN = re.compile(r"^\{([a-z0-9_]+)\}$")


def normalize_timestamp(value: datetime, *, field: str) -> datetime:
    """Return a timezone-aware timestamp normalized to UTC.

    Args:
        value: Timestamp supplied by application code.
        field: Field name used in error messages.

    Returns:
        The same instant expressed in UTC.

    Raises:
        TypeError: If `value` is not a `datetime`.
        ValueError: If `value` has no timezone.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{field} must be a datetime.")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware.")
    return value.astimezone(UTC)


def format_timestamp(value: datetime) -> str:
    """Format a timestamp as a JSON-compatible ISO 8601 string in UTC.

    Args:
        value: Timezone-aware timestamp.

    Returns:
        An ISO 8601 string ending in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str = "timestamp") -> datetime:
    """Parse a timezone-aware ISO 8601 string into a UTC timestamp.

    Args:
        value: ISO 8601 timestamp string.
        field: Field name used in error messages.

    Returns:
        The parsed instant in UTC.

    Raises:
        ValueError: If the text is not a timezone-aware ISO 8601 timestamp.
    """

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} must be an ISO 8601 timestamp.") from error
    return normalize_timestamp(parsed, field=field)


def required_text(value: object, *, field: str) -> str:
    """Return stripped required text.

    Args:
        value: Candidate text.
        field: Field name used in error messages.

    Returns:
        The text without surrounding whitespace.

    Raises:
        ValueError: If the value is not a non-empty string.
    """

    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} cannot be empty.")
    return value.strip()


def unique_strings(values: object, *, field: str) -> tuple[str, ...]:
    """Return unique non-empty strings in input order.

    Args:
        values: Iterable of candidate strings.
        field: Field name used in error messages.

    Returns:
        Unique stripped strings.

    Raises:
        TypeError: If `values` is not a sequence of strings.
        ValueError: If any entry is empty.
    """

    if isinstance(values, str | bytes) or not isinstance(values, Iterable):
        raise TypeError(f"{field} must be a sequence of strings.")
    selected: list[str] = []
    for value in values:
        item = required_text(value, field=field)
        if item not in selected:
            selected.append(item)
    return tuple(selected)


def tokenize(value: str) -> tuple[str, ...]:
    """Split text into lowercase alphanumeric tokens in first-seen order.

    Search terms keep their order so that a query produces the same sequence of
    Genie reads on every run.

    Args:
        value: Text to tokenize.

    Returns:
        The distinct tokens found in the text.
    """

    seen: list[str] = []
    for token in _TOKEN_PATTERN.findall(value.casefold()):
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def namespace(*segments: str) -> Namespace:
    """Return a validated namespace from its segments.

    Args:
        segments: One or more namespace segments, template or literal.

    Returns:
        The namespace as a tuple.

    Raises:
        ValueError: If no segment is given or a segment is empty or contains a
            path separator.
    """

    if not segments:
        raise ValueError("A namespace needs at least one segment.")
    return tuple(_segment(value) for value in segments)


def resolve_namespace(template: Sequence[str], values: Mapping[str, str]) -> Namespace:
    """Fill a namespace template's placeholders from request-scoped values.

    LangMem writes a namespace once, as `("memories", "{user_id}")`, and fills
    the placeholder from configuration on each call. Keeping the substitution
    here means the tool arguments never carry the user id, so a tool set bound
    to one user cannot read another user's namespace.

    Args:
        template: Namespace whose segments may be `{placeholder}` names.
        values: Placeholder names mapped to their runtime values.

    Returns:
        The namespace with every placeholder replaced.

    Raises:
        KeyError: If a placeholder has no supplied value.
        ValueError: If a segment is empty or contains a path separator.
    """

    resolved: list[str] = []
    for value in template:
        segment = _segment(value)
        match = _PLACEHOLDER_PATTERN.match(segment)
        if match is None:
            resolved.append(segment)
            continue
        name = match.group(1)
        if name not in values:
            raise KeyError(f"Namespace placeholder {segment!r} has no supplied value.")
        resolved.append(_segment(values[name]))
    return namespace(*resolved)


def namespace_path(value: Sequence[str]) -> str:
    """Return the printable slash-separated form of a namespace.

    Args:
        value: Namespace segments.

    Returns:
        The segments joined with `/`.
    """

    return "/".join(value)


def is_below(value: Namespace, prefix: Namespace) -> bool:
    """Return whether one namespace sits at or below a prefix.

    Args:
        value: Namespace being tested.
        prefix: Candidate prefix.

    Returns:
        `True` when `prefix` is a prefix of `value`.
    """

    return value[: len(prefix)] == prefix


class ValueSchema:
    """Runtime schema supplied for the values stored under one namespace.

    LangMem takes Pydantic models in `schemas=[...]` and uses them both to
    shape extraction and to name the memory kind. Genie stores whatever its
    codec accepts and has no opinion on value shape, so the schema lives here
    in application code. This dependency-free stand-in checks required fields
    and their types; a real application would pass its own Pydantic models.
    """

    __slots__ = ("_fields", "name")

    def __init__(self, name: str, fields: Mapping[str, type]) -> None:
        """Create an exact dictionary schema.

        Args:
            name: Schema name recorded in block metadata and filterable in
                search, the way a LangMem schema names a memory kind.
            fields: Required field names and their expected Python types.

        Raises:
            TypeError: If a field does not declare a Python type.
            ValueError: If the name is empty or no field is declared.
        """

        self.name = required_text(name, field="schema name")
        if not fields:
            raise ValueError("A value schema requires at least one field.")
        normalized: dict[str, type] = {}
        for field_name, expected in fields.items():
            key = required_text(field_name, field="schema field")
            if not isinstance(expected, type):
                raise TypeError(f"Schema field {key!r} must use a Python type.")
            normalized[key] = expected
        self._fields = normalized

    @property
    def fields(self) -> dict[str, type]:
        """Return an isolated copy of the schema's field mapping."""

        return dict(self._fields)

    def validate(self, value: object) -> dict[str, Any]:
        """Validate one value and return an isolated copy of it.

        Args:
            value: Candidate value for this namespace.

        Returns:
            A deep copy of the validated dictionary.

        Raises:
            TypeError: If the value is not a dictionary or a field has the
                wrong type.
            ValueError: If a required field is missing or an extra field is
                supplied.
        """

        if not isinstance(value, dict):
            raise TypeError(f"Schema {self.name!r} requires a dictionary value.")
        missing = self._fields.keys() - value.keys()
        extra = value.keys() - self._fields.keys()
        if missing:
            raise ValueError(f"Schema {self.name!r} is missing fields: {', '.join(sorted(missing))}.")
        if extra:
            raise ValueError(f"Schema {self.name!r} does not define fields: {', '.join(sorted(extra))}.")
        for field_name, expected in self._fields.items():
            if not isinstance(value[field_name], expected):
                raise TypeError(f"Schema {self.name!r} field {field_name!r} must be {expected.__name__}.")
        return deepcopy(cast(dict[str, Any], value))


@dataclass(frozen=True, slots=True)
class Message:
    """One conversation turn the manager reads.

    Attributes:
        role: Speaker role, such as `user` or `assistant`.
        content: Message text.
    """

    role: str
    content: str

    def as_json(self) -> dict[str, str]:
        """Return the message in the shape LangMem submits."""

        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """A resolved `(namespace, key, value)` item.

    Attributes:
        block: Genie block holding the item.
        namespace: Resolved namespace the item is addressed in.
        key: Key inside that namespace.
        value: Stored application value.
        schema_name: Registered schema for the namespace, when there is one.
    """

    block: MemoryBlock
    namespace: Namespace
    key: str
    value: Any
    schema_name: str | None

    @property
    def id(self) -> str:
        """Return the stored item id, which is the Genie block id."""

        return self.block.id

    @property
    def version(self) -> int:
        """Return how many times the item has been written.

        LangMem keeps a change history for an updated memory. Genie counts
        writes to one block, so an in-place patch is visible here rather than
        in an audit record the lamp maintains.
        """

        return self.block.version

    @property
    def address(self) -> str:
        """Return the printable `namespace/key` address."""

        return f"{namespace_path(self.namespace)}/{self.key}"

    def canonical_value(self) -> dict[str, Any]:
        """Return the exact namespace/key/value wrapper stored in Genie."""

        return deepcopy(cast(dict[str, Any], self.block.value))


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One item returned by the lamp's lexical retrieval.

    Attributes:
        item: Matching item.
        score: Share of query terms Genie matched in the item.
        matched: Query terms that matched, in query order.
    """

    item: MemoryItem
    score: float
    matched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Patch:
    """One change a memory manager proposes to the store.

    LangMem's manager returns the changes it wants rather than writing them, so
    inserts, updates, and deletes can be permitted separately. This is that
    proposal.

    Attributes:
        action: `insert`, `update`, or `delete`.
        collection: Namespace segment below the bound namespace, such as
            `semantic` or `episodes`.
        key: Key inside that namespace.
        value: New value. Required except for a delete.
    """

    action: PatchAction
    collection: str
    key: str
    value: Any | None = None

    def __post_init__(self) -> None:
        """Validate the proposed change.

        Raises:
            ValueError: If the action is unknown, the collection or key is
                empty, or a value is missing or supplied where it must not be.
        """

        if self.action not in PATCH_ACTIONS:
            raise ValueError(f"Patch action must be one of {sorted(PATCH_ACTIONS)}.")
        required_text(self.collection, field="collection")
        required_text(self.key, field="key")
        if self.action == "delete" and self.value is not None:
            raise ValueError("A delete patch must not carry a value.")
        if self.action != "delete" and self.value is None:
            raise ValueError(f"A {self.action} patch requires a value.")


@dataclass(frozen=True, slots=True)
class PatchOutcome:
    """What the store did with one proposed change.

    Attributes:
        patch: Change the manager proposed.
        action: Change that was actually made, which differs from the proposal
            when an insert lands on an address that already holds an item.
        applied: Whether the store was written.
        reason: Plain-language explanation, including why a change was refused.
        item: Item after the change, or the deleted item, when there is one.
    """

    patch: Patch
    action: PatchAction
    applied: bool
    reason: str
    item: MemoryItem | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    """A conversation submitted for memory formation.

    Attributes:
        thread: `Conversation` thread. Submissions debounce per thread.
        messages: Turns in the exchange.
        patches: Changes an extractor proposed from those turns.
        profile: Profile fields the exchange revises.
    """

    thread: str
    messages: tuple[Message, ...]
    patches: tuple[Patch, ...] = ()
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Submission:
    """One deferred memory-formation task on the reflection queue.

    Attributes:
        block: Genie block recording the submission.
        sequence: Position in the append-only queue log.
        thread: `Conversation` thread this submission belongs to.
        state: `pending`, `superseded`, or `processed`.
        submitted_at: When the conversation was handed to the queue.
        due_at: When the manager may run it.
        messages: Accumulated turns, including any carried from a superseded
            submission for the same thread.
        patches: Accumulated proposed changes.
        profile: Accumulated profile revisions.
        superseded_by: Id of the submission that replaced this one.
    """

    block: MemoryBlock
    sequence: int
    thread: str
    state: SubmissionState
    submitted_at: datetime
    due_at: datetime
    messages: tuple[Message, ...]
    patches: tuple[Patch, ...]
    profile: dict[str, Any]
    superseded_by: str | None = None


@dataclass(frozen=True, slots=True)
class ManagerRun:
    """The result of running the memory manager over one conversation.

    Attributes:
        thread: `Conversation` thread that was processed.
        source_id: Block the conversation was recorded in, when the run came
            from the queue rather than from a hot-path tool call.
        ran_at: When the manager ran.
        outcomes: What happened to every proposed change, in proposal order.
        profile: Profile after the run, when the conversation revised one. It
            is the profile as it still stands when the revision was refused.
        profile_reason: Why the profile revision was refused, when it was.
    """

    thread: str
    source_id: str | None
    ran_at: datetime
    outcomes: tuple[PatchOutcome, ...]
    profile: Profile | None = None
    profile_reason: str | None = None

    @property
    def applied(self) -> tuple[PatchOutcome, ...]:
        """Return the changes that were written."""

        return tuple(outcome for outcome in self.outcomes if outcome.applied)

    @property
    def refused(self) -> tuple[PatchOutcome, ...]:
        """Return the changes the manager was not permitted to make."""

        return tuple(outcome for outcome in self.outcomes if not outcome.applied)


@dataclass(frozen=True, slots=True)
class Profile:
    """The single continuously-updated document for one namespace.

    Attributes:
        identity: Stable block the revisions hang from.
        block: Revision holding the current value.
        namespace: Namespace the profile belongs to.
        name: Profile name inside that namespace.
        value: Current merged value.
        revision: Revision number, counting from one.
        updated_at: When this revision was written.
    """

    identity: MemoryBlock
    block: MemoryBlock
    namespace: Namespace
    name: str
    value: dict[str, Any]
    revision: int
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class PromptRule:
    """One standing instruction in a prompt's rule set.

    Attributes:
        key: Stable rule name. A later rule with the same key replaces this
            one instead of being appended beside it.
        text: Instruction as it appears in the composed prompt.
    """

    key: str
    text: str

    def __post_init__(self) -> None:
        """Validate the rule.

        Raises:
            ValueError: If the key or text is empty.
        """

        required_text(self.key, field="rule key")
        required_text(self.text, field="rule text")


@dataclass(frozen=True, slots=True)
class AnnotatedTrajectory:
    """A past exchange plus the feedback the optimizer learns from.

    Attributes:
        name: Stable trajectory name, used to report provenance.
        messages: Turns in the exchange.
        feedback: What the reviewer said about it.
        rules: Rules the feedback asks the prompt to carry.
    """

    name: str
    messages: tuple[Message, ...]
    feedback: str
    rules: tuple[PromptRule, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """One version of an agent's procedural memory.

    Attributes:
        identity: Stable block naming the prompt.
        block: Revision holding this version, absent before the first one.
        name: Prompt name.
        base: Prompt text before any learned rule is appended.
        text: Composed prompt the agent would run with.
        rules: Rules carried by this version, in acquisition order.
        revision: Revision number, or zero before the first optimization.
        when_to_update: Condition recorded for the optimizer that reads it.
        sources: Names of the annotated exchanges this version was written
            from, read off the lineage edges into its revision.
    """

    identity: MemoryBlock
    block: MemoryBlock | None
    name: str
    base: str
    text: str
    rules: tuple[PromptRule, ...]
    revision: int
    when_to_update: str
    sources: tuple[str, ...] = ()


def compose_prompt(base: str, rules: Sequence[PromptRule]) -> str:
    """Return the prompt text an agent runs with.

    Args:
        base: Prompt as first registered.
        rules: Rules learned from feedback, in acquisition order.

    Returns:
        The base prompt followed by its numbered rules.
    """

    if not rules:
        return base
    lines = [base, "Rules learned from feedback:"]
    lines.extend(f"{index}. {rule.text}" for index, rule in enumerate(rules, start=1))
    return "\n".join(lines)


def item_from_block(block: MemoryBlock) -> MemoryItem:
    """Decode one stored item wrapper.

    Args:
        block: Block holding a `namespace`/`key`/`value` wrapper.

    Returns:
        The resolved item.

    Raises:
        TypeError: If the block is not a stored item.
    """

    if block.type != ITEM_TYPE or not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} is not a LangMem item.")
    payload = cast(dict[str, Any], block.value)
    if set(payload) != {"namespace", "key", "value"}:
        raise TypeError(f"Block {block.id!r} does not hold a namespace/key/value item.")
    stored = payload["namespace"]
    if not isinstance(stored, list):
        raise TypeError(f"Block {block.id!r} has an invalid namespace.")
    schema_name = block.metadata.get("schema")
    return MemoryItem(
        block=block,
        namespace=namespace(*cast(list[str], stored)),
        key=required_text(payload["key"], field="key"),
        value=deepcopy(payload["value"]),
        schema_name=cast(str | None, schema_name),
    )


def item_metadata(value: Namespace, key: str, *, schema_name: str | None) -> dict[str, Any]:
    """Return the portable index metadata stored beside an item.

    The address is duplicated into metadata so Genie can filter and scope a
    search without the lamp decoding every candidate value first. `schema` is
    what LangMem's search `filter` narrows on when several schemas share a
    namespace.

    Args:
        value: Resolved namespace.
        key: Key inside that namespace.
        schema_name: Registered schema name, when the namespace has one.

    Returns:
        Metadata for the item block.
    """

    return {
        "namespace": list(value),
        "namespace_path": namespace_path(value),
        "collection": value[-1],
        "key": key,
        "schema": schema_name,
    }


def patch_from_json(payload: Mapping[str, Any]) -> Patch:
    """Build one patch from JSON-compatible tool arguments.

    Args:
        payload: Mapping with `action`, `collection`, `key`, and an optional
            `value`.

    Returns:
        The proposed change.

    Raises:
        ValueError: If the payload does not describe a valid change.
    """

    action = payload.get("action")
    if action not in PATCH_ACTIONS:
        raise ValueError(f"Patch action must be one of {sorted(PATCH_ACTIONS)}.")
    return Patch(
        action=cast(PatchAction, action),
        collection=required_text(payload.get("collection"), field="collection"),
        key=required_text(payload.get("key"), field="key"),
        value=deepcopy(payload.get("value")),
    )


def patch_as_json(patch: Patch) -> dict[str, Any]:
    """Return one proposed change as JSON-compatible arguments.

    Args:
        patch: Change to serialize.

    Returns:
        A mapping a tool call can carry.
    """

    payload: dict[str, Any] = {
        "action": patch.action,
        "collection": patch.collection,
        "key": patch.key,
    }
    if patch.value is not None:
        payload["value"] = deepcopy(patch.value)
    return payload


def messages_from_json(payload: object) -> tuple[Message, ...]:
    """Build conversation turns from JSON-compatible tool arguments.

    Args:
        payload: Sequence of `{"role": ..., "content": ...}` mappings.

    Returns:
        The turns in supplied order.

    Raises:
        TypeError: If the payload is not a sequence of mappings.
        ValueError: If a turn has an empty role or content.
    """

    if isinstance(payload, str | bytes) or not isinstance(payload, Iterable):
        raise TypeError("messages must be a sequence of role/content mappings.")
    turns: list[Message] = []
    for entry in payload:
        if not isinstance(entry, Mapping):
            raise TypeError("messages must be a sequence of role/content mappings.")
        turns.append(
            Message(
                role=required_text(entry.get("role"), field="role"),
                content=required_text(entry.get("content"), field="content"),
            )
        )
    return tuple(turns)


def json_mapping(value: object, *, field: str) -> dict[str, Any]:
    """Return an isolated copy of a JSON-compatible mapping.

    Args:
        value: Candidate mapping, or `None` for an empty one.
        field: Field name used in error messages.

    Returns:
        A deep copy with string keys.

    Raises:
        TypeError: If the value is not a mapping with string keys.
    """

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise TypeError(f"{field} must be a mapping.")
    for key in value:
        if not isinstance(key, str):
            raise TypeError(f"{field} keys must be strings.")
    return deepcopy(dict(cast(Mapping[str, Any], value)))


def searchable_text(value: Any) -> str:
    """Return the text a lexical search reads out of a stored value.

    Args:
        value: Stored application value of any supported shape.

    Returns:
        Flattened text for tokenizing.
    """

    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        return " ".join(searchable_text(entry) for entry in value.values())
    if isinstance(value, Sequence):
        return " ".join(searchable_text(entry) for entry in value)
    return str(value)


def _segment(value: object) -> str:
    """Validate one namespace segment.

    Raises:
        ValueError: If the segment is empty or contains a path separator.
    """

    segment = required_text(value, field="namespace segment")
    if "/" in segment:
        raise ValueError("A namespace segment cannot contain '/'.")
    return segment
