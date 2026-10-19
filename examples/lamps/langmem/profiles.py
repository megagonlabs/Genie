"""Semantic memory in its profile shape: one document, revised in place.

LangMem has 2 shapes for semantic memory:

- **Collection:** many small documents accumulate, and retrieval selects the
  relevant ones.
- **Profile:** a single document for each namespace, with a strict schema. An
  update changes the document and does not add a new one. This shape suits
  "what does this user prefer". A second answer replaces the first answer, and
  the 2 answers do not stay side by side.

The 2 shapes need different Genie structures. That is why this module is
separate from `store.py`:

- **Collection:** `KeyedCollection`. It has many addresses, and each address
  holds one block.
- **Profile:** `VersionedRecords`. One address holds one identity, and each
  update adds an ordered revision behind it. This gives the upstream behavior:
  one current document that each write replaces. It also adds what a single
  mutable document cannot show: its earlier text.
- **Merge:** an update merges fields and does not replace the whole document.
  The store writes a revision only if the merged value is really different. So
  a conversation that repeats the profile adds no version.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from copy import deepcopy
from datetime import datetime
from typing import Any, cast

from examples.lamps.langmem.model import (
    PROFILE_REVISION_TYPE,
    PROFILE_TYPE,
    SEARCH_LIMIT,
    Namespace,
    Profile,
    ValueSchema,
    format_timestamp,
    json_mapping,
    namespace,
    namespace_path,
    normalize_timestamp,
    parse_timestamp,
    required_text,
)
from examples.lamps.langmem.structures import LangMemStructures
from genie import MemoryBlock, MemoryHandle
from genie.structures import MemoryAddress, RevisionItem


class ProfileStore:
    """Single-document semantic memory, with one identity for each profile."""

    def __init__(
        self,
        memory: MemoryHandle,
        *,
        structures: LangMemStructures,
        schema: ValueSchema | None = None,
    ) -> None:
        """Create a profile store over one bounded handle.

        Args:
            memory: Bounded Genie handle that owns the profile tier.
            structures: Facade bundle on the same handle.
            schema: Optional schema for each profile value after the merge.
                LangMem requires a strict schema for the profile shape. Here,
                without a schema, the store also accepts free-form profiles.
        """

        self.memory = memory
        self.structures = structures
        self.schema = schema

    def register(
        self,
        value: Sequence[str],
        name: str,
        *,
        default: Mapping[str, Any],
        at: datetime,
    ) -> Profile:
        """Create a profile and write its first version.

        The LangMem store manager takes a `default`. So the first conversation
        patches a document that exists, and does not make a new one. This method
        writes the default as revision 1. That keeps the upstream behavior, and
        a reader can read the start value later.

        Args:
            value: Resolved namespace of the profile.
            name: Profile name in that namespace.
            default: First value.
            at: Timestamp for the first revision.

        Returns:
            The profile at revision 1.

        Raises:
            TypeError: If the default is not a mapping or fails the schema.
            ValueError: If the profile already exists or the schema rejects the
                default.
        """

        target = namespace(*value)
        profile_name = required_text(name, field="name")
        address = MemoryAddress(target, profile_name)
        if self.structures.profiles.identity(address) is not None:
            raise ValueError(f"Profile {profile_name!r} already exists in {namespace_path(target)!r}.")
        opening = self._check(json_mapping(default, field="default"))
        identity = self.structures.tiers.create_child(
            parent=self.structures.ensure_namespace(target),
            type=PROFILE_TYPE,
            value={"namespace": list(target), "name": profile_name},
            metadata={"namespace_path": namespace_path(target), "name": profile_name},
        )
        self.structures.profiles.register(address, identity)
        return self._write(identity, address, opening, at=at)

    def update(
        self,
        value: Sequence[str],
        name: str,
        changes: Mapping[str, Any],
        *,
        at: datetime,
    ) -> Profile:
        """Merge fields into a profile, and write a version only if the value changed.

        Args:
            value: Resolved namespace that holds the profile.
            name: Profile name.
            changes: Fields to merge into the current value.
            at: Timestamp for the revision, if the store writes one.

        Returns:
            The profile after the merge. Its `revision` does not change if the
            merge changed no field.

        Raises:
            KeyError: If the profile is not registered.
            TypeError: If the changes are not a mapping or fail the schema.
            ValueError: If the schema rejects the merged value.
        """

        current = self.require(value, name)
        merged = self._check({**current.value, **json_mapping(changes, field="changes")})
        if merged == current.value:
            return current
        return self._write(
            current.identity,
            MemoryAddress(current.namespace, current.name),
            merged,
            at=at,
        )

    def current(self, value: Sequence[str], name: str) -> Profile | None:
        """Return the current version of a profile.

        Args:
            value: Resolved namespace.
            name: Profile name.

        Returns:
            The profile, or `None` if it is not registered.
        """

        address = MemoryAddress(namespace(*value), required_text(name, field="name"))
        identity = self.structures.profiles.identity(address)
        revision = self.structures.profiles.current(address)
        if identity is None or revision is None:
            return None
        return _profile(identity, revision)

    def require(self, value: Sequence[str], name: str) -> Profile:
        """Return a profile, and raise `KeyError` if it is not registered.

        Args:
            value: Resolved namespace.
            name: Profile name.

        Returns:
            The current profile.

        Raises:
            KeyError: If the profile is not registered.
        """

        profile = self.current(value, name)
        if profile is None:
            raise KeyError(f"Profile {name!r} is not registered in {namespace_path(namespace(*value))!r}.")
        return profile

    def history(self, value: Sequence[str], name: str) -> list[Profile]:
        """Return all versions of a profile, oldest first.

        Args:
            value: Resolved namespace.
            name: Profile name.

        Returns:
            Versions in ascending revision order. The list is empty if the
            profile is not registered.
        """

        address = MemoryAddress(namespace(*value), required_text(name, field="name"))
        identity = self.structures.profiles.identity(address)
        if identity is None:
            return []
        return [
            _profile(identity, revision)
            for revision in self.structures.profiles.history(address, limit=SEARCH_LIMIT)
        ]

    def profiles(self) -> list[Profile]:
        """Return the current version of each registered profile.

        Returns:
            Profiles in ascending namespace and name order.
        """

        found: list[Profile] = []
        for entry in self.structures.profiles.identities(recursive=True, limit=SEARCH_LIMIT):
            revision = self.structures.profiles.current(entry.binding.address)
            if revision is not None:
                found.append(_profile(entry.block, revision))
        return sorted(found, key=lambda profile: (profile.namespace, profile.name))

    def _write(
        self,
        identity: MemoryBlock,
        address: MemoryAddress,
        value: dict[str, Any],
        *,
        at: datetime,
    ) -> Profile:
        """Append one profile revision, and return the profile with that revision."""

        written_at = normalize_timestamp(at, field="at")
        revision_block = self.structures.tiers.create_child(
            parent=identity,
            type=PROFILE_REVISION_TYPE,
            value={
                "namespace": list(address.namespace),
                "name": address.key,
                "value": value,
                "updated_at": format_timestamp(written_at),
            },
            metadata={"name": address.key},
        )
        self.structures.profiles.append(address, revision_block)
        revision = self.structures.profiles.current(address)
        if revision is None:
            raise ValueError(f"Profile {address.key!r} lost its revision during the update.")
        return _profile(identity, revision)

    def _check(self, value: dict[str, Any]) -> dict[str, Any]:
        """Validate a merged profile value against the configured schema."""

        if self.schema is None:
            return value
        return self.schema.validate(value)


def _profile(identity: MemoryBlock, revision: RevisionItem) -> Profile:
    """Assemble one profile from its identity and a revision."""

    payload = _payload(revision.block)
    stored = payload.get("namespace")
    if not isinstance(stored, list):
        raise TypeError(f"Profile revision {revision.block.id!r} has an invalid namespace.")
    value = payload.get("value")
    if not isinstance(value, dict):
        raise TypeError(f"Profile revision {revision.block.id!r} must hold a dictionary value.")
    return Profile(
        identity=identity,
        block=revision.block,
        namespace=cast(Namespace, namespace(*cast(list[str], stored))),
        name=required_text(payload.get("name"), field="name"),
        value=deepcopy(cast(dict[str, Any], value)),
        revision=revision.entry.revision,
        updated_at=parse_timestamp(required_text(payload.get("updated_at"), field="updated_at")),
    )


def _payload(block: MemoryBlock) -> dict[str, Any]:
    """Return the dictionary value of a profile revision block."""

    if not isinstance(block.value, dict):
        raise TypeError(f"Block {block.id!r} does not hold a profile revision.")
    return cast(dict[str, Any], block.value)
