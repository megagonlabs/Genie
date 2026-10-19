"""Genie facades connected to the 5 ownership tiers of the LangMem lamp.

LangMem stores memories in a namespaced key-value store with vector search.
There, a namespace is only a name. Nothing owns it, so nothing happens to the
memories in it when a user leaves.

This lamp mirrors each namespace as an ownership path. This gives 2 results
that a plain key-value store cannot give:

- **Prefix search:** it becomes a Genie subtree scope, not a filter that the
  lamp applies after the search.
- **Namespace delete:** it deletes everything that belongs to the user: the
  items at addresses in the namespace, the user profile, and each conversation
  that is still on the queue for the user.

Ownership and structure membership are separate. This makes the namespace
delete work:

- **Tier anchor:** a structure keeps its bindings at a tier anchor. The tier
  anchor does not own the blocks.
- **User blocks:** the namespace block of the user owns the profile identity
  and each queued submission. The profile records still register the identity,
  and the queue log still contains the submission.
- **One delete:** one `delete_subtree` call retires a user. The bindings go
  away with the blocks that they referred to.
- **One exception, on purpose:** prompts. A prompt is the instructions of the
  agent, not the memory of one user. So the prompt tier owns the prompt
  identities, and they stay after the lamp retires a user.

The model also depends on 2 other design choices:

- **`VersionedRecords` for profiles and prompts:** both are single documents.
  Each write revises the document and does not add a new document. Both can
  show their earlier text.
- **Reflection queue:** an `EventLog` and a `TemporalCollection` have their
  anchor on the same tier. The log keeps the arrival order of the submissions.
  The temporal entry keeps the due time of each submission. So the LangMem
  debounce window becomes a time query, and a cancelled submission stays on
  the log.
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
    """Backend capabilities that the LangMem structure bundle requires."""


@dataclass(frozen=True, slots=True)
class LangMemStructures:
    """The structure views that this lamp composes over one memory store.

    Attributes:
        tiers: Ownership view of the 5 tiers below the store root.
        items: Namespaced addresses of all stored memories.
        profiles: One identity for each profile, and one revision for each
            update. The namespace of each identity owns it. This tier does not
            own it.
        prompts: One identity for each prompt, and one revision for each
            optimization.
        queue: Append-only order of the conversation submissions. The
            namespace that a submission writes to owns it. This tier does not
            own it.
        schedule: The due time of each queued submission.
        lineage: Acyclic derivation edges. They go from conversations to the
            memories and profile revisions that they wrote, and from
            trajectories to the prompt versions that they produced.
        namespaces: Block that owns the mirrored namespace paths.
        trajectories: Block that owns the annotated past exchanges.
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
        """Build or reopen all structure anchors below one bounded root.

        The method finds the anchors that exist in the children of the root.
        So when a caller reopens a store, it gets the same structures and no
        duplicates.

        Args:
            memory: Handle whose root is the block that owns the whole store.
            store: Backend with the acyclic-link, keyed, sequence, and temporal
                capabilities.

        Returns:
            The structure bundle for that store.

        Raises:
            ValueError: If the handle is not bounded.
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
        """Return the block that owns the prompt identities and revisions.

        Prompts belong to the agent, not to one user. So this tier is their
        ownership parent, and they stay when the lamp retires a namespace.
        """

        return self.prompts.collection

    def namespace_node(self, value: Sequence[str]) -> MemoryBlock | None:
        """Return the block that owns one namespace, if it exists.

        Args:
            value: Namespace to find.

        Returns:
            The namespace block, or `None` if the path does not exist.
        """

        node = self.namespaces
        for segment in namespace(*value):
            child = self._segment_child(node, segment)
            if child is None:
                return None
            node = child
        return node

    def ensure_namespace(self, value: Sequence[str]) -> MemoryBlock:
        """Return the block that owns one namespace, and create the path if necessary.

        This block owns everything that belongs to one user: the items at
        addresses in the namespace, the user profile, and the queued
        conversations of the user. So one `delete_subtree` call retires a
        user, and no cleanup list is necessary.

        Args:
            value: Namespace to create or reopen.

        Returns:
            The block that owns the last segment.
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
        """Return the namespace child of `node` with the name `segment`, or `None`."""

        return next(
            (
                child
                for child in self.tiers.children(node)
                if child.type == NAMESPACE_TYPE and child.metadata.get("segment") == segment
            ),
            None,
        )


def _anchor(tiers: OwnershipTree, name: str) -> MemoryBlock:
    """Return the tier anchor with this name, and create it if it does not exist.

    The function matches anchors on metadata, not on value. So it can find a
    tier again, even if the tier keeps state in its own value.
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
