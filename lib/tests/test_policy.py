"""Tests for principals, roles, and block-level policies."""

from __future__ import annotations

from copy import deepcopy
from typing import Any, cast

import pytest

from genie import (
    AccessDenied,
    AccessRule,
    Memory,
    MemoryBlock,
    MemoryPolicy,
    Operation,
    PolicyEngine,
    Principal,
)
from genie.policy.policy import BUILTIN_OPERATION_NAMES


def test_block_policy_denies_read_to_other_principal() -> None:
    """A read policy denies a principal that it does not explicitly allow."""

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
    """The `Operation` enum and `BUILTIN_OPERATION_NAMES` contain the same names."""

    assert {operation.value for operation in Operation} == BUILTIN_OPERATION_NAMES


def test_write_policy_blocks_set_update_and_delete() -> None:
    """A write policy protects the `set`, `update`, and `delete` operations."""

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
    """`children`, `ancestors`, and search filter their results by the read and search policies."""

    owner = Memory(principal="owner")
    root = owner.put(type="root", value="root", policy=MemoryPolicy.public())
    public_child = owner.put(
        parent=root,
        type="note",
        value="needle public",
        policy=MemoryPolicy.public(),
    )
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
        policy=MemoryPolicy(read=AccessRule.allow(), search=AccessRule.only(principals={"owner"})),
    )
    private_parent = owner.put(
        type="root",
        value="private parent",
        policy=MemoryPolicy(read=AccessRule.only(principals={"owner"})),
    )
    public_leaf = owner.put(
        parent=private_parent,
        type="note",
        value="public leaf",
        policy=MemoryPolicy.public(),
    )
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
    """A role-based policy allows a principal with a role that the policy lists, even if the principal id does not match."""

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


def test_principals_validate_identity_and_isolate_caller_containers() -> None:
    """Principal construction fails closed and copies mutable caller data."""

    roles = {"reader"}
    metadata = {"team": {"name": "search"}}
    principal = Principal(id="agent", roles=roles, metadata=metadata)  # type: ignore[arg-type]

    roles.add("admin")
    metadata["team"]["name"] = "changed"

    assert principal.roles == frozenset({"reader"})
    assert principal.metadata == {"team": {"name": "search"}}
    with pytest.raises(TypeError, match="Principal, string id, or None"):
        Principal.coerce(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="id must not be empty"):
        Principal(id="")


def test_access_rules_and_policies_validate_and_freeze_their_inputs() -> None:
    """Policy objects freeze the input sets and reject malformed rule values."""

    principals = {"agent:reader"}
    rule = AccessRule.only(principals=principals)
    principals.add("agent:admin")

    assert rule.principals == frozenset({"agent:reader"})
    with pytest.raises(TypeError, match="allow_all must be a boolean"):
        AccessRule(allow_all="yes")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="read must be an AccessRule"):
        MemoryPolicy(read="allow")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="must use an AccessRule"):
        MemoryPolicy(operations={"export": "allow"})  # type: ignore[dict-item]
    with pytest.raises(TypeError, match="must use an AccessRule"):
        PolicyEngine(operations={"export": "allow"})  # type: ignore[dict-item]


def test_policy_models_reject_malformed_runtime_container_values() -> None:
    """`Principal`, `AccessRule`, and `MemoryPolicy` reject malformed container values at runtime, also if the caller ignores the type hints."""

    with pytest.raises(TypeError, match="id must be a string"):
        Principal(id=cast(str, 7))
    with pytest.raises(TypeError, match="metadata must be a dictionary"):
        Principal(metadata=cast(dict[str, Any], []))
    with pytest.raises(TypeError, match="roles must be a set"):
        Principal(roles=cast(frozenset[str], ["reader"]))
    with pytest.raises(TypeError, match="roles must contain only strings"):
        Principal(roles=cast(frozenset[str], frozenset({1})))
    with pytest.raises(ValueError, match="roles must not contain empty"):
        Principal(roles=frozenset({""}))

    with pytest.raises(TypeError, match="allow_owner must be a boolean"):
        AccessRule(allow_owner=cast(bool, "yes"))
    with pytest.raises(TypeError, match="principals must be a set"):
        AccessRule(principals=cast(frozenset[str], ["agent"]))
    with pytest.raises(TypeError, match="roles must contain only strings"):
        AccessRule(roles=cast(frozenset[str], frozenset({1})))
    with pytest.raises(ValueError, match="principals must not contain empty"):
        AccessRule(principals=frozenset({""}))

    with pytest.raises(TypeError, match="operations must be a mapping"):
        MemoryPolicy(operations=cast(dict[str, AccessRule], []))
    with pytest.raises(TypeError, match="names must be strings"):
        MemoryPolicy(operations=cast(dict[str, AccessRule], {1: AccessRule.allow()}))
    with pytest.raises(ValueError, match="names must not be empty"):
        MemoryPolicy(operations={"": AccessRule.allow()})


def test_access_rule_defaults_deny_unless_a_grant_is_given() -> None:
    """A rule with no grants denies everyone, and a rule with listed grants allows only those grants, not everyone."""

    alice = Principal(id="alice")
    mallory = Principal(id="mallory", roles=frozenset({"intruder"}))

    assert AccessRule() == AccessRule.deny()
    assert not AccessRule().allows(alice, owner_id="alice")

    by_principal = AccessRule(principals=frozenset({"alice"}))
    assert by_principal.allows(alice)
    assert not by_principal.allows(mallory)

    by_role = AccessRule(roles=frozenset({"reader"}))
    assert by_role.allows(Principal(id="bob", roles=frozenset({"reader"})))
    assert not by_role.allows(mallory)

    by_owner = AccessRule(allow_owner=True)
    assert by_owner == AccessRule.owner()
    assert by_owner.allows(alice, owner_id="alice")
    assert not by_owner.allows(mallory, owner_id="alice")


def test_access_rule_rejects_allow_all_combined_with_grants() -> None:
    """`AccessRule` rejects grants together with `allow_all`, because `allow_all` makes the grants have no effect."""

    with pytest.raises(ValueError, match="allow_all cannot be combined"):
        AccessRule(allow_all=True, principals=frozenset({"alice"}))
    with pytest.raises(ValueError, match="allow_all cannot be combined"):
        AccessRule(allow_all=True, roles=frozenset({"reader"}))
    with pytest.raises(ValueError, match="allow_all cannot be combined"):
        AccessRule(allow_all=True, allow_owner=True)


def test_memory_policy_fields_default_to_owner_only() -> None:
    """A policy field that the caller does not set stays owner-only and does not allow everyone."""

    assert MemoryPolicy() == MemoryPolicy.owner_private()
    public = MemoryPolicy.public()
    assert all(getattr(public, name) == AccessRule.allow() for name in BUILTIN_OPERATION_NAMES)

    alice = Memory(principal="alice")
    block = alice.put(type="note", value="original", policy=MemoryPolicy(read=AccessRule.allow()))
    mallory = Memory(backend=alice.backend, principal="mallory")

    read_by_mallory = mallory.get(block)
    assert read_by_mallory is not None
    assert read_by_mallory.value == "original"
    with pytest.raises(AccessDenied):
        mallory.set(block, "overwritten")
    with pytest.raises(AccessDenied):
        mallory.update(block, value={"text": "updated"})
    with pytest.raises(AccessDenied):
        mallory.delete(block)

    retrieved = alice.get(block)
    assert retrieved is not None
    assert retrieved.value == "original"


def test_memory_rejects_invalid_principals_and_policy_objects_at_entry() -> None:
    """The `Memory` constructor and `put` reject malformed principals, policies, and other components."""

    with pytest.raises(TypeError, match="principal must be"):
        Memory(principal=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="default_policy must be"):
        Memory(default_policy=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="MemoryBackend protocol"):
        Memory(backend=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="policy_engine must be"):
        Memory(policy_engine=object())  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="codecs must be"):
        Memory(codecs=object())  # type: ignore[arg-type]

    memory = Memory()
    with pytest.raises(TypeError, match="policy must be"):
        memory.put(type="note", value="value", policy=object())  # type: ignore[arg-type]


def test_policy_engine_rejects_malformed_evaluation_inputs() -> None:
    """`PolicyEngine.allows` raises a clear error for an invalid operation, block, principal, or policy."""

    engine = PolicyEngine()
    principal = Principal(id="agent")
    block = MemoryBlock(type="note", value="value", created_by=principal.id)

    with pytest.raises(TypeError, match="operation must be"):
        engine.allows(cast(str, 1), block, principal)
    with pytest.raises(ValueError, match="operation must not be empty"):
        engine.allows("", block, principal)
    with pytest.raises(TypeError, match="block must be"):
        engine.allows(Operation.READ, cast(MemoryBlock, object()), principal)
    with pytest.raises(TypeError, match="principal must be"):
        engine.allows(Operation.READ, block, cast(Principal, object()))
    with pytest.raises(TypeError, match="policy must be"):
        engine.allows(
            Operation.READ,
            MemoryBlock(type="note", value="value", policy=cast(MemoryPolicy, "bad")),
            principal,
        )


def test_policy_rule_can_allow_roles_and_block_owner() -> None:
    """A rule can allow both the listed roles and the block creator."""

    owner = Memory(principal="owner")
    policy = MemoryPolicy(read=AccessRule.only(roles={"reader"}, owner=True))
    block = owner.put(type="note", value="shared with owner", policy=policy)
    reader = Memory(
        backend=owner.backend,
        principal=Principal(id="agent", roles=frozenset({"reader"})),
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )
    stranger = Memory(
        backend=owner.backend,
        principal="stranger",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert owner.get(block) is not None
    assert reader.get(block) is not None
    with pytest.raises(AccessDenied):
        stranger.get(block)


def test_memory_defaults_new_blocks_to_owner_private_policy() -> None:
    """If the application does not set `default_policy`, new blocks use `MemoryPolicy.owner_private()`."""

    owner = Memory(principal="agent:owner")
    block = owner.put(type="note", value="owner default")
    reader = Memory(
        backend=owner.backend,
        principal="agent:reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert block.policy == MemoryPolicy.owner_private()
    assert owner.get(block) is not None
    with pytest.raises(AccessDenied):
        reader.get(block)


def test_memory_default_policy_can_be_configured_to_public() -> None:
    """Applications can choose a different default policy for new blocks."""

    writer = Memory(principal="agent:writer", default_policy=MemoryPolicy.public())
    block = writer.put(type="note", value="shared by default")
    reader = Memory(
        backend=writer.backend,
        principal="agent:reader",
        policy_engine=writer.policy_engine,
        hooks=writer.hooks,
        codecs=writer.codecs,
    )

    assert block.policy == MemoryPolicy.public()
    assert reader.get(block) is not None


def test_owner_or_policy_allows_creator_and_configured_principal() -> None:
    """`MemoryPolicy.owner_or` can allow both the block creator and a supervisor principal."""

    memory = Memory(
        principal="service:memory",
        default_policy=MemoryPolicy.owner_or(principals={"service:memory"}),
    )
    root = memory.put(
        type="root",
        value="shared workspace",
        policy=MemoryPolicy.owner_or(principals={"service:memory", "agent:writer"}),
    )
    agent = memory.get_handle(root=root, principal="agent:writer")

    agent_note = agent.put(type="note", value="agent-authored")
    reader = Memory(
        backend=memory.backend,
        principal="agent:reader",
        policy_engine=memory.policy_engine,
        hooks=memory.hooks,
        codecs=memory.codecs,
        default_policy=memory.default_policy,
    )

    assert agent_note.created_by == "agent:writer"
    assert agent.get(agent_note) is not None
    assert memory.get(agent_note) is not None
    with pytest.raises(AccessDenied):
        reader.get(agent_note)


def test_owner_or_policy_allows_configured_roles() -> None:
    """`MemoryPolicy.owner_or` can allow roles instead of fixed principal ids."""

    writer = Memory(principal="agent:writer", default_policy=MemoryPolicy.owner_or(roles={"memory_admin"}))
    block = writer.put(type="note", value="role-admin visible")
    admin = Memory(
        backend=writer.backend,
        principal=Principal(id="service:memory", roles=frozenset({"memory_admin"})),
        policy_engine=writer.policy_engine,
        hooks=writer.hooks,
        codecs=writer.codecs,
        default_policy=writer.default_policy,
    )

    assert admin.get(block) is not None


def test_handle_writes_preserve_configured_default_policy() -> None:
    """`MemoryHandle` writes use the same `default_policy` as their parent memory."""

    memory = Memory(principal="agent:owner", default_policy=MemoryPolicy.public())
    root = memory.put(type="root", value="shared root")
    writer = memory.get_handle(root=root, principal="agent:writer")

    child = writer.put(type="note", value="writer-owned")
    reader = Memory(
        backend=memory.backend,
        principal="agent:reader",
        policy_engine=memory.policy_engine,
        hooks=memory.hooks,
        codecs=memory.codecs,
    )

    assert child.created_by == "agent:writer"
    assert child.policy == MemoryPolicy.public()
    assert reader.get(child) is not None


def test_owner_rules_use_block_created_by_for_missing_policy_fallback() -> None:
    """The policy engine uses `created_by` to treat a block with no policy as owner-private."""

    engine = PolicyEngine()
    block = MemoryBlock(id="imported", type="note", value="stored", created_by="agent:owner")
    owner = Principal(id="agent:owner")
    reader = Principal(id="agent:reader")

    assert engine.allows(Operation.READ, block, owner)
    assert not engine.allows(Operation.READ, block, reader)


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
    """An application can attach its own operations directly to a block policy."""

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
    """A custom operation default on the engine applies if the block policy has no override."""

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
    """Adapters can copy policy objects that have custom operations with `deepcopy`."""

    policy = MemoryPolicy(operations={"export": AccessRule.only(roles={"auditor"})})
    engine = PolicyEngine(operations={"export": AccessRule.only(roles={"auditor"})})

    copied_policy = deepcopy(policy)
    copied_engine = deepcopy(engine)

    assert copied_policy.operations["export"] == policy.operations["export"]
    assert copied_engine.operations["export"] == engine.operations["export"]
    assert "export" in repr(policy.operations)


def test_policy_engine_rejects_unknown_and_reserved_custom_operations() -> None:
    """The policy engine raises `ValueError` for an unknown custom operation, and policies reject a custom operation that overrides a built-in."""

    memory = Memory()
    block = memory.put(type="note", value="hello")

    with pytest.raises(ValueError, match="Unknown policy operation"):
        memory.policy_engine.allows("export", block, memory.principal)
    with pytest.raises(ValueError, match="cannot override built-ins"):
        MemoryPolicy(operations={"read": AccessRule.deny()})
    with pytest.raises(ValueError, match="cannot override built-ins"):
        PolicyEngine(operations={"write": AccessRule.deny()})
