"""Hook interface that observes or customizes memory operations."""

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
    """No-op base class for hooks that customize Genie behavior.

    - **Overrides:** a subclass can override one or more methods.
    - **Synchronous:** hooks run inside the public `Memory` operation flow. So a
      slow or unreliable side effect can make the operation slow, or make it
      raise an error.
    - **Isolated observers:** callbacks that only observe finished work are
      isolated from each other. Each hook gets them, also when another hook
      raises. So one sink that is not available cannot take the record away
      from the other hooks.
    - **`fail_open` covers the observing callbacks:** the `after_*` callbacks
      that return `None`, `on_denied`, `on_suppressed`, and
      `on_operation_end`. What they report is already decided when they run.
      For `on_suppressed`, it is decided immediately after. So a best-effort
      audit sink can declare that its own outages do not get to callers.
    - **`fail_open` does not cover transforms:** it never applies to `before_*`
      callbacks or to `after_search`, because they still change what is stored
      or returned. If the registry contained the failure of a redaction hook
      there, the value that the hook exists to change would silently pass
      through.

    Attributes:
        fail_open: If `True`, the registry logs the failures of the observing
            callbacks of this hook and does not raise them.
    """

    fail_open: bool = False

    def before_put(self, context: HookContext, block: MemoryBlock) -> MemoryBlock:
        """Run before `Memory` stores a new block.

        - **Allowed changes:** a hook can change `value`, `metadata`,
          `content_type`, and `policy`. A change to any other field raises
          `ValueError`.
        - **Parent:** `Memory` checks the parent placement before hooks run. So
          a hook cannot change `parent_id`.

        Args:
            context: Operation context that the paired after callback shares.
            block: Block before `Memory` stores it.

        Returns:
            Block to give to the backend. A hook can return a changed copy.
        """

        return block

    def after_put(self, context: HookContext, block: MemoryBlock) -> None:
        """Run after the backend stores a block.

        Args:
            context: Operation context that the paired before callback shares.
            block: Stored block.
        """

        return

    def before_update(
        self,
        context: HookContext,
        current: MemoryBlock,
        proposed: MemoryBlock,
    ) -> MemoryBlock:
        """Run before an update of a block value or metadata.

        - **`set` or `update`:** the hook context tells `set` and `update` apart.
        - **Allowed changes:** a hook can change the proposed `value`,
          `metadata`, or `content_type`.
        - **Protected fields:** after hooks run, `Memory` checks the stable
          identity, type, ownership, policy, creator, timestamp, and version
          fields.

        Args:
            context: Operation context that the paired after callback shares.
            current: Stored block before the update.
            proposed: Proposed replacement block.

        Returns:
            Replacement block to give to the backend.
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
            context: Operation context that the paired before callback shares.
            previous: Block as read before the update.
            stored: Replacement block that the backend stored.
        """

        return

    def before_get(self, context: HookContext, block_id: str) -> None:
        """Run before a block lookup.

        Args:
            context: Operation context that the paired after callback shares.
            block_id: Id of the block to get.
        """

        return

    def after_get(self, context: HookContext, block: MemoryBlock | None) -> None:
        """Run after a block lookup.

        Args:
            context: Operation context that the paired before callback shares.
            block: Block from the lookup, or `None` if the block does not exist.
        """

        return

    def before_search(self, context: HookContext, query: MemoryQuery) -> MemoryQuery:
        """Run before `Memory` sends a search to the backend.

        Args:
            context: Operation context that the paired after callback shares.
            query: Normalized memory query.

        Returns:
            Query to send to the backend. A hook can return a changed query.
        """

        return query

    def after_search(
        self,
        context: HookContext,
        query: MemoryQuery,
        results: list[SearchResult],
    ) -> list[SearchResult]:
        """Run after `Memory` decodes the search results and filters them by policy.

        Args:
            context: Operation context that the paired before callback shares.
            query: Query that ran.
            results: Search results.

        Returns:
            Results to return to the caller. A hook can rerank or filter them.
        """

        return results

    def before_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Run before a structural read follows relationships between blocks.

        - **Coverage:** direct children, ancestors, link listing, mount listing
          and discovery, and subtree membership tests.
        - **Observe only:** traversal results belong to the backend, and the
          `read` policy of the principal filters them first. So this callback
          observes the read. It does not make the read narrower or wider.

        Args:
            context: Operation context that the paired after callback shares.
            traversal: Read before it runs. It discloses nothing yet.
        """

        return

    def after_traverse(self, context: HookContext, traversal: Traversal) -> None:
        """Run after a structural read discloses blocks, links, or membership.

        Here an audit trail can see a subtree read. The traversal holds each
        block or link that the caller gets. So a hook can record the volume and
        the identity of what one call disclosed.

        Args:
            context: Operation context that the paired before callback shares.
            traversal: Completed read, with what it disclosed.
        """

        return

    def before_add_link(self, context: HookContext, link: MemoryLink) -> MemoryLink:
        """Run before `Memory` stores a regular, acyclic, or mount link.

        A hook can change `metadata` only. After hooks run, `Memory` checks the
        link identity, endpoints, relation, and creation time.

        Args:
            context: Operation context that the paired after callback shares.
            link: Proposed link.

        Returns:
            Link to give to the backend.
        """

        return link

    def after_add_link(self, context: HookContext, link: MemoryLink) -> None:
        """Run after a regular, acyclic, or mount link commits.

        Args:
            context: Operation context that the paired before callback shares.
            link: Stored link.
        """

        return

    def before_update_link(
        self,
        context: HookContext,
        current: MemoryLink,
        proposed: MemoryLink,
    ) -> MemoryLink:
        """Run before `Memory` stores new link metadata.

        A hook can change `metadata` only. After hooks run, `Memory` checks the
        other link fields.

        Args:
            context: Operation context that the paired after callback shares.
            current: Stored link before the update.
            proposed: Proposed replacement link.

        Returns:
            Replacement link to give to the backend.
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
            context: Operation context that the paired before callback shares.
            previous: Stored link before the update.
            stored: Replacement link that the backend stored.
        """

        return

    def before_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Run before a change to keyed, log, or temporal structural state.

        Structural records cannot change, and they belong to the backend. So
        this callback observes the proposed change and does not transform it.

        Args:
            context: Operation context that the paired after callback shares.
            change: Proposed structural change.
        """

        return

    def after_structure_change(
        self,
        context: HookContext,
        change: StructureChange[Any],
    ) -> None:
        """Run after a structural state change commits.

        Args:
            context: Operation context that the paired before callback shares.
            change: Stored structural change.
        """

        return

    def before_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Run before the backend deletes a block.

        Args:
            context: Operation context that the paired after callback shares.
            block: Block to delete.
        """

        return

    def after_delete(self, context: HookContext, block: MemoryBlock) -> None:
        """Run after the backend deletes a block.

        A recursive delete calls this once for each removed block. Those
        deletions are already committed. So each block gets to each hook, also
        when one hook raises.

        Args:
            context: Operation context that the paired before callback shares.
            block: Deleted block.
        """

        return

    def on_denied(
        self,
        context: HookContext,
        operation: Operation | HandleCapability | str,
        subject_id: str | None,
        reason: str,
    ) -> None:
        """Run when a policy or a handle boundary refuses an operation.

        This is an observation, not a decision. The refusal stays, and hooks
        cannot reverse it. An `Exception` from this callback never replaces the
        `AccessDenied` that the caller gets.

        Args:
            context: Context that the other callbacks of the operation share.
            operation: Policy operation that the check used, or the
                `HandleCapability` boundary that refused. A custom operation
                that `require_access` checks arrives as a plain `str`. Built-in
                policy names have no namespace, and handle names have a
                namespace, so the two never collide.
            subject_id: Block that the refusal applies to. For a capability
                refusal that names no block, this is the handle root. It is
                `None` when an unbounded handle has no root.
            reason: Message that describes the refusal.
        """

        return

    def on_suppressed(self, context: HookContext, suppression: Suppression) -> None:
        """Run when trusted code turns off hook dispatch.

        - **All hooks get it:** each registered hook gets this callback, for any
          reach of the window. This includes the hooks that the window will
          silence. So a hook knows that it will no longer report before that happens.
        - **Nested windows:** a hook that an outer window already silences does
          not get this callback from an inner window.
        - **End of the window:** the terminal event of the same operation closes
          the window. Each hook gets it after dispatch starts again.
        - **Parent of the work inside:** operations inside the window report the
          suppression as their parent operation. So a mandatory hook records
          the window, what happened in it, and its end.
        - **Not decided yet:** unlike the other observing callbacks, this one
          reports work that is not decided yet. If it fails, dispatch stays on
          and the suppressed body does not run. Genie allows a suppression only
          because of this record.

        Args:
            context: Context of the suppression window. Its terminal event
                shares it, and the operations inside it inherit it.
            suppression: Reach of the window, the hooks that it silences, and
                the mandatory hooks that continue to report through it.
        """

        return

    def on_operation_end(
        self,
        context: HookContext,
        outcome: HookOutcome,
        error: BaseException | None,
    ) -> None:
        """Run exactly once when a logical operation ends.

        - **Always runs:** it runs for each operation that starts, if the
          operation commits, is refused, or fails. The paired callbacks do not
          always run.
        - **Use:** so it is the correct place to close a span, record latency,
          and release state that a before callback saved.
        - **Refusals and failures:** it is also the only callback that sees
          refusals and failures of operations whose before callbacks never ran.
        - **`error`:** the reported `error` is the exception that leaves the
          operation. An `Exception` from this callback never replaces it.

        Args:
            context: Context that the other callbacks of the operation share.
            outcome: Whether the operation committed, was denied, or failed.
            error: Exception that ended the operation, or `None` after success.
        """

        return
