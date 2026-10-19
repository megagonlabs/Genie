# Mem0-inspired agent memory

Mem0 is a memory layer for agents. Its main idea is that the storage of the
agent input is the easy half of the work.

- **2 model passes run before Mem0 writes anything.** A call to `add` gives Mem0 messages. The first pass reads the exchange and makes candidate facts.
- **The second pass decides.** It takes each candidate and gets the stored memories that are most similar to it. Then it returns one of 4 operations: `ADD` the candidate beside them, `UPDATE` one of them in place, `DELETE` one that the candidate contradicts, or `NONE`. The caller does not select the operation.
- **Mem0 appends each write to a change history.** Thus the question "why does it think this?" has an answer that a caller can read. The caller does not guess.
- **The scope of a memory** comes from `user_id`, `agent_id`, and `run_id`.
- **If the application configures a graph store,** the same messages also give entities and the relationships between them.

This lamp builds the same architecture again from Genie blocks, links, and
structure facades. It is an example of the architecture. It is not a
replacement for Mem0.

## Run

From the repository root, with no API key and no external service:

```bash
python -m examples.lamps.mem0.demo
python -m pytest examples/lamps/mem0/tests -q
```

- **`demo.py` is the entry point.** Run the module path from the repository root. Then Python can find both `examples` and `genie`.
- **What it does:** it runs a travel assistant through each write path of Mem0, and prints the answer.
- **Deterministic.** Each timestamp comes from the scenario, not from a clock. Thus each run prints the same output.

## Modules

The lamp has one module for each topic, not one large file. Each module imports
only modules above it in the table:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, the session, records, and the text helpers for the scores |
| `structures.py` | Genie facades for the 3 ownership tiers of the store |
| `store.py` | Scoped records: place, address, list, search, neighbors, retire |
| `history.py` | The change history: the events of one memory, and the full journal |
| `graph.py` | Entities and typed relationships, and the retraction of a relationship |
| `pipeline.py` | The 2 phases: extraction, then the decision between 4 events |
| `client.py` | The public surface of Mem0 over the store, the history, the graph, and the pipeline |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | One user, 2 runs, each write path, one answer |

## Mapping

| Mem0 concept | Genie representation |
| --- | --- |
| A stored memory | One `mem0.memory` block that holds the payload that Mem0 returns |
| Memory id | `KeyedCollection` address in one flat namespace |
| `user_id` / `agent_id` / `run_id` | `OwnershipTree` path, one block for each identifier |
| A filter on the ids that you supplied | `Scope.SUBTREE` with its root at that path block |
| `actor_id` | Block metadata. Thus it makes a search narrower and does not set the place of a memory |
| Vector-payload fields | Block metadata, copied for portable filters |
| `search(filters=...)` | `MemoryHandle.search` metadata filters |
| Top-k similar memories before the decision | `MemoryStore.neighbors` over the session scope |
| Fact extraction | Declared candidates, and for each candidate the turn that gives its speaker |
| The `ADD`/`UPDATE`/`DELETE`/`NONE` decision | `MemoryUpdater.decide`, which returns the decision and does not apply it |
| `ADD` | New block under the path block, and a keyed binding |
| `UPDATE` | `MemoryHandle.update`. The id stays the same, and `version` increases |
| `DELETE` | `MemoryHandle.delete`. The binding goes away with the block |
| `NONE` | No write and no history row |
| MD5 hash of the memory | `content_hash`, indexed in metadata to find repeats |
| Change-history row | One `mem0.history_event` block that the history tier owns |
| `history(memory_id)` | `VersionedRecords` revisions of the identity of that memory |
| The full history table | `EventLog` on the history tier |
| `is_deleted` on a history row | The `DELETE` event, which removed the block of the memory |
| `infer=False` | The turns, stored word for word, one memory for each turn |
| `memory_type="procedural_memory"` | One summary, stored without the update phase |
| `update(memory_id, data)` | The same write, with the caller as the reason |
| `delete_all(user_id=...)` | `OwnershipTree.delete_subtree` on that path block |
| Graph entity | `mem0.entity` block, with its case-folded name as its address |
| Entity resolution | `KeyedCollection` lookup in the entity namespace |
| Graph relationship | `PropertyGraph` edge with a typed relation |
| A contradicted relationship | The edge, marked as retracted, because Genie has no link delete |
| `search` that returns `relations` | The graph read that runs together with the memory search |
| Graph memory cleared with the scope | `OwnershipTree.delete_subtree` on that namespace block |
| Agent access | Named JSON-compatible tools |

3 ownership tiers are below one store root. This copies the upstream design,
where these are 3 independent stores:

```text
mem0.store                        one memory layer
  memories                        the session tree, and the memories in it
  history                         one identity per memory, one event per change
  entities                        one namespace per user, its nodes, and their edges
```

## A filter and a subtree select the same memories

- **The scope of Mem0 is flat.** The identifiers are payload fields, and a call filters on the identifiers that you supplied. Thus `search(user_id="alice")` matches the memories of Alice from all agents and all runs, because `agent_id` and `run_id` are not in the filter.
- **An ownership path of the supplied identifiers gives the same behavior,** because a subtree contains everything below it. It also adds the one operation that a payload filter cannot give.

| Operation | Without ownership | Here |
| --- | --- | --- |
| Read all memories of `alice` | Filter candidates on `user_id` | `Scope.SUBTREE` at that block |
| Read one run | Add `run_id` to the filter | Put the root of the same scope deeper |
| Retire one run | List and delete each record | One `delete_subtree` |

- **The last row is the important one.** The lamp does not delete the keyed bindings. They go away when their blocks go away, because a binding refers to a block and does not copy it. Thus a retire of a run cannot stop half-way, and a free memory id can get a new binding.
- **The id space stays flat.** This is the other half of the split. Upstream, `get`, `update`, and `delete` take a memory id and no scope. Thus the id is a `KeyedCollection` address in one namespace, and the session is a path in the tree. One lookup is by identity, and one is by scope. Each lookup uses the structure that suits it. A flat id space trusts its caller. For this reason, the tool layer above it checks the scope, not the client.
- **Search follows the split that Genie uses.** The backend selects the candidates: the session becomes a scope, the block type and any metadata filter go into the query, and one search runs for each query term. The lamp calculates the scores, because Genie has no ranking.
- **Score detail:** a candidate counts a term only if the term occurs in the text of the memory. The score is the fraction of the terms that occur. For equal scores, the order uses creation time and then text, not `MemoryBlock.id`. Block ids are new UUIDs in each run, so an order by id would give different results in different runs.

## The decision is the memory system

Reconciliation separates Mem0 from a store that you write to. Thus it is
important to know where it occurs here.

- **`MemoryUpdater.decide`** receives the candidate and the stored memories that are nearest to it. It returns one of the 4 events and does not change Genie.
- **`client.py`** then applies the event.
- **Why the split:** because of this split, a caller can print a decision, make assertions about it, and disagree with it.

| Event | Condition | Effect |
| --- | --- | --- |
| `NONE` | One of the nearest stored memories has the same content hash | No write, and no history row |
| `UPDATE` | The similarity of the nearest memory is at or above the floor | An in-place write to the block |
| `DELETE` | The same, and the candidate contradicts the memory | A delete of the block |
| `NONE` | The candidate contradicts a memory, but no memory is at or above the floor | No write, and no history row |
| `ADD` | No memory is at or above the floor | A new block and a binding |

**The lamp replaces 2 model judgments:**

- **Similarity is token overlap,** not embedding distance.
- **The extractor declares a contradiction. The lamp does not infer it.** The extractor states it as `retracts`. This flag is the one signal that separates `DELETE` from `UPDATE`: it tells if a candidate contradicts a memory or only gives it new words. No deterministic comparison of 2 sentences can find this difference.

**One difference from upstream, on purpose:** the lamp does the neighbor lookup
again for each candidate in a call, not once for the full batch. Thus a
candidate sees the memories that earlier candidates of the same call wrote. 2
wordings of the same fact give an `ADD` and then an `UPDATE`, not 2 adds. The
extractor removes exact repeats in a call before this step.

## What the history outlives

- **The history is a second tier, not a field on the memory.** The history tier owns the event blocks. The memory that they describe does not own them. Thus a delete of a memory does not change its history. The delete is the last event that you can read about a memory. It does not stop the reads of that history.

| Facade | What it reads |
| --- | --- |
| `VersionedRecords` | The events of one memory, in order. This is `history(memory_id)` |
| `EventLog` | The events of all memories, in the order that the lamp applied them. This is the table itself |

- **A row holds** the old wording, the new wording, a flag that tells if the change was a delete, and the speaker of the turn. Upstream keeps the same fields in its history table.
- **A `NONE` writes no row.** Thus the history records changes, not attempts.
- **In the demo:** the lamp adds the seat preference, and then changes its wording 2 days later. The stored block is at version 2, and its history has 2 events. Thus a caller can read the current wording and the old wording. The lamp adds the layover memory, and then deletes it because of a fact that contradicts it. The memory does not exist now, but its 2 events are still there.

## Two stores, and what that costs

- **Upstream, the vector store and the graph store are independent.** `add` writes to both, but `update(memory_id, data)` writes only the memory.
- **The demo shows this clearly.** The direct path corrects the hotel memory from the Riverside Inn to the Cedar Lodge. The entity graph still says `Alice stays at Riverside Inn`, because no graph extraction ran. This looks like an inconsistency, because upstream it is one.
- **The address of an entity is its case-folded name in the outermost identifier of the session.** Upstream scopes graph nodes at this level. Thus 2 runs of the same user share one node for each entity. The resolution of an extracted name is a keyed lookup, not a scan.
- **The lamp marks a contradicted relationship as retracted and does not delete it,** because Genie has no operation that removes a single link. Reads skip the retracted relationship. This agrees with the rest of the lamp, where a change stays readable. It also means that a retracted edge and its replacement are both in the graph.
- **A retire of a full entity namespace clears the graph,** as upstream clears the graph memory together with the vector store. The namespace owns its nodes, so this is one `delete_subtree`. The reason is the same as for the retire of a run. A delete of the nodes is sufficient: Genie removes the links and the keyed bindings of a deleted block. Thus the relationships go away with their entities, and the lamp does not remove the links one at a time.
- **This is not only a clean-up detail.** A walk that deletes each node that it read can retire only as many nodes as one read page returns. Thus the lamp would clear only a part of a namespace larger than `READ_LIMIT`. A subtree is always the full namespace.
- **A retire of a single run does not change the entities,** because the entities are shared above that level.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_memories` | Send an exchange through the 2 phases and apply their decisions |
| `search_memories` | Search the scope, and return the relationships of the entities that the query names |
| `list_memories` | Read all memories in the scope, or in one run in it |
| `update_memory` | Rewrite one memory by id, and record the old wording |
| `delete_memory` | Delete one memory by id. Its history stays readable |
| `memory_history` | Read all changes that the history recorded for one memory |
| `forget_scope` | Retire the bound scope, or one run in it |

- **The tool set gets one user and one agent at construction,** so no tool argument can name the memories of a different user. The lamp tests check this. They make 2 tool sets over one store. Then they call `search_memories`, `list_memories`, and the 3 tools that take an id from the wrong tool set.
- **The 3 tools that take an id check the binding themselves.** `Mem0Memory` keeps one flat id space, as upstream does. Thus `update_memory`, `delete_memory`, and `memory_history` check that the id is in the bound scope before they act. The check is in `Mem0Tools`, not in the client. Application code calls the client, and that code knows already which user it serves. But a model selects the tool arguments.
- **The tools refuse an id from a different scope in the same way as an unknown id.** Thus a caller cannot use a tool set to find which ids exist in other scopes. `Mem0Memory.owner` gets the answer for the check from the history tier, not from the store. Thus a deleted id still resolves to the scope where the client wrote it.
- **A run *is* a tool argument,** because upstream a run is one conversation and the caller gives it for each call. But a run can only make the bound scope narrower.

The agent learns the travel preferences of Alice in 2 runs:

- **First exchange:** no stored memories exist for a comparison. Thus the lamp adds all 5 candidates and 3 relationships.
- **Second exchange:** the update phase has work to do. The new seat preference replaces the old one in place. The repeated diet fact writes nothing. The layover retraction removes the memory that it contradicts. The same turn also retracts one relationship and adds a different one.
- **Then the paths that skip the pipeline:** a procedural summary of the task steps, and a later conversation in its own run and in its original words. Then the caller corrects one memory and deletes one memory. Last, one call retires that full run.

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

**Not in the lamp:**

- **Model prompts:** the Mem0 fact extraction and update prompts for the language model, and the custom-prompt hooks for both.
- **Vector search:** embeddings and vector search.
- **Providers:** all concrete vector and graph providers.
- **API parts:** `reset`, the async client, and multimodal inputs.
- **Retrieval:** criteria-based retrieval and reranking.
- **Memory features:** expiration dates, categories, and rules that include and exclude selected memories.
- **Hosted products:** the hosted platform API, and OpenMemory and its MCP server.

**Replacements:**

| Upstream | Here |
| --- | --- |
| Extraction that a model does | Declared candidates |
| Embedding distance | Token overlap |
| The judgment of the update prompt | A similarity floor and a `retracts` flag |
| A clock | Supplied timestamps |

- **These omissions are on purpose.** A lamp shows that the blocks, links, and structure facades of Genie can hold the memory model of a different system. Some upstream features are only ranking, model inference, or provider integration. By design, Genie leaves exactly these features to applications. A copy of them here would add a hand-written algorithm and show nothing.

**Read the results with these 4 notes about the model:**

- **The session path puts the identifiers in the order `user`, `agent`, `run`.** Thus a session that names only a run gets its own top-level path. Upstream, the 3 identifiers are unordered payload fields, and any subset is permitted.
- **`TemporalCollection` is not used, on purpose.** The lamp records creation and update times on each memory. But open-source Mem0 has no time-range read for them. Thus an index of these times would be a lamp feature, not an upstream feature.
- **The lamp makes one choice explicit: it scopes entities at the outermost identifier.** For this reason, a user-scoped read shows a relationship that the lamp wrote in one run.
- **The lamp marks a retracted relationship and does not remove it,** because Genie has no single-link delete.

References: [open-source repository](https://github.com/mem0ai/mem0),
[open-source implementation](https://github.com/mem0ai/mem0/blob/main/mem0/memory/main.py),
[memory operations](https://docs.mem0.ai/open-source/python-quickstart), and
[graph memory](https://docs.mem0.ai/platform/features/graph-memory).

The [lamp guide](../README.md) describes the shared output format of the agent
demos.
