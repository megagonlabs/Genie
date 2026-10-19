"""The memory manager: it reads a conversation and applies the permitted changes.

This part makes LangMem a memory system, not only a store.

- **Proposal:** a manager reads an exchange and examines the stored memories.
  Then it returns the changes that it wants: insert this memory, patch that
  memory in place, remove the memory that is now wrong.
- **Permissions:** configuration sets which kinds of change are allowed.
  LangMem controls them with `enable_inserts`, `enable_updates`, and
  `enable_deletes`. Deletes are off by default. So a configuration can give a
  manager a store that it can add to but never erase from.

The lamp keeps this separation and the reconciliation that goes with it. It
replaces only the judgment:

- **Upstream:** a language model reads the messages and decides the changes.
- **Here:** each conversation comes with its changes already proposed. The
  Graphiti lamp makes the same replacement: it accepts structured triplets
  instead of natural-language extraction.

This module still does the steps that touch storage:

- **Reconcile:** it reads the address to find if a proposed insert is really an
  update.
- **Refuse:** it refuses the changes that the permissions do not allow.
- **Apply:** it applies the other changes.
- **Provenance:** it records the conversation that each write came from.

Provenance is a `LineageDAG` edge, not a stored field. So the question "what
taught the agent this?" is a graph read. A memory that 3 conversations patched
has 3 predecessors, and the lamp keeps no list.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime

from examples.lamps.langmem.model import (
    Conversation,
    ManagerRun,
    MemoryItem,
    Namespace,
    Patch,
    PatchAction,
    PatchOutcome,
    Profile,
    namespace,
    normalize_timestamp,
    required_text,
)
from examples.lamps.langmem.profiles import ProfileStore
from examples.lamps.langmem.store import MemoryStore
from genie import MemoryBlock


class MemoryManager:
    """Applies the changes that a conversation proposes, within its permissions."""

    def __init__(
        self,
        store: MemoryStore,
        profiles: ProfileStore,
        *,
        profile_name: str = "profile",
        enable_inserts: bool = True,
        enable_updates: bool = True,
        enable_deletes: bool = False,
    ) -> None:
        """Create a manager over one store and its profiles.

        Args:
            store: Collection-shaped memory that the manager writes to.
            profiles: Profile-shaped memory that the manager revises.
            profile_name: Name of the profile that receives the profile fields
                of a conversation.
            enable_inserts: If `True`, the manager can add new memories.
            enable_updates: If `True`, the manager can patch stored memories in
                place. A profile revision also needs this permission.
            enable_deletes: If `True`, the manager can remove memories. It is
                off by default, as upstream, so the decision to forget is
                always explicit.
        """

        self.store = store
        self.profiles = profiles
        self.profile_name = required_text(profile_name, field="profile_name")
        self.enable_inserts = bool(enable_inserts)
        self.enable_updates = bool(enable_updates)
        self.enable_deletes = bool(enable_deletes)

    @property
    def permissions(self) -> dict[str, bool]:
        """Return the kinds of change that this manager can make."""

        return {
            "insert": self.enable_inserts,
            "update": self.enable_updates,
            "delete": self.enable_deletes,
        }

    def apply(
        self,
        conversation: Conversation,
        *,
        base: Sequence[str],
        source: MemoryBlock | None = None,
        at: datetime,
    ) -> ManagerRun:
        """Apply the proposed changes of one conversation, and report each outcome.

        Args:
            conversation: The exchange and its proposed changes.
            base: Resolved namespace that contains the collections of the
                conversation.
            source: Block that records the conversation. The manager links
                each write to it, so a reader can trace the write back. `None`
                if no recorded conversation exists, for example for a direct
                call. Then the manager adds no lineage edge.
            at: Timestamp for the run and for profile revisions.

        Returns:
            The outcome of each proposed change, in proposal order. If the
            conversation revised the profile, the result also has the profile.
            If the manager refused the revision, the result also has the
            reason.

        Raises:
            KeyError: If the conversation revises a profile that is not
                registered in the namespace.
        """

        ran_at = normalize_timestamp(at, field="at")
        target = namespace(*base)
        outcomes = tuple(self._apply_patch(patch, base=target, source=source) for patch in conversation.patches)
        profile, profile_reason = self._revise_profile(conversation, base=target, source=source, at=ran_at)
        return ManagerRun(
            thread=conversation.thread,
            source_id=None if source is None else source.id,
            ran_at=ran_at,
            outcomes=outcomes,
            profile=profile,
            profile_reason=profile_reason,
        )

    def _apply_patch(
        self,
        patch: Patch,
        *,
        base: Namespace,
        source: MemoryBlock | None,
    ) -> PatchOutcome:
        """Reconcile one proposed change with its target address, and apply it if the permissions allow it."""

        target = namespace(*base, patch.collection)
        existing = self.store.get(target, patch.key)
        action = self._reconcile(patch.action, occupied=existing is not None)
        if not self.permissions[action]:
            return PatchOutcome(
                patch=patch,
                action=action,
                applied=False,
                reason=f"{action} is not enabled for this manager",
                item=existing,
            )
        if action == "delete":
            if existing is None:
                return PatchOutcome(patch, action, False, "nothing is stored at that address")
            return PatchOutcome(patch, action, True, "removed the stored memory", self.store.delete(target, patch.key))
        item = (
            self.store.insert(target, patch.key, patch.value)
            if action == "insert"
            else self.store.update(target, patch.key, patch.value)
        )
        self._record_source(source, item)
        reason = "stored a new memory" if action == "insert" else f"patched the memory in place, now version {item.version}"
        return PatchOutcome(patch, action, True, reason, item)

    def _reconcile(self, action: PatchAction, *, occupied: bool) -> PatchAction:
        """Return the change that the address really needs.

        - **Problem:** an extractor that proposes an insert possibly did not
          examine the stored memories.
        - **Upstream:** the manager does a search first to reconcile the two.
        - **Here:** the equivalent read is the address itself. A proposed insert
          that collides with a stored memory becomes a patch of that memory. A
          proposed patch of a memory that is gone becomes an insert.
        """

        if action == "insert" and occupied:
            return "update"
        if action == "update" and not occupied:
            return "insert"
        return action

    def _revise_profile(
        self,
        conversation: Conversation,
        *,
        base: Namespace,
        source: MemoryBlock | None,
        at: datetime,
    ) -> tuple[Profile | None, str | None]:
        """Merge the profile fields of a conversation, and record the source of the change.

        - **Permission:** the permissions also apply to this write. Only
          `register` creates a profile. A revision always patches the profile
          in place, so it needs the `update` permission.
        - **Refusal:** the method returns a refusal and does not raise it, as
          for a refused patch. The profile stays at its current version.
        - **Lineage:** if a source block exists and the merge wrote a new
          revision, the method links the source to that revision.
        """

        if not conversation.profile:
            return None, None
        before = self.profiles.require(base, self.profile_name)
        if not self.permissions["update"]:
            return before, "update is not enabled for this manager"
        after = self.profiles.update(base, self.profile_name, conversation.profile, at=at)
        if source is not None and after.revision != before.revision:
            self._link(source, after.block, role="revised")
        return after, None

    def _record_source(self, source: MemoryBlock | None, item: MemoryItem) -> None:
        """Record the conversation that a stored memory came from."""

        if source is not None:
            self._link(source, item.block, role="extracted")

    def _link(self, source: MemoryBlock, target: MemoryBlock, *, role: str) -> None:
        """Add one derivation edge, if the pair has no edge yet."""

        existing = self.store.structures.lineage.successors(source)
        if any(neighbor.block.id == target.id for neighbor in existing):
            return
        self.store.structures.lineage.add(source, target, metadata={"role": role})
