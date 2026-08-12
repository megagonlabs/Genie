# Hindsight-inspired agent memory

This lamp combines atomic facts, experiences, derived observations, mental
models, and temporal and graph retrieval. It demonstrates part of Hindsight's
retain/recall/reflect model, not a compatibility layer.

## Mapping

| Hindsight concept | Genie representation |
| --- | --- |
| Memory bank | Root block and bounded handle |
| Atomic unit | `hindsight.memory_unit` block |
| `text`, type, state, entities, dates, metadata, tags, provenance | Complete application-owned block value |
| Retention order | `EventLog` |
| Each occurred date | Owned `hindsight.occurrence` child in `TemporalCollection` |
| Temporal, semantic, causal graphs | Relation-specific `PropertyGraph` edges |
| Mental model | Separate named block in `KeyedCollection` |
| Recall | Text/entity matching plus temporal and graph queries |
| Agent access | Named JSON-compatible tools |

Each atomic value has this shape:

```text
MemoryUnit {
  text,
  fact_type,        # world, experience, or observation
  state,            # valid or invalidated
  entities,
  occurred_dates,
  metadata,
  tags,
  provenance
}
```

The lamp owns validation, state, relation names, ranking, and mental-model
meaning; Genie treats them as application data. Because one unit may have
several dates while one temporal membership has one event time, each date gets
an occurrence child linked to the canonical unit. Recall resolves and
deduplicates those units.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `retain` | Store a world fact, experience, or observation |
| `link_memories` | Add a temporal, semantic, or causal edge |
| `create_mental_model` | Save a synthesis with evidence ids |
| `recall` | Rank active units by text, entity, type, and tags |
| `recall_temporal` | Query experiences by occurred date |
| `recall_graph` | Traverse typed relations |
| `get_mental_model` | Fetch a named synthesis |

The agent records an Atlas deployment timeout, a successful smaller-batch
retry, and the derived observation that small migration batches are more
reliable. It links five units with four edges and creates a deployment
playbook. The answer recommends a Friday deployment with small batches and
separate retries, supported by the March 15 failure, March 16 success, and
temporal, semantic, and causal graph context.

| Tool | Calls |
| --- | ---: |
| `retain` | 5 |
| `link_memories` | 4 |
| `create_mental_model` | 1 |
| `get_mental_model` | 1 |
| `recall` | 1 |
| `recall_temporal` | 1 |
| `recall_graph` | 1 |

## Limits

The lamp omits Hindsight's LLM extraction, canonical entity resolution,
embeddings, BM25, temporal-language parsing, rank fusion, reranking,
asynchronous consolidation, causal inference, and automatic mental-model
refresh. Transparent token overlap replaces semantic similarity; structured
agent decisions supply observations and synthesized text.

References: [repository](https://github.com/vectorize-io/hindsight), [retain
documentation](https://hindsight.vectorize.io/developer/retain), and [recall
documentation](https://hindsight.vectorize.io/developer/retrieval).

Run and test commands plus the shared agent format are in the [lamp
guide](../README.md).
