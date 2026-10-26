"""Typed context and event values that Genie hooks share."""

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

    - **Operations with no paired callbacks:** mount resolution, handle
      creation, and `require_access` checks are members, so that hooks can get
      their refusals. These operations have no paired before and after
      callbacks, and they open an operation only when they refuse. So they get
      to hooks only through `on_denied` and the related `denied` terminal
      event.
    - **`SUPPRESS`:** the registry turns off its own dispatch. This is an
      operation like the others. So `on_suppressed` announces the suppression
      window, a terminal event closes it, and each operation inside it names it
      as the parent.
    - **`SCOPE`:** the only member that dispatches nothing. It marks a
      caller-defined span that `HookRegistry.scope` opens. The span names work
      that Genie does not model, and `HookContext.scope_name` holds that name.
      Hooks never get a scope context. So each `operation` that a hook sees is
      an operation that Genie performed.
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
    PROTECT_RELATION = "link.protect_relation"
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

    - **Source:** a refusal with one of these names comes from a `MemoryHandle`
      boundary, not from a block policy. So a hook can tell an attempt to leave
      a sandbox apart from an ordinary permission failure.
    - **No collision:** the values have a namespace. So they never collide with
      the built-in policy `Operation` names that use the same parameter.
    """

    SUBTREE = "handle.subtree"
    ANCESTORS = "handle.ancestors"
    GLOBAL_SEARCH = "handle.global_search"
    MOUNTS = "handle.mounts"
    POLICY_OVERRIDE = "handle.policy_override"
    CUSTOM_IDS = "handle.custom_ids"
    ROOT_REQUIRED = "handle.root_required"
    PRINCIPAL = "handle.principal"


class HookOutcome(StrEnum):
    """Terminal result of one logical operation.

    - **`DENIED`:** the operation raised `AccessDenied`, from a block policy or
      from a `MemoryHandle` boundary. A refusal is an expected decision, not a
      fault.
    - **`ERROR`:** all other failures. A hook can examine the reported
      exception when it needs a finer classification.
    """

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


def freeze_attributes(attributes: Mapping[str, str] | None) -> Mapping[str, str]:
    """Check hook context attributes and return a copy that cannot change.

    Attributes are flat pairs of strings. So audit records, metric labels, and
    tracing spans can hold them with no more conversion.

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
        operation_id: Unique id that the before and after callbacks of the
            operation share.
        parent_operation_id: Id of the operation or scope that was active
            when this operation started.
        operation: Logical operation. A caller-defined span reports
            `HookOperation.SCOPE`.
        origin: `"caller"` if the operation started in caller code, or
            `"hook"` if it started inside a hook.
        depth: Zero-based depth of nested operations and scopes.
        principal_id: Acting principal, if one is available.
        attributes: Read-only string labels. They describe this operation, the
            access object that performed it, and the scopes that contain it.
            Nested operations inherit them at any depth. So hooks can group
            related work with no knowledge of how the calls nest.
        started_at: Timezone-aware UTC timestamp of the operation start. With
            the terminal event, it gives the duration of the operation.
        scope_name: Caller-defined span name. It has a value for
            `HookOperation.SCOPE` contexts, and it is `None` for each operation
            that Genie performs.
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
        """Freeze the attributes so that hooks cannot change them, and check the scope name.

        Raises:
            TypeError: If the attributes are not a mapping of strings to
                strings.
            ValueError: If a scope has no name, or an operation that Genie
                performs has one.
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

        Scope contexts dispatch no callbacks. So hooks see a scope only as the
        parent id and the inherited attributes of the operations inside it.
        """

        return self.operation is HookOperation.SCOPE


class StructureKind(StrEnum):
    """Families of structural state that hooks can see."""

    KEYED_COLLECTION = "keyed_collection"
    EVENT_LOG = "event_log"
    TEMPORAL_COLLECTION = "temporal_collection"


class StructureAction(StrEnum):
    """Changes that structural hook events report."""

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

    - **Records:** `before` and `after` hold the immutable structural record of
      the facade, such as a key binding, a log entry, or a temporal entry.
    - **No final record yet:** a before callback can get `after=None` when the
      backend makes the final record.

    Attributes:
        kind: Family of structural state.
        action: Change in that family.
        container_id: Id of the collection block or the log block.
        block_id: Id of the memory block that the record refers to.
        before: Current structural record, if there is one.
        after: Proposed or stored structural record, if it is available.
    """

    kind: StructureKind
    action: StructureAction
    container_id: str
    block_id: str
    before: RecordT | None = None
    after: RecordT | None = None


class TraversalKind(StrEnum):
    """Relationships that a structural read can follow.

    - **`LINKS`:** link listing in any direction.
    - **`CONTAINS`:** a subtree membership test. It follows ownership and does
      not return what it finds.
    - **`MOUNTS` and `SUBTREE_MOUNTS`:** `MOUNTS` is the symbolic mounts of one
      block. `SUBTREE_MOUNTS` is a scan that collects the mounts of each
      readable block under a root. The two report the same kind of record from
      very different amounts of memory. If a trail cannot tell them apart, it
      shows the scan as a much smaller read than it is.
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

    - **Start block:** a traversal names the block where the read starts, not
      the blocks that it returns. So one call can disclose many blocks.
    - **Same value for both callbacks:** the before callback sees the read
      before it runs. The after callback sees the same read with what it
      disclosed.
    - **Already filtered:** the `read` policy of the principal filters the
      results first. A block that the policy hides is not in the results, and
      the read does not report it as a refusal.

    Attributes:
        kind: Relationship that the read follows.
        origin_id: Block where the read starts. For a subtree read, this is the
            scanned root.
        target_id: Candidate block of a containment test, and `None` for all
            other kinds.
        relation: Relation that the read filtered on, if one applies. A mount
            read reports the reserved mount relation.
        direction: Link direction relative to `origin_id` for a link read or a
            mount read, and `None` for all other kinds.
        blocks: Decoded blocks that a `children` or `ancestors` read disclosed.
            Empty until the read is complete.
        links: Links that a link read or a mount read disclosed. Empty until
            the read is complete.
        contained: Result of a containment test. `None` until the test is
            complete, and for all other kinds.
    """

    kind: TraversalKind
    origin_id: str
    target_id: str | None = None
    relation: str | None = None
    direction: Literal["out", "in", "both"] | None = None
    blocks: tuple[MemoryBlock, ...] = ()
    links: tuple[MemoryLink, ...] = ()
    contained: bool | None = None


@dataclass(frozen=True, slots=True)
class RelationProtection:
    """One request to protect a link relation as an acyclic graph.

    - **Whole backend:** the protection changes link writes for all callers.
      So an audit trail can record which principal protected which relation.
    - **Same value for both callbacks:** the before callback sees the request
      before the backend runs it. The after callback sees the same value after
      the protection commits.

    Attributes:
        relation: Relation that the call protects.
    """

    relation: str


class SuppressionScope(StrEnum):
    """Reach of one suppression window.

    - **`SELECTED`:** silences the hooks that it names. The other hooks continue
      to report.
    - **`REGISTRY`:** silences each hook that is not mandatory. This includes
      hooks that are registered after the window opens.
    - **Not interchangeable:** the two scopes are different, also when they
      silence the same hooks now.
    """

    SELECTED = "selected"
    REGISTRY = "registry"


@dataclass(frozen=True, slots=True)
class Suppression:
    """One window in which hook dispatch is off on purpose.

    Suppression is a trusted escape hatch, so the escape is also an event. A
    hook knows that it will no longer report before that happens. The window:

    - starts with `on_suppressed`;
    - ends with the terminal event of the same `HookOperation.SUPPRESS`
      operation; and
    - is the parent of each operation inside it.

    Attributes:
        scope: Whether the window names its hooks or covers the registry.
        hooks: Hooks that no longer report, in dispatch order. A window for the
            whole registry lists the hooks that were registered when it opened.
            Hooks that are registered while it is open are also suppressed,
            but they are not in this list.
        mandatory: Registered hooks that continue to report because they are
            mandatory, in dispatch order. Only these hooks can record what
            happens inside the window.
    """

    scope: SuppressionScope
    hooks: tuple[MemoryHook, ...] = ()
    mandatory: tuple[MemoryHook, ...] = ()
