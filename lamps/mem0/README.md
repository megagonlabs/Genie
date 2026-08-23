# Mem0-inspired lamp

This lamp builds a small, deterministic Mem0-like service from Genie blocks,
scoped handles, and links. It is not a Mem0 compatibility layer.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.mem0.demo
conda run -n "genie" python -m pytest lamps/mem0/tests -q
```

`demo.py` is the entry point; the module path must be run from the repository
root so both `lamps` and `genie` resolve. It runs the scoped-memory agent and
prints the result. Nothing is random or clock-driven, so every run prints the
same output.

## Memory shape

Each `mem0.fact_record` value uses the main fields returned by the open-source
Mem0 memory API, plus lamp-owned metadata:

```python
{
    "id": "mem0_...",
    "memory": "Alice now prefers window seats on flights.",
    "user_id": "alice",
    "agent_id": "travel-agent",
    "run_id": "planning-session-42",
    "hash": "...",
    "metadata": {
        "kind": "fact",
        "entities": ["Alice", "flight"],
        "occurred_at": None,
        "source": "conversation-41",
    },
    "created_at": "...Z",
    "updated_at": "...Z",
}
```

The Genie block id is also the record id. Open-source Mem0 accepts `user_id`,
`agent_id`, and `run_id`; `app_id` belongs to the hosted platform and is not
used here. MD5 and exact duplicate suppression are transparent deterministic
substitutes for Mem0's LLM-based extraction and conflict handling, not a claim
about its current deduplication algorithm. Event time in
`metadata.occurred_at` is a lamp extension; creation and update times describe
ingestion.

Mental models are separate `mem0.mental_model` blocks with source ids. That is
a demo convention, not part of an open-source Mem0 record.

## Genie mapping

| Behavior | Genie implementation |
| --- | --- |
| Extracted memory | One block with an OSS-shaped `memory` field |
| User/agent/run scope | `MemoryScope` mapped to a `KeyedCollection` address |
| Extraction/conflict stand-in | Exact MD5 duplicate suppression within the bound scope |
| Update | `MemoryHandle.update`; id and `created_at` remain stable |
| Facts and experiences | `metadata.kind`; event time indexed by `TemporalCollection` |
| Relevant-memory retrieval | Lexical scoring over text and entities |
| Shared entities | `PropertyGraph` edges between records |
| Mental model | Separate keyed synthesis block |
| Agent access | `Mem0Tools` wraps a bounded `Mem0Memory` |

Ownership contains the records; links express related content. Taxonomy, hash
rules, ranking, scope meaning, and agent behavior remain application code.

## Demo workflow

The agent calls `add_memory` four times, `update_memory` once,
`create_mental_model` once, and each search/get tool once. The searches cover
lexical, temporal, and graph retrieval; every invocation remains available in
`agent.tool_calls`.

See [the lamp guide](../README.md) for the shared output format.

## Limits

The lamp begins with agent-supplied extracted-memory candidates, whereas Mem0
normally accepts messages and uses an LLM to extract and reconcile memories.
It does not reproduce embeddings, BM25, reranking, expiration, deletion,
history, external vector stores, or the hosted API. Current open-source Mem0
uses entity links during hybrid retrieval but no longer exposes its earlier
graph store as a traversable graph, so `search_graph` is an application-owned
Genie extension rather than a Mem0 API. Temporal search and the separate mental
model are also deliberate extensions.

References: [open-source repository](https://github.com/mem0ai/mem0),
[open-source implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py),
[graph-memory migration](https://docs.mem0.ai/platform/features/graph-memory),
and [memory operations](https://docs.mem0.ai/open-source/python-quickstart).
