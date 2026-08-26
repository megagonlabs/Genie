"""Tests for public package import surfaces."""

from __future__ import annotations

import importlib

import pytest


def test_package_all_exports_are_importable() -> None:
    """Every package-level `__all__` member resolves to an attribute."""

    package_names = [
        "genie",
        "genie.backend",
        "genie.backend.composite",
        "genie.backend.graph",
        "genie.backend.in_memory",
        "genie.backend.key_value",
        "genie.backend.relational",
        "genie.codecs",
        "genie.hooks",
        "genie.memory",
        "genie.policy",
        "genie.query",
        "genie.structures",
    ]

    for package_name in package_names:
        package = importlib.import_module(package_name)
        for exported_name in package.__all__:
            assert getattr(package, exported_name) is not None


def test_layered_import_styles_work_for_common_types() -> None:
    """Users can import common types from either `genie` or focused packages."""

    from genie import (
        AcyclicLinkStore,
        KeyedStore,
        LinkRelations,
        Memory,
        MemoryBlock,
        MemoryHandle,
        Neo4jBackend,
        PolicyEngine,
        RedisBackend,
        SequenceStore,
        SQLiteBackend,
        TemporalIndex,
        TemporalStore,
    )
    from genie.backend.composite import RoutedBackend as CompositeRoutedBackend
    from genie.backend.graph import Neo4jBackend as GraphNeo4jBackend
    from genie.backend.in_memory import InMemoryBackend as GroupedInMemoryBackend
    from genie.backend.key_value import RedisBackend as KeyValueRedisBackend
    from genie.backend.relational import SQLiteBackend as RelationalSQLiteBackend
    from genie.codecs import BytesCodec
    from genie.codecs.stdlib import BytesCodec as StdlibBytesCodec
    from genie.memory import LinkRelations as MemoryLinkRelations
    from genie.memory import Memory as MemoryFromPackage
    from genie.memory import MemoryBlock as MemoryBlockFromPackage
    from genie.memory import MemoryHandle as MemoryHandleFromPackage
    from genie.policy import PolicyEngine as PolicyEngineFromPackage
    from genie.structures import (
        AcyclicLinkWriter,
        EventLog,
        KeyedCollection,
        LineageDAG,
        MemoryAccess,
        OwnershipTree,
        PropertyGraph,
        TemporalCollection,
        TemporalItem,
        ValidityInterval,
        VersionedRecords,
        VersionedStore,
    )

    assert MemoryFromPackage is Memory
    assert MemoryBlockFromPackage is MemoryBlock
    assert MemoryHandleFromPackage is MemoryHandle
    assert PolicyEngineFromPackage is PolicyEngine
    assert MemoryLinkRelations.MOUNT == LinkRelations.MOUNT
    assert BytesCodec is StdlibBytesCodec
    assert MemoryAccess is not None
    assert AcyclicLinkWriter is not None
    assert AcyclicLinkStore is not None
    assert EventLog is not None
    assert KeyedCollection is not None
    assert LineageDAG is not None
    assert OwnershipTree is not None
    assert PropertyGraph is not None
    assert KeyedStore is not None
    assert SequenceStore is not None
    assert SQLiteBackend is not None
    assert Neo4jBackend is GraphNeo4jBackend
    assert RedisBackend is KeyValueRedisBackend
    assert CompositeRoutedBackend is not None
    assert GroupedInMemoryBackend is not None
    assert RelationalSQLiteBackend is SQLiteBackend
    assert TemporalIndex is not None
    assert TemporalStore is not None
    assert TemporalCollection is not None
    assert TemporalItem is not None
    assert ValidityInterval is not None
    assert VersionedRecords is not None
    assert VersionedStore is not None


def test_lazy_import_modules_reject_unknown_attributes() -> None:
    """Focused lazy-import packages fail with normal attribute errors."""

    from genie import memory, policy

    with pytest.raises(AttributeError, match="Missing"):
        memory.__getattr__("Missing")
    with pytest.raises(AttributeError, match="Missing"):
        policy.__getattr__("Missing")
