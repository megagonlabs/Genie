# LangMem-inspired lamp

This lamp shows how Genie can support a small LangMem-like application model:
schema-defined values stored as `(namespace, key, value)` items, with tools that
let an agent manage and retrieve those items. It is a focused example, not a
LangMem compatibility layer.

It demonstrates:

- three different application-supplied value schemas;
- hierarchical namespace and contextual-key isolation;
- semantic facts, episodic experiences, and a synthesized mental model;
- direct, lexical, temporal, and graph retrieval; and
- an agent that can access memory only through namespace-bound tools.

## Stored memory structure

Every `langmem.item` Genie block has the same small wrapper:

```python
{
    "namespace": ["travel-assistant", "alice", "semantic"],
    "key": "seat-preference",
    "value": {
        "subject": "Alice",
        "predicate": "prefers",
        "object": "window seats",
        "context": "air travel",
    },
}
```

The wrapper is stable; the value is not. The demo supplies these schemas:

```python
# semantic
{"subject": str, "predicate": str, "object": str, "context": str}

# episode
{"observation": str, "thoughts": str, "action": str, "result": str}

# mental model
{"name": str, "summary": str, "principles": list, "source_keys": list}
```

`ValueSchema` is a small dependency-free validator used only by this lamp. It
stands in for a supplied Pydantic or other application schema. An unregistered
namespace can still store any value supported by Genie's configured codec,
which preserves the central point: Genie and LangMem do not impose one memory
value schema.

Event timestamps and entity hints live in Genie block metadata. They are
optional retrieval annotations, not fields injected into the supplied value.
This keeps the example episode value faithful to the four-field episodic shape.

## Mapping to Genie

| LangMem-inspired concept | Implementation in this lamp |
| --- | --- |
| `(namespace, key, value)` item | One owned `langmem.item` block |
| Supplied value schema | `ValueSchema` registered for an exact namespace |
| Hierarchical namespace | `MemoryAddress.namespace` plus the canonical wrapper field |
| Contextual key | Atomic `KeyedCollection` binding within an exact namespace |
| Create/update management | `LangMemStore.create` and `update`; update preserves block identity |
| Direct retrieval | Exact `KeyedCollection` address lookup |
| Semantic retrieval | Deterministic token overlap across schema-shaped values |
| Episodic retrieval | `TemporalCollection` event query, with time retained as an annotation |
| Graph retrieval | `PropertyGraph` edges with application relation labels |
| Mental model | A regular item using a separate synthesis schema and namespace |
| Agent tools | Namespace-bound `manage_memory`, `get_memory`, and search/link tools |

Genie parent/child structure owns all items under one store root. Links express
non-owning relationships. Schemas, namespace meaning, ranking, graph relation
meaning, synthesis, and agent behavior remain outside Genie core.

## Agent workflow

The deterministic travel agent:

1. calls `manage_memory` five times for two facts, two episodes, and one mental
   model;
2. calls `link_memories` three times;
3. retrieves the mental model directly;
4. searches relevant values and the dated experience interval; and
5. traverses two graph hops from the seat-preference fact to its successful
   episode and supporting mental model.

The same tools use JSON-compatible inputs and results, so they could sit behind
an LLM function-calling loop. The deterministic policy keeps this lamp runnable
without an API key or agent-framework dependency.

## Run it

From the repository root:

```bash
conda run -n "genie" python -m lamps.langmem.demo
```

The terminal output first names the lamp, its purpose, and the Genie structures
it uses. It then shows the shared lamp sections: available tools, agent answer,
ordered tool calls, and one-line-per-tool usage counts.

Run its co-located tests with:

```bash
conda run -n "genie" python -m pytest lamps/langmem/tests -q
```

## Deliberate limits

This lamp does not run LangMem's LLM extraction or consolidation managers,
Pydantic validation, prompt optimization, background reflection, embeddings,
or a LangGraph `BaseStore`. Its lexical search is not semantic similarity.

Temporal and graph traversal are requested application extensions. LangMem's
documented storage retrieval covers direct access, semantic search, and
metadata filtering; it does not define the graph traversal API used here. The
extension demonstrates how Genie metadata and links can add those behaviors
without changing the namespace/key/value structure.

Likewise, `mental_model` is a schema supplied by this demo, not a built-in
LangMem memory type. It illustrates how an application can store a synthesized
document alongside semantic and episodic values without introducing a special
case in Genie or the generic item store.

References:

- [LangMem core concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/)
- [LangMem semantic-memory schemas](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/)
- [LangMem memory tools](https://langchain-ai.github.io/langmem/reference/tools/)
- [LangMem memory manager reference](https://langchain-ai.github.io/langmem/reference/memory/)
