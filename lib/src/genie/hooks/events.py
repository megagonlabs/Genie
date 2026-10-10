"""Typed context and events shared by Genie hooks."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Generic, Literal, TypeVar

from genie.memory.block import MemoryBlock, utc_now
from genie.memory.links import MemoryLink

if TYPE_CHECKING:
    from genie.hooks.base import MemoryHook

_EMPTY_ATTRIBUTES: Mapping[str, str] = MappingProxyType({})


class HookOperation(StrEnum):
    """Logical operations and spans that appear in a hook context.

    Mount resolution, handle creation, and `require_access` checks appear
    here so refusals can be reported. They do not have paired before and after
    callbacks, so they reach hooks only through `on_denied` and the matching
    terminal event.

    `SUPPRESS` is the registry disabling its own dispatch. It is an operation
    like any other, so a suppression window is announced by `on_suppressed`,
    bounded by a terminal event, and named as the parent of everything done
    inside it.

    `SCOPE` is the one member that dispatches nothing. It marks a
    caller-defined span opened with `HookRegistry.scope`, which names work
    Genie does not model and carries that name in `HookContext.scope_name`.
    Hooks never receive a scope context, so every `operation` they observe is
    one Genie performed.
    """

    PUT = "put"
    GET = "get"
    SET = "set"
    UPDATE = "update"
    DELETE = "delete"
    SEARCH = "search"
    CHILDREN = "children"
    ANCESTORS = "ancestors"
    CONTAINS = "contains"
    ADD_LINK = "link.add"
    ADD_ACYCLIC_LINK = "link.add_acyclic"
    ADD_MOUNT = "mount.add"
    UPDATE_LINK = "link.update"
    LIST_LINKS = "link.list"
    LIST_MOUNTS = "mount.list"
    DISCOVER_MOUNTS = "mount.discover"
    RESOLVE_MOUNT = "mount.resolve"
    CREATE_HANDLE = "handle.create"
    REQUIRE_ACCESS = "access.require"
    KEY_BIND = "key.bind"
    KEY_REBIND = "key.rebind"
    KEY_UNBIND = "key.unbind"
    LOG_APPEND = "log.append"
    TEMPORAL_ADD = "temporal.add"
    TEMPORAL_REPLACE = "temporal.replace"
    TEMPORAL_REMOVE = "temporal.remove"
    SUPPRESS = "hooks.suppress"
    SCOPE = "scope"


HookOrigin = Literal["caller", "hook"]


class HandleCapability(StrEnum):
    """Handle boundaries and capabilities that can refuse an operation.

    A refusal reported with one of these names came from a `MemoryHandle`
    boundary rather than from a block policy, which distinguishes an attempt to
    leave a sandbox from an ordinary permission failure. Namespaced values
    never collide with the policy `Operation` names that share the parameter.
    """

    SUBTREE = "handle.subtree"
    ANCESTORS = "handle.ancestors"
    GLOBAL_SEARCH = "handle.global_search"
    MOUNTS = "handle.mounts"
    POLICY_OVERRIDE = "handle.policy_override"
    ROOT_REQUIRED = "handle.root_required"
    PRINCIPAL = "handle.principal"


class HookOutcome(StrEnum):
    """Terminal result of one logical operation.

    `DENIED` marks a policy refusal, which is an expected decision rather
    than a fault. Every other failure is `ERROR`; hooks can inspect the
    reported exception when they need a finer classification.
    """

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


def freeze_attributes(attributes: Mapping[str, str] | None) -> Mapping[str, str]:
    """Validate hook context attributes and return an immutable copy.

    Attributes are flat string pairs, so audit records, metric labels, and
    tracing spans can carry them without further conversion.

    Args:
        attributes: Attributes to normalize, or `None` for no attributes.

    Returns:
        Read-only mapping of string keys to string values.

    Raises:
        TypeError: If the value is not a mapping of strings to strings.
    """

    if attributes is None:
        return _EMPTY_ATTRIBUTES
    if not isinstance(attributes, Mapping):
        raise TypeError("attributes must be a mapping of string keys to string values.")
    for key, value in attributes.items():
        if not isinstance(key, str) or not isinstance(value, str):
            raise TypeError(
                "attributes must be a mapping of string keys to string values."
            )
    if not attributes:
        return _EMPTY_ATTRIBUTES
    return MappingProxyType(dict(attributes))


@dataclass(frozen=True, slots=True)
class HookContext:
    """Causal context for one logical operation.

    Attributes:
        operation_id: Unique id shared by the operation's before and after
            callbacks.
        parent_operation_id: Id of the operation or scope active when this one
            began.
        operation: Logical operation being performed. A caller-defined span
            reports `HookOperation.SCOPE`.
        origin: Whether the operation began in caller code or inside a hook.
        depth: Zero-based nesting depth.
        principal_id: Acting principal when one is available.
        attributes: Read-only string labels describing this operation, the
            access object that performed it, and the scopes containing it.
            Nested operations inherit them at any depth, so hooks can group
            related work without knowing how calls nest.
        started_at: Timezone-aware UTC timestamp taken when the operation
            began. With the terminal event it gives the operation's duration.
        scope_name: Caller-defined span name. Set for `HookOperation.SCOPE`
            contexts and `None` for every operation Genie performs.
    """

    operation_id: str
    parent_operation_id: str | None
    operation: HookOperation
    origin: HookOrigin
    depth: int
    principal_id: str | None = None
    attributes: Mapping[str, str] = _EMPTY_ATTRIBUTES
    started_at: datetime = field(default_factory=utc_now)
    scope_name: str | None = None

    def __post_init__(self) -> None:
        """Freeze attributes against hook mutation and pair the scope name.

        Raises:
            TypeError: If the attributes are not a mapping of strings to
                strings.
            ValueError: If a scope has no name or a performed operation has
                one.
        """

        object.__setattr__(self, "attributes", freeze_attributes(self.attributes))
        if self.is_scope and not self.scope_name:
            raise ValueError("A scope context requires a scope_name.")
        if not self.is_scope and self.scope_name is not None:
            raise ValueError(
                "Only a HookOperation.SCOPE context can carry a scope_name."
            )

    @property
    def is_scope(self) -> bool:
        """Return whether this context names a caller-defined scope.

        Scope contexts dispatch no callbacks, so hooks see them only as the
        parent id and inherited attributes of the operations inside them.
        """

        return self.operation is HookOperation.SCOPE


class StructureKind(StrEnum):
    """Structural state families exposed to hooks."""

    KEYED_COLLECTION = "keyed_collection"
    EVENT_LOG = "event_log"
    TEMPORAL_COLLECTION = "temporal_collection"


class StructureAction(StrEnum):
    """Mutations supported by structural hook events."""

    BIND = "bind"
    REBIND = "rebind"
    UNBIND = "unbind"
    APPEND = "append"
    ADD = "add"
    REPLACE = "replace"
    REMOVE = "remove"


RecordT = TypeVar("RecordT")


@dataclass(frozen=True, slots=True)
class StructureChange(Generic[RecordT]):
    """One logical change to structure membership.

    `before` and `after` contain the facade's immutable structural record,
    such as a key binding, log entry, or temporal entry. A before callback may
    see `after=None` when the backend must allocate the final record.

    Attributes:
        kind: Structural state family.
        action: Mutation within that family.
        container_id: Collection or log block id.
        block_id: Referenced memory block id.
        before: Existing structural record, when applicable.
        after: Proposed or persisted structural record, when available.
    """

    kind: StructureKind
    action: StructureAction
    container_id: str
    block_id: str
    before: RecordT | None = None
    after: RecordT | None = None


class TraversalKind(StrEnum):
    """Relationships a structural read can follow.

    `LINKS` covers link listing in any direction, and `CONTAINS` covers a
    subtree membership test, which follows ownership without returning what it
    finds.

    `MOUNTS` and `SUBTREE_MOUNTS` separate the symbolic mounts of one block
    from a scan that collects them from every readable block under a root. The
    two report the same kind of record from very different amounts of memory, so
    a trail that could not tell them apart would understate the second.
    """

    CHILDREN = "children"
    ANCESTORS = "ancestors"
    LINKS = "links"
    CONTAINS = "contains"
    MOUNTS = "mounts"
    SUBTREE_MOUNTS = "mounts.subtree"


@dataclass(frozen=True, slots=True)
class Traversal:
    """One structural read that follows relationships between blocks.

    A traversal names a starting block rather than the blocks it returns, so
    one call can disclose many. Both callbacks receive the same value: the
    before callback sees the read that is about to run, and the after callback
    sees the same read carrying what it disclosed.

    Traversal results are already filtered by the reading principal's read
    policy, and blocks that policy hides are absent rather than reported as
    refusals.

    Attributes:
        kind: Relationship the read follows.
        origin_id: Block the read starts from, which is the scanned root of a
            subtree read.
        target_id: Candidate block of a containment test, and `None` for every
            other kind.
        relation: Relation the read filtered on, when one applies. A mount read
            reports the reserved mount relation.
        direction: Link direction relative to `origin_id` for a link or mount
            read, and `None` for every other kind.
        blocks: Decoded blocks a `children` or `ancestors` read disclosed.
            Empty until the read completes.
        links: Links a link or mount read disclosed. Empty until the read
            completes.
        contained: Result of a containment test. `None` until the test
            completes, and for every other kind.
    """

    kind: TraversalKind
    origin_id: str
    target_id: str | None = None
    relation: str | None = None
    direction: Literal["out", "in", "both"] | None = None
    blocks: tuple[MemoryBlock, ...] = ()
    links: tuple[MemoryLink, ...] = ()
    contained: bool | None = None


class SuppressionScope(StrEnum):
    """Reach of one suppression window.

    `SELECTED` names the hooks it silences and leaves the rest reporting.
    `REGISTRY` silences every hook that is not mandatory, including hooks
    registered after the window opens, so the two are not interchangeable even
    when they silence the same hooks today.
    """

    SELECTED = "selected"
    REGISTRY = "registry"


@dataclass(frozen=True, slots=True)
class Suppression:
    """One window in which hook dispatch is deliberately disabled.

    Suppression is a trusted escape hatch, so the escape is itself an event. A
    hook learns that it is about to stop reporting before it does. The window
    is:

    - announced by `on_suppressed`;
    - closed by the terminal event of the same `HookOperation.SUPPRESS`
      operation; and
    - named as the parent of every operation performed inside it.

    Attributes:
        scope: Whether the window names its hooks or covers the registry.
        hooks: Hooks that stop reporting, in dispatch order. A registry-wide
            window lists the hooks registered when it opened; hooks registered
            while it is open are suppressed too without appearing here.
        mandatory: Registered hooks that keep reporting because they were
            registered as mandatory, in dispatch order. These are the hooks
            that can still witness what happens inside the window.
    """

    scope: SuppressionScope
    hooks: tuple[MemoryHook, ...] = ()
    mandatory: tuple[MemoryHook, ...] = ()
