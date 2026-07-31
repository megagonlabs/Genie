# Hindsight-inspired agent memory

This lamp shows how Genie primitives can support a small Hindsight-inspired
memory bank with atomic facts, agent experiences, derived observations, mental
models, and temporal and graph retrieval. It is an architectural example, not
a compatibility layer or a complete Hindsight implementation.

Hindsight describes retain, recall, and reflect operations over world facts,
agent experiences, observations, and separately stored mental models. Its
recall pipeline combines semantic, keyword, graph, and temporal strategies. See
the official [Hindsight repository](https://github.com/vectorize-io/hindsight),
[retain documentation](https://hindsight.vectorize.io/developer/retain), and
[recall documentation](https://hindsight.vectorize.io/developer/retrieval).

## Concept mapping

| Hindsight concept | Genie representation in this lamp |
| --- | --- |
| Memory bank | One root block and bounded `MemoryHandle` |
| Atomic memory unit | One `hindsight.memory_unit` block |
| `text` | Memory-unit block value field |
| `fact_type` | `world`, `experience`, or `observation` value and index metadata |
| `state` | `valid` or `invalidated` value and index metadata |
| Entities, occurred dates, tags | Atomic value fields plus portable index metadata |
| Unit metadata and provenance | Nested atomic value fields |
| Temporal graph | `hindsight.temporal` typed links |
| Semantic graph | `hindsight.semantic` typed links |
| Causal graph | `hindsight.causal` typed links |
| Mental model | Separate `hindsight.mental_model` synthesized-document block |
| Recall | Deterministic text/entity matching, explicit time filtering, and link traversal |
| Agent access | Named tools with JSON-compatible arguments and results |

Each atomic block stores the complete application-owned structure:

```text
MemoryUnit {
  text,
  fact_type,
  state,
  entities,
  occurred_dates,
  metadata,
  tags,
  provenance
}
```

Genie remains unaware of these field meanings. The lamp owns validation,
curation state, graph-relation names, ranking, temporal matching, and mental
model semantics.

## Agent and tools

The demo gives a deterministic operations agent seven memory tools instead of
direct access to Genie or `HindsightMemoryBank`:

| Tool | Agent capability |
| --- | --- |
| `retain` | Store one complete atomic world fact, experience, or observation |
| `link_memories` | Add a temporal, semantic, or causal relationship |
| `create_mental_model` | Save a synthesized document with evidence references |
| `recall` | Rank active atomic units by text, entity, type, and tags |
| `recall_temporal` | Retrieve experiences within an occurred-date range |
| `recall_graph` | Traverse typed relationships around one memory |
| `get_mental_model` | Retrieve a named synthesized document |

The agent remembers an Atlas deployment failure and successful retry, derives
an observation about smaller migration batches, connects the memories through
all three graph relation types, and stores an Atlas deployment playbook. It then
answers a question by combining mental-model, general, temporal, and graph
retrieval.

Run it from the repository root:

```bash
conda run -n "genie" python -m lamps.hindsight
```

The compact answer includes:

```text
Mental model (atlas-deployment-playbook): Deploy Atlas on Friday, use small database-migration batches, and retry failed batches separately.
Relevant atomic memories:
- [experience] I retried Atlas 2.0 on March 16 using smaller migration batches; the deployment succeeded.
- [observation] Atlas database migrations are more reliable when run in small batches.
Experience timeline for Atlas:
- 2025-03-15: I deployed Atlas 2.0 on March 15; the database migration timed out.
- 2025-03-16: I retried Atlas 2.0 on March 16 using smaller migration batches; the deployment succeeded.
Graph context around timeout:
- causal (in): Atlas 2.0 migrated all customer records in one large batch.
- semantic (in): Atlas database migrations are more reliable when run in small batches.
- temporal (out): I retried Atlas 2.0 on March 16 using smaller migration batches; the deployment succeeded.

Inspectable tool calls:
01. retain
02. retain
03. retain
04. retain
05. retain
06. link_memories
07. link_memories
08. link_memories
09. link_memories
10. create_mental_model
11. get_mental_model
12. recall
13. recall_temporal
14. recall_graph

Tool usage:
- retain x5
- link_memories x4
- create_mental_model x1
- get_mental_model x1
- recall x1
- recall_temporal x1
- recall_graph x1
```

## Deliberate limits

The lamp does not reproduce Hindsight's LLM extraction, canonical entity
resolution, embeddings, BM25 index, temporal-language parser, reciprocal-rank
fusion, cross-encoder reranking, asynchronous observation consolidation, causal
inference, or automatic mental-model refresh.

`recall` uses transparent token overlap rather than claiming true semantic
similarity. Observations and mental-model text are supplied as structured agent
decisions so the example remains deterministic and runs without an API key or
agent-framework dependency. A model-backed agent could use the same tool
adapter to extract units, select links, synthesize documents, and choose
retrieval calls.
