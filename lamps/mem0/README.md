# Mem0-inspired lamp

This lamp shows how an application can build a small Mem0-like memory service
with Genie blocks, bounded handles, and typed links. It is intentionally
deterministic and provider-independent: the example agent uses named tools in
the same shape that an LLM function-calling loop could invoke.

It demonstrates:

- canonical extracted fact records and exact content-hash deduplication;
- exact `user_id` / `agent_id` / `app_id` / `run_id` scoping;
- facts, dated experiences, updates, and lexical retrieval;
- shared-entity graph traversal;
- a separate, evidence-backed mental-model document; and
- an agent that can access these capabilities only through bound tools.

## Stored memory shape

Every `mem0.fact_record` block value has exactly this shape:

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

The ten top-level fields follow the requested canonical extracted-fact record.
The application uses Mem0's extensible `metadata` for `kind`, `entities`, and
event time, rather than changing the record schema. Ingestion times remain in
`created_at` and `updated_at`; `metadata.occurred_at` is the time described by
an experience.

The record id is also the Genie block id. The MD5 value is used only as a
content identifier for exact deduplication, not as a security primitive.

Mental models are stored separately as `mem0.mental_model` blocks with source
memory ids. This is a lamp convention for the requested synthesis feature, not
a claim that mental models are part of Mem0's canonical fact-record schema.

## Mapping to Genie

| Mem0-inspired behavior | Implementation in this lamp |
| --- | --- |
| Extracted fact record | One `mem0.fact_record` block with the canonical value shape |
| Entity scope and record id | `MemoryScope` mapped to an atomic `KeyedCollection` address |
| Exact deduplication | MD5 of normalized text, compared only inside the bound scope |
| Update | `MemoryHandle.update`; id and `created_at` remain stable while text, hash, metadata, and `updated_at` change |
| Facts and experiences | `metadata.kind`; event time is also indexed by `TemporalCollection` |
| Semantic retrieval | Portable lexical scoring over text and entities |
| Graph context | `PropertyGraph` edges between records sharing normalized entities |
| Mental model | Separate synthesis block addressed by scoped name in `KeyedCollection` |
| Agent access | `Mem0Tools` wraps a bounded `Mem0Memory`; the agent never receives `Memory` or `MemoryHandle` |

Parent/child structure owns the lamp's records. Links express related content
without implying ownership. The taxonomy, hashes, scope semantics, ranking,
and agent behavior all remain outside Genie core.

## Run it

From the repository root:

```bash
conda run -n "genie" python -m lamps.mem0.demo
```

The agent calls:

- `add_memory` four times;
- `update_memory` once;
- `create_mental_model` once;
- `search_memories`, `search_temporal`, `search_graph`, and
  `get_mental_model` once each.

The output first names the lamp, its purpose, and the Genie structures it uses.
It then uses the same four terminal sections as the other agent lamps: tools,
answer, ordered call trace, and one-line-per-tool usage counts. Each invocation
is also directly inspectable in `agent.tool_calls`.

Run the co-located tests with:

```bash
conda run -n "genie" python -m pytest lamps/mem0/tests -q
```

## Deliberate limits

This is not a drop-in Mem0 implementation. It does not call an extraction LLM,
generate embeddings, implement BM25, persist to external vector or graph
stores, expose deletion history, or reproduce Mem0's hosted API.

Mem0's current open-source memory algorithm documents hash deduplication and
entity-linked hybrid retrieval, and no longer exposes its earlier graph store
as a traversable graph. This lamp keeps a small `search_graph` extension because
the goal is to show how Genie links can support explicit graph retrieval. The
link is application-owned and should not be mistaken for a current Mem0 API.

References:

- [Mem0 get-memory response](https://docs.mem0.ai/api-reference/memory/get-memory)
- [Mem0 add-memory API](https://docs.mem0.ai/api-reference/memory/add-memories)
- [Mem0 open-source memory algorithm migration](https://docs.mem0.ai/platform/features/graph-memory)
- [Mem0 memory updates](https://docs.mem0.ai/core-concepts/memory-operations/update)
