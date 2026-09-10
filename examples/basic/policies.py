"""Use the `MemoryPolicy.owner_private()` default, then share blocks explicitly.

Run from the repository root:

    python examples/basic/policies.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from _bootstrap import ensure_repo_on_path
else:
    from examples._bootstrap import ensure_repo_on_path

ensure_repo_on_path()

from genie import (
    AccessDenied,
    AccessRule,
    InMemoryBackend,
    Memory,
    MemoryBlock,
    MemoryPolicy,
    PolicyEngine,
    Principal,
)


def export_value(memory: Memory, block: MemoryBlock) -> str:
    """Export a block after checking an application-defined operation.

    Args:
        memory: Memory handle carrying the acting principal.
        block: Block to export.

    Returns:
        String representation of the block value.

    Raises:
        AccessDenied: If the principal cannot perform the custom `"export"`
            operation or cannot read the block.
    """

    memory.policy_engine.require("export", block, memory.principal)
    stored = memory.get(block)
    assert stored is not None
    return str(stored.value)


def main() -> None:
    """Run a principal and role based policy walkthrough."""

    print("== Access policies ==")
    policy_engine = PolicyEngine(
        operations={
            # Custom operations are opt-in. This default says that blocks
            # without an explicit export rule can be exported by compliance.
            "export": AccessRule.only(roles={"compliance"}),
        }
    )
    owner = Memory(
        backend=InMemoryBackend(),
        principal="agent:owner",
        policy_engine=policy_engine,
    )
    owner_only = AccessRule.owner()
    researcher_shared = AccessRule.only(roles={"researcher"}, owner=True)
    compliance_shared = AccessRule.only(roles={"compliance"}, owner=True)
    public = owner.put(type="note", value="Public context", policy=MemoryPolicy.public())
    # No policy argument, so this block gets the `MemoryPolicy.owner_private()`
    # default: only `agent:owner` passes any built-in operation on it.
    private = owner.put(type="note", value="Owner-only reasoning")
    role_gated = owner.put(
        type="note",
        value="Research team context",
        policy=MemoryPolicy(
            read=researcher_shared,
            write=owner_only,
            search=researcher_shared,
            traverse_up=researcher_shared,
            traverse_down=researcher_shared,
        ),
    )
    compliance_export = owner.put(
        type="report",
        value="Compliance export package",
        policy=MemoryPolicy(
            read=compliance_shared,
            write=owner_only,
            search=compliance_shared,
            traverse_up=compliance_shared,
            traverse_down=compliance_shared,
        ),
    )
    owner_export = owner.put(
        type="report",
        value="Owner-only export package",
        policy=MemoryPolicy(
            read=owner_only,
            write=owner_only,
            search=owner_only,
            traverse_up=owner_only,
            traverse_down=owner_only,
            operations={"export": AccessRule.only(principals={"agent:owner"})},
        ),
    )

    reader = Memory(
        backend=owner.backend,
        principal="agent:reader",
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )
    researcher = Memory(
        backend=owner.backend,
        principal=Principal(id="agent:researcher", roles=frozenset({"researcher"})),
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )
    compliance = Memory(
        backend=owner.backend,
        principal=Principal(id="agent:compliance", roles=frozenset({"compliance"})),
        policy_engine=owner.policy_engine,
        hooks=owner.hooks,
        codecs=owner.codecs,
    )

    assert reader.get(public) is not None
    assert owner.get(private) is not None
    assert researcher.get(role_gated) is not None
    assert export_value(compliance, compliance_export) == "Compliance export package"
    assert export_value(owner, owner_export) == "Owner-only export package"

    try:
        reader.get(private)
    except AccessDenied:
        print("ordinary reader reading an owner_private note: blocked")
    else:
        raise AssertionError("An ordinary reader must not read an owner_private note.")
    try:
        reader.policy_engine.require("export", compliance_export, reader.principal)
    except AccessDenied:
        print("ordinary reader exporting a compliance report: blocked")
    else:
        raise AssertionError("An ordinary reader must not export a compliance report.")
    try:
        export_value(compliance, owner_export)
    except AccessDenied:
        print("compliance role exporting an owner-only report: blocked by block override")
    else:
        raise AssertionError("A block policy override must restrict the compliance role.")

    print("allowed checks: public read, researcher read, and compliance export")


if __name__ == "__main__":
    main()
