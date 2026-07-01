"""Tests for public package import surfaces."""

from __future__ import annotations

import importlib


def test_package_all_exports_are_importable() -> None:
    """Every package-level `__all__` member resolves to an attribute."""

    package_names = [
        "genie",
        "genie.backend",
        "genie.codecs",
        "genie.hooks",
        "genie.memory",
        "genie.policy",
        "genie.query",
    ]

    for package_name in package_names:
        package = importlib.import_module(package_name)
        for exported_name in package.__all__:
            assert getattr(package, exported_name) is not None


def test_layered_import_styles_work_for_common_types() -> None:
    """Users can import common types from either `genie` or focused packages."""

    from genie import LinkRelations, Memory, MemoryBlock, PolicyEngine
    from genie.codecs import BytesCodec
    from genie.codecs.stdlib import BytesCodec as StdlibBytesCodec
    from genie.memory import LinkRelations as MemoryLinkRelations
    from genie.memory import Memory as MemoryFromPackage
    from genie.memory import MemoryBlock as MemoryBlockFromPackage
    from genie.policy import PolicyEngine as PolicyEngineFromPackage

    assert MemoryFromPackage is Memory
    assert MemoryBlockFromPackage is MemoryBlock
    assert PolicyEngineFromPackage is PolicyEngine
    assert MemoryLinkRelations.MOUNT == LinkRelations.MOUNT
    assert BytesCodec is StdlibBytesCodec
