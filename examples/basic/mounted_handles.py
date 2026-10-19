"""Let a bounded handle follow symbolic mounts and keep its subtree boundary.

Mount access is a separate handle capability (`allow_mounts`).

- **Without `allow_mounts`:** a handle cannot list, discover, resolve, or create
  mounts.
- **With `allow_mounts`:** a handle can resolve a mount and get a block outside
  its own subtree.
- **Same boundary:** ordinary reads, searches, and deletes stay inside the
  boundary.
- **No escalation:** a sub-handle cannot give itself a capability that its
  parent does not have.

Run from the repository root:

    python examples/basic/mounted_handles.py
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

from genie import AccessDenied, InMemoryBackend, Memory, MemoryPolicy


def main() -> None:
    """Compare a bounded handle with and without mount access."""

    print("== Mounted handles ==")
    # This example shows handle capabilities, not policies. So
    # `MemoryPolicy.public()` lets every principal read the blocks.
    memory = Memory(
        backend=InMemoryBackend(),
        principal="system",
        default_policy=MemoryPolicy.public(),
    )
    project = memory.put(type="project", value="Rollout plan")
    task = memory.put(parent=project, type="task", value="Draft the checklist")
    reference = memory.put(type="project", value="Shared compliance reference")
    reference_note = memory.put(parent=reference, type="note", value="Retention is 30 days")

    # Trusted application code decides which memory a subtree can refer to.
    mount = memory.add_mount(project, reference, metadata={"label": "compliance"})

    plain = memory.get_handle(root=project, principal="agent:planner")
    linked = memory.get_handle(root=project, principal="agent:auditor", allow_mounts=True)

    # Without the capability, the handle raises `AccessDenied` for every mount operation.
    try:
        plain.list_mounts(project)
    except AccessDenied:
        print("listing mounts without the capability: blocked as expected")
    else:
        raise AssertionError("A handle without mount access must not list mounts.")
    try:
        plain.resolve_mount(mount)
    except AccessDenied:
        print("resolving a mount without the capability: blocked as expected")
    else:
        raise AssertionError("A handle without mount access must not resolve mounts.")

    # With the capability, the handle lists, discovers, and resolves the mount.
    mounts = linked.list_mounts(project)
    discovered = linked.discover_mounts(project)
    target = linked.resolve_mount(mount)

    print(f"mounts visible to the auditor handle: {len(mounts)}")
    print(f"mount label: {mounts[0].metadata['label']}")
    print(f"resolved target outside the handle's subtree: {target.value if target else None}")

    assert mounts == [mount]
    assert discovered == [mount]
    assert target is not None
    assert target.id == reference.id

    # A resolve is explicit and does not make the boundary wider. For each
    # ordinary operation, the mounted tree stays outside the subtree of the handle.
    assert linked.search("retention", scope="subtree") == []
    assert [block.id for block in linked.children(project)] == [task.id]
    try:
        linked.get(reference_note)
    except AccessDenied:
        print("reading inside the mounted tree: still blocked")
    else:
        raise AssertionError("A mount must not widen ordinary subtree reads.")

    # A sub-handle cannot add a capability that its parent does not have.
    try:
        plain.get_handle(root=task, allow_mounts=True)
    except AccessDenied:
        print("granting mount access from a handle that lacks it: blocked as expected")
    else:
        raise AssertionError("A sub-handle must not escalate its capabilities.")

    # A sub-handle can keep mount access. But its narrower root puts the mount
    # source (the project) outside its subtree, so it cannot resolve the mount.
    # The capability does not bypass the boundary.
    nested = linked.get_handle(root=task, allow_mounts=True)
    assert nested.allow_mounts
    try:
        nested.resolve_mount(mount)
    except AccessDenied:
        print("resolving a mount rooted above a sub-handle: blocked as expected")
    else:
        raise AssertionError("A sub-handle must not reach mounts above its root.")


if __name__ == "__main__":
    main()
