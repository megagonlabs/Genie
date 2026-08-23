# Hindsight-inspired agent memory

Hindsight is a memory service for agents that are meant to improve. It retains
source payloads and extracts facts from them, keeps what the agent did apart
from what the agent knows, consolidates related facts into evidence-backed
observations that are refined rather than overwritten, keeps standing answers
current in the background, and answers a query by running four retrievers at
once and fusing them into one budgeted result set.

This lamp reproduces that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.hindsight.demo
conda run -n "genie" python -m pytest lamps/hindsight/tests -q
```

`demo.py` is the entry point; the module path must be run from the repository
root so both `lamps` and `genie` resolve. It declares two entities and one
standing question, retains four documents, and prints the answer. Every
timestamp comes from a document rather than a clock, so each run prints the
same output.

## Modules

The lamp is split by topic rather than kept in one file, and the dependency
order runs downward:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, relations, taxonomy, records, and time helpers |
| `structures.py` | Genie facades wired to the bank's five ownership tiers |
| `bank.py` | Retain, entity resolution, links, invalidation, both time axes |
| `observations.py` | Consolidating facts into versioned, evidence-backed beliefs |
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
| Deduplication on retain | Metadata-filtered `MemoryHandle.search` |
| TEMPR keyword retriever | One `MemoryHandle.search` per query term |
| TEMPR graph retriever | `PropertyGraph.walk` from an entity |
| TEMPR temporal retriever | `TemporalCollection.events` and `.overlapping` |
| Deleting a bank or a belief | `OwnershipTree.delete_subtree` |
| Agent access | Named JSON-compatible tools |

Hindsight's layers become ownership tiers below one root, so deleting a bank
deletes everything in it and deleting a belief deletes its history:

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
out, and leaves the first absent for an ongoing fact such as a preference. A
`TemporalEntry` already carries both, so each fact joins the timeline once:

| Axis | Field | Question it answers |
| --- | --- | --- |
| Learned | `occurred_at` | When did the bank find this out? |
| Described | `validity` | What period is this fact about? |

That is why the temporal retriever reads the collection two ways. In the demo a
June 2025 call reports that billing has been on Postgres since 2024: a 2024
window reaches that fact through the described axis, and only a June 2025 window
reaches it through the learned axis.

## Consolidation without a model

Upstream, a language model decides which facts belong to the same belief and
how the wording should change when new evidence lands. A deterministic example
cannot, so each extracted fact names the belief it speaks to and whether it
supports or contradicts it — the same substitution the Graphiti lamp makes when
it accepts structured triplets instead of natural-language extraction.

The three outcomes Hindsight describes then fall out of that:

- **Strengthen.** A fact repeating a belief already on file adds a lineage
  edge and nothing else. The proof count rises; no revision is written.
- **Extend.** A supporting fact with something new to say appends to the
  wording and writes a revision.
- **Reconcile.** A contradicting fact writes a revision that leads with the new
  belief and keeps the old one visible, which is Hindsight's "full journey"
  rather than an overwrite.

Because evidence is a `LineageDAG` edge rather than a stored list, the proof
count is counted off the graph and cannot drift from the facts. Because a
belief is a `VersionedRecords` identity, its earlier wording is a read rather
than an audit table the lamp maintains.

Consolidation is a batch over the retention log after a stored cursor, which is
also what makes staleness answerable: an observation is stale when facts naming
its claim sit past the cursor. Upstream this runs in the background after every
`retain`; here it is an explicit call so the example can show when it happened.

## Recall

Hindsight runs four retrievers per query and fuses them, rather than hitting one
index. Observations ride along with raw facts, so a consolidated belief can
outrank the sentences it was built from.

| Retriever | How it runs |
| --- | --- |
| `similarity` | Token-set cosine overlap, a visible stand-in for embeddings |
| `keyword` | One `MemoryHandle.search` per query term |
| `graph` | `PropertyGraph.walk` outward from an entity named in the query |
| `temporal` | `TemporalCollection` reads on both axes inside a window |

Three of the four go through Genie, which is the point: ranking is what Genie
leaves to applications, so the retrievers are where the library does the work
and reciprocal rank fusion is the only scoring the lamp writes. The graph walk
is worth reading twice — it crosses three relations without knowing it, from an
entity to the facts that mention it, along the associative edges between those
facts, and out to the observations derived from them.

Results then fill a token budget instead of a row count, so a query reports what
the budget left out. Every ranking step breaks ties on the candidate's own text,
never on `MemoryBlock.id`: block ids are fresh UUIDs on each run, and an
id-ordered tie would change a candidate's rank inside a retriever, change its
reciprocal-rank contribution, and make scores differ between runs.

## Mental models and reflection

A mental model is a question asked once whose answer the bank keeps current, so
reading it is a lookup rather than a retrieval and a synthesis. That is
`VersionedRecords` almost verbatim: the question is the identity, each rebuild
is a revision, and the previous wording stays readable.

A rebuild happens only when the answer changes. The lamp composes an answer from
the beliefs in the model's tag scope and deliberately leaves proof counts out of
it, so more evidence for a belief already on file moves the count without
writing a version. In the demo the third document does exactly that.

`reflect` then keeps Hindsight's preference order — mental model, observations,
raw facts — and reports every layer it consulted as evidence. Each item names
what it was derived from by walking the lineage relation: a fact points back at
its document, a belief at its supporting facts, a model version at the beliefs
it summarized.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `register_entity` | Declare a canonical entity and the forms it answers to |
| `retain` | Store a payload and the facts extracted from it |
| `link_memories` | Add a temporal, semantic, or causal connection |
| `invalidate_memory` | Retire a fact without deleting it |
| `consolidate` | Fold new facts into versioned, evidence-backed beliefs |
| `create_mental_model` | Register a standing question |
| `refresh_mental_model` | Rebuild its answer if the scope changed |
| `get_mental_model` | Read the current answer and the versions before it |
| `recall` | Rank facts and beliefs with four retrievers under a budget |
| `reflect` | Answer from the highest layer that already knows |

The agent learns a Northwind platform bank from four payloads: a January
onboarding call, two agent runs against the billing service, and a June planning
call. Along the way `Alice`, `Alice Chen`, and `Alice C.` resolve to one entity;
the June correction retires the MySQL fact without deleting it; the February
timeout and the March retry strengthen one belief; and the June move from React
to Vue reconciles another.

The standing question `northwind-change-brief` is refreshed after each payload
and rebuilt three times out of four — the March retry raises a proof count
without changing the answer, so no version is written:

- r1: Northwind frontends are built in React.
- r2: adds that Northwind billing migrations finish only in small batches.
- r3: Northwind frontends are built in Vue, previously React.

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

The lamp omits Hindsight's language-model fact extraction and observation
synthesis, its embeddings and BM25 backends, cross-encoder reranking and the
recency, temporal-proximity, and proof-count boosts, temporal-language parsing,
fuzzy entity resolution by co-occurrence, background consolidation and scheduled
model refresh, near-duplicate observation reconciliation, observation scopes,
knowledge pages, the reflect agent's disposition and directives, Memory Defense,
multilingual handling, webhooks, and its PostgreSQL and Oracle storage. Declared
claims replace model-judged consolidation, token overlap replaces embeddings,
term matching replaces BM25, name subsumption over a keyed alias table replaces
fuzzy resolution, and explicit calls replace background work.

Those omissions are deliberate rather than unfinished. A lamp exists to show
that Genie's blocks, links, and structure facades can carry another system's
memory model, so upstream features that are pure ranking, clustering, or
scheduling — which Genie leaves to applications by design — add hand-written
algorithm without adding any demonstration. The features kept are the ones that
land on a facade.

Two modelling notes matter when reading the results. An observation revision is
a separate block owned by the belief it revises, so history survives a rewording
but a belief and its whole history are one subtree delete; upstream instead
re-synthesizes an observation in place and exposes a `clear_observations`
endpoint. And a mental model here is composed only from the beliefs in its
scope, which is what makes "rebuild only when the answer changed" observable;
upstream also reads raw facts and edits the previous version incrementally.

References: [Hindsight repository](https://github.com/vectorize-io/hindsight),
[retain](https://hindsight.vectorize.io/developer/retain),
[recall](https://hindsight.vectorize.io/developer/retrieval),
[observations](https://hindsight.vectorize.io/developer/observations),
[mental models](https://hindsight.vectorize.io/developer/mental-models), and
[reflect](https://hindsight.vectorize.io/developer/reflect).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
