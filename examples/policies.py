"""Attach policies to memory blocks and read them through different principals.

Run from the repository root:

    conda run -n "genie" python examples/policies.py
"""

from __future__ import annotations

if __package__ in {None, ""}:
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
    public = owner.put(type="note", value="Public context")
    private = owner.put(
        type="note",
        value="Owner-only reasoning",
        policy=MemoryPolicy(read=AccessRule.only(principals={"agent:owner"})),
    )
    role_gated = owner.put(
        type="note",
        value="Research team context",
        policy=MemoryPolicy(read=AccessRule.only(roles={"researcher"})),
    )
    compliance_export = owner.put(type="report", value="Compliance export package")
    owner_export = owner.put(
        type="report",
        value="Owner-only export package",
        policy=MemoryPolicy(
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
    assert researcher.get(role_gated) is not None
    assert export_value(compliance, compliance_export) == "Compliance export package"
    assert export_value(owner, owner_export) == "Owner-only export package"

    try:
        reader.get(private)
    except AccessDenied as exc:
        print(f"blocked private read: {exc}")
    try:
        reader.policy_engine.require("export", compliance_export, reader.principal)
    except AccessDenied as exc:
        print(f"blocked reader export: {exc}")
    try:
        export_value(compliance, owner_export)
    except AccessDenied as exc:
        print(f"blocked compliance export override: {exc}")

    print("public reads, role-gated reads, and custom export checks succeeded")


if __name__ == "__main__":
    main()
