# Hindsight-inspired agent memory

Hindsight is a memory service for agents that improve over time.

- **Keeps the source payloads** and extracts facts from them.
- **Separates what the agent did from what the agent knows.**
- **Merges related facts into observations** with evidence. It refines the observations and does not overwrite them.
- **Keeps standing answers current** in the background.
- **Answers a query with 4 retrievers in parallel,** and combines their results into one result set that fits a budget.

This lamp builds that architecture again from Genie blocks, links, and structure
facades. It is an architectural example, not a replacement for Hindsight.

## Run

From the repository root, with no API key and no external service:

```bash
python -m examples.lamps.hindsight.demo
python -m pytest examples/lamps/hindsight/tests -q
```

- **`demo.py` is the entry point.** Run the module path from the repository root, so that Python can find both `examples` and `genie`.
- **What it does:** declares 2 entities and 1 standing question, retains 4 documents, and prints the answer.
- **Deterministic.** Every timestamp comes from a document, not from a clock. So each run prints the same output.

## Modules

The lamp has one module for each topic, not one large file. Each module depends
only on the modules above it in this table:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, relations, taxonomy, records, and time helpers |
| `structures.py` | Genie facades connected to the 5 ownership tiers of the bank |
| `bank.py` | Retain, entity resolution, links, invalidation, and both time axes |
| `observations.py` | Consolidation of facts into versioned beliefs with evidence |
| `mental_models.py` | Standing questions and the versions of their answers |
| `recall.py` | 4 retrievers, reciprocal rank fusion, and the token budget |
| `reflect.py` | Answers from the highest layer that already knows |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | 2 entities, 1 standing question, 4 documents, and 1 answer |

## Mapping

| Hindsight concept | Genie representation |
| --- | --- |
| Memory bank | Root block and bounded handle |
| Bank isolation | Handle boundary. The handle refuses an id outside the root |
| Retained payload | `hindsight.document` block that the documents tier owns |
| Extracted fact | `hindsight.memory` block that the memories tier owns |
| `world` and `experience` perspective | `fact_type` field on that block |
| `valid` and `invalidated` state | `state` field, updated in place |
| Retention order | `EventLog` anchored on the memories tier |
| Consolidation progress | `consolidated_through` on the observations anchor |
| When the bank learned a fact | `TemporalCollection` event time |
| `occurred_start` / `occurred_end` | `ValidityInterval` on the same entry |
| Entity | `hindsight.entity` block that the entities tier owns |
| Entity resolution and aliases | Many `KeyedCollection` addresses for one block |
| Entity connections | `PropertyGraph` `hindsight.mentions` edges |
| Temporal, semantic, and causal connections | `PropertyGraph` edges of those relations |
| Provenance and `based_on` | `LineageDAG` on `hindsight.derived_from` |
| Observation | `VersionedRecords` identity with the claim as its key |
| Observation refinement and history | Revisions of that record |
| Proof count | Lineage predecessors of the current revision |
| Stale observation | Retention cursor compared with the claims that are still pending |
| Mental model | `VersionedRecords` identity with the name as its key |
| Mental-model versions | Revisions plus lineage edges from observations |
| Deduplication on retain | `MemoryHandle.search` with a metadata filter |
| TEMPR keyword retriever | One `MemoryHandle.search` for each query term |
| TEMPR graph retriever | `PropertyGraph.walk` from an entity |
| TEMPR temporal retriever | `TemporalCollection.events` and `.overlapping` |
| Delete of a bank or a belief | `OwnershipTree.delete_subtree` |
| Agent access | Named JSON-compatible tools |

The Hindsight layers become ownership tiers below one root. So a delete of a
bank deletes everything in it, and a delete of a belief deletes its history:

```text
hindsight.bank                    one isolated memory bank
  documents                       retained source payloads
  memories                        extracted facts, retention log, both time axes
  entities                        canonical entities and every alias bound to them
  observations                    one identity per belief, one revision per change
  mental_models                   one identity per standing question, one per rebuild
```

## Two time axes in one entry

Hindsight records when something happened separately from when the bank learned
it. An ongoing fact, such as a preference, has no "happened" time. A
`TemporalEntry` already holds both axes, so each fact joins the timeline one time:

| Axis | Field | Question it answers |
| --- | --- | --- |
| Learned | `occurred_at` | When did the bank learn this fact? |
| Described | `validity` | What period is this fact about? |

- **So the temporal retriever reads the collection in 2 ways.** In the demo, a call in June 2025 reports that billing started to use Postgres in 2024. A 2024 window finds that fact through the described axis. Only a June 2025 window finds it through the learned axis.

## Consolidation without a model

- **Upstream, a language model decides** which facts belong to the same belief, and how the wording changes when new evidence arrives.
- **A deterministic example cannot do that.** So each extracted fact names its belief, and says if it supports or contradicts that belief. The Graphiti lamp makes the same kind of substitution: it accepts structured triplets, not extraction from natural language.

The 3 outcomes that Hindsight describes then follow directly:

- **Strengthen.** A fact that repeats a belief already on file only adds a lineage edge. The proof count goes up, and the lamp writes no revision. Exception: if the fact adds a new tag, the lamp writes a revision with the same wording and the merged tags.
- **Extend.** If a fact supports the belief and has new content, the lamp adds that content to the wording and writes a revision.
- **Reconcile.** A fact that contradicts the belief writes a revision that starts with the new belief and keeps the old belief visible. That is the Hindsight "full journey", not an overwrite.

What Genie supplies:

- **Evidence is a `LineageDAG` edge, not a stored list.** So the lamp gets the proof count from the graph, and the count always agrees with the facts.
- **A belief is a `VersionedRecords` identity.** So its earlier wording is a read, not an audit table that the lamp maintains.
- **Consolidation is a batch over the retention log,** from a stored cursor. The cursor also makes staleness answerable: an observation is stale when valid facts that name its claim are at or after the cursor.
- **Upstream runs this work in the background after every `retain`.** Here it is an explicit call, so the example can show when it happened.

## Recall

Hindsight runs 4 retrievers for each query and combines their results. It does
not use only one index. Recall ranks observations together with raw facts, so a
consolidated belief can rank above the sentences that it was built from.

| Retriever | How it runs |
| --- | --- |
| `similarity` | Token-set cosine overlap, a visible replacement for embeddings |
| `keyword` | One `MemoryHandle.search` for each query term |
| `graph` | `PropertyGraph.walk` from the `entity` argument, or from an entity that the query names |
| `temporal` | `TemporalCollection` reads on both axes inside a window |

- **Genie runs 3 of the 4 retrievers, and that is the point.** Genie leaves ranking to applications. So the retrievers are where the library does the work. The lamp only sorts those Genie results by match count, hop count, or time distance. The lamp computes its own scores only in the `similarity` replacement and in reciprocal rank fusion.
- **The graph walk is the most interesting retriever.** It has no relation filter, so it crosses 3 relations: from an entity to the facts that mention it, along the associative edges between those facts, and out to the observations derived from them.
- **Results fill a token budget, not a row count.** So a query reports what the budget left out.
- **Ties break on a stable key from the candidate content, never on `MemoryBlock.id`.** The key is the fact text for a fact, and the claim key for an observation. Block ids are new UUIDs on every run. A tie broken by id would change the rank of a candidate inside a retriever, change its part of the fused rank, and make the scores different between runs.

## Mental models and reflection

- **A mental model is a question that a caller asks one time, and the bank keeps its answer current.** So a read of the model is a lookup, not a retrieval followed by a synthesis.
- **That is almost exactly `VersionedRecords`:** the question is the identity, each rebuild is a revision, and the previous wording stays readable.
- **A rebuild happens only when the answer changes.** The lamp builds an answer from the beliefs in the tag scope of the model, and deliberately leaves proof counts out of it. So more evidence for a known belief changes the count but writes no version. In the demo, the third document does exactly that.
- **`reflect` keeps the Hindsight order of preference:** mental model, then observations, then raw facts. It reports every layer that it consulted as evidence.
- **Each item names its sources** through the lineage relation. A fact refers back to its document, a belief to the facts that support it, and a model version to the beliefs that it summarized.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `register_entity` | Declare a canonical entity and its surface forms (other written names) |
| `retain` | Store a payload and the facts extracted from it |
| `link_memories` | Add a temporal, semantic, or causal connection |
| `invalidate_memory` | Retire a fact, but do not delete it |
| `consolidate` | Merge new facts into versioned beliefs with evidence |
| `create_mental_model` | Register a standing question |
| `refresh_mental_model` | Compose its answer again, and write a version only if the answer changed |
| `get_mental_model` | Read the current answer and the earlier versions |
| `recall` | Rank facts and beliefs with 4 retrievers inside a budget |
| `reflect` | Answer from the highest layer that already knows |

The agent learns a Northwind platform bank from 4 payloads: a January onboarding
call, 2 agent runs on the billing service, and a June planning call.

- **Entity resolution:** `Alice`, `Alice Chen`, and `Alice C.` all resolve to one entity.
- **Invalidation:** the June correction retires the MySQL fact, but does not delete it.
- **Strengthen:** the February timeout and the March retry support one belief.
- **Reconcile:** the June move from React to Vue reconciles another belief.

The agent refreshes the standing question `northwind-change-brief` after each
payload. The store rebuilds the answer 3 times out of 4. The March retry raises
a proof count but does not change the answer, so the store writes no version:

- **r1:** Northwind frontends are built in React.
- **r2:** adds that Northwind billing migrations finish only in small batches.
- **r3:** Northwind frontends are built in Vue, previously React.

| Tool | Calls |
| --- | ---: |
| `register_entity` | 2 |
| `create_mental_model` | 1 |
| `retain` | 4 |
| `consolidate` | 4 |
| `refresh_mental_model` | 4 |
| `link_memories` | 3 |
| `invalidate_memory` | 1 |
| `get_mental_model` | 1 |
| `recall` | 1 |
| `reflect` | 1 |

## Limits

**Left out:**

- **Language-model work:** the Hindsight fact extraction and observation synthesis.
- **Search backends:** its embeddings and BM25 backends.
- **Ranking:** cross-encoder reranking, and the recency, temporal-proximity, and proof-count boosts.
- **Time and entities:** parsing of temporal language, and fuzzy entity resolution by co-occurrence.
- **Background work:** background consolidation and scheduled model refresh.
- **Observations:** reconciliation of near-duplicate observations, and observation scopes.
- **Other features:** knowledge pages, the disposition and directives of the reflect agent, Memory Defense, multilingual support, and webhooks.
- **Storage:** its PostgreSQL and Oracle storage.

**Replaced by:**

| Upstream | Here |
| --- | --- |
| Consolidation that a model judges | Declared claims |
| Embeddings | Token overlap |
| BM25 | Term matching |
| Fuzzy resolution | Name subsumption over a keyed alias table |
| Background work | Explicit calls |

- **These omissions are deliberate, not unfinished work.** A lamp shows that the Genie blocks, links, and structure facades can hold the memory model of another system. Upstream features that are only ranking, clustering, or scheduling are what Genie leaves to applications by design. So a copy of them here adds a hand-written algorithm and demonstrates nothing. The lamp keeps the features that map onto a facade.

**Read the results with these 2 notes about the model:**

- **Each observation revision is a separate block, and the belief that it revises owns it.** So the history survives a new wording, but one subtree delete removes a belief together with its full history. Upstream instead rewrites an observation in place, and has a `clear_observations` endpoint.
- **A mental model here uses only the beliefs in its scope.** This makes the rule "rebuild only when the answer changed" observable. Upstream also reads raw facts, and edits the previous version step by step.

References: [Hindsight repository](https://github.com/vectorize-io/hindsight),
[retain](https://hindsight.vectorize.io/developer/retain),
[recall](https://hindsight.vectorize.io/developer/retrieval),
[observations](https://hindsight.vectorize.io/developer/observations),
[mental models](https://hindsight.vectorize.io/developer/mental-models), and
[reflect](https://hindsight.vectorize.io/developer/reflect).

The [lamp guide](../README.md) describes the shared output format of the agent
demos.
