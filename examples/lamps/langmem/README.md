# LangMem-inspired agent memory

LangMem is the LangChain SDK for long-term agent memory.

- **An agent has 4 kinds of memory, not one:** facts that it learned, experiences that it can copy, a profile of the person that it works for, and its own instructions.
- **Each memory has an address:** a namespace and a key.
- **Memories form in one of 2 places:** on the hot path, that is, during the conversation, or in a debounced background pass. A debounced pass waits until no new turn arrives on the thread for a set time.
- **A *manager* decides** if new information goes into the store next to the stored memories, patches a stored memory, or deletes a stored memory. The application does not make this decision.
- **The fourth kind is unusual.** Procedural memory is the prompt of the agent. LangMem revises it from feedback on past runs. So an improvement to the agent is a memory write.

This lamp builds that architecture with Genie blocks, links, and structure
facades. It is an architectural example, not a replacement for LangMem.

## Run

Run these commands from the repository root. They need no API key and no external service:

```bash
python -m examples.lamps.langmem.demo
python -m pytest examples/lamps/langmem/tests -q
```

- **Entry point:** `demo.py`. Run the module path from the repository root, so that Python can find both `examples` and `genie`.
- **What it does:** it runs a code-review assistant through both write paths, revises its prompt from 3 pieces of feedback, and prints the answer.
- **Deterministic:** each timestamp comes from the scenario, not from a clock. So each run prints the same output.

## Modules

The lamp has one module for each topic. Each module imports only the modules
above it in this table:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, namespace addresses and templates, records, schemas |
| `structures.py` | Genie facades connected to the 5 ownership tiers of the store |
| `store.py` | Namespaced items: insert, patch, delete, and scoped and filtered search |
| `profiles.py` | The profile shape: one document for each namespace, revised in place |
| `manager.py` | Reconciles proposed changes and applies the permitted changes |
| `reflection.py` | The deferred queue, its due times, and the debounce for each thread |
| `prompts.py` | Procedural memory: prompts, feedback, and the versions between them |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | One user, 3 conversations, 2 write paths, one answer |

## Mapping

| LangMem concept | Genie representation |
| --- | --- |
| `BaseStore` item at a namespace and key | `KeyedCollection` binding on a `MemoryAddress` |
| Namespace template `("memories", "{user_id}")` | Resolved once, when the lamp binds a tool set |
| Namespace hierarchy | `OwnershipTree` path, one block for each segment |
| Namespace-prefix search | `Scope.SUBTREE` with that namespace block as the root |
| Search `filter` | `MemoryHandle.search` metadata filters |
| Semantic memory, collection shape | Many keyed `langmem.item` blocks |
| Semantic memory, profile shape | `VersionedRecords` identity, one revision for each update |
| Manager `default` for a profile | The first revision of the profile |
| Episodic memory | Items under an `episodes` namespace with the episode schema |
| Procedural memory (the prompt) | `VersionedRecords` identity, one revision for each optimization |
| Annotated trajectory | `langmem.trajectory` block that the trajectories tier owns |
| Which feedback produced a prompt version | `LineageDAG` edges into that revision |
| Which conversation wrote a memory | `LineageDAG` edges into that block |
| `schemas=[...]` Pydantic models | One `ValueSchema` for each namespace, named in block metadata |
| Patch that updates in place | `MemoryHandle.update`. The id stays, and `version` increases |
| `enable_inserts` / `enable_updates` / `enable_deletes` | Manager permissions, checked for each change |
| `actions_permitted` on the hot-path tool | Tool-level gate, separate from the manager gate |
| `ReflectionExecutor.submit` | `EventLog` append plus a `TemporalCollection` due time |
| `after_seconds` debounce window | Half-open time query against that due time |
| Cancel a pending reflection | Remove the temporal entry. The log entry stays |
| Retire a user | `OwnershipTree.delete_subtree` on the namespace of the user |
| Agent access | Named JSON-compatible tools |

One store root owns 5 tiers. So a delete of the store deletes everything in it.
A delete of a namespace deletes everything that belongs to that user:

```text
langmem.store                     one namespaced memory store
  namespaces                      the namespace tree: items, profiles, and queued work
  profiles                        one identity per profile, one revision per update
  prompts                         one identity per prompt, one revision per optimization
  trajectories                    annotated exchanges the feedback came from
  queue                           deferred submissions: arrival order and due time
```

- **A structure keeps its bindings at a tier anchor. The tier anchor does not own the blocks.** In Genie, ownership and structure membership are separate. So one tier can own a block, and a structure with its anchor on a different tier can register the same block.
- **The namespace block of a user owns the state of that user.** `memories/{user_id}` owns the items, the profile identity and its revisions, and each queued submission. These blocks are also bound into `KeyedCollection`, `VersionedRecords`, `EventLog`, and `TemporalCollection` on their own tiers.
- **Prompts and trajectories are the exception, on purpose.** They belong to the agent, not to one user. So their tiers own them, and they stay after the lamp deletes a namespace.

## A namespace with a lifecycle

- **Upstream, a namespace is only a name.** Nothing owns it, so it has no effect on other operations. A prefix search is a scan, and the store filters the results. For "this user is gone", the application finds and deletes each key, one at a time.
- **Here, each segment is a block that owns the blocks at the addresses below it.** This changes both operations.

| Operation | Without ownership | Here |
| --- | --- | --- |
| Search below `memories/dana` | Filter candidates by prefix | `Scope.SUBTREE` with that block as the root |
| Delete everything for `dana` | List and delete each key | One `delete_subtree` call |

- **The second row is the important one.** The lamp does not delete the keyed bindings. A binding refers to a block and does not copy it. So the binding goes away when its block goes away. The lamp cannot retire only part of a namespace, and a later insert can use a freed address again.
- **"Everything" means everything, not only the items.** The delete also removes the profile of Dana, its revision history, and each conversation that is still queued for her. So nothing stays that can write into the namespace after the lamp retires it. A per-key cleanup is most likely to miss this failure. If a queued conversation stays after the cleanup, its reflection becomes due later and creates the deleted namespace again.
- **`forget` returns only the items that it removed.** The profile and the queued submissions are not items. So the list does not include them, but the delete removed them too.
- **Search is the other half of the division of work in Genie.** The backend selects the candidates. The prefix becomes a scope. The item type and the metadata filter, if any, go into the query. One search runs for each query term. The lamp calculates the score, because Genie has no ranking.
- **Score detail:** a candidate counts a term only if the term is in the value of the candidate. The score is the share of terms that the value contains. The item address breaks ties, not `MemoryBlock.id`. Block ids are new UUIDs on each run, so a tie broken by id makes the result order different between runs.

## Two shapes for semantic memory

LangMem has 2 shapes for the same kind of memory. They need 2 different Genie
structures. That is why `store.py` and `profiles.py` are separate modules.

| Shape | Behavior | Structure |
| --- | --- | --- |
| Collection | Many documents accumulate. Retrieval selects the relevant ones | `KeyedCollection` |
| Profile | One document for each namespace. Each write replaces it | `VersionedRecords` |

- **A profile is almost exactly `VersionedRecords`.** The profile is the identity. So a reader always asks for "Dana's preferences", not for a specific document.
- **Each write is a revision.** So the replaced text is still readable. A single mutable document cannot give this.
- **Updates merge fields.** They do not replace the whole value. The store writes a revision only if the merged value is really different. So a conversation that repeats the profile adds no version.

## Who is allowed to forget

- **The manager makes LangMem a memory system, not only a store.** It reads an exchange, examines the stored memories, and returns the changes that it wants to make.
- **The manager returns the changes and does not write them. So a configuration can permit each kind of change separately.** Upstream, deletes are off by default.
- **The same permissions control the profile.** A profile revision patches a document that `register` created before. So a revision needs `enable_updates`. A manager without the update permission keeps the profile at its current version and reports `profile_refused`. In the same way, a refused patch comes back as a result, not as an error.

This has 2 results. The lamp tests show the first, and the demo shows the second:

- **Reconciliation.** An extractor that proposes an insert possibly did not examine the address. So a proposed insert that collides with a stored memory becomes a patch of that memory. A proposed patch of a memory that is gone becomes an insert. A read of the address itself decides this.
- **One path can allow a change that the other path refuses.** Here, the hot-path tool has `actions_permitted=("create", "update")`, and the background manager has `enable_deletes=True`. So the tool refuses a delete from the agent during the conversation. A later reflection, at 11:00, deletes the same memory. Both gates return a refusal as a result and do not raise an error, so the refusal stays visible in the call log.

Where the lamp records provenance:

- **Provenance is a `LineageDAG` edge, not a stored field.** So "what taught the agent this?" is a graph read. A memory that 3 conversations patched has 3 predecessors, and the lamp keeps no list.
- **Hot-path writes have no such edge.** The reason is simple: the lamp recorded no conversation for the edge to refer to.

## The debounce, and what it leaves behind

- **`ReflectionExecutor` holds the important part of the LangMem background path.** A caller submits work with an `after_seconds` window. If another turn arrives on the same thread before that window closes, the executor cancels the pending task. Then it schedules the task again, with the new messages merged in. So a 3-turn exchange produces one reflection over the whole exchange, not 3 reflections over parts of it.

The lamp uses 2 facades with their anchor on the same tier:

| Facade | What it holds |
| --- | --- |
| `EventLog` | The arrival order of the submissions, with the cancelled ones |
| `TemporalCollection` | The due time of each submission, as its event time |

- **"What is ready to run?" is a half-open time query.** It is not a scan that the lamp writes.
- **To cancel, the lamp removes the temporal entry.** The log entry stays, and that is the useful part. A superseded submission is still readable. Its state is `superseded`, and it names the submission that replaced it. In a memory system, deferred work that never ran is usually the hardest thing to see. Here, a reader finds it with the same kind of read as all other data.
- **The merge is deterministic.** Messages accumulate, so the manager sees the whole exchange. If the newer conversation has a change for the same address, the queue drops the older proposed change. The newer proposal is the one to apply. Profile fields merge the same way.
- **In the demo:** the first turn says that the billing service runs on FastAPI. The second turn, 4 minutes later, corrects this to Litestar. The manager applies only the correction. The stored memory is at version 1, not 2, because the superseded proposal never reached the store.

## Procedural memory

- **The other lamps change what an agent knows. This one changes how it behaves.** LangMem stores the prompt of the agent as memory. It revises the prompt from feedback on past runs. So memory and configuration are no longer separate things.
- **`VersionedRecords` fits again.** The prompt is the identity. So the agent reads "the current code-review prompt", not a fixed text. Each optimization is a revision. So if a revision makes the prompt worse, a reader can read the earlier versions.
- **`LineageDAG` edges from the annotated exchanges** answer a question that a versioned prompt alone cannot answer: not only what changed, but which piece of feedback changed it.
- **Upstream, a model rewrites the prompt** with one of 3 strategies. The lamp keeps the shape of the strategy with the lowest cost, `prompt_memory`. That strategy takes successful patterns from past interactions into the next prompt.
- **Feedback names a keyed rule.** A rule with a known key replaces its earlier text, so the 2 texts do not accumulate side by side. The prompt is the base text plus its rules, in the order that the optimizer learned them. The optimizer writes a revision only if the rule set changes. It credits only the exchanges that changed the rule set.

In the demo, 3 pieces of feedback produce 2 versions:

- **r1:** name the test that fails in every comment.
- **r2:** also lead with what breaks before any style comment.
- **The third repeats a rule that the prompt already has.** So the optimizer writes no version.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `manage_memory` | Write one memory now, within `actions_permitted` |
| `search_memory` | Search below the bound namespace, with a metadata filter, an offset, and a limit |
| `submit_conversation` | Queue a conversation for background formation, debounced for each thread |
| `run_reflection` | Apply each submission that is due |
| `get_profile` | Read the single profile document and its earlier versions |
| `optimize_prompt` | Merge feedback into a prompt, if it changes the rules |
| `get_prompt` | Read the prompt that the agent uses, and its earlier versions |

- **Each namespace-addressed tool is bound to `("memories", "{user_id}")`.** The caller supplies `user_id` once, at construction. No tool argument names a user. So a tool set for one user cannot address the memories of another user. The lamp tests make 2 tool sets over one store to check this.
- **One queue tier holds the submissions of all namespaces.** So `submit_conversation` and `run_reflection` give the bound namespace to the queue. A tool set runs only the due work of its own user, and sees only the pending threads of its own user. A thread name is unique in one namespace, not in the whole store. So the debounce matches on the pair of namespace and thread. If 2 users choose the same thread name, their submissions do not affect each other.
- **`optimize_prompt` and `get_prompt` are outside that boundary, on purpose.** A prompt is the instructions of the agent, not the memory of one user. So prompts are in a shared `("prompts",)` namespace outside the user path. All tool sets read and revise the same prompts. A revision from the feedback of one user controls the agent for all users.

The agent learns the code-review preferences of Dana from 3 conversations:

- **The first 2 conversations arrive 4 minutes apart on one thread.** So the second supersedes the first inside its debounce window, and one reflection covers the whole exchange. The reflection stores 3 memories and revises the profile to r2.
- **Then the hot path:** the agent writes an episode. The tool refuses a delete there.
- **A later thread** deletes the same memory through the manager. It also patches the memory about test tools in place, to version 2.
- **At the end,** 3 pieces of feedback revise the review prompt 2 times.

| Tool | Calls |
| --- | ---: |
| `submit_conversation` | 3 |
| `run_reflection` | 2 |
| `manage_memory` | 2 |
| `optimize_prompt` | 3 |
| `get_prompt` | 1 |
| `get_profile` | 1 |
| `search_memory` | 2 |

## Limits

**Left out:**

- **Model work:** the LangMem language-model extraction and prompt rewrites.
- **Pydantic:** the LangMem Pydantic integration.
- **Vectors:** embeddings and vector search.
- **Optimizers:** the `gradient` and `metaprompt` optimizer strategies, and `create_multi_prompt_optimizer` with its feedback attribution across several prompts.
- **Execution:** real threaded or remote background execution.
- **Manager retrieval:** `query_model` and `query_limit` for the retrieval pass of the manager itself.
- **Other options:** `default_factory`, `phases`, `update_instructions`, and `response_format`.
- **LangGraph:** the `BaseStore` and `RunnableConfig` integration that supplies the namespace values upstream.

**Replaced by:**

| Upstream | Here |
| --- | --- |
| Extraction judged by a model | Declared changes |
| Prompts written by a model | Keyed feedback rules |
| Embeddings | Token overlap |
| A clock and a thread pool | Explicit calls with supplied timestamps |

- **These omissions are on purpose.** A lamp shows that Genie blocks, links, and structure facades can hold the memory model of another system. By design, Genie leaves pure ranking, model inference, and task schedules to applications. If the lamp implemented these upstream features, it would add a hand-written algorithm and show nothing about Genie.

**Read the results with these 3 model notes:**

- **The prompt identity stores `when_to_update`, but the lamp does not enforce it.** The lamp accepts a change by its rule key. Upstream, a model reads the condition.
- **A profile revision is a separate block, and the profile that it revises owns it.** So the history stays after a change of text. But one subtree delete removes a profile and its whole history together. Upstream, a profile is one mutable document with no history.
- **The map from a namespace to an ownership path is an addition of this lamp.** It is not an upstream feature. `BaseStore` namespaces have no lifecycle, so `forget` has no upstream equivalent.

References: [conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/),
[memory API](https://langchain-ai.github.io/langmem/reference/memory/),
[tools](https://langchain-ai.github.io/langmem/reference/tools/),
[prompt optimization](https://langchain-ai.github.io/langmem/reference/prompt_optimization/),
[semantic memory guide](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/), and
[delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/).

The [lamp guide](../README.md) describes the shared agent-demo output format.
