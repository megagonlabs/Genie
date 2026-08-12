# Mem0-inspired lamp

This lamp builds a small, deterministic Mem0-like service from Genie blocks,
scoped handles, and links. It is not a Mem0 compatibility layer.

## Memory shape

Each `mem0.fact_record` value has the requested canonical fields:

```python
{
    "id": "mem0_...",
    "text": "Alice now prefers window seats on flights.",
    "user_id": "alice",
    "agent_id": "travel-agent",
    "app_id": "trip-planner",
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

The Genie block id is also the record id. MD5 is used only for exact content
deduplication within one scope, not for security. Event time belongs in
`metadata.occurred_at`; creation and update times describe ingestion.

Mental models are separate `mem0.mental_model` blocks with source ids. That is
a demo convention, not part of the canonical fact record.

## Genie mapping

| Behavior | Genie implementation |
| --- | --- |
| Fact record | One block with the canonical value shape |
| User/agent/app/run scope | `MemoryScope` mapped to a `KeyedCollection` address |
| Exact deduplication | MD5 of normalized text within the bound scope |
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

See [the lamp guide](../README.md) for run commands and the shared output
format.

## Limits

The lamp does not use an extraction LLM, embeddings, BM25, external vector or
graph stores, deletion history, or the hosted Mem0 API. Current open-source
Mem0 no longer exposes its earlier graph store as a traversable graph, so
`search_graph` is an application-owned Genie extension rather than a Mem0 API.

References: [get memory](https://docs.mem0.ai/api-reference/memory/get-memory),
[add memories](https://docs.mem0.ai/api-reference/memory/add-memories),
[graph-memory migration](https://docs.mem0.ai/platform/features/graph-memory),
and [updates](https://docs.mem0.ai/core-concepts/memory-operations/update).
