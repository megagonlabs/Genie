"""The memory manager: a conversation in, a set of permitted changes out.

This is the piece that makes LangMem a memory system rather than a store.

- A manager reads an exchange, looks at what is already on file, and returns the
  changes it wants: insert this, patch that in place, remove the one that is now
  wrong.
- Whether each kind of change is allowed is configuration. LangMem gates them
  with `enable_inserts`, `enable_updates`, and `enable_deletes`, and defaults
  deletes to off, so a manager can be given a store it may add to but never
  erase from.

The lamp keeps that separation and the reconciliation that goes with it, and
substitutes only the judgment. Upstream a language model reads the messages and
decides the changes. Here each conversation arrives with its changes already
proposed, which is the same substitution the Graphiti lamp makes when it accepts
structured triplets instead of natural-language extraction.

What still happens here is the part that has to touch storage:

- reading the address to see whether a proposed insert is really an update;
- refusing what the permissions do not allow;
- applying the rest; and
- recording which conversation each write came from.

That last step is a `LineageDAG` edge rather than a stored field, so asking
"what taught the agent this?" is a graph read, and a memory patched by three
conversations has three predecessors without the lamp maintaining a list.
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
    """Applies the changes a conversation proposes, within its permissions."""

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
            store: Collection-shaped memory the manager writes to.
            profiles: Profile-shaped memory the manager revises.
            profile_name: Profile a conversation's profile fields are merged
                into.
            enable_inserts: Whether new memories may be added.
            enable_updates: Whether existing memories may be patched in place.
            enable_deletes: Whether memories may be removed. Off by default,
                as upstream, so forgetting is an explicit decision.
        """

        self.store = store
        self.profiles = profiles
        self.profile_name = required_text(profile_name, field="profile_name")
        self.enable_inserts = bool(enable_inserts)
        self.enable_updates = bool(enable_updates)
        self.enable_deletes = bool(enable_deletes)

    @property
    def permissions(self) -> dict[str, bool]:
        """Return which kinds of change this manager may make."""

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
        """Apply one conversation's proposed changes and report each outcome.

        Args:
            conversation: `Exchange` and the changes proposed from it.
            base: Resolved namespace the conversation's collections sit under.
            source: Block recording the conversation, so each write can be
                traced back to it. Hot-path writes have no such block.
            at: Timestamp used for profile revisions.

        Returns:
            What happened to every proposed change, in proposal order, plus the
            profile if the conversation revised one.

        Raises:
            KeyError: If the conversation revises a profile that is not
                registered in the namespace.
        """

        ran_at = normalize_timestamp(at, field="at")
        target = namespace(*base)
        outcomes = tuple(self._apply_patch(patch, base=target, source=source) for patch in conversation.patches)
        return ManagerRun(
            thread=conversation.thread,
            source_id=None if source is None else source.id,
            ran_at=ran_at,
            outcomes=outcomes,
            profile=self._revise_profile(conversation, base=target, source=source, at=ran_at),
        )

    def _apply_patch(
        self,
        patch: Patch,
        *,
        base: Namespace,
        source: MemoryBlock | None,
    ) -> PatchOutcome:
        """Reconcile one proposed change against the address it targets."""

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
        """Return the change the address actually calls for.

        An extractor proposing an insert has not necessarily looked at what is
        already stored. Upstream the manager reconciles the two by searching
        first; the equivalent read here is the address itself, so a proposal
        that collides with a stored memory becomes a patch of it, and a proposal
        to patch a memory that is gone becomes an insert.
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
    ) -> Profile | None:
        """Merge a conversation's profile fields, recording what changed it."""

        if not conversation.profile:
            return None
        before = self.profiles.require(base, self.profile_name)
        after = self.profiles.update(base, self.profile_name, conversation.profile, at=at)
        if source is not None and after.revision != before.revision:
            self._link(source, after.block, role="revised")
        return after

    def _record_source(self, source: MemoryBlock | None, item: MemoryItem) -> None:
        """Record which conversation a stored memory came from."""

        if source is not None:
            self._link(source, item.block, role="extracted")

    def _link(self, source: MemoryBlock, target: MemoryBlock, *, role: str) -> None:
        """Add one derivation edge unless the pair is already connected."""

        existing = self.store.structures.lineage.successors(source)
        if any(neighbor.block.id == target.id for neighbor in existing):
            return
        self.store.structures.lineage.add(source, target, metadata={"role": role})
