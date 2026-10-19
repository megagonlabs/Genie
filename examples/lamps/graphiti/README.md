# Graphiti-inspired bi-temporal knowledge graph

Graphiti builds a temporal context graph from a stream of episodes.

- **Extraction:** Graphiti takes entities and the facts between them from the episodes, one episode at a time.
- **2 time axes:** each fact has a time window on each axis.
- **Invalidation, not deletion:** a contradiction invalidates the old fact. It does not delete it.
- **Communities:** Graphiti groups strongly connected entities and summarizes each group.
- **Retrieval is not one index lookup:** it is a preset combination of retrievers and rerankers.

This lamp builds the same architecture again from Genie building blocks. It is
an architectural example. It is not a replacement for Graphiti.

## Run

From the repository root, with no API key and no external service:

```bash
python -m examples.lamps.graphiti.demo
python -m pytest examples/lamps/graphiti/tests -q
```

- **`demo.py` is the entry point.** Run the module path from the repository root, so that Python can find both `examples` and `genie`.
- **What it does:** the demo declares the ontology, ingests 4 episodes, and prints the bi-temporal report.
- **Deterministic:** ingestion times come from the episodes and not from a clock. So every run prints the same output.

## Modules

The lamp has one module for each topic, not one large file. A module imports
only modules that are above it in this table:

| Module | Contents |
| --- | --- |
| `ontology.py` | Declared or learned entity and edge types, with cardinality |
| `model.py` | Block types, relation names, time helpers, and the record values |
| `structures.py` | Genie facades connected to the episodic, semantic, and community tiers |
| `temporal_graph.py` | Episode, entity, summary, and fact behavior on both time axes |
| `communities.py` | Community nodes derived from a time slice |
| `search.py` | The one hybrid-search recipe that this lamp implements |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | The declared ontology, 4 episodes, and the printed run |

## Mapping

| Graphiti concept | Genie representation |
| --- | --- |
| Graph partition (`group_id`) | Root block and bounded handle |
| Episodic subgraph | `graphiti.episode` blocks that the episodic tier owns |
| Ingestion order | `EventLog` with the episodic tier as its anchor |
| `reference_time` | `TemporalCollection` event time |
| `EpisodeType` | `source` field: `message`, `text`, `json`, `fact_triple` |
| `MENTIONS` edge | `PropertyGraph` `graphiti.mentions` edge, episode to entity |
| Entity subgraph (`EntityNode`) | `graphiti.entity` blocks that the semantic tier owns |
| Entity resolution | `VersionedRecords` identity keyed by type and name |
| `EntityNode.summary` history | `graphiti.entity_summary` revisions in the same record |
| `EntityEdge` (fact) | `graphiti.fact` block, and subject and object graph edges |
| `EntityEdge.fact` sentence | `fact` field on the block value |
| `valid_at` / `invalid_at` | Valid-time `TemporalCollection` |
| `created_at` / `expired_at` | Transaction-time `TemporalCollection` |
| `EntityEdge.episodes` | `graphiti.provenance` edges, fact to episode |
| Edge invalidation | Both intervals closed, and a `LineageDAG` succession edge |
| Community subgraph (`CommunityNode`) | `graphiti.community` blocks and `graphiti.has_member` edges |
| Custom entity and edge types | `Ontology`, `EntityType`, `EdgeType` in the lamp |
| `EDGE_HYBRID_SEARCH_RRF` recipe | `GraphitiSearch` over `MemoryHandle.search` and `PropertyGraph` |
| `SearchFilters` valid time | `SearchFilters.as_of` and `SearchFilters.known_at` |
| Agent access | Named JSON-compatible tools |

The 3 subgraphs of Graphiti are ownership tiers below one root. So a delete of
the graph root deletes everything in the graph:

```text
graphiti.graph                    one graph partition
  episodic                        episode blocks, ingestion log, reference times
  semantic                        entity, summary, and fact blocks
    valid_time                    fact valid-time membership
    transaction_time              fact transaction-time membership
  community                       community blocks
```

## Two time axes

A fact block is a member of 2 separate `TemporalCollection` facades, one for
each time axis.

| Axis | Interval | Question it answers |
| --- | --- | --- |
| Valid time | `[valid_at, invalid_at)` | When was this true in the world? |
| Transaction time | `[ingested_at, expired_at)` | When did the graph hold it? |

- **Intervals are half-open:** each interval includes its start and does not include its end. An interval with no end is still open.
- **Separate structures:** so a point-in-time query is a slice of one axis, or the intersection of the 2 slices:
  - `facts_at(when)`: what was true at that time, as far as we know now.
  - `facts_known_at(when)`: what the graph had on record at that time.
  - `facts_at(when, known_at=...)`: both questions together.
- **Ingestion time comes with each episode:** the lamp does not read it from a clock, so the example is deterministic.
- **Order rules:**
  - The graph accepts episodes only in non-decreasing order of ingestion time.
  - The graph refuses a fact that contradicts an older fact if its ingestion time is not later than the ingestion time of the older fact.
  - The reason: transaction time is the clock that closes the interval of the replaced fact.
- **The demo shows the delay:** Alice moved to Seattle on 2025-06-01. The graph ingested the episode that reports the move on 2025-06-20. So on 2025-06-10, the graph still had San Francisco on record.

## Contradiction without a model

- **Upstream asks a language model:** Graphiti asks a model if a new fact contradicts an existing fact. A deterministic example cannot do that. So the lamp declares cardinality.
- **`single_valued` on an `EdgeType`:** one subject holds 1 active object for that relation. A new fact then closes the previous fact on both axes, and `LineageDAG` links the 2 facts.
- **A repeated triplet adds provenance, not a duplicate:** if a triplet is already on record, the graph adds the new episode to the existing fact. It does not store another fact.
- **`Ontology.learned()` declares no cardinality:** so the graph invalidates nothing automatically.

## Search

Graphiti has 16 preset recipes. Each recipe pairs retrievers with a reranker.
This lamp implements 1 of them, `EDGE_HYBRID_SEARCH_RRF`. This recipe uses 3
retrievers over facts and combines their results with reciprocal rank fusion.

| Retriever | How it runs |
| --- | --- |
| `keyword` | One `MemoryHandle.search` for each query term. The score is the number of matched terms |
| `similarity` | Cosine overlap of token sets, a visible replacement for embeddings |
| `graph` | `PropertyGraph.walk` outward from an optional center entity |

- **2 of the 3 retrievers run through Genie, and this is intentional:** Genie leaves ranking to applications. So the library does its work in the retrievers. The only scoring that the lamp writes itself is the term count, the token overlap, and reciprocal rank fusion.
- **`SearchFilters` narrows the candidates first:** its `as_of` and `known_at` fields read the same temporal collections in which the graph stores facts.
- **Ties use the resolved triplet of the fact and the start of its valid time, never `MemoryBlock.id`:** block ids are new UUIDs on every run. A tie that the id breaks moves the rank of a fact inside a retriever. That changes its contribution to the fused rank. Then the scores and the order differ between runs.
- **The lamp does not reproduce the other recipes,** or the MMR, node-distance, episode-mention, and cross-encoder rerankers. These are only ranking, and no Genie feature supports them. So an implementation adds code and shows nothing new.

## Communities

- **Derived from a slice:** `CommunityBuilder` derives communities from the facts that are valid on one bi-temporal slice. So the same graph gives different groups at different times.
- **This is the useful part:** the community subgraph is a projection of the semantic subgraph. A rebuild is a subtree delete, because community nodes are owned blocks.
- **Connected components:** a community in this lamp is a connected component of the entities that those facts link.
- **Upstream uses label propagation:** label propagation can split one component into several communities. It can also place a new entity when the entity arrives. The connected-component rule is less exact. But it needs no scoring, and it reads its groups directly from the graph that Genie already stores.
- **Community summary:** the summaries of the members, joined together.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_episode` | Store one source record as ground truth |
| `add_triplet` | Store a fact. Resolve entities and invalidate contradictions |
| `update_entity_summary` | Append a summary revision and keep the earlier ones |
| `build_communities` | Group and summarize the entities that a slice connects |
| `snapshot` | Read a bi-temporal slice |
| `get_entity_history` | Read both axes and the sources for one relationship |
| `search` | Rank facts with the hybrid recipe |
| `get_community` | Read the community of an entity |

The agent ingests 4 episodes. Then it reports on Alice:

- **As of 2025-08-01:** she lives in Seattle and works at Cascade Analytics.
- **On 2025-06-10:** the graph still had San Francisco and Acme on record.
- **Her `lives_in` history:**
  - San Francisco, true `[2024-01-10, 2025-06-01)`, on record `[2024-01-10, 2025-06-20)`, from the January and June chats.
  - Seattle, true `[2025-06-01, open)`, on record `[2025-06-20, open)`, from the June chat.

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

**Left out:** the lamp does not include these Graphiti features:

- **Model work:** language-model extraction and summarization.
- **Indexes:** embeddings, and the BM25 and vector indexes.
- **Search:** the other 15 search recipes, and the MMR, node-distance, episode-mention, and cross-encoder rerankers.
- **Communities:** label-propagation community detection.
- **Entity resolution:** MinHash and LSH entity resolution.
- **Operations:** bulk ingestion and episode removal.
- **Storage:** graph-database engines.

**Replaced by:**

| Upstream | Here |
| --- | --- |
| Extraction from natural language | Structured triplets |
| Contradiction judged by a model | Declared cardinality |
| Hybrid retrieval indexes | Term match counts and token overlap |
| Label propagation | Connected components |

- **The omissions are intentional, not unfinished work.** A lamp shows that the blocks, links, and structure facades of Genie can hold the memory model of another system.
- **Ranking and clustering stay out:** by design, Genie leaves upstream features that are only ranking or clustering to applications. In this lamp, such a feature adds a hand-written algorithm and shows nothing.
- **Kept features map onto a facade:** the lamp keeps the upstream features that map onto a Genie facade.

**Read the results with these 2 model notes:**

- **Facts are blocks, not relationships:** Graphiti stores facts as relationships between entity nodes. This lamp makes each fact a block, because Genie temporal membership indexes blocks. The subject and object edges keep the same triplet.
- **The graph rewrites a valid interval in place when it closes it,** as Graphiti does. So a transaction-time slice shows the intervals as they are now, not as they were at that time.

References: [Graphiti repository](https://github.com/getzep/graphiti), [adding
episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes),
[bi-temporal model](https://mintlify.wiki/getzep/graphiti/concepts/temporal-model),
and [communities](https://help.getzep.com/graphiti/core-concepts/communities).

The [lamp guide](../README.md) describes the shared agent-demo output format.
