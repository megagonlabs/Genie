# LangMem-inspired agent memory

LangMem is LangChain's SDK for long-term agent memory. Its starting idea is that
an agent's memory has four kinds, not one: facts it has learned, experiences it
can copy, a profile of the person it works for, and its own operating
instructions. Everything is addressed by a namespace and a key. Memories are
formed either on the hot path or in a debounced background pass. And a *manager*
decides whether new information should be inserted next to what is on file,
patched into it, or allowed to delete it — the application does not decide that.
The fourth kind is the unusual one: procedural memory is the agent's prompt,
revised from feedback on past runs, so improving the agent is a memory write.

This lamp rebuilds that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.langmem.demo
conda run -n "genie" python -m pytest lamps/langmem/tests -q
```

`demo.py` is the entry point. Run the module path from the repository root so
that both `lamps` and `genie` can be found. It runs a code-review assistant
through both write paths, revises its prompt from three pieces of feedback, and
prints the answer. Every timestamp comes from the scenario and not from a clock,
so each run prints the same output.

## Modules

The lamp is split by topic instead of kept in one file. Each module depends on
the ones above it:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, namespace addressing and templating, records, schemas |
| `structures.py` | Genie facades wired to the store's five ownership tiers |
| `store.py` | Namespaced items: insert, patch, delete, scoped and filtered search |
| `profiles.py` | The profile shape: one document per namespace, revised in place |
| `manager.py` | Reconciling proposed changes and applying the permitted ones |
| `reflection.py` | The deferred queue, its due times, and the per-thread debounce |
| `prompts.py` | Procedural memory: prompts, feedback, and the versions between |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | One user, three conversations, two write paths, one answer |

## Mapping

| LangMem concept | Genie representation |
| --- | --- |
| `BaseStore` item at a namespace and key | `KeyedCollection` binding on a `MemoryAddress` |
| Namespace template `("memories", "{user_id}")` | Resolved once when a tool set is bound |
| Namespace hierarchy | `OwnershipTree` path, one block per segment |
| Namespace-prefix search | `Scope.SUBTREE` rooted at that namespace block |
| Search `filter` | `MemoryHandle.search` metadata filters |
| Semantic memory, collection shape | Many keyed `langmem.item` blocks |
| Semantic memory, profile shape | `VersionedRecords` identity, one revision per update |
| Manager `default` for a profile | The profile's first revision |
| Episodic memory | Items under an `episodes` namespace with the episode schema |
| Procedural memory (the prompt) | `VersionedRecords` identity, one revision per optimization |
| Annotated trajectory | `langmem.trajectory` block owned by the trajectories tier |
| Which feedback produced a prompt version | `LineageDAG` edges into that revision |
| Which conversation wrote a memory | `LineageDAG` edges into that block |
| `schemas=[...]` Pydantic models | `ValueSchema` per namespace, named in block metadata |
| Patch that updates in place | `MemoryHandle.update`; the id stays, `version` rises |
| `enable_inserts` / `enable_updates` / `enable_deletes` | Manager permissions checked per change |
| `actions_permitted` on the hot-path tool | Tool-level gate, separate from the manager's |
| `ReflectionExecutor.submit` | `EventLog` append plus a `TemporalCollection` due time |
| `after_seconds` debounce window | Half-open time query against that due time |
| Cancelling a pending reflection | Dropping the temporal entry; the log entry stays |
| Retiring a user | `OwnershipTree.delete_subtree` on their namespace |
| Agent access | Named JSON-compatible tools |

Five ownership tiers sit below one store root. So deleting the store deletes
everything in it, and deleting a namespace deletes the memories addressed inside
it:

```text
langmem.store                     one namespaced memory store
  namespaces                      the namespace tree, and the items it owns
  profiles                        one identity per profile, one revision per update
  prompts                         one identity per prompt, one revision per optimization
  trajectories                    annotated exchanges the feedback came from
  queue                           deferred submissions: arrival order and due time
```

## A namespace with a lifecycle

Upstream, a namespace is just a name. Nothing owns it, so nothing follows from
it: prefix search is a scan that the store filters, and "this user is gone"
means finding and deleting every key one at a time. Making each segment a block
that owns what is addressed below it changes both of those.

| Operation | Without ownership | Here |
| --- | --- | --- |
| Search below `memories/dana` | Filter candidates by prefix | `Scope.SUBTREE` rooted at that block |
| Delete everything for `dana` | List and delete each key | One `delete_subtree` |

The second row is the important one. The lamp does not delete the keyed
bindings. They go when the blocks they point at go, because a binding refers to
a block rather than copying it. So a namespace cannot be half-retired, and a
freed address can be inserted into again.

Search is the other half of the split that Genie draws. Choosing candidates is
the backend's job: the prefix becomes a scope, the item type and any metadata
filter go into the query, and one search runs per query term. Scoring is the
lamp's job, because Genie has no ranking. A candidate counts a term only when
the term appears in the candidate's own value, and the score is the share of
terms that do. Ties break on the item address rather than on `MemoryBlock.id`,
because block ids are new UUIDs on every run and a tie broken by id would make
scores differ between runs.

## Two shapes for semantic memory

LangMem's two shapes for the same kind of memory need two different Genie
structures. That is why `store.py` and `profiles.py` are separate modules.

| Shape | Behavior | Structure |
| --- | --- | --- |
| Collection | Many documents build up; retrieval picks the relevant ones | `KeyedCollection` |
| Profile | One document per namespace, replaced on write | `VersionedRecords` |

A profile is almost exactly `VersionedRecords`. The profile is the identity, so
a reader always asks for "Dana's preferences" rather than for a particular
document. Each write is a revision, so the wording it replaced is still
readable, which a single mutable document cannot offer at all. Updates merge
fields instead of replacing the whole value, and a revision is written only
when the merged value really differs. So a conversation that repeats what the
profile already records leaves no version behind.

## Who is allowed to forget

The manager is what makes LangMem a memory system rather than a store. It reads
an exchange, looks at what is already on file, and returns the changes it wants
to make. Because the changes are returned rather than written, each kind can be
permitted separately — and upstream, deletes are off by default.

Two things follow, and the demo shows both directly. First, reconciliation: an
extractor that proposes an insert has not necessarily looked at the address. So
a proposal that collides with a stored memory becomes a patch of it, and a
proposal to patch a memory that is gone becomes an insert. The read that
decides this is the address itself.

Second, the same change can be allowed on one path and refused on the other.
The hot-path tool here is built with `actions_permitted=("create", "update")`,
and the background manager with `enable_deletes=True`. So the agent is refused
a delete during the conversation, and the same memory is deleted an hour later
by a reflection. Both refusals are returned as results rather than raised, so
they stay visible in the call log.

Provenance is a `LineageDAG` edge rather than a stored field. So "what taught
the agent this?" is a graph read, and a memory patched by three conversations
has three predecessors without the lamp keeping a list. Hot-path writes have no
such edge, for the honest reason that no conversation was recorded to point at.

## The debounce, and what it leaves behind

`ReflectionExecutor` is where LangMem's background path gets interesting. Work
is submitted with an `after_seconds` window. If another turn arrives on the same
thread before that window closes, the pending task is cancelled and rescheduled
with the new messages folded in. A three-turn exchange then produces one
reflection over the whole exchange instead of three over fragments.

Two facades anchored on the same tier carry this:

| Facade | What it holds |
| --- | --- |
| `EventLog` | The order submissions arrived in, including the cancelled ones |
| `TemporalCollection` | Each submission's due time, as its event time |

So "what is ready to run?" is a half-open time query rather than a scan the
lamp writes, and cancelling is dropping the temporal entry. The log entry
stays, and that is the part worth keeping: a superseded submission is still
readable, marked `superseded` and naming the submission that replaced it.
Deferred work that never ran is usually the hardest thing to see in a memory
system. Here it is the same kind of read as everything else.

Folding is deterministic. Messages build up, so the manager sees the whole
exchange. A proposed change is dropped when the newer conversation speaks to the
same address, because the fresher proposal is the one worth applying. Profile
fields merge the same way.

In the demo the first turn says the billing service runs on FastAPI, and the
second, four minutes later, corrects it to Litestar. Only the correction is ever
applied. The stored memory is at version 1, not 2, because the superseded
proposal never reached the store.

## Procedural memory

The other lamps change what an agent knows. This one changes how it behaves.
LangMem stores the agent's prompt as memory and revises it from feedback on past
runs, so memory and configuration stop being separate things.

`VersionedRecords` fits again. The prompt is the identity, so the agent reads
"the current code-review prompt" rather than a fixed text. Each optimization is
a revision, so a prompt that drifted somewhere unhelpful can be read back. And
`LineageDAG` edges from the annotated exchanges answer what a versioned prompt
otherwise cannot: not just what changed, but which piece of feedback changed
it.

Upstream a model rewrites the prompt, using one of three strategies. The lamp
keeps the shape of the cheapest one, `prompt_memory`, which carries successful
patterns forward from past interactions. Feedback names a keyed rule, a rule
with a known key replaces its earlier wording instead of piling up beside it,
and the prompt is the base text plus its rules in the order they were acquired.
A revision is written only when the rule set changes, and only the exchanges
that changed it are credited for it.

In the demo, three pieces of feedback produce two versions:

- r1: name the test that fails in every comment.
- r2: also lead with what breaks before any style comment.
- the third repeats a rule already on file, so no version is written.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `manage_memory` | Write one memory now, within `actions_permitted` |
| `search_memory` | Search below the bound namespace, with a metadata filter and paging |
| `submit_conversation` | Queue a conversation for background formation, debounced per thread |
| `run_reflection` | Apply every submission that has come due |
| `get_profile` | Read the single profile document and the versions before it |
| `optimize_prompt` | Fold feedback into a prompt, if it changes the rules |
| `get_prompt` | Read the prompt the agent runs with, and its earlier versions |

Every tool is bound to `("memories", "{user_id}")`, with `user_id` supplied once
at construction. No tool argument names a user, so a tool set built for one user
cannot address another user's memories. The lamp's tests check that by building
two tool sets over one store.

The agent learns Dana's code-review preferences from three conversations. The
first two arrive four minutes apart on one thread, so the second supersedes the
first inside its debounce window and one reflection covers the whole exchange:
three memories stored, and the profile revised to r2. The agent then writes an
episode on the hot path and is refused a delete there. A later thread deletes
the same memory through the manager, and patches the test-tooling memory in
place, taking it to version 2. Finally, three pieces of feedback revise the
review prompt twice.

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

The lamp leaves out LangMem's language-model extraction and prompt rewriting,
its Pydantic integration, embeddings and vector search, the `gradient` and
`metaprompt` optimizer strategies, `create_multi_prompt_optimizer` and its
feedback attribution across several prompts, real threaded or remote background
execution, `query_model` and `query_limit` for the manager's own retrieval pass,
`default_factory`, `phases`, `update_instructions`, `response_format`, and the
LangGraph `BaseStore` and `RunnableConfig` integration that supplies the
namespace values upstream.

These omissions are on purpose. A lamp exists to show that Genie's blocks,
links, and structure facades can carry another system's memory model. Upstream
features that are pure ranking, model inference, or scheduling are exactly what
Genie leaves to applications by design, so writing them here would add a
hand-written algorithm and demonstrate nothing. Declared changes replace
extraction judged by a model. Keyed feedback rules replace prompts written by a
model. Token overlap replaces embeddings. And explicit calls with supplied
timestamps replace a clock and a thread pool.

Three modelling notes matter when you read the results. First, `when_to_update`
is stored on the prompt identity but is not enforced. The lamp's acceptance
rule is the rule key, while upstream a model reads the condition. Second, a
profile revision is a separate block owned by the profile it revises. So
history survives a rewording, but a profile and its whole history go together
in one subtree delete; upstream a profile is one mutable document with no
history at all. Third, mapping a namespace to an ownership path is this lamp's
addition rather than an upstream feature. `BaseStore` namespaces have no
lifecycle, so `forget` has no upstream counterpart.

References: [conceptual guide](https://langchain-ai.github.io/langmem/concepts/conceptual_guide/),
[memory API](https://langchain-ai.github.io/langmem/reference/memory/),
[tools](https://langchain-ai.github.io/langmem/reference/tools/),
[prompt optimization](https://langchain-ai.github.io/langmem/reference/prompt_optimization/),
[semantic memory guide](https://langchain-ai.github.io/langmem/guides/extract_semantic_memories/), and
[delayed processing](https://langchain-ai.github.io/langmem/guides/delayed_processing/).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
