"""Let a bounded handle follow symbolic mounts without widening its subtree.

Mount access is a separate handle capability.

- Without `allow_mounts`, a handle cannot list, resolve, or create mounts at
  all.
- With it, a handle may resolve a mount and receive a block outside its own
  subtree.
- Ordinary reads, searches, and deletes still stay inside the boundary.
- A sub-handle can never grant itself a capability its parent lacks.

Run from the repository root:

    conda run -n "genie" python examples/basic/mounted_handles.py
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
    # This example focuses on handle capabilities rather than policies, so
    # blocks are readable by every principal.
    memory = Memory(
        backend=InMemoryBackend(),
        principal="system",
        default_policy=MemoryPolicy.public(),
    )
    project = memory.put(type="project", value="Rollout plan")
    task = memory.put(parent=project, type="task", value="Draft the checklist")
    reference = memory.put(type="project", value="Shared compliance reference")
    reference_note = memory.put(parent=reference, type="note", value="Retention is 30 days")

    # Trusted application code decides which memory a subtree may point at.
    mount = memory.add_mount(project, reference, metadata={"label": "compliance"})

    plain = memory.get_handle(root=project, principal="agent:planner")
    linked = memory.get_handle(root=project, principal="agent:auditor", allow_mounts=True)

    # Without the capability, every mount operation is refused.
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

    # With the capability, the handle discovers and resolves the mount.
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

    # Resolving is explicit and does not widen the boundary. The mounted tree
    # is still outside the handle's subtree for every ordinary operation.
    assert linked.search("retention", scope="subtree") == []
    assert [block.id for block in linked.children(project)] == [task.id]
    try:
        linked.get(reference_note)
    except AccessDenied:
        print("reading inside the mounted tree: still blocked")
    else:
        raise AssertionError("A mount must not widen ordinary subtree reads.")

    # A sub-handle cannot add a capability its parent does not have.
    try:
        plain.get_handle(root=task, allow_mounts=True)
    except AccessDenied:
        print("granting mount access from a handle that lacks it: blocked as expected")
    else:
        raise AssertionError("A sub-handle must not escalate its capabilities.")

    # A sub-handle may keep mount access, but narrowing its root moves the
    # project's mount out of reach. The capability is not a way around the
    # boundary.
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
