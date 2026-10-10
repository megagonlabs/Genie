"""Optional structural building blocks for memory systems."""

from genie.structures.access import (
    AccessChecker,
    AcyclicLinkWriter,
    BlockReader,
    BlockRef,
    BlockWriter,
    LinkDirection,
    LinkReader,
    LinkWriter,
    MemoryAccess,
    SearchAccess,
    TreeReader,
)
from genie.structures.graph import GraphAccess, PropertyGraph
from genie.structures.keyed import KeyedAccess, KeyedCollection
from genie.structures.lineage import LineageAccess, LineageDAG
from genie.structures.log import EventLog, SequenceAccess
from genie.structures.records import (
    GraphNeighbor,
    GraphPath,
    KeyBinding,
    KeyedItem,
    LogEntry,
    LogItem,
    MemoryAddress,
    RevisionEntry,
    RevisionItem,
    TemporalEntry,
    TemporalItem,
    ValidityInterval,
)
from genie.structures.temporal import TemporalAccess, TemporalCollection
from genie.structures.tree import OwnershipAccess, OwnershipTree
from genie.structures.versioned import VersionedAccess, VersionedRecords, VersionedStore

__all__ = [
    "AccessChecker",
    "AcyclicLinkWriter",
    "BlockReader",
    "BlockRef",
    "BlockWriter",
    "EventLog",
    "GraphAccess",
    "GraphNeighbor",
    "GraphPath",
    "KeyBinding",
    "KeyedAccess",
    "KeyedCollection",
    "KeyedItem",
    "LineageAccess",
    "LineageDAG",
    "LinkDirection",
    "LinkReader",
    "LinkWriter",
    "LogEntry",
    "LogItem",
    "MemoryAccess",
    "MemoryAddress",
    "OwnershipAccess",
    "OwnershipTree",
    "PropertyGraph",
    "RevisionEntry",
    "RevisionItem",
    "SearchAccess",
    "SequenceAccess",
    "TemporalAccess",
    "TemporalCollection",
    "TemporalEntry",
    "TemporalItem",
    "TreeReader",
    "ValidityInterval",
    "VersionedAccess",
    "VersionedRecords",
    "VersionedStore",
]
