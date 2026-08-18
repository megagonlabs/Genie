"""Typed context and events shared by Genie hooks."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Generic, Literal, TypeVar


class HookOperation(StrEnum):
    """Logical operations that can dispatch hooks.

    Traversal, link listing, mount listing, and handle creation appear here so
    refusals can be reported. They do not have paired before and after
    callbacks, so they reach hooks only through ``on_denied`` and the matching
    terminal event.
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
    KEY_BIND = "key.bind"
    KEY_REBIND = "key.rebind"
    KEY_UNBIND = "key.unbind"
    LOG_APPEND = "log.append"
    TEMPORAL_ADD = "temporal.add"
    TEMPORAL_REPLACE = "temporal.replace"
    TEMPORAL_REMOVE = "temporal.remove"


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

    ``DENIED`` marks a policy refusal, which is an expected decision rather
    than a fault. Every other failure is ``ERROR``; hooks can inspect the
    reported exception when they need a finer classification.
    """

    SUCCESS = "success"
    DENIED = "denied"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class HookContext:
    """Causal context for one logical operation.

    Attributes:
        operation_id: Unique id shared by the operation's before and after
            callbacks.
        parent_operation_id: Id of the operation active when this one began.
        operation: Logical operation being performed.
        origin: Whether the operation began in caller code or inside a hook.
        depth: Zero-based nesting depth.
        principal_id: Acting principal when one is available.
    """

    operation_id: str
    parent_operation_id: str | None
    operation: HookOperation
    origin: HookOrigin
    depth: int
    principal_id: str | None = None


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

    ``before`` and ``after`` contain the facade's immutable structural record,
    such as a key binding, log entry, or temporal entry. A before callback may
    see ``after=None`` when the backend must allocate the final record.

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
