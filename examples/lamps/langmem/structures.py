"""Genie facades wired to the LangMem lamp's five ownership tiers.

LangMem stores memories in a namespaced key-value store with vector search. The
namespace is only a name there: nothing owns it, so nothing happens to the
memories inside it when a user goes away.

This lamp mirrors every namespace as an ownership path instead, which buys two
things a plain key-value store cannot give:

- Prefix search becomes a Genie subtree scope rather than a filter the lamp
  applies afterwards.
- Deleting a namespace deletes everything that belongs to the user: the items
  addressed inside it, their profile, and any conversation still queued for
  them.

Ownership and structure membership are separate, and that is what makes the
second point work. A tier anchor is where a structure keeps its bindings, not
where the blocks live. So a profile identity and a queued submission are owned
by the user's namespace block while still being registered with the profile
records and appended to the queue log. Retiring a user is one `delete_subtree`,
and the bindings drop with the blocks they pointed at.

The one deliberate exception is prompts. A prompt is the agent's own
instructions rather than one user's memory, so prompt identities are owned by
the prompt tier and outlive any user.

Two other wiring choices carry most of the model:

- Profiles and prompts are both `VersionedRecords`, because both are single
  documents that are revised rather than accumulated, and both must be able to
  show what they used to say.
- The deferred reflection queue anchors an `EventLog` and a
  `TemporalCollection` on the same tier. The log is the order submissions
  arrived, and the temporal entry is when each one comes due, so LangMem's
  debounce window becomes a time query and a cancelled submission stays on the
  log.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol

from examples.lamps.langmem.model import (
    ANCHOR_TYPE,
    DERIVED_FROM_RELATION,
    NAMESPACE_TYPE,
    TIERS,
    namespace,
    namespace_path,
)
from genie import MemoryBlock, MemoryHandle
from genie.backend import AcyclicLinkStore, KeyedStore, SequenceStore, TemporalStore
from genie.structures import (
    EventLog,
    KeyedCollection,
    LineageDAG,
    OwnershipTree,
    TemporalCollection,
    VersionedRecords,
)


class LangMemStructureStore(
    AcyclicLinkStore,
    KeyedStore,
    SequenceStore,
    TemporalStore,
    Protocol,
):
    """Backend capabilities required by the LangMem structure bundle."""


@dataclass(frozen=True, slots=True)
class LangMemStructures:
    """The structural views this lamp composes over one memory store.

    Attributes:
        tiers: Ownership view of the five tiers below the store root.
        items: Namespaced addresses of every stored memory.
        profiles: One identity per profile, one revision per update. The
            identities are owned by the namespace they belong to, not by this
            tier.
        prompts: One identity per prompt, one revision per optimization.
        queue: Append-only order in which conversations were submitted. The
            submissions are owned by the namespace they will write to, not by
            this tier.
        schedule: When each queued submission comes due.
        lineage: Acyclic derivation edges from conversations to the memories
            they wrote and from trajectories to the prompt versions they
            produced.
        namespaces: Block that owns the mirrored namespace paths.
        trajectories: Block that owns annotated past exchanges.
    """

    tiers: OwnershipTree
    items: KeyedCollection
    profiles: VersionedRecords
    prompts: VersionedRecords
    queue: EventLog
    schedule: TemporalCollection
    lineage: LineageDAG
    namespaces: MemoryBlock
    trajectories: MemoryBlock

    @classmethod
    def create(
        cls,
        memory: MemoryHandle,
        store: LangMemStructureStore,
    ) -> LangMemStructures:
        """Build or reopen every structural anchor below one bounded root.

        Existing anchors are found by scanning the root's children, so
        reopening a store returns the same structures instead of duplicating
        them.

        Args:
            memory: Handle rooted at the block that owns the whole store.
            store: Backend providing acyclic-link, keyed, sequence, and
                temporal capabilities.

        Returns:
            The structure bundle for that store.

        Raises:
            ValueError: If the handle is unbounded.
        """

        if memory.root_id is None:
            raise ValueError("LangMemStructures requires a bounded MemoryHandle.")
        tiers = OwnershipTree(memory, root=memory.root_id)
        anchors = {name: _anchor(tiers, name) for name in TIERS}
        return cls(
            tiers=tiers,
            items=KeyedCollection(memory, store, collection=anchors["namespaces"]),
            profiles=VersionedRecords(memory, store, collection=anchors["profiles"]),
            prompts=VersionedRecords(memory, store, collection=anchors["prompts"]),
            queue=EventLog(memory, store, log=anchors["queue"]),
            schedule=TemporalCollection(memory, store, collection=anchors["queue"]),
            lineage=LineageDAG(memory, relation=DERIVED_FROM_RELATION),
            namespaces=anchors["namespaces"],
            trajectories=anchors["trajectories"],
        )

    @property
    def prompt_root(self) -> MemoryBlock:
        """Return the block that owns prompt identities and revisions.

        Prompts belong to the agent rather than to one user, so this tier is
        their ownership parent and they survive any namespace being retired.
        """

        return self.prompts.collection

    def namespace_node(self, value: Sequence[str]) -> MemoryBlock | None:
        """Return the block that owns one namespace, when it exists.

        Args:
            value: Namespace to look up.

        Returns:
            The namespace block, or `None` when the path was never created.
        """

        node = self.namespaces
        for segment in namespace(*value):
            child = self._segment_child(node, segment)
            if child is None:
                return None
            node = child
        return node

    def ensure_namespace(self, value: Sequence[str]) -> MemoryBlock:
        """Return the block owning one namespace, creating the path as needed.

        Everything that belongs to one user hangs off this block: the items
        addressed inside the namespace, the user's profile, and the
        conversations queued for them. That is what makes retiring a user one
        `delete_subtree` rather than a cleanup list.

        Args:
            value: Namespace to create or reopen.

        Returns:
            The block owning the last segment.
        """

        node = self.namespaces
        path: list[str] = []
        for segment in namespace(*value):
            path.append(segment)
            child = self._segment_child(node, segment)
            if child is None:
                child = self.tiers.create_child(
                    parent=node,
                    type=NAMESPACE_TYPE,
                    value={"segment": segment, "path": namespace_path(path)},
                    metadata={"segment": segment, "path": namespace_path(path)},
                )
            node = child
        return node

    def _segment_child(self, node: MemoryBlock, segment: str) -> MemoryBlock | None:
        """Return the namespace child of `node` named `segment`, if any."""

        return next(
            (
                child
                for child in self.tiers.children(node)
                if child.type == NAMESPACE_TYPE and child.metadata.get("segment") == segment
            ),
            None,
        )


def _anchor(tiers: OwnershipTree, name: str) -> MemoryBlock:
    """Return the tier anchor with this name, creating it when absent.

    Anchors are matched on metadata rather than on value so that a tier which
    keeps state in its own value can still be found again.
    """

    existing = next(
        (
            child
            for child in tiers.children()
            if child.type == ANCHOR_TYPE and child.metadata.get("tier") == name
        ),
        None,
    )
    if existing is not None:
        return existing
    return tiers.create_child(
        type=ANCHOR_TYPE,
        value={"tier": name},
        metadata={"tier": name},
    )
