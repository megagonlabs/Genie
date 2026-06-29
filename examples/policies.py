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

from genie import AccessDenied, AccessRule, InMemoryBackend, Memory, MemoryPolicy, Principal


def main() -> None:
    """Run a principal and role based policy walkthrough."""

    owner = Memory(backend=InMemoryBackend(), principal="agent:owner")
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

    assert reader.get(public) is not None
    assert researcher.get(role_gated) is not None

    try:
        reader.get(private)
    except AccessDenied as exc:
        print(f"blocked private read: {exc}")

    print("public and role-gated reads succeeded")


if __name__ == "__main__":
    main()
