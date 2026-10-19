"""Value types, constants, and helpers for the LangMem lamp.

LangMem stores each memory at an address: a hierarchical namespace plus a key in
that namespace. The namespace is a template, for example `("memories", "{user_id}")`.
LangMem fills in the template at request time. So one configured tool set serves
all users, and no tool argument names a user.

The rest of the lamp shares these 4 parts:

- **Block types:** they keep the 4 memory kinds apart.
- **Address helpers:** they resolve and compare namespaces.
- **Records:** the stores return these read-only records.
- **Timestamp and token helpers:** they normalize times and split text into
  search terms.

This module does not write to Genie. So each other module can depend on it
without an import cycle.
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
        value: Timestamp from application code.
        field: Field name for error messages.

    Returns:
        The same instant in UTC.

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
        An ISO 8601 string that ends in `Z`.
    """

    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_timestamp(value: str, *, field: str = "timestamp") -> datetime:
    """Parse a timezone-aware ISO 8601 string into a UTC timestamp.

    Args:
        value: ISO 8601 timestamp string.
        field: Field name for error messages.

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
    """Return required text without whitespace at the start and end.

    Args:
        value: Candidate text.
        field: Field name for error messages.

    Returns:
        The text without whitespace at the start and end.

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
        field: Field name for error messages.

    Returns:
        Unique strings without whitespace at the start and end.

    Raises:
        TypeError: If `values` is a string, bytes, or not iterable.
        ValueError: If an entry is empty or is not a string.
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
    """Split text into lowercase alphanumeric tokens, in the order they first occur.

    The search terms keep their order. So a query makes the same sequence of
    Genie reads on each run.

    Args:
        value: Text to split.

    Returns:
        The distinct tokens in the text.
    """

    seen: list[str] = []
    for token in _TOKEN_PATTERN.findall(value.casefold()):
        if token not in seen:
            seen.append(token)
    return tuple(seen)


def namespace(*segments: str) -> Namespace:
    """Return a validated namespace from its segments.

    Args:
        segments: One or more namespace segments. A segment can be a template
            placeholder or a literal.

    Returns:
        The namespace as a tuple.

    Raises:
        ValueError: If there is no segment, or a segment is empty or contains
            a path separator (`/`).
    """

    if not segments:
        raise ValueError("A namespace needs at least one segment.")
    return tuple(_segment(value) for value in segments)


def resolve_namespace(template: Sequence[str], values: Mapping[str, str]) -> Namespace:
    """Fill the placeholders of a namespace template from request-scoped values.

    - **Upstream:** LangMem writes a namespace once, as `("memories", "{user_id}")`.
      It fills the placeholder from configuration on each call.
    - **Here:** this function does the substitution, so no tool argument
      contains the user id. A tool set bound to one user cannot read the
      namespace of another user.

    Args:
        template: Namespace. A segment can be a `{placeholder}` name.
        values: Placeholder names and their runtime values.

    Returns:
        The namespace with each placeholder replaced by its value.

    Raises:
        KeyError: If `values` has no value for a placeholder.
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
        The segments, joined with `/`.
    """

    return "/".join(value)


def is_below(value: Namespace, prefix: Namespace) -> bool:
    """Return `True` if a namespace is at or below a prefix.

    Args:
        value: Namespace to test.
        prefix: Candidate prefix.

    Returns:
        `True` if `prefix` is a prefix of `value`.
    """

    return value[: len(prefix)] == prefix


class ValueSchema:
    """Runtime schema for the values stored under one namespace.

    - **Upstream:** LangMem takes Pydantic models in `schemas=[...]`. It uses
      them to shape extraction and to name the memory kind.
    - **Genie:** Genie stores each value that its codec accepts. It does not
      check the shape of a value, so the schema is in application code.
    - **This class:** a stand-in with no dependencies. It checks the required
      fields and their types, and it rejects extra fields. A real application
      gives its own Pydantic models.
    """

    __slots__ = ("_fields", "name")

    def __init__(self, name: str, fields: Mapping[str, type]) -> None:
        """Create an exact dictionary schema.

        Args:
            name: Schema name. The store records it in block metadata, and a
                search can filter on it. It names a memory kind, as a LangMem
                schema does.
            fields: Required field names and their expected Python types.

        Raises:
            TypeError: If the type of a field is not a Python type.
            ValueError: If the name or a field name is empty, or `fields` is
                empty.
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
        """Return an independent copy of the field mapping of the schema."""

        return dict(self._fields)

    def validate(self, value: object) -> dict[str, Any]:
        """Validate one value and return an independent copy of it.

        Args:
            value: Candidate value for this namespace.

        Returns:
            A deep copy of the validated dictionary.

        Raises:
            TypeError: If the value is not a dictionary or a field has the
                wrong type.
            ValueError: If the value does not have a required field, or has
                an extra field.
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
    """One conversation turn that the manager reads.

    Attributes:
        role: Speaker role, for example `user` or `assistant`.
        content: Message text.
    """

    role: str
    content: str

    def as_json(self) -> dict[str, str]:
        """Return the message in the shape that LangMem submits."""

        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class MemoryItem:
    """A resolved `(namespace, key, value)` item.

    Attributes:
        block: Genie block that holds the item.
        namespace: Resolved namespace of the item address.
        key: Key in that namespace.
        value: Stored application value.
        schema_name: Name of the registered schema for the namespace, or
            `None` if the namespace has no schema.
    """

    block: MemoryBlock
    namespace: Namespace
    key: str
    value: Any
    schema_name: str | None

    @property
    def id(self) -> str:
        """Return the item id. It is the Genie block id."""

        return self.block.id

    @property
    def version(self) -> int:
        """Return the number of writes to the item.

        LangMem keeps a change history for an updated memory. Genie counts the
        writes to one block. So an in-place patch shows here, and the lamp does
        not keep an audit record for it.
        """

        return self.block.version

    @property
    def address(self) -> str:
        """Return the printable `namespace/key` address."""

        return f"{namespace_path(self.namespace)}/{self.key}"

    def canonical_value(self) -> dict[str, Any]:
        """Return a copy of the exact namespace/key/value wrapper that Genie stores."""

        return deepcopy(cast(dict[str, Any], self.block.value))


@dataclass(frozen=True, slots=True)
class SearchHit:
    """One item that the lexical search of the lamp returns.

    Attributes:
        item: Item that matched.
        score: Share of query terms that occur in the item value. The lamp
            calculates it, not Genie.
        matched: Query terms that matched, in query order.
    """

    item: MemoryItem
    score: float
    matched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Patch:
    """One change that a memory manager proposes to the store.

    The LangMem manager returns the changes that it wants, and does not write
    them. So a configuration can permit inserts, updates, and deletes
    separately. This class is one such proposal.

    Attributes:
        action: `insert`, `update`, or `delete`.
        collection: Namespace segment below the bound namespace, for example
            `semantic` or `episodes`.
        key: Key in that namespace.
        value: New value. An insert and an update require a value. A delete
            has no value.
    """

    action: PatchAction
    collection: str
    key: str
    value: Any | None = None

    def __post_init__(self) -> None:
        """Validate the proposed change.

        Raises:
            ValueError: If the action is unknown, the collection or key is
                empty, an insert or update has no value, or a delete has a
                value.
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
        patch: Change that the manager proposed.
        action: Change that the address calls for after reconciliation. It is
            different from the proposal if an insert targets an address that
            holds an item (`update`), or an update targets an empty address
            (`insert`). A refused outcome also records this action.
        applied: `True` if the store wrote the change.
        reason: Plain-language explanation. For a refused change, it tells
            why.
        item: Item after the change, or the deleted item. If the permissions
            refuse the change, it is the item at the address. `None` if
            there is no item.
    """

    patch: Patch
    action: PatchAction
    applied: bool
    reason: str
    item: MemoryItem | None = None


@dataclass(frozen=True, slots=True)
class Conversation:
    """A conversation that a caller submits for memory formation.

    Attributes:
        thread: Name of the conversation thread. The queue debounces the
            submissions of each thread in one namespace.
        messages: Turns in the exchange.
        patches: Changes that an extractor proposed from those turns.
        profile: Profile fields that the exchange revises.
    """

    thread: str
    messages: tuple[Message, ...]
    patches: tuple[Patch, ...] = ()
    profile: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Submission:
    """One deferred memory-formation task on the reflection queue.

    Attributes:
        block: Genie block that records the submission.
        sequence: Position in the append-only queue log.
        thread: Name of the `Conversation` thread of this submission.
        state: `pending`, `superseded`, or `processed`.
        submitted_at: Time when the caller gave the conversation to the queue.
        due_at: Earliest time when the manager can run the submission.
        messages: Accumulated turns. They include the turns from a superseded
            submission on the same thread.
        patches: Accumulated proposed changes.
        profile: Accumulated profile fields.
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
    """The result of one memory manager run over one conversation.

    Attributes:
        thread: Name of the `Conversation` thread that the manager processed.
        source_id: Id of the block that records the conversation, if the run
            came from the queue. `None` if the caller gave no source block.
        ran_at: Time of the manager run.
        outcomes: The outcome of each proposed change, in proposal order.
        profile: Profile after the run, if the conversation revised one. If
            the manager refused the revision, it is the unchanged profile.
        profile_reason: The reason why the manager refused the profile
            revision, or `None`.
    """

    thread: str
    source_id: str | None
    ran_at: datetime
    outcomes: tuple[PatchOutcome, ...]
    profile: Profile | None = None
    profile_reason: str | None = None

    @property
    def applied(self) -> tuple[PatchOutcome, ...]:
        """Return the changes that the store wrote."""

        return tuple(outcome for outcome in self.outcomes if outcome.applied)

    @property
    def refused(self) -> tuple[PatchOutcome, ...]:
        """Return the changes that the store did not write.

        A refused change has no permission, or it is a delete at an empty
        address.
        """

        return tuple(outcome for outcome in self.outcomes if not outcome.applied)


@dataclass(frozen=True, slots=True)
class Profile:
    """The single document for one namespace that each update revises.

    Attributes:
        identity: Stable block that owns the revisions.
        block: Revision that holds the current value.
        namespace: Namespace of the profile.
        name: Profile name in that namespace.
        value: Current merged value.
        revision: Revision number. The first revision is 1.
        updated_at: Time when the store wrote this revision.
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
    """One permanent instruction in the rule set of a prompt.

    Attributes:
        key: Stable rule name. A later rule with the same key replaces this
            rule. The optimizer does not add the later rule next to it.
        text: Instruction text as it shows in the composed prompt.
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
    """A past exchange and the feedback that the optimizer learns from.

    Attributes:
        name: Stable trajectory name. Provenance reports use it.
        messages: Turns in the exchange.
        feedback: What the reviewer said about the exchange.
        rules: Rules that the feedback asks the prompt to include.
    """

    name: str
    messages: tuple[Message, ...]
    feedback: str
    rules: tuple[PromptRule, ...] = ()


@dataclass(frozen=True, slots=True)
class PromptVersion:
    """One version of the procedural memory of an agent.

    Attributes:
        identity: Stable block that names the prompt.
        block: Revision that holds this version. `None` before the first
            revision.
        name: Prompt name.
        base: Prompt text without the learned rules.
        text: Composed prompt that the agent uses.
        rules: Rules in this version, in acquisition order.
        revision: Revision number. It is 0 before the first optimization.
        when_to_update: Condition that the store records for an optimizer to
            read.
        sources: Names of the annotated exchanges that produced this version.
            The optimizer reads them from the lineage edges into the revision.
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
    """Return the prompt text that an agent uses.

    Args:
        base: Prompt text at registration.
        rules: Rules learned from feedback, in acquisition order.

    Returns:
        The base prompt, then its numbered rules. Without rules, only the base
        prompt.
    """

    if not rules:
        return base
    lines = [base, "Rules learned from feedback:"]
    lines.extend(f"{index}. {rule.text}" for index, rule in enumerate(rules, start=1))
    return "\n".join(lines)


def item_from_block(block: MemoryBlock) -> MemoryItem:
    """Decode one stored item wrapper.

    Args:
        block: Block that holds a `namespace`/`key`/`value` wrapper.

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
    """Return the portable index metadata that the store keeps next to an item.

    - **Address copy:** the metadata repeats the address. So Genie can filter
      and scope a search, and the lamp does not decode each candidate value
      first.
    - **`schema`:** the LangMem search `filter` uses this field when several
      schemas share a namespace.

    Args:
        value: Resolved namespace.
        key: Key in that namespace.
        schema_name: Registered schema name, or `None` if the namespace has no
            schema.

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
        A mapping for a tool call.
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
        The turns, in the given order.

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
    """Return an independent copy of a JSON-compatible mapping.

    Args:
        value: Candidate mapping, or `None` for an empty mapping.
        field: Field name for error messages.

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
    """Return the text that a lexical search reads from a stored value.

    Args:
        value: Stored application value of any supported shape.

    Returns:
        Flat text for `tokenize`.
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
