# Hindsight-inspired agent memory

This lamp combines world facts, agent experiences, derived observations,
mental models, and temporal and graph retrieval. It demonstrates the main
Hindsight memory-bank workflow, not its API or wire schema.

## Mapping

| Hindsight concept | Genie representation |
| --- | --- |
| Memory bank | Root block and bounded handle |
| Extracted world or experience fact | `hindsight.memory_unit` block |
| Derived observation | Same compact block type with `fact_type="observation"` |
| Text, curation state, entities, dates, metadata, tags, provenance | Application-owned block value, not Hindsight's response schema |
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

Hindsight classifies retained facts as `world` or `experience`; it creates
observations later through asynchronous consolidation. The deterministic agent
in this lamp performs that consolidation explicitly, then stores the resulting
observation with its source ids. The lamp owns validation, state, relation
names, ranking, and mental-model meaning; Genie treats them as application
data. Because one unit may have several dates while one temporal membership
has one event time, each date gets an occurrence child linked to the unit.
Recall resolves and deduplicates those units.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `retain` | Store an extracted world/experience fact or the demo's consolidated observation |
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

The final answer assembly is the lamp's deterministic stand-in for Hindsight
`reflect`: it combines a stored mental model with recalled evidence. Upstream
`reflect` performs model-based synthesis directly; `get_mental_model` is an
inspectable helper specific to this example.

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

The lamp omits Hindsight's LLM extraction, entity resolution, embeddings,
keyword and semantic indexes, temporal-language parsing, rank fusion,
reranking, asynchronous observation consolidation, causal inference, and
automatic mental-model refresh. Transparent token overlap replaces semantic
similarity; structured agent decisions supply observations and synthesized
text. Its `MemoryUnit` is deliberately compact and should not be read as the
complete Hindsight memory-response object.

References: [repository](https://github.com/vectorize-io/hindsight), [retain
documentation](https://hindsight.vectorize.io/developer/retain), [recall
documentation](https://hindsight.vectorize.io/developer/api/recall), and [reflect
documentation](https://hindsight.vectorize.io/developer/api/reflect).

Run and test commands plus the shared agent format are in the [lamp
guide](../README.md).
