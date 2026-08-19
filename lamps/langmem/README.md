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

The demo registers three application schemas that correspond to LangMem's
semantic collection, episodic, and semantic-profile patterns:

| Namespace | Value fields |
| --- | --- |
| Semantic | `subject`, `predicate`, `object`, `context` |
| Episode | `observation`, `thoughts`, `action`, `result` |
| Semantic profile | `name`, `summary`, `principles`, `source_keys` |

`ValueSchema` is a dependency-free stand-in for an application-supplied
validator such as Pydantic. Unregistered namespaces may hold any value accepted
by the configured Genie codec. Event times and entity hints stay in block
metadata, so they do not change the supplied value schema.

## Genie mapping

| Behavior | Genie implementation |
| --- | --- |
| Hierarchical namespace | `MemoryAddress.namespace` and the wrapper field |
| Contextual key | Atomic `KeyedCollection` binding within one namespace |
| Create, update, and delete | `LangMemStore`; updates preserve block identity |
| Direct retrieval | Exact keyed lookup |
| Relevant-value retrieval | Deterministic token overlap |
| Dated episodes | `TemporalCollection` using metadata annotations |
| Related items | Application-labeled `PropertyGraph` edges |
| Semantic profile | Regular item in its own schema and namespace |
| Agent access | Namespace-bound manage, get, search, and link tools |

Ownership keeps items under one store root; links do not imply ownership.
Schema meaning, ranking, synthesis, and agent behavior remain application code.

## Demo workflow

The deterministic travel agent stores two facts, two episodes, and one semantic
profile; adds three links; retrieves the profile by key; searches values and a
time range; then follows two graph hops from a seat preference to a successful
episode and its supporting profile. The `manage_memory` tool also supports the
same create/update/delete lifecycle as LangMem's default management tool.

See [the lamp guide](../README.md) for run commands and the shared output
format.

## Limits

The lamp does not reproduce LangMem's LLM extraction and consolidation,
Pydantic integration, prompt optimization, background memory managers,
embeddings, or LangGraph `BaseStore`. The deterministic agent demonstrates the
hot-path tool pattern; it does not run an automatic background manager. Token
overlap is lexical, not semantic similarity.

Temporal and graph queries are application extensions; documented LangMem
storage covers direct access, semantic search, and metadata filtering. The
profile schema is supplied by this demo; LangMem accepts application-defined
schemas rather than requiring these particular fields.

References: [concepts](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/),
[semantic schemas](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/),
[tools](https://langchain-ai.github.io/langmem/reference/tools/), and
[memory managers](https://langchain-ai.github.io/langmem/reference/memory/).
