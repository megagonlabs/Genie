"""Tests for principals, roles, and block-level policies."""

from __future__ import annotations

from copy import deepcopy

import pytest

from genie import AccessDenied, AccessRule, Memory, MemoryPolicy, Operation, PolicyEngine, Principal
from genie.policy.policy import BUILTIN_OPERATION_NAMES


def test_block_policy_denies_read_to_other_principal() -> None:
    """Read policies deny principals that are not explicitly allowed."""

    owner_memory = Memory(principal="owner")
    private_policy = MemoryPolicy(read=AccessRule.only(principals={"owner"}))
    private = owner_memory.put(type="secret", value="hidden", policy=private_policy)

    other_memory = Memory(
        backend=owner_memory.backend,
        principal="other",
        policy_engine=owner_memory.policy_engine,
        hooks=owner_memory.hooks,
        codecs=owner_memory.codecs,
    )

    with pytest.raises(AccessDenied):
        other_memory.get(private.id)


def test_builtin_operation_names_match_reserved_policy_names() -> None:
    """The engine enum and policy reserved-name set stay in sync."""

    assert {operation.value for operation in Operation} == BUILTIN_OPERATION_NAMES


def test_write_policy_blocks_set_update_and_delete() -> None:
    """Write policies protect set, update, and delete operations."""

    owner = Memory(principal="owner")
    block = owner.put(
        type="note",
        value={"body": "original"},
        policy=MemoryPolicy(write=AccessRule.only(principals={"owner"})),
    )
    writer = Memory(
        backend=owner.backend,
        principal="writer",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    with pytest.raises(AccessDenied):
        writer.set(block, {"body": "set"})
    with pytest.raises(AccessDenied):
        writer.update(block, value={"body": "updated"})
    with pytest.raises(AccessDenied):
        writer.delete(block)

    retrieved = owner.get(block)
    assert retrieved is not None
    assert retrieved.value == {"body": "original"}


def test_children_ancestors_and_search_filter_inaccessible_blocks() -> None:
    """Traversal and search filter results by read and search policies."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root")
    public_child = owner.put(parent=root, type="note", value="needle public")
    owner.put(
        parent=root,
        type="note",
        value="needle private",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    unsearchable_child = owner.put(
        parent=root,
        type="note",
        value="needle unsearchable",
        policy=MemoryPolicy(search=AccessRule.only(principals={"owner"})),
    )
    private_parent = owner.put(
        type="root",
        value="private parent",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    public_leaf = owner.put(parent=private_parent, type="note", value="public leaf")
    reader = Memory(
        backend=owner.backend,
        principal="reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert [block.id for block in reader.children(root)] == [public_child.id, unsearchable_child.id]
    assert reader.ancestors(public_leaf) == []
    assert [result.block.id for result in reader.search("needle", root=root, scope="subtree")] == [
        public_child.id
    ]


def test_policy_roles_allow_access_without_principal_id_match() -> None:
    """Role-based policies allow matching roles without id matches."""

    owner = Memory(principal="owner")
    policy = MemoryPolicy(read=AccessRule.only(roles={"reader"}))
    block = owner.put(type="note", value="role gated", policy=policy)
    reader = Memory(
        backend=owner.backend,
        principal=Principal(id="agent", roles=frozenset({"reader"})),
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    retrieved = reader.get(block)

    assert retrieved is not None
    assert retrieved.value == "role gated"


def test_policy_can_deny_parent_writes_and_traversal() -> None:
    """Policies can deny child writes and downward traversal."""

    memory = Memory()
    parent = memory.put(
        type="root",
        value="locked",
        policy=MemoryPolicy(write=AccessRule.deny(), traverse_down=AccessRule.deny()),
    )

    with pytest.raises(AccessDenied):
        memory.put(parent=parent, type="child", value="blocked")
    with pytest.raises(AccessDenied):
        memory.children(parent)


def test_builtin_operations_can_be_checked_by_enum_or_string() -> None:
    """The policy engine accepts built-in operations as enums or strings."""

    memory = Memory(principal="owner")
    block = memory.put(
        type="note",
        value="private",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )

    assert memory.policy_engine.allows(Operation.READ, block, memory.principal)
    assert memory.policy_engine.allows("read", block, memory.principal)


def test_policy_engine_supports_block_level_custom_operations() -> None:
    """Application-defined operations can be attached directly to a block policy."""

    owner = Memory(principal="agent:owner")
    block = owner.put(
        type="report",
        value="draft",
        policy=MemoryPolicy(
            operations={"export": AccessRule.only(roles={"compliance"})},
        ),
    )
    compliance = Principal(id="agent:compliance", roles=frozenset({"compliance"}))
    writer = Principal(id="agent:writer", roles=frozenset({"writer"}))

    assert owner.policy_engine.allows("export", block, compliance)
    assert not owner.policy_engine.allows("export", block, writer)

    with pytest.raises(AccessDenied):
        owner.policy_engine.require("export", block, writer)


def test_policy_engine_custom_operation_defaults_can_be_registered() -> None:
    """Engine-level custom operation defaults apply when a block has no override."""

    engine = PolicyEngine(operations={"export": AccessRule.only(roles={"auditor"})})
    memory = Memory(policy_engine=engine)
    default_block = memory.put(type="report", value="default policy")
    override_block = memory.put(
        type="report",
        value="owner export only",
        policy=MemoryPolicy(
            operations={"export": AccessRule.only(principals={"agent:owner"})},
        ),
    )
    auditor = Principal(id="agent:auditor", roles=frozenset({"auditor"}))
    owner = Principal(id="agent:owner")

    assert engine.allows("export", default_block, auditor)
    assert not engine.allows("export", default_block, owner)
    assert engine.allows("export", override_block, owner)
    assert not engine.allows("export", override_block, auditor)


def test_custom_operation_rules_are_deepcopy_friendly() -> None:
    """Policy objects with custom operations can be copied by adapters."""

    policy = MemoryPolicy(operations={"export": AccessRule.only(roles={"auditor"})})
    engine = PolicyEngine(operations={"export": AccessRule.only(roles={"auditor"})})

    copied_policy = deepcopy(policy)
    copied_engine = deepcopy(engine)

    assert copied_policy.operations["export"] == policy.operations["export"]
    assert copied_engine.operations["export"] == engine.operations["export"]


def test_policy_engine_rejects_unknown_and_reserved_custom_operations() -> None:
    """Unknown custom operations are denied with clear configuration errors."""

    memory = Memory()
    block = memory.put(type="note", value="hello")

    with pytest.raises(ValueError, match="Unknown policy operation"):
        memory.policy_engine.allows("export", block, memory.principal)
    with pytest.raises(ValueError, match="cannot override built-ins"):
        MemoryPolicy(operations={"read": AccessRule.deny()})
    with pytest.raises(ValueError, match="cannot override built-ins"):
        PolicyEngine(operations={"write": AccessRule.deny()})
