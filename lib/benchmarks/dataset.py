"""Seeded corpus every benchmark variant runs against.

The corpus is described first as plain specs and only then written to a store.
That is what lets the backend variant and the `Memory` variant of one case see
identical data: both write the same specs, so the gap between their timings is
the cost of the code above the store and nothing else.

Shape of the corpus:

- **One root** with the hub block and the section chain under it.
- **A chain of sections,** each one a child of the section before it. The chain
  sets tree depth, so `ancestors` and `contains` have real work to do.
- **Leaves spread round-robin over the sections,** so every section has a
  similar number of children and `children` has real work to do.
- **A hub block** that carries the outgoing links the `list_links` case reads.
"""

from __future__ import annotations

import random
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

from benchmarks.settings import BenchmarkSettings
from genie import CodecRegistry, Memory, MemoryBlock, MemoryPolicy

ROOT_ID = "bench_root"
"""Id of the corpus root. Stable, so a report can be read against the data."""

HUB_ID = "bench_hub"
"""Id of the block that carries the outgoing links `list_links` reads."""

TOPICS = ("alpha", "beta", "gamma", "delta", "epsilon")
"""Topic words mixed into leaf values and metadata."""

TEXT_NEEDLE = "gamma"
"""Topic the text search cases look for. Matches roughly one leaf in five."""

FILTER_FIELD = "metadata.bucket"
"""Field the metadata filter cases match on."""

FILTER_VALUE = 3
"""Bucket the metadata filter cases match. Roughly one leaf in ten."""

BUCKETS = 10
"""Number of metadata buckets leaves are spread across."""


@dataclass(frozen=True, slots=True)
class BlockSpec:
    """One block described before it reaches a store.

    Attributes:
        id: Caller-provided block id, stable across runs.
        type: Application-defined block type.
        value: Decoded user-level value.
        parent_id: Parent block id, or `None` for the root.
        metadata: Metadata used by the filter and search cases.
    """

    id: str
    type: str
    value: dict[str, Any]
    parent_id: str | None
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Corpus:
    """Every block a variant writes before measuring starts.

    Attributes:
        root: Corpus root.
        hub: Block that carries the outgoing links `list_links` reads.
        sections: Section chain, where `sections[0]` sits under the root.
        leaves: Leaf blocks spread round-robin over the sections.
    """

    root: BlockSpec
    hub: BlockSpec
    sections: tuple[BlockSpec, ...]
    leaves: tuple[BlockSpec, ...]

    def specs(self) -> Iterator[BlockSpec]:
        """Yield every spec parents first, so a store never sees an orphan."""

        yield self.root
        yield self.hub
        yield from self.sections
        yield from self.leaves

    @property
    def block_count(self) -> int:
        """Total blocks in the corpus."""

        return 2 + len(self.sections) + len(self.leaves)

    @property
    def depth(self) -> int:
        """Ownership steps from the root down to the deepest leaf."""

        return len(self.sections) + 1

    @property
    def deepest_leaf_id(self) -> str:
        """Id of a leaf under the deepest section, for the longest walk up."""

        deepest = self.sections[-1].id
        for leaf in reversed(self.leaves):
            if leaf.parent_id == deepest:
                return leaf.id
        return self.leaves[-1].id

    def link_target_ids(self, count: int) -> tuple[str, ...]:
        """Return leaf ids the hub links to.

        Args:
            count: Number of link targets to return.

        Returns:
            Up to `count` leaf ids, in corpus order.
        """

        return tuple(leaf.id for leaf in self.leaves[:count])


def build(settings: BenchmarkSettings) -> Corpus:
    """Build the corpus specs for one run.

    The generator is seeded, so the same settings always describe the same
    blocks and two reports stay comparable.

    Args:
        settings: Run settings holding the sizes and the seed.

    Returns:
        The corpus specs. Nothing is written to a store here.
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
    """Build the encoded block a backend call would store.

    The block carries the same policy, owner, and encoded value that
    `Memory.put` would attach, so the backend variant and the `Memory` variant
    store the same state.

    Args:
        spec: Block spec to encode.
        principal_id: Owner recorded on the block.
        policy: Policy attached to the block.
        codecs: Registry used to encode the value.

    Returns:
        An encoded block ready for `put_block`.
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
    """Write the corpus straight through the backend protocol.

    Args:
        backend: Backend to write into.
        corpus: Corpus specs to write.
        principal_id: Owner recorded on every block.
        policy: Policy attached to every block.
        codecs: Registry used to encode values.
    """

    for spec in corpus.specs():
        backend.put_block(make_block(spec, principal_id=principal_id, policy=policy, codecs=codecs))


def populate_memory(memory: Memory, corpus: Corpus) -> None:
    """Write the corpus through the public `Memory` API.

    Args:
        memory: Memory to write into.
        corpus: Corpus specs to write.
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
    """Build the value a write case sends.

    Payloads are built before measuring starts, so a timed call covers the
    Genie operation and not payload construction.

    Args:
        index: Call index the payload belongs to.
        value_size: Padding characters to add.

    Returns:
        A JSON-compatible value.
    """

    return {
        "index": index,
        "topic": TOPICS[index % len(TOPICS)],
        "text": f"written {index} about {TOPICS[index % len(TOPICS)]}",
        "padding": "x" * value_size,
    }
