# LangMem-inspired lamp

This lamp stores application-shaped values as `(namespace, key, value)` items.
It is a focused Genie example, not a LangMem compatibility layer.

## Memory shape

Every `langmem.item` block uses a stable wrapper while the supplied value can
follow any schema:

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

The demo registers three schemas:

| Namespace | Value fields |
| --- | --- |
| Semantic | `subject`, `predicate`, `object`, `context` |
| Episode | `observation`, `thoughts`, `action`, `result` |
| Mental model | `name`, `summary`, `principles`, `source_keys` |

`ValueSchema` is a dependency-free stand-in for an application-supplied
validator such as Pydantic. Unregistered namespaces may hold any value accepted
by the configured Genie codec. Event times and entity hints stay in block
metadata, so they do not change the supplied value schema.

## Genie mapping

| Behavior | Genie implementation |
| --- | --- |
| Hierarchical namespace | `MemoryAddress.namespace` and the wrapper field |
| Contextual key | Atomic `KeyedCollection` binding within one namespace |
| Create and update | `LangMemStore`; updates preserve block identity |
| Direct retrieval | Exact keyed lookup |
| Relevant-value retrieval | Deterministic token overlap |
| Dated episodes | `TemporalCollection` using metadata annotations |
| Related items | Application-labeled `PropertyGraph` edges |
| Mental model | Regular item in its own schema and namespace |
| Agent access | Namespace-bound manage, get, search, and link tools |

Ownership keeps items under one store root; links do not imply ownership.
Schema meaning, ranking, synthesis, and agent behavior remain application code.

## Demo workflow

The deterministic travel agent stores two facts, two episodes, and one mental
model; adds three links; retrieves the model by key; searches values and a time
range; then follows two graph hops from a seat preference to a successful
episode and its supporting model.

See [the lamp guide](../README.md) for run commands and the shared output
format.

## Limits

The lamp does not reproduce LangMem's LLM extraction and consolidation,
Pydantic integration, prompt optimization, background reflection, embeddings,
or LangGraph `BaseStore`. Token overlap is lexical, not semantic similarity.

Temporal and graph queries are application extensions; documented LangMem
storage covers direct access, semantic search, and metadata filtering. The
mental-model schema is also supplied by this demo rather than built into
LangMem or Genie.

References: [concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/),
[semantic schemas](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/),
[tools](https://langchain-ai.github.io/langmem/reference/tools/), and
[memory managers](https://langchain-ai.github.io/langmem/reference/memory/).
