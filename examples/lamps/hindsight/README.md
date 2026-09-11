# Hindsight-inspired agent memory

Hindsight is a memory service for agents that are meant to get better over time.

- **Keeps the source payloads** and extracts facts from them.
- **Separates what the agent did from what the agent knows.**
- **Folds related facts into observations** backed by evidence, and refines them instead of overwriting them.
- **Keeps standing answers up to date** in the background.
- **Answers a query with four retrievers at once,** combined into one result set that fits a budget.

This lamp rebuilds that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
python -m examples.lamps.hindsight.demo
python -m pytest examples/lamps/hindsight/tests -q
```

- **`demo.py` is the entry point.** Run the module path from the repository root so that both `examples` and `genie` can be found.
- **What it does:** declares two entities and one standing question, retains four documents, and prints the answer.
- **Deterministic.** Every timestamp comes from a document and not from a clock, so each run prints the same output.

## Modules

The lamp is split by topic instead of kept in one file. Each module depends on
the ones above it:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, relations, taxonomy, records, and time helpers |
| `structures.py` | Genie facades wired to the bank's five ownership tiers |
| `bank.py` | Retain, entity resolution, links, invalidation, both time axes |
| `observations.py` | Folding facts into versioned beliefs backed by evidence |
| `mental_models.py` | Standing questions and the versions of their answers |
| `recall.py` | Four retrievers, reciprocal rank fusion, and the token budget |
| `reflect.py` | Answering from the highest layer that already knows |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | Two entities, one standing question, four documents, one answer |

## Mapping

| Hindsight concept | Genie representation |
| --- | --- |
| Memory bank | Root block and bounded handle |
| Bank isolation | Handle boundary; an id outside the root is refused |
| Retained payload | `hindsight.document` block owned by the documents tier |
| Extracted fact | `hindsight.memory` block owned by the memories tier |
| `world` and `experience` perspective | `fact_type` field on that block |
| `valid` and `invalidated` state | `state` field, updated in place |
| Retention order | `EventLog` anchored on the memories tier |
| Consolidation progress | `consolidated_through` on the observations anchor |
| When the bank learned a fact | `TemporalCollection` event time |
| `occurred_start` / `occurred_end` | `ValidityInterval` on the same entry |
| Entity | `hindsight.entity` block owned by the entities tier |
| Entity resolution and aliases | Many `KeyedCollection` addresses, one block |
| Entity connections | `PropertyGraph` `hindsight.mentions` edges |
| Temporal, semantic, causal connections | `PropertyGraph` edges of those relations |
| Provenance and `based_on` | `LineageDAG` on `hindsight.derived_from` |
| Observation | `VersionedRecords` identity keyed by claim |
| Observation refinement and history | Revisions of that record |
| Proof count | Lineage predecessors of the current revision |
| Stale observation | Retention cursor against the claims still pending |
| Mental model | `VersionedRecords` identity keyed by name |
| Mental-model versions | Revisions plus lineage edges from observations |
| Deduplication on retain | `MemoryHandle.search` with a metadata filter |
| TEMPR keyword retriever | One `MemoryHandle.search` per query term |
| TEMPR graph retriever | `PropertyGraph.walk` from an entity |
| TEMPR temporal retriever | `TemporalCollection.events` and `.overlapping` |
| Deleting a bank or a belief | `OwnershipTree.delete_subtree` |
| Agent access | Named JSON-compatible tools |

Hindsight's layers become ownership tiers below one root. So deleting a bank
deletes everything in it, and deleting a belief deletes its history:

```text
hindsight.bank                    one isolated memory bank
  documents                       retained source payloads
  memories                        extracted facts, retention log, both time axes
  entities                        canonical entities and every alias bound to them
  observations                    one identity per belief, one revision per change
  mental_models                   one identity per standing question, one per rebuild
```

## Two time axes in one entry

Hindsight tracks when something happened separately from when the bank found
out. For an ongoing fact such as a preference, the first one is simply absent. A
`TemporalEntry` already carries both, so each fact joins the timeline once:

| Axis | Field | Question it answers |
| --- | --- | --- |
| Learned | `occurred_at` | When did the bank find this out? |
| Described | `validity` | What period is this fact about? |

- **So the temporal retriever reads the collection in two ways.** In the demo a call in June 2025 reports that billing has been on Postgres since 2024. A 2024 window reaches that fact through the described axis, and only a June 2025 window reaches it through the learned axis.

## Consolidation without a model

- **Upstream, a language model decides** which facts belong to the same belief, and how the wording should change when new evidence arrives.
- **A deterministic example cannot do that.** So each extracted fact names the belief it speaks to, and says whether it supports or contradicts it. The Graphiti lamp makes the same kind of substitution when it accepts structured triplets instead of extraction from natural language.

The three outcomes that Hindsight describes then follow directly:

- **Strengthen.** A fact that repeats a belief already on file adds a lineage edge and nothing else. The proof count rises, and no revision is written.
- **Extend.** A supporting fact that has something new to say is appended to the wording, and a revision is written.
- **Reconcile.** A contradicting fact writes a revision that starts with the new belief and keeps the old one visible. That is Hindsight's "full journey" rather than an overwrite.

What Genie carries:

- **Evidence is a `LineageDAG` edge, not a stored list,** so the proof count is counted off the graph and cannot drift away from the facts.
- **A belief is a `VersionedRecords` identity,** so its earlier wording is a read rather than an audit table that the lamp has to maintain.
- **Consolidation is a batch over the retention log,** starting after a stored cursor. That is also what makes staleness answerable: an observation is stale when facts naming its claim sit past the cursor.
- **Upstream runs this in the background after every `retain`.** Here it is an explicit call, so the example can show when it happened.

## Recall

Hindsight runs four retrievers per query and combines them, instead of hitting
one index. Observations are ranked together with raw facts, so a consolidated
belief can rank above the sentences it was built from.

| Retriever | How it runs |
| --- | --- |
| `similarity` | Token-set cosine overlap, a visible stand-in for embeddings |
| `keyword` | One `MemoryHandle.search` per query term |
| `graph` | `PropertyGraph.walk` outward from an entity named in the query |
| `temporal` | `TemporalCollection` reads on both axes inside a window |

- **Three of the four run through Genie, and that is the point.** Genie leaves ranking to applications, so the retrievers are where the library does the work, and reciprocal rank fusion is the only scoring the lamp writes itself.
- **The graph walk is the interesting one.** It crosses three relations without being told to: from an entity to the facts that mention it, along the associative edges between those facts, and out to the observations derived from them.
- **Results fill a token budget rather than a row count,** so a query reports what the budget left out.
- **Ties break on the candidate's own text, never on `MemoryBlock.id`.** Block ids are new UUIDs on every run, and a tie broken by id would change a candidate's rank inside a retriever, change how much it contributes to the fused rank, and make the scores differ between runs.

## Mental models and reflection

- **A mental model is a question asked once, whose answer the bank keeps up to date.** So reading it is a lookup, not a retrieval followed by a synthesis.
- **That is almost exactly `VersionedRecords`:** the question is the identity, each rebuild is a revision, and the previous wording stays readable.
- **A rebuild happens only when the answer changes.** The lamp builds an answer from the beliefs in the model's tag scope, and deliberately leaves proof counts out of it. So more evidence for a belief already on file moves the count without writing a version. In the demo the third document does exactly that.
- **`reflect` keeps Hindsight's order of preference** — mental model, observations, raw facts — and reports every layer it consulted as evidence.
- **Each item names what it was derived from** by walking the lineage relation: a fact points back at its document, a belief at its supporting facts, and a model version at the beliefs it summarized.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `register_entity` | Declare a canonical entity and the forms it answers to |
| `retain` | Store a payload and the facts extracted from it |
| `link_memories` | Add a temporal, semantic, or causal connection |
| `invalidate_memory` | Retire a fact without deleting it |
| `consolidate` | Fold new facts into versioned beliefs backed by evidence |
| `create_mental_model` | Register a standing question |
| `refresh_mental_model` | Rebuild its answer if the scope changed |
| `get_mental_model` | Read the current answer and the versions before it |
| `recall` | Rank facts and beliefs with four retrievers under a budget |
| `reflect` | Answer from the highest layer that already knows |

The agent learns a Northwind platform bank from four payloads: a January
onboarding call, two agent runs against the billing service, and a June planning
call.

- **Entity resolution:** `Alice`, `Alice Chen`, and `Alice C.` all resolve to one entity.
- **Invalidation:** the June correction retires the MySQL fact without deleting it.
- **Strengthening:** the February timeout and the March retry back one belief.
- **Reconciliation:** the June move from React to Vue reconciles another.

The standing question `northwind-change-brief` is refreshed after each payload
and rebuilt three times out of four. The March retry raises a proof count
without changing the answer, so no version is written:

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

**Left out:** Hindsight's language-model fact extraction and observation
synthesis, its embeddings and BM25 backends, cross-encoder reranking and the
recency, temporal-proximity, and proof-count boosts, parsing of temporal
language, fuzzy entity resolution by co-occurrence, background consolidation and
scheduled model refresh, reconciling near-duplicate observations, observation
scopes, knowledge pages, the reflect agent's disposition and directives, Memory
Defense, multilingual handling, webhooks, and its PostgreSQL and Oracle storage.

**Replaced by:**

| Upstream | Here |
| --- | --- |
| Consolidation judged by a model | Declared claims |
| Embeddings | Token overlap |
| BM25 | Term matching |
| Fuzzy resolution | Name subsumption over a keyed alias table |
| Background work | Explicit calls |

- **These omissions are on purpose, not unfinished work.** A lamp exists to show that Genie's blocks, links, and structure facades can carry another system's memory model. Upstream features that are pure ranking, clustering, or scheduling are exactly what Genie leaves to applications by design, so writing them here would add a hand-written algorithm and demonstrate nothing. The features that were kept are the ones that land on a facade.

**Two modelling notes matter when you read the results:**

- **An observation revision is a separate block owned by the belief it revises.** So history survives a rewording, but a belief and its whole history go together in one subtree delete. Upstream instead rewrites an observation in place and offers a `clear_observations` endpoint.
- **A mental model here is built only from the beliefs in its scope,** which is what makes "rebuild only when the answer changed" something you can observe. Upstream also reads raw facts and edits the previous version step by step.

References: [Hindsight repository](https://github.com/vectorize-io/hindsight),
[retain](https://hindsight.vectorize.io/developer/retain),
[recall](https://hindsight.vectorize.io/developer/retrieval),
[observations](https://hindsight.vectorize.io/developer/observations),
[mental models](https://hindsight.vectorize.io/developer/mental-models), and
[reflect](https://hindsight.vectorize.io/developer/reflect).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
