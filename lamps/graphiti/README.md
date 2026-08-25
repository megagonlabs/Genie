# Graphiti-inspired bi-temporal knowledge graph

Graphiti builds a temporal context graph from a stream of episodes. It pulls out
entities and the facts between them one episode at a time. Every fact carries a
window on two time axes. A fact that is contradicted is invalidated rather than
deleted. Entities that are strongly connected are grouped into communities with
summaries. Retrieval is not one index lookup: it is a preset combination of
retrievers and rerankers.

This lamp rebuilds that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.graphiti.demo
conda run -n "genie" python -m pytest lamps/graphiti/tests -q
```

`demo.py` is the entry point. Run the module path from the repository root so
that both `lamps` and `genie` can be found. It declares the ontology, ingests
four episodes, and prints the bi-temporal report. Ingestion times come from the
episodes and not from a clock, so every run prints the same output.

## Modules

The lamp is split by topic instead of kept in one file. Each module depends on
the ones above it:

| Module | Contents |
| --- | --- |
| `ontology.py` | Declared or learned entity and edge types, including cardinality |
| `model.py` | Block types, relation names, time helpers, and the record values |
| `structures.py` | Genie facades wired to the episodic, semantic, and community tiers |
| `temporal_graph.py` | Episode, entity, summary, and fact behavior on both time axes |
| `communities.py` | Community nodes derived from a time slice |
| `search.py` | The one hybrid-search recipe this lamp implements |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | The declared ontology, four episodes, and the printed run |

## Mapping

| Graphiti concept | Genie representation |
| --- | --- |
| Graph partition (`group_id`) | Root block and bounded handle |
| Episodic subgraph | `graphiti.episode` blocks owned by the episodic tier |
| Ingestion order | `EventLog` anchored on the episodic tier |
| `reference_time` | `TemporalCollection` event time |
| `EpisodeType` | `source` field: `message`, `text`, `json`, `fact_triple` |
| `MENTIONS` edge | `PropertyGraph` `graphiti.mentions` edge, episode to entity |
| Entity subgraph (`EntityNode`) | `graphiti.entity` blocks owned by the semantic tier |
| Entity resolution | `VersionedRecords` identity keyed by type and name |
| `EntityNode.summary` history | `graphiti.entity_summary` revisions in the same record |
| `EntityEdge` (fact) | `graphiti.fact` block plus subject and object graph edges |
| `EntityEdge.fact` sentence | `fact` field on the block value |
| `valid_at` / `invalid_at` | Valid-time `TemporalCollection` |
| `created_at` / `expired_at` | Transaction-time `TemporalCollection` |
| `EntityEdge.episodes` | `graphiti.provenance` edges, fact to episode |
| Edge invalidation | Both intervals closed, plus a `LineageDAG` succession edge |
| Community subgraph (`CommunityNode`) | `graphiti.community` blocks and `graphiti.has_member` edges |
| Custom entity and edge types | `Ontology`, `EntityType`, `EdgeType` in the lamp |
| `EDGE_HYBRID_SEARCH_RRF` recipe | `GraphitiSearch` over `MemoryHandle.search` and `PropertyGraph` |
| `SearchFilters` valid time | `SearchFilters.as_of` and `SearchFilters.known_at` |
| Agent access | Named JSON-compatible tools |

Graphiti's three subgraphs become ownership tiers below one root, so deleting a
graph deletes everything in it:

```text
graphiti.graph                    one graph partition
  episodic                        episode blocks, ingestion log, reference times
  semantic                        entity, summary, and fact blocks
    valid_time                    fact valid-time membership
    transaction_time              fact transaction-time membership
  community                       community blocks
```

## Two time axes

A fact block joins two separate `TemporalCollection` facades, one per time axis.
The intervals are half-open, and a missing end means the interval is still open.

| Axis | Interval | Question it answers |
| --- | --- | --- |
| Valid time | `[valid_at, invalid_at)` | When was this true in the world? |
| Transaction time | `[ingested_at, expired_at)` | When did the graph hold it? |

The two axes are separate structures, so a point-in-time query is a slice of one
axis, or the intersection of both:

- `facts_at(when)` — what was true then, as far as we know now.
- `facts_known_at(when)` — what the graph had on record then.
- `facts_at(when, known_at=...)` — both at once.

Ingestion time is supplied with each episode instead of read from a clock, so
the example is deterministic. Episodes must be ingested in non-decreasing
ingestion order, and a fact that contradicts another must be ingested after it,
because transaction time is what closes the interval of the fact it replaces.

The demo makes this delay visible. Alice moved to Seattle on 2025-06-01, but the
episode that reports it was not ingested until 2025-06-20. So on 2025-06-10 the
graph still had San Francisco on record.

## Contradiction without a model

Upstream, Graphiti asks a language model whether an incoming fact contradicts an
existing one. A deterministic example cannot do that, so it declares cardinality
instead. An `EdgeType` marked `single_valued` means one subject holds one active
object for that relation. A new fact then closes the previous one on both axes
and links the two with `LineageDAG`. Asserting a triplet that is already on
record adds the new episode as provenance instead of storing a duplicate. Under
`Ontology.learned()` there is no declared cardinality, so nothing is invalidated
automatically.

## Search

Graphiti offers sixteen preset recipes, and each one pairs retrievers with a
reranker. This lamp implements one of them, `EDGE_HYBRID_SEARCH_RRF`: three
retrievers over facts, combined by reciprocal rank fusion.

| Retriever | How it runs |
| --- | --- |
| `keyword` | One `MemoryHandle.search` per query term, scored by how many terms matched |
| `similarity` | Token-set cosine overlap, a visible stand-in for embeddings |
| `graph` | `PropertyGraph.walk` outward from an optional center entity |

Two of the three run through Genie, and that is the point. Genie leaves ranking
to applications, so the retrievers are where the library does the work, and
reciprocal rank fusion is the only scoring the lamp writes itself.
`SearchFilters` narrows the candidates first, and its `as_of` and `known_at`
fields read the same temporal collections that the graph stores facts in.

Every ranking step breaks ties on the fact's resolved triplet and the start of
its valid time, never on `MemoryBlock.id`. Block ids are new UUIDs on every run.
A tie broken by id would move a fact's rank inside a retriever, change how much
it contributes to the fused rank, and make both the scores and the order differ
between runs.

Graphiti's other recipes are not reproduced, and neither are its MMR,
node-distance, episode-mention, and cross-encoder rerankers. They are pure
ranking with nothing in Genie behind them, so implementing them would add code
without showing anything new.

## Communities

`CommunityBuilder` derives communities from the facts that are valid on one
bi-temporal slice. So the same graph groups differently at different times. That
is the useful part of deriving communities instead of storing them: the
community subgraph is a projection of the semantic subgraph, and rebuilding it
is a subtree delete, because community nodes are owned blocks.

A community here is a connected component of the entities that those facts link.
Graphiti instead runs label propagation, which can split one component into
several communities and can place a new entity as it arrives. The
connected-component rule is rougher, but it needs no scoring and reads straight
off the graph that Genie already stores. A community summary is the summaries of
its members joined together.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_episode` | Store one source record as ground truth |
| `add_triplet` | Store a fact, resolving entities and invalidating contradictions |
| `update_entity_summary` | Append a summary revision and keep the earlier ones |
| `build_communities` | Group and summarize the entities that a slice connects |
| `snapshot` | Read a bi-temporal slice |
| `get_entity_history` | Both axes plus sources for one relationship |
| `search` | Rank facts with the hybrid recipe |
| `get_community` | Read the community an entity belongs to |

The agent ingests four episodes and then reports on Alice. As of 2025-08-01 she
lives in Seattle and works at Cascade Analytics. On 2025-06-10 the graph still
had San Francisco and Acme on record. Her `lives_in` history is:

- San Francisco, true `[2024-01-10, 2025-06-01)`, on record
  `[2024-01-10, 2025-06-20)`, from the January and June chats.
- Seattle, true `[2025-06-01, open)`, on record `[2025-06-20, open)`, from the
  June chat.

| Tool | Calls |
| --- | ---: |
| `add_episode` | 4 |
| `add_triplet` | 9 |
| `update_entity_summary` | 3 |
| `build_communities` | 1 |
| `snapshot` | 2 |
| `get_entity_history` | 1 |
| `search` | 1 |
| `get_community` | 1 |

## Limits

The lamp leaves out Graphiti's language-model extraction and summarization, its
embeddings, its BM25 and vector indexes, its other fifteen search recipes, the
MMR, node-distance, episode-mention and cross-encoder rerankers,
label-propagation community detection, MinHash and LSH entity resolution, bulk
ingestion, episode removal, and graph-database engines. Structured triplets
replace extraction from natural language. Declared cardinality replaces
contradiction judged by a model. Term matching plus token overlap replace hybrid
retrieval indexes. Connected components replace label propagation.

These omissions are on purpose, not unfinished work. A lamp exists to show that
Genie's blocks, links, and structure facades can carry another system's memory
model. Upstream features that are pure ranking or clustering are exactly what
Genie leaves to applications by design, so writing them here would add a
hand-written algorithm and demonstrate nothing. The features that were kept are
the ones that land on a facade.

Two modelling notes matter when you read the results. Graphiti stores facts as
relationships between entity nodes. This lamp makes each fact a block instead,
because Genie temporal membership indexes blocks, and the subject and object
edges keep the same triplet. Also, closing a valid interval rewrites it in
place, exactly as Graphiti does, so a transaction-time slice reports the
intervals as they read now rather than as they read at that moment.

References: [Graphiti repository](https://github.com/getzep/graphiti), [adding
episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes),
[bi-temporal model](https://mintlify.wiki/getzep/graphiti/concepts/temporal-model),
and [communities](https://help.getzep.com/graphiti/core-concepts/communities).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
