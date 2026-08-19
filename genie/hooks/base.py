"""Hook interface for observing or customizing memory operations."""

from __future__ import annotations

from typing import Any

from genie.hooks.events import (
    HandleCapability,
    HookContext,
    HookOutcome,
    StructureChange,
    Suppression,
    Traversal,
)
from genie.memory.block import MemoryBlock
from genie.memory.links import MemoryLink
from genie.policy.engine import Operation
from genie.query.query import MemoryQuery
from genie.query.result import SearchResult


class MemoryHook:
    """No-op hook base class for customizing Genie behavior.

    Subclasses can override one or more methods. Hooks run inside the public
    `Memory` operation flow, so they should avoid long-running or unreliable
    side effects unless the application is prepared for those failures.

    Callbacks that only observe finished work are isolated from one another:
    every hook receives them even when another hook raises, so one unreachable
    sink cannot cost the others the record they are owed.

    Attributes:
        fail_open: Whether failures of this hook's observing callbacks are
            contained and logged instead of raised. Observing callbacks are the
            `after_*` callbacks that return `None`, `on_denied`,
            `on_suppressed`, and `on_operation_end`; what they report is
            already decided when they run, or in the case of `on_suppressed`
            about to be, so a best-effort audit sink can declare that its own
            outages must not break callers. This never applies to `before_*`
            callbacks or to `after_search`, which still shape what gets stored
            or returned: a redaction hook whose failure was contained there
            would silently pass through the value it existed to change.
    """

    fail_open: bool = False

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Run before a block is stored.

        Hooks may annotate or rewrite fields, but they must not change
        `parent_id`; parent placement is checked before hooks run.

        Args:
            context: Operation context shared with the matching after callback.
            block: Block about to be stored.

        Returns:
            Block to pass to the backend. Hooks may return a modified copy.
        """

        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Run after a block is stored.

        Args:
            context: Operation context shared with the matching before callback.
            block: Stored block.
        """

        return None

    def before_update(
        self,
        context: HookContext,
        current: MemoryBlock,
        proposed: MemoryBlock,
    ) -> MemoryBlock:
        """Run before a block value or metadata update.

        The hook context distinguishes ``set`` from ``update``.
        Hooks may change the proposed value, metadata, or content type. Stable
        identity, ownership, policy, creator, timestamps, and version fields
        are checked by ``Memory`` after hooks run.

        Args:
            context: Operation context shared with the matching after callback.
            current: Stored block before the update.
            proposed: Proposed replacement block.

        Returns:
            Replacement block to pass to the backend.
        """

        return proposed

    def after_update(
        self,
        context: HookContext,
        previous: MemoryBlock,
        stored: MemoryBlock,
    ) -> None:
        """Run after a block update commits.

        Args:
            context: Operation context shared with the matching before callback.
            previous: Block state read before the update.
            stored: Persisted replacement block.
        """

        return None

    def before_get(self, context: HookContext, block_id: str) -> None:
        """Run before a block lookup.

        Args:
            context: Operation context shared with the matching after callback.
            block_id: Id being fetched.
        """

        return None

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Run after a block lookup.

        Args:
            context: Operation context shared with the matching before callback.
            block: Fetched block, or `None` when missing.
        """

        return None

    def before_search(self, context: HookContext, query: MemoryQuery) -> MemoryQuery:
        """Run before search is sent to the backend.

        Args:
            context: Operation context shared with the matching after callback.
            query: Normalized memory query.

        Returns:
            Query to send to the backend. Hooks may return a modified query.
        """

        return query

    def after_search(
        self,
        context: HookContext,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Run after search results are decoded and policy-filtered.

        Args:
            context: Operation context shared with the matching before callback.
            query: Query that was executed.
            results: Search results.

        Returns:
            Results to return to the caller. Hooks may rerank or filter them.
        """

        return results

    def before_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Run before a structural read follows relationships between blocks.

        This covers direct children, ancestors, link listing, mount listing and
        discovery, and subtree membership tests. Traversal results are
        backend-owned and already filtered by the reading principal's read
        policy, so this callback observes the read rather than narrowing or
        widening it.

        Args:
            context: Operation context shared with the matching after callback.
            traversal: Read about to run, with nothing disclosed yet.
        """

        return None

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Run after a structural read discloses blocks, links, or membership.

        This is where a subtree read becomes visible to an audit trail: the
        traversal carries every block or link the caller receives, so a hook
        can record the volume and identity of what one call disclosed.

        Args:
            context: Operation context shared with the matching before callback.
            traversal: Completed read, carrying what it disclosed.
        """

        return None

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Run before a regular, acyclic, or mount link is stored.

        Hooks may change metadata. Link identity, endpoints, relation, and
        creation time are checked by ``Memory`` after hooks run.

        Args:
            context: Operation context shared with the matching after callback.
            link: Proposed link.

        Returns:
            Link to pass to the backend.
        """

        return link

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Run after a regular, acyclic, or mount link commits.

        Args:
            context: Operation context shared with the matching before callback.
            link: Persisted link.
        """

        return None

    def before_update_link(
        self,
        context: HookContext,
        current: MemoryLink,
        proposed: MemoryLink,
    ) -> MemoryLink:
        """Run before replacement link metadata is stored.

        Args:
            context: Operation context shared with the matching after callback.
            current: Stored link before the update.
            proposed: Proposed replacement link.

        Returns:
            Replacement link to pass to the backend.
        """

        return proposed

    def after_update_link(
        self,
        context: HookContext,
        previous: MemoryLink,
        stored: MemoryLink,
    ) -> None:
        """Run after a link metadata update commits.

        Args:
            context: Operation context shared with the matching before callback.
            previous: Stored link before the update.
            stored: Persisted replacement link.
        """

        return None

    def before_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Run before keyed, log, or temporal structural state changes.

        Structural records are immutable and backend-owned, so this callback
        observes rather than transforms the proposed change.

        Args:
            context: Operation context shared with the matching after callback.
            change: Proposed structural change.
        """

        return None

    def after_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Run after a structural state change commits.

        Args:
            context: Operation context shared with the matching before callback.
            change: Persisted structural change.
        """

        return None

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Run before a block is deleted.

        Args:
            context: Operation context shared with the matching after callback.
            block: Block about to be deleted.
        """

        return None

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Run after a block is deleted.

        Recursive deletion calls this once per removed block. Those deletions
        are already committed, so every block reaches every hook even when one
        of them raises.

        Args:
            context: Operation context shared with the matching before callback.
            block: Deleted block.
        """

        return None

    def on_denied(
        self,
        context: HookContext,
        operation: Operation | HandleCapability | str,
        subject_id: str | None,
        reason: str,
    ) -> None:
        """Run when a policy or handle boundary refuses an operation.

        This is an observation, not a decision: the refusal stands and hooks
        cannot reverse it. Exceptions raised here never replace the
        `AccessDenied` the caller receives.

        Args:
            context: Context shared with the operation's other callbacks.
            operation: Policy operation that was checked, or the
                `HandleCapability` boundary that refused. Policy names are
                bare and handle names are namespaced, so the two never collide.
            subject_id: Block the refusal applies to. This is the handle root
                for capability refusals that name no block, and `None` when an
                unbounded handle has no root.
            reason: Message describing the refusal.
        """

        return None

    def on_suppressed(self, context: HookContext, suppression: Suppression) -> None:
        """Run when trusted code disables hook dispatch.

        Every registered hook is told, whatever the window's reach, including
        the hooks it is about to silence: a hook learns that it is going to
        stop reporting before it does. The window is bounded by the terminal
        event of the same operation, which every hook receives once dispatch is
        restored, and operations performed inside it report the suppression as
        their parent operation. A mandatory hook therefore records the window,
        what happened during it, and its end.

        Unlike the other observing callbacks, this one reports work that is not
        decided yet. A failure here leaves dispatch enabled and the suppressed
        body unrun, because the record of a suppression is the reason the
        suppression is allowed at all.

        Args:
            context: Context of the suppression window, shared with its
                terminal event and inherited by the operations inside it.
            suppression: Reach of the window, the hooks it silences, and the
                mandatory hooks that keep reporting through it.
        """

        return None

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Run exactly once when a logical operation finishes.

        Unlike the paired callbacks, this one runs for every operation that
        starts, whether it commits, is refused, or fails. It is the right place
        to close a span, record latency, and release state stashed by a before
        callback, and it is the only callback that observes refusals and
        failures on operations whose before callbacks never ran.

        The reported ``error`` is the exception leaving the operation. Genie
        never lets an exception raised here replace it.

        Args:
            context: Context shared with the operation's other callbacks.
            outcome: Whether the operation committed, was denied, or failed.
            error: Exception ending the operation, or `None` after success.
        """

        return None
