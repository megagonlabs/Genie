# Mem0-inspired agent memory

Mem0 is a memory layer for agents. Its starting idea is that storing what an
agent was told is the easy half. A call to `add` hands it messages, and two
model passes run before anything is written. The first reads the exchange and
produces candidate facts. The second takes each candidate, fetches the stored
memories most similar to it, and returns one of four operations: `ADD` it
beside them, `UPDATE` one of them in place, `DELETE` one it contradicts, or
`NONE`. The caller does not choose. Every write that does happen is appended to
a change history, so "why does it think this?" is a read rather than a guess. A
memory is scoped by `user_id`, `agent_id`, and `run_id`. And with a graph store
configured, the same messages also produce entities and the relationships
between them.

This lamp rebuilds that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.mem0.demo
conda run -n "genie" python -m pytest lamps/mem0/tests -q
```

`demo.py` is the entry point. Run the module path from the repository root so
that both `lamps` and `genie` can be found. It runs a travel assistant through
every write path that Mem0 offers, and prints the answer. Every timestamp comes
from the scenario and not from a clock, so each run prints the same output.

## Modules

The lamp is split by topic instead of kept in one file. Each module depends on
the ones above it:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, the session, records, and the text helpers scoring uses |
| `structures.py` | Genie facades wired to the store's three ownership tiers |
| `store.py` | Scoped records: place, address, list, search, neighbors, retire |
| `history.py` | The change history: one memory's events, and the whole journal |
| `graph.py` | Entities and typed relationships, and retracting one |
| `pipeline.py` | The two phases: extraction, then the four-way decision |
| `client.py` | Mem0's public surface over those four pieces |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | One user, two runs, every write path, one answer |

## Mapping

| Mem0 concept | Genie representation |
| --- | --- |
| A stored memory | One `mem0.memory` block holding the payload Mem0 returns |
| Memory id | `KeyedCollection` address in one flat namespace |
| `user_id` / `agent_id` / `run_id` | `OwnershipTree` path, one block per identifier |
| Filtering on the ids you supplied | `Scope.SUBTREE` rooted at that path block |
| `actor_id` | Block metadata, so it narrows a search rather than placing a memory |
| Vector-payload fields | Block metadata, duplicated for portable filtering |
| `search(filters=...)` | `MemoryHandle.search` metadata filters |
| Top-k similar memories before deciding | `MemoryStore.neighbors` over the session scope |
| Fact extraction | Declared candidates, plus the turn each one is credited to |
| The `ADD`/`UPDATE`/`DELETE`/`NONE` decision | `MemoryUpdater.decide`, returned rather than applied |
| `ADD` | New block under the path block, plus a keyed binding |
| `UPDATE` | `MemoryHandle.update`; the id stays, `version` rises |
| `DELETE` | `MemoryHandle.delete`; the binding goes with the block |
| `NONE` | No write and no history row |
| MD5 hash of the memory | `content_hash`, indexed in metadata to detect repeats |
| Change-history row | One `mem0.history_event` block owned by the history tier |
| `history(memory_id)` | `VersionedRecords` revisions of that memory's identity |
| The history table read whole | `EventLog` on the history tier |
| `is_deleted` on a history row | The event of a memory whose block is gone |
| `infer=False` | The turns stored word for word, one memory each |
| `memory_type="procedural_memory"` | One summary stored without the update phase |
| `update(memory_id, data)` | The same write, with the caller as the reason |
| `delete_all(user_id=...)` | `OwnershipTree.delete_subtree` on that path block |
| Graph entity | `mem0.entity` block, addressed by folded name |
| Entity resolution | `KeyedCollection` lookup in the entity namespace |
| Graph relationship | `PropertyGraph` edge with a typed relation |
| A contradicted relationship | The edge marked retracted, since Genie has no link delete |
| `search` returning `relations` | The graph read that runs beside the memory search |
| Graph memory cleared with the scope | The entity blocks deleted; Genie drops their links |
| Agent access | Named JSON-compatible tools |

Three ownership tiers sit below one store root. That mirrors the fact that
upstream these are three independent stores:

```text
mem0.store                        one memory layer
  memories                        the session tree, and the memories in it
  history                         one identity per memory, one event per change
  entities                        graph nodes, and the edges between them
```

## A filter and a subtree select the same memories

Mem0's scoping is flat. The identifiers are payload fields, and a call filters
on whichever ones you supplied. So `search(user_id="alice")` matches Alice's
memories from every agent and every run, because `agent_id` and `run_id` are
simply not in the filter. Mirroring the supplied identifiers as an ownership
path gives exactly that behavior back, since a subtree contains everything
below it. And it adds the one operation a payload filter has no answer for.

| Operation | Without ownership | Here |
| --- | --- | --- |
| Read everything for `alice` | Filter candidates on `user_id` | `Scope.SUBTREE` at that block |
| Read one run | Add `run_id` to the filter | Root the same scope deeper |
| Retire one run | List and delete each record | One `delete_subtree` |

The last row is the important one. The lamp does not delete the keyed bindings.
They go when the blocks they point at go, because a binding refers to a block
rather than copying it. So a run cannot be half-retired, and a freed memory id
can be bound again.

The id space stays flat, which is the other half of the split. Upstream, `get`,
`update`, and `delete` take a memory id and no scope. So the id is a
`KeyedCollection` address in one namespace, while the session is a path in the
tree. One lookup by identity, one by scope, each on the structure that fits it.

Search is split the same way Genie draws the line. Choosing candidates is the
backend's job: the session becomes a scope, the block type and any metadata
filter go into the query, and one search runs per query term. Scoring is the
lamp's job, because Genie has no ranking. A candidate counts a term only when
the term appears in the memory's own text, and the score is the share of terms
that do. Ties break on creation time and then on text, not on `MemoryBlock.id`,
because block ids are new UUIDs on every run and a tie broken by id would make
results differ between runs.

## The decision is the memory system

Reconciliation is what separates Mem0 from a store you write to, so it is worth
being exact about where it happens here. `MemoryUpdater.decide` receives the
candidate and the stored memories nearest to it, and returns one of the four
events without touching Genie. `client.py` then applies it. That split is why a
decision can be printed, asserted on, and disagreed with.

| Event | Condition | What is written |
| --- | --- | --- |
| `NONE` | A stored memory has the same content hash | Nothing, not even a history row |
| `UPDATE` | The nearest memory is above the similarity floor | The block, in place |
| `DELETE` | The same, and the candidate contradicts it | The block is removed |
| `ADD` | Nothing is close enough | A new block and binding |

Two model judgments are replaced. Similarity is token overlap rather than
embedding distance. And whether a candidate *contradicts* a memory or only
rewords it — the one signal that separates `DELETE` from `UPDATE` — is stated by
the extractor as `retracts`, because no deterministic reading of two sentences
can tell those apart.

The neighbor lookup runs again for every candidate in a call, rather than once
for the whole batch. That is a small, deliberate difference from upstream: two
candidates saying the same thing become an add and then a repeat, instead of two
adds.

## What the history outlives

The change history is a second store upstream, and keeping it a second tier here
gives you the behavior that makes it worth having. The event blocks are owned by
the history tier, not by the memory they describe. So deleting a memory leaves
its history intact, and the deletion is the last thing you can read about a
memory rather than the end of reading about it.

Two facades sit on the same event blocks:

| Facade | What it reads |
| --- | --- |
| `VersionedRecords` | One memory's events in order, which is `history(memory_id)` |
| `EventLog` | Every memory's events in the order they were applied, which is the table itself |

A row carries the wording it replaced, the wording that replaced it, whether it
was a deletion, and who the turn came from. Those are the fields upstream keeps
in its history table. A `NONE` writes no row at all, so the history records
changes rather than attempts.

In the demo the seat preference is added, then reworded four days later. The
stored block is at version 2 and its history has two events, so both the current
wording and the one it displaced are readable. The layover memory is added and
then deleted by a contradicting fact. The memory is gone, and its two events are
still there.

## Two stores, and what that costs

Upstream the vector store and the graph store are independent. `add` writes
both, but `update(memory_id, data)` writes only the memory. The demo shows that
honestly. The hotel memory is corrected from the Riverside Inn to the Cedar
Lodge through the direct path, and the entity graph still says `Alice stays at
Riverside Inn`, because no graph extraction ran. It reads like an inconsistency
because upstream it is one.

Entities are addressed by folded name inside the session's outermost identifier,
which is the level upstream scopes graph nodes at. So two runs of the same user
share one node per entity, and resolving an extracted name is a keyed lookup
rather than a scan.

Genie has no operation that removes a single link. So a contradicted
relationship is marked retracted and filtered out of reads instead of being
deleted. That fits the rest of the lamp, where a change stays readable, and it
means a retracted edge and its replacement sit side by side.

Retiring a whole entity namespace does clear the graph, the way upstream clears
graph memory alongside the vector store. It clears it by deleting the entity
blocks, because Genie removes the links of a deleted block. So the relationships
go with the entities that anchored them, rather than being unlinked one at a
time. Retiring a single run leaves them alone, since entities are shared above
that level.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_memories` | Run an exchange through both phases and apply what they decide |
| `search_memories` | Search the scope and return the relationships beside the hits |
| `list_memories` | Read every memory in the scope, or in one run inside it |
| `update_memory` | Rewrite one memory by id, recording the wording it replaced |
| `delete_memory` | Delete one memory by id; its history stays readable |
| `memory_history` | Read every change recorded for one memory |
| `forget_scope` | Retire the bound scope, or one run inside it |

The tool set is bound to one user and one agent at construction, so no tool
argument can name another user's memories. The lamp's tests check that by
building two tool sets over one store. A run *is* a tool argument, because
upstream a run is one conversation and is passed per call, but it can only
narrow the bound scope.

The agent learns Alice's travel preferences across two runs. The first exchange
has nothing to reconcile against, so all five candidates are added, along with
three relationships. The second exchange is where the update phase earns its
place: the new seat preference replaces the old one in place, the repeated diet
fact writes nothing, and the layover retraction removes the memory it
contradicts — while the same turn retracts one relationship and adds another.
Then come the paths that skip the pipeline: a procedural summary of how the task
was carried out, a later conversation stored word for word in its own run, one
memory corrected and one deleted by the caller, and finally that whole run
retired in a single call.

| Tool | Calls |
| --- | ---: |
| `add_memories` | 4 |
| `memory_history` | 2 |
| `update_memory` | 1 |
| `delete_memory` | 1 |
| `forget_scope` | 1 |
| `search_memories` | 1 |
| `list_memories` | 1 |

## Limits

The lamp leaves out Mem0's language-model fact extraction and update prompts and
the custom-prompt hooks for both, embeddings and vector search, every concrete
vector and graph provider, `reset`, the async client, multimodal inputs,
criteria-based retrieval and reranking, expiration dates, categories, rules for
including and excluding selected memories, the hosted platform API, and
OpenMemory and its MCP server.

These omissions are on purpose. A lamp exists to show that Genie's blocks,
links, and structure facades can carry another system's memory model. Upstream
features that are pure ranking, model inference, or provider integration are
exactly what Genie leaves to applications by design, so writing them here would
add a hand-written algorithm and demonstrate nothing. Declared candidates
replace extraction judged by a model. Token overlap replaces embedding
distance. A similarity floor and a `retracts` flag replace the judgment of the
update prompt. And supplied timestamps replace a clock.

Four modelling notes matter when you read the results. First, the session path
orders the identifiers `user`, `agent`, `run`, so a session that names only a
run gets its own top-level path; upstream the three are unordered payload
fields and any subset is allowed. Second, `TemporalCollection` is deliberately
unused. Creation and update times are recorded on every memory, but open-source
Mem0 offers no time-range read for them, so indexing them would be a lamp
feature rather than an upstream one. Third, scoping entities at the outermost
identifier is a choice this lamp makes explicit, and it is why a relationship
written in one run is visible to a user-scoped read. Fourth, a retracted
relationship is marked rather than removed, because Genie has no single-link
delete.

References: [open-source repository](https://github.com/mem0ai/mem0),
[open-source implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py),
[memory operations](https://docs.mem0.ai/open-source/python-quickstart), and
[graph memory](https://docs.mem0.ai/platform/features/graph-memory).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
