"""Namespaced keyed-collection facade over an atomic keyed store."""

from __future__ import annotations

from dataclasses import replace
from typing import Protocol, runtime_checkable

from genie.backend.structure_capabilities import KeyedStore
from genie.hooks import HookOperation, HookRegistry, StructureAction, StructureChange, StructureKind
from genie.memory.block import MemoryBlock
from genie.memory.errors import MemoryConflict, MemoryNotFound, UnsupportedOperation
from genie.policy.engine import AccessDenied
from genie.structures._hooks import after_change, before_change, hook_operation, principal_id, resolve_hooks
from genie.structures.access import BlockReader, BlockRef
from genie.structures.records import KeyBinding, KeyedItem, MemoryAddress


@runtime_checkable
class KeyedAccess(BlockReader, Protocol):
    """Small access surface required by ``KeyedCollection``."""


class KeyedCollection:
    """A visible namespaced key map for existing memory blocks.

    Blocks remain owned and stored by normal Genie memory. The keyed store owns
    only address-to-block bindings and enforces atomic uniqueness and
    compare-and-swap updates.
    """

    __slots__ = ("_access", "_hooks", "_principal_id", "_store", "collection_id")

    def __init__(
        self,
        access: KeyedAccess,
        store: KeyedStore,
        *,
        collection: BlockRef,
        hooks: HookRegistry | None = None,
    ) -> None:
        """Create a keyed view anchored by a visible collection block.

        Args:
            access: Full or scoped memory access used to resolve blocks.
            store: Backend capability that stores key bindings atomically.
            collection: Block or id that identifies this collection's key space.
            hooks: Optional registry. Defaults to the registry owned by a
                ``Memory`` or ``MemoryHandle`` access object.

        Raises:
            MemoryNotFound: If the collection block is missing.
            UnsupportedOperation: If ``store`` lacks the keyed capability.
        """

        if not isinstance(store, KeyedStore):
            raise UnsupportedOperation("The configured store does not support keyed collections.")
        stored_collection = access.get(collection)
        if stored_collection is None:
            raise MemoryNotFound(
                f"Keyed collection block {_block_id(collection)!r} does not exist."
            )
        self._access = access
        self._store = store
        self._hooks = resolve_hooks(access, hooks)
        self._principal_id = principal_id(access)
        self.collection_id = stored_collection.id

    @property
    def collection(self) -> MemoryBlock:
        """Return the current visible collection block."""

        return self._require_collection()

    def bind(self, address: MemoryAddress, block: BlockRef) -> KeyedItem:
        """Atomically bind an unused address to an existing visible block.

        Args:
            address: Namespace and key to bind.
            block: Existing visible memory block or id.

        Returns:
            Stored binding and resolved block.

        Raises:
            MemoryConflict: If the address is already bound.
            MemoryNotFound: If the collection or block is missing.
        """

        with hook_operation(
            self._hooks,
            HookOperation.KEY_BIND,
            principal=self._principal_id,
        ) as context:
            _require_address(address)
            self._require_collection()
            stored_block = self._require_block(block)
            proposed = KeyBinding(
                collection_id=self.collection_id,
                address=address,
                block_id=stored_block.id,
            )
            change = StructureChange(
                kind=StructureKind.KEYED_COLLECTION,
                action=StructureAction.BIND,
                container_id=self.collection_id,
                block_id=stored_block.id,
                after=proposed,
            )
            before_change(self._hooks, context, change)
            binding = self._store.bind_key(proposed)
            after_change(self._hooks, context, replace(change, after=binding))
            return KeyedItem(binding=binding, block=stored_block)

    def rebind(
        self,
        address: MemoryAddress,
        block: BlockRef,
        *,
        expected: BlockRef,
    ) -> KeyedItem:
        """Atomically move an address when its current block still matches.

        Args:
            address: Existing collection address.
            block: New visible memory block or id.
            expected: Block or id that must currently own the address.

        Returns:
            Replacement binding and resolved new block.

        Raises:
            MemoryConflict: If the address is missing or changed.
        """

        with hook_operation(
            self._hooks,
            HookOperation.KEY_REBIND,
            principal=self._principal_id,
        ) as context:
            _require_address(address)
            self._require_collection()
            current = self.get_item(address)
            expected_id = _block_id(expected)
            if current is None or current.binding.block_id != expected_id:
                raise MemoryConflict(
                    f"Key address {address!r} no longer points to expected block "
                    f"{expected_id!r}."
                )
            stored_block = self._require_block(block)
            proposed = KeyBinding(
                collection_id=self.collection_id,
                address=address,
                block_id=stored_block.id,
            )
            change = StructureChange(
                kind=StructureKind.KEYED_COLLECTION,
                action=StructureAction.REBIND,
                container_id=self.collection_id,
                block_id=stored_block.id,
                before=current.binding,
                after=proposed,
            )
            before_change(self._hooks, context, change)
            binding = self._store.rebind_key(
                proposed,
                expected_block_id=expected_id,
            )
            after_change(self._hooks, context, replace(change, after=binding))
            return KeyedItem(binding=binding, block=stored_block)

    def get(self, address: MemoryAddress) -> MemoryBlock | None:
        """Return the visible block at an exact address, if one is bound."""

        item = self.get_item(address)
        return item.block if item is not None else None

    def get_item(self, address: MemoryAddress) -> KeyedItem | None:
        """Return an exact visible binding and block."""

        _require_address(address)
        self._require_collection()
        binding = self._store.get_key(self.collection_id, address)
        if binding is None:
            return None
        block = self._access.get(binding.block_id)
        if block is None:
            raise MemoryNotFound(
                f"Key address {address!r} references missing memory block "
                f"{binding.block_id!r}."
            )
        return KeyedItem(binding=binding, block=block)

    def items(
        self,
        *,
        namespace: tuple[str, ...] | None = None,
        recursive: bool = False,
        limit: int = 100,
    ) -> list[KeyedItem]:
        """List visible items in stable namespace-and-key order.

        Args:
            namespace: Optional exact namespace. With ``recursive=True``, it is
                treated as a namespace prefix.
            recursive: Whether to include nested namespaces below ``namespace``.
            limit: Maximum visible items to return.

        Returns:
            Visible keyed items. Missing or inaccessible targets are omitted.
        """

        self._require_collection()
        _validate_limit(limit)
        if not isinstance(recursive, bool):
            raise TypeError("recursive must be a boolean.")
        selected_namespace = _normalize_namespace(namespace)
        if limit == 0:
            return []
        items: list[KeyedItem] = []
        after: MemoryAddress | None = None
        while len(items) < limit:
            batch_limit = min(100, limit - len(items))
            bindings = self._store.list_keys(
                self.collection_id,
                namespace=selected_namespace,
                recursive=recursive,
                after=after,
                limit=batch_limit,
            )
            if not bindings:
                break
            after = bindings[-1].address
            for binding in bindings:
                try:
                    block = self._access.get(binding.block_id)
                except AccessDenied:
                    continue
                if block is None:
                    continue
                items.append(KeyedItem(binding=binding, block=block))
                if len(items) >= limit:
                    break
            if len(bindings) < batch_limit:
                break
        return items

    def unbind(self, address: MemoryAddress, *, expected: BlockRef) -> None:
        """Atomically remove an address when its current block still matches.

        The memory block is not deleted. Removing structural membership and
        deleting a block are separate operations.

        Args:
            address: Existing collection address.
            expected: Block or id that must currently own the address.

        Raises:
            MemoryConflict: If the address is missing or changed.
        """

        with hook_operation(
            self._hooks,
            HookOperation.KEY_UNBIND,
            principal=self._principal_id,
        ) as context:
            _require_address(address)
            self._require_collection()
            current = self.get_item(address)
            expected_id = _block_id(expected)
            if current is None or current.binding.block_id != expected_id:
                raise MemoryConflict(
                    f"Key address {address!r} no longer points to expected block "
                    f"{expected_id!r}."
                )
            change = StructureChange(
                kind=StructureKind.KEYED_COLLECTION,
                action=StructureAction.UNBIND,
                container_id=self.collection_id,
                block_id=current.binding.block_id,
                before=current.binding,
            )
            before_change(self._hooks, context, change)
            self._store.unbind_key(
                self.collection_id,
                address,
                expected_block_id=expected_id,
            )
            after_change(self._hooks, context, change)

    def _require_collection(self) -> MemoryBlock:
        """Return the visible collection block or raise when it was deleted."""

        collection = self._access.get(self.collection_id)
        if collection is None:
            raise MemoryNotFound(
                f"Keyed collection block {self.collection_id!r} does not exist."
            )
        return collection

    def _require_block(self, block: BlockRef) -> MemoryBlock:
        """Return a visible block or raise a clear missing-block error."""

        stored = self._access.get(block)
        if stored is None:
            raise MemoryNotFound(f"Memory block {_block_id(block)!r} does not exist.")
        return stored


def _block_id(block: BlockRef) -> str:
    """Return a block id from a block object or id string."""

    return block.id if isinstance(block, MemoryBlock) else block


def _normalize_namespace(
    namespace: tuple[str, ...] | None,
) -> tuple[str, ...] | None:
    """Validate and freeze an optional namespace tuple."""

    if namespace is None:
        return None
    return MemoryAddress(namespace=namespace, key="_").namespace


def _require_address(address: object) -> None:
    """Require a typed collection address."""

    if not isinstance(address, MemoryAddress):
        raise TypeError("address must be a MemoryAddress.")


def _validate_limit(limit: object) -> None:
    """Require a non-negative integer result limit."""

    if isinstance(limit, bool) or not isinstance(limit, int):
        raise TypeError("limit must be an integer.")
    if limit < 0:
        raise ValueError("limit must be greater than or equal to zero.")
