# Graphiti-inspired temporal knowledge graph

This lamp shows how Genie primitives can support a small temporal knowledge
graph with provenance and effective-time validity. It is an architectural
example, not a drop-in implementation of Graphiti.

## Concept mapping

| Temporal graph concept | Genie representation |
| --- | --- |
| Graph boundary | One root block and a bounded `MemoryHandle` |
| Entity node | `graphiti.entity` memory block |
| Source episode | `graphiti.episode` memory block |
| Temporal fact | First-class `graphiti.temporal_fact` memory block |
| Subject and object | `PropertyGraph` edges from a fact to entity blocks |
| Provenance | `PropertyGraph` edges from a fact to episodes |
| Episode time | Event membership in `TemporalCollection` |
| Validity period | `ValidityInterval` membership in `TemporalCollection` |
| Supersession | Close the old interval and add a cycle-protected `LineageDAG` edge |
| Agent access | Named tools with JSON-compatible arguments and results |

Validity intervals are half-open: `[valid_from, valid_to)`. An absent
`valid_to` means that the fact remains valid. When an exclusive relationship
such as `lives_in` changes, the example closes the previous fact at the new
fact's `valid_from`. The update episode is also attached to the previous fact
because it is the evidence for ending that validity period.

## Agent and tools

The demo gives a small agent five tools instead of direct access to Genie or
the temporal graph:

| Tool | Agent capability |
| --- | --- |
| `add_episode` | Store raw source material for provenance |
| `add_fact` | Store a dated relationship supported by an episode |
| `query_entity` | Retrieve facts valid for an entity at one time |
| `query_neighborhood` | Inspect incoming and outgoing valid relationships |
| `get_history` | Retrieve validity periods and supporting sources |

The agent first learns from three observations through the write tools. It then
answers a question about Alice by selecting the three query tools. The agent is
deterministic so the lamp runs without an API key or agent-framework
dependency. Its tool interface is intentionally similar to an LLM
function-calling interface, so a model-backed agent could use the same adapter.

Run the demo from the repository root:

```bash
conda run -n "genie" python -m lamps.graphiti
```

The resulting graph connects Alice, Bob, Acme, San Francisco, and Seattle. The
agent answers with current context, related company context, location history,
and provenance:

```text
== Graphiti-inspired temporal knowledge graph ==
An agent stores source episodes, temporal facts, provenance, and relationship history.
Genie structures: property graph, temporal collection, lineage DAG

Tools available to the agent:
- add_episode: Store raw source material for later provenance.
- add_fact: Store a temporal subject-relation-object fact and link it to its source episode. Entities are reused by name and type.
- query_entity: Return facts about an entity that are valid at a given time.
- query_neighborhood: Return incoming and outgoing facts around an entity at a given time.
- get_history: Return validity periods and provenance for an entity relationship.

Agent answer:
Question: Where does Alice live now, where did she live before, and who works with her?
Current facts for Alice as of 2025-07-01:
- Alice -[lives_in]-> Seattle
- Alice -[works_at]-> Acme
Related context around Acme:
- Acme -[headquartered_in]-> San Francisco
- Bob -[works_at]-> Acme
lives_in history for Alice:
- Alice -[lives_in]-> San Francisco; valid [2024-01-10, 2025-06-01); provenance: chat/2024-01-10, chat/2025-06-02
- Alice -[lives_in]-> Seattle; valid [2025-06-01, open); provenance: chat/2025-06-02

Inspectable tool calls:
01. add_episode
02. add_fact
03. add_fact
04. add_episode
05. add_fact
06. add_fact
07. add_fact
08. add_episode
09. add_fact
10. query_entity
11. query_neighborhood
12. get_history

Tool usage:
- add_episode x3
- add_fact x6
- query_entity x1
- query_neighborhood x1
- get_history x1
```

## Deliberate limits

The example does not reproduce Graphiti's LLM extraction, entity resolution,
ontology management, embeddings, hybrid ranking, or graph-database query
engine. Those are application and backend concerns. The lamp focuses on the
portable model Genie can provide: owned blocks, scoped access, a property
graph, provenance, temporal membership, and cycle-protected succession.

The example agent receives structured fact candidates rather than extracting
them from raw text. Its deterministic policy demonstrates tool access and an
agent loop, not natural-language reasoning. A production agent would typically
use a model to choose tool calls and extract or validate candidate facts.
