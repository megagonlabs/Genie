# Graphiti-inspired bi-temporal knowledge graph

Graphiti builds a temporal context graph from a stream of episodes. Entities
and the facts between them are extracted incrementally, every fact carries a
bi-temporal window, contradicted facts are invalidated rather than deleted,
strongly connected entities are clustered into summarized communities, and
retrieval is a preset combination of retrievers and rerankers rather than a
single index lookup.

This lamp reproduces that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.graphiti.demo
conda run -n "genie" python -m pytest lamps/graphiti/tests -q
```

`demo.py` is the entry point; the module path must be run from the repository
root so both `lamps` and `genie` resolve. It declares the ontology, ingests
four episodes, and prints the bi-temporal report. Ingestion times come from the
episodes rather than a clock, so every run prints the same output.

## Modules

The lamp is split by topic rather than kept in one file, and the dependency
order runs downward:

| Module | Contents |
| --- | --- |
| `ontology.py` | Declared or learned entity and edge types, including cardinality |
| `model.py` | Block types, relation names, time helpers, and the record values |
| `structures.py` | Genie facades wired to the episodic, semantic, and community tiers |
| `temporal_graph.py` | Episode, entity, summary, and fact behavior on both time axes |
| `communities.py` | Community nodes derived from a time slice |
| `search.py` | The one hybrid-search recipe this lamp implements |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | The prescribed ontology, four episodes, and the printed run |

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
| Edge invalidation | Both intervals closed plus a `LineageDAG` succession edge |
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

## Bi-temporality

A fact block joins two independent `TemporalCollection` facades, one per time
axis. Intervals are half-open and a missing end means still open.

| Axis | Interval | Question it answers |
| --- | --- | --- |
| Valid time | `[valid_at, invalid_at)` | When was this true in the world? |
| Transaction time | `[ingested_at, expired_at)` | When did the graph hold it? |

Because the two axes are separate structures, a point-in-time query is a slice
of one or the intersection of both:

- `facts_at(when)` — what was true then, as best we know now.
- `facts_known_at(when)` — what the graph had on record then.
- `facts_at(when, known_at=...)` — both at once.

Ingestion time is supplied per episode rather than read from a clock, so the
example is deterministic. Episodes must be ingested in non-decreasing ingestion
order, and a fact that contradicts another must be ingested after it, because
transaction time is what closes the superseded interval.

The demo makes the lag visible. Alice moved to Seattle on 2025-06-01, but the
episode reporting it was not ingested until 2025-06-20, so on 2025-06-10 the
graph still had San Francisco on record.

## Contradiction without a model

Upstream Graphiti asks a language model whether an incoming fact contradicts an
existing one. A deterministic example cannot, so cardinality is declared
instead: an `EdgeType` marked `single_valued` means one subject holds one active
object for that relation, and a new fact closes the previous one on both axes
and links it with `LineageDAG`. Re-asserting a triplet already on record adds
the new episode as provenance rather than storing a duplicate. Under
`Ontology.learned()` there is no declared cardinality, so nothing is
invalidated automatically.

## Search

Graphiti offers sixteen preset recipes, each pairing retrievers with a reranker.
This lamp implements one, `EDGE_HYBRID_SEARCH_RRF`: three retrievers over facts,
fused by reciprocal rank fusion.

| Retriever | How it runs |
| --- | --- |
| `keyword` | One `MemoryHandle.search` per query term, scored by terms matched |
| `similarity` | Token-set cosine overlap, a visible stand-in for embeddings |
| `graph` | `PropertyGraph.walk` outward from an optional center entity |

Two of the three go through Genie, which is the point: ranking is what Genie
leaves to applications, so the retrievers are where the library does the work
and reciprocal rank fusion is the only scoring the lamp writes. `SearchFilters`
narrows candidates first, and its `as_of` and `known_at` fields reach the same
temporal collections the graph stores facts in.

Every ranking step breaks ties on the fact's resolved triplet and valid-time
start, never on `MemoryBlock.id`. Block ids are fresh UUIDs on each run, so an
id-ordered tie would move a fact's rank inside a retriever, change its
reciprocal-rank contribution, and make both the scores and the order differ
between runs.

Graphiti's other recipes and its MMR, node-distance, episode-mention, and
cross-encoder rerankers are not reproduced. They are pure ranking with no Genie
counterpart, so implementing them would add code without showing anything new.

## Communities

`CommunityBuilder` derives communities from the facts valid on one bi-temporal
slice, so the same graph groups differently at different times. That is the
useful part of deriving them rather than storing them: the community subgraph is
a projection of the semantic subgraph, and a rebuild is a subtree delete because
community nodes are owned blocks.

A community here is a connected component of the entities those facts link.
Graphiti instead runs label propagation, which can split one component into
several communities and can place a new entity incrementally. The component rule
is coarser, but it needs no scoring and reads directly off the graph Genie
already stores. Community summaries are joined member summaries.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_episode` | Store one source record as ground truth |
| `add_triplet` | Store a fact, resolving entities and invalidating contradictions |
| `update_entity_summary` | Append a summary revision, keeping earlier ones |
| `build_communities` | Group and summarize the entities a slice connects |
| `snapshot` | Read a bi-temporal slice |
| `get_entity_history` | Both axes plus sources for one relationship |
| `search` | Rank facts with the hybrid recipe |
| `get_community` | Read the community an entity belongs to |

The agent ingests four episodes, then reports on Alice. As of 2025-08-01 she
lives in Seattle and works at Cascade Analytics; on 2025-06-10 the graph still
had San Francisco and Acme on record. Her `lives_in` history is:

- San Francisco, true `[2024-01-10, 2025-06-01)`, on record
  `[2024-01-10, 2025-06-20)`, sourced from the January and June chats.
- Seattle, true `[2025-06-01, open)`, on record `[2025-06-20, open)`, sourced
  from the June chat.

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

The lamp omits Graphiti's language-model extraction and summarization,
embeddings, BM25 and vector indexes, its other fifteen search recipes, the MMR,
node-distance, episode-mention and cross-encoder rerankers, label-propagation
community detection, MinHash and LSH entity resolution, bulk ingestion, episode
removal, and graph-database engines. Structured triplets replace
natural-language extraction, declared cardinality replaces model-judged
contradiction, term matching plus token overlap replace hybrid retrieval
indexes, and connected components replace label propagation.

Those omissions are deliberate rather than unfinished. A lamp exists to show
that Genie's blocks, links, and structure facades can carry another system's
memory model, so upstream features that are pure ranking or clustering — which
Genie leaves to applications by design — add hand-written algorithm without
adding any demonstration. The features kept are the ones that land on a facade.

Two modelling notes matter when reading the results. Graphiti stores facts as
relationships between entity nodes; this lamp materializes each one as a block
because Genie temporal membership indexes blocks, and the subject and object
edges preserve the same triplet. And closing a valid interval rewrites it in
place, exactly as Graphiti does, so a transaction-time slice reports intervals
as they read now rather than as they read at that moment.

References: [Graphiti repository](https://github.com/getzep/graphiti), [adding
episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes),
[bi-temporal model](https://mintlify.wiki/getzep/graphiti/concepts/temporal-model),
and [communities](https://help.getzep.com/graphiti/core-concepts/communities).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
