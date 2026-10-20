"""The seeded corpus that each benchmark variant uses.

The module first describes the corpus as plain specs. Only then does it write
them to a store. So the backend variant and the `Memory` variant of one case
see identical data. Both variants write the same specs, so the gap between
their durations is only the cost of the code above the store.

The structure of the corpus:

- **One root,** with the hub block and the section chain under it.
- **A chain of sections.** Each section is a child of the section before it.
  The chain sets the tree depth, so `ancestors` and `contains` do real work.
- **Leaves, spread round-robin over the sections.** So each section has a
  similar number of children, and `children` does real work.
- **A hub block.** The `list_links` case adds outgoing links to it, and then
  reads them.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from benchmarks.settings import BenchmarkSettings
from genie import CodecRegistry, Memory, MemoryBlock, MemoryPolicy

ROOT_ID = "bench_root"
"""The id of the corpus root. It does not change, so you can compare a report
with the data."""

HUB_ID = "bench_hub"
"""The id of the block with the outgoing links that `list_links` reads."""

TOPICS = ("alpha", "beta", "gamma", "delta", "epsilon")
"""The topic words in the leaf values and metadata."""

TEXT_NEEDLE = "gamma"
"""The topic that the text search cases look for. It matches approximately 1
leaf in 5."""

FILTER_FIELD = "metadata.bucket"
"""The field that the metadata filter cases match."""

FILTER_VALUE = 3
"""The bucket that the metadata filter cases match. Approximately 1 leaf in
10."""

BUCKETS = 10
"""The number of metadata buckets for the leaves."""


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """The description of one block before it goes to a store.

    Attributes:
        id: The caller-provided block id. It is the same in each run.
        type: The application-defined block type.
        value: The decoded user-level value.
        parent_id: The parent block id, or `None` for the root.
        metadata: The metadata for the filter and search cases.
    """

    id: str
    type: str
    value: dict[str, Any]
    parent_id: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Corpus:
    """All blocks that a variant writes before the measurement starts.

    Attributes:
        root: The corpus root.
        hub: The block with the outgoing links that `list_links` reads.
        sections: The section chain. `sections[0]` is under the root.
        leaves: The leaf blocks, spread round-robin over the sections.
    """

    root: BlockSpec
    hub: BlockSpec
    sections: tuple[BlockSpec, ...]
    leaves: tuple[BlockSpec, ...]

    def specs(self) -> Iterator[BlockSpec]:
        """Yield all specs, parents first, so a store never gets an orphan."""

        yield self.root
        yield self.hub
        yield from self.sections
        yield from self.leaves

    @property
    def block_count(self) -> int:
        """The total number of blocks in the corpus."""

        return 2 + len(self.sections) + len(self.leaves)

    @property
    def depth(self) -> int:
        """The ownership steps from the root to the deepest leaf."""

        return len(self.sections) + 1

    @property
    def deepest_leaf_id(self) -> str:
        """The id of a leaf under the deepest section, for the longest walk up."""

        deepest = self.sections[-1].id
        for leaf in reversed(self.leaves):
            if leaf.parent_id == deepest:
                return leaf.id
        return self.leaves[-1].id

    def link_target_ids(self, count: int) -> tuple[str, ...]:
        """Return the leaf ids that the hub links to.

        Args:
            count: The number of link targets to return.

        Returns:
            A maximum of `count` leaf ids, in corpus order.
        """

        return tuple(leaf.id for leaf in self.leaves[:count])


def build(settings: BenchmarkSettings) -> Corpus:
    """Build the corpus specs for one run.

    The generator has a seed. So the same settings always describe the same
    blocks, and you can compare 2 reports.

    Args:
        settings: The run settings with the sizes and the seed.

    Returns:
        The corpus specs. This function writes nothing to a store.
    """

    generator = random.Random(settings.seed)
    padding = "x" * settings.value_size
    root = BlockSpec(
        id=ROOT_ID,
        type="bench.root",
        value={"name": "benchmark corpus", "padding": padding},
        parent_id=None,
        metadata={"role": "root", "bucket": 0},
    )
    hub = BlockSpec(
        id=HUB_ID,
        type="bench.hub",
        value={"name": "link hub", "padding": padding},
        parent_id=ROOT_ID,
        metadata={"role": "hub", "bucket": 0},
    )
    sections: list[BlockSpec] = []
    for index in range(settings.sections):
        parent_id = ROOT_ID if index == 0 else sections[index - 1].id
        sections.append(
            BlockSpec(
                id=f"bench_section_{index:04d}",
                type="bench.section",
                value={"name": f"section {index}", "depth": index, "padding": padding},
                parent_id=parent_id,
                metadata={"role": "section", "depth": index, "bucket": index % BUCKETS},
            )
        )
    leaves: list[BlockSpec] = []
    for index in range(settings.blocks):
        topic = TOPICS[generator.randrange(len(TOPICS))]
        bucket = generator.randrange(BUCKETS)
        leaves.append(
            BlockSpec(
                id=f"bench_leaf_{index:06d}",
                type="bench.leaf",
                value={
                    "index": index,
                    "topic": topic,
                    "text": f"leaf {index} about {topic}",
                    "score": round(generator.random(), 6),
                    "padding": padding,
                },
                parent_id=sections[index % len(sections)].id,
                metadata={"role": "leaf", "topic": topic, "bucket": bucket, "index": index},
            )
        )
    return Corpus(root=root, hub=hub, sections=tuple(sections), leaves=tuple(leaves))


def make_block(
    spec: BlockSpec,
    *,
    principal_id: str,
    policy: MemoryPolicy,
    codecs: CodecRegistry,
) -> MemoryBlock:
    """Build the encoded block that a backend call stores.

    The block has the same policy, owner, and encoded value that `Memory.put`
    adds. So the backend variant and the `Memory` variant store the same state.

    Args:
        spec: The block spec to encode.
        principal_id: The owner to record on the block.
        policy: The policy of the block.
        codecs: The registry that encodes the value.

    Returns:
        An encoded block, ready for `put_block`.
    """

    encoded = codecs.encode(spec.value)
    return MemoryBlock(
        id=spec.id,
        type=spec.type,
        value=encoded.data,
        parent_id=spec.parent_id,
        content_type=encoded.content_type,
        metadata=dict(spec.metadata),
        policy=policy,
        created_by=principal_id,
    )


def populate_backend(
    backend: Any,
    corpus: Corpus,
    *,
    principal_id: str,
    policy: MemoryPolicy,
    codecs: CodecRegistry,
) -> None:
    """Write the corpus directly through the backend protocol.

    Args:
        backend: The backend to write into.
        corpus: The corpus specs to write.
        principal_id: The owner to record on each block.
        policy: The policy of each block.
        codecs: The registry that encodes the values.
    """

    for spec in corpus.specs():
        backend.put_block(make_block(spec, principal_id=principal_id, policy=policy, codecs=codecs))


def populate_memory(memory: Memory, corpus: Corpus) -> None:
    """Write the corpus through the public `Memory` API.

    Args:
        memory: The memory to write into.
        corpus: The corpus specs to write.
    """

    for spec in corpus.specs():
        memory.put(
            type=spec.type,
            value=spec.value,
            parent=spec.parent_id,
            metadata=spec.metadata,
            id=spec.id,
        )


def payload(index: int, *, value_size: int) -> dict[str, Any]:
    """Build the value that a write case sends.

    The cases build the payloads before the measurement starts. So a timed call
    measures the Genie operation, not the construction of the payload.

    Args:
        index: The call index of the payload.
        value_size: The number of padding characters to add.

    Returns:
        A JSON-compatible value.
    """

    return {
        "index": index,
        "topic": TOPICS[index % len(TOPICS)],
        "text": f"written {index} about {TOPICS[index % len(TOPICS)]}",
        "padding": "x" * value_size,
    }
