# MemoryOS-inspired tiered agent memory

MemoryOS copies its design from an operating system. Memory is a hierarchy of
tiers, not one store.

- **Short-term memory:** a queue with a fixed size. It holds the last few exchanges word for word, and it removes the oldest exchange first. This order is first-in, first-out (FIFO).
- **Mid-term memory:** a paged tier. It groups exchanges into topic *sessions*. MemoryOS also calls a session a segment.
- **Long-term memory:** a profile of the user, and 2 knowledge bases with a capacity. One knowledge base holds what is known about the user. The other holds what the assistant learned about how to do its job.
- **Heat, not age, decides what moves up:**

```text
H_segment = alpha * N_visit + beta * L_interaction + gamma * R_recency
```

- **`N_visit` increases** when a retrieval reaches a session.
- **`L_interaction` increases** when more dialogue goes into the session.
- **`R_recency` decreases** with time.
- **A heat at or above the threshold starts a promotion.** MemoryOS analyzes the pages of the session and writes what they showed into long-term memory. Then the session cools, and another session can become hot.
- **Eviction removes a session that nobody comes back to,** at some time after the tier is full.
- **So the questions of an agent help decide what its memory keeps.** Dialogue that never becomes hot leaves memory before it gets to the profile.

This lamp builds that architecture again from Genie parts. It is an
architectural example. It is not an implementation that can replace MemoryOS.

## Run

From the repository root, with no API key and no external service:

```bash
python -m examples.lamps.memoryos.demo
python -m pytest examples/lamps/memoryos/tests -q
```

- **`demo.py` is the entry point.** Run the module path from the repository root, so that Python can find both `examples` and `genie`.
- **What it does:** a travel assistant talks through each tier boundary, and the demo prints the answer.
- **Deterministic:** each timestamp comes from the scenario and not from a clock. So each run prints the same output.

## Modules

The lamp uses the same module split as upstream. Each module imports only the
modules above it in this table:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, the records that each tier returns, and the heat formulas |
| `structures.py` | Genie facades for the 4 tiers |
| `short_term.py` | The FIFO queue: append, read, report full, remove the oldest |
| `mid_term.py` | Sessions and pages: group by topic, search, visit, cool, evict |
| `long_term.py` | The revisions of the profile, and the 2 knowledge bases with a capacity |
| `updater.py` | The 2 tier crossings: page-out, and promotion caused by heat |
| `retriever.py` | One read across all 4 tiers, with the upstream limits |
| `memoryos.py` | The public MemoryOS interface over those parts |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | One user, 10 exchanges, one promotion from heat, one eviction, one forced analysis, one answer |

## Mapping

| MemoryOS concept | Genie representation |
| --- | --- |
| The four memory modules | 4 `OwnershipTree` tiers below one system root |
| One user's whole memory | A bounded `MemoryHandle`. No tool argument can go outside it |
| Short-term FIFO of QA pairs | `EventLog` on the short-term tier. The arrival order is the queue |
| `is_full` / `pop_oldest` | The length of the log, and a delete of the block at its oldest end |
| A dialogue page | One `memoryos.page` block that its session owns |
| A mid-term session | One `memoryos.segment` block that owns its pages |
| `page_id` / `session_id` | `KeyedCollection` addresses in 2 namespaces |
| `get_page_by_id` | One keyed read, not a scan across each session |
| `pre_page` / `next_page` | `LineageDAG` edges on `memoryos.next_page`, which cannot make a cycle |
| Topic groups for a paged-out batch | Summary overlap plus the MemoryOS keyword-overlap (Jaccard) term |
| `L_interaction` | The pages that a session owns and that no analysis has read |
| `N_visit` / `access_count_lfu` | Counters on the session block. Retrieval increases them |
| `last_visit_time` | The entry of the session in a `TemporalCollection` |
| `R_recency` | `time_decay` over that entry, with the MemoryOS decay constant (tau) of 24 hours |
| `H_segment` | `segment_heat` over the 3 terms, calculated at each read |
| Least-frequently-used (LFU) eviction at `mid_term_capacity` | `OwnershipTree.delete_subtree` on the least used session |
| Mid-term page search | `MemoryHandle.search` inside the mid-term subtree, one query for each term |
| Promotion caused by heat | Read the hottest session, write to long-term memory, mark its pages, and reset `N_visit` |
| `update_user_profile` | A `VersionedRecords` identity with one revision for each analysis |
| User / assistant knowledge base | 2 `EventLog`s. The lamp keeps each one at a capacity and evicts the oldest entry first |
| Where a promoted fact came from | `PropertyGraph` edges on `memoryos.extracted_from` |
| `retrieve_context` | One read for each tier, with `retrieval_queue_capacity` as the limit |
| `get_memory_stats` | Counts read from each tier |
| Agent access | Named JSON-compatible tools |

4 tiers are below one system root. This copies the upstream design, where these
are 4 independent stores with 4 different eviction rules:

```text
memoryos.system                   one user, paired with one assistant
  short_term                      the FIFO queue of recent exchanges
  mid_term                        sessions, and the pages each one owns
  user_long_term                  the profile's revisions, and the user's facts
  assistant_long_term             what the assistant learned about the job
```

## A tier is an owner

Each tier is an owner, not a label. This keeps the rest of the lamp short.
Upstream keeps 3 things correct by hand. Here they come directly from
ownership:

- **`L_interaction` is a read of the store,** because a session owns its pages.
- **Eviction of a session is one `delete_subtree`,** and its pages go with it. Upstream walks the pages that it will drop, and it clears the `pre_page` and `next_page` fields that refer to them. Here, Genie removes the links of a deleted block.
- **The memory of one user is a bounded handle.** Upstream keeps users apart with different files.

**Ownership does not give a move.** Genie has no reparent operation. So a
page-out first deletes the exchange under its old owner. Then it writes a page
with the same exchange under its new owner. Upstream does the same, because the
queue and the paged tier are 2 different files: upstream removes the QA pair
from the queue and builds a page from it.

## Length is a query, not a counter

- **Upstream keeps `L_interaction` on the session.** It adds to the counter when it inserts pages. It sets the counter to zero at the same time that it marks each page `analyzed`. These 2 writes always occur together. So the counter is a cache of a value that is already stored: the number of pages that no analysis has read.
- **`Segment.length` reads that value and does not keep a counter:**

```python
@property
def length(self) -> int:
    return sum(1 for page in self.pages if not page.analyzed)
```

- **So promotion does not reset it.** The marks on the pages are the reset. Later pages count again, because they arrive with no analysis. Heat cannot drift away from the pages that it describes.
- **`R_recency` also comes from the temporal index.** So `N_visit` is the only heat term that the lamp writes.
- **The cost:** upstream keeps a heap of sessions in heat order. This lamp calculates heat again at each read. With a few sessions, this is the correct trade-off. At the upstream default of 2,000 sessions, it would not be. This README names the cost because it is a real difference.

## A read is a write

- **Upstream, `search_sessions` increases `N_visit`,** moves `last_visit_time`, increases the LFU counter, and calculates heat again for each session that a query reached. So retrieval is the mechanism with which memory decides what to keep. It is not a read-only view.
- **The lamp keeps this behavior, and the demo depends on it.** 3 exchanges about seat preferences page into one session. One retrieval visits it. The `add_memory` call that pages in the third page brings its heat to `1 + 3 + 1 = 5.0`, which is the upstream default threshold exactly. The heat check in the same call promotes the session.
- **The agent did not ask for that promotion.** The heat table that the agent read just before shows the session at `3.99`, one page short.

| When | `N_visit` | `L_interaction` | `R_recency` | `H_segment` |
| --- | ---: | ---: | ---: | ---: |
| 2 pages in, after one retrieval | 1 | 2 | 0.99 | 3.99 |
| Third page paged in | 1 | 3 | 1.00 | 5.00 |
| Promoted and cooled | 0 | 0 | 1.00 | 1.00 |

## The chain follows the dialogue, not the storage

- **MemoryOS links consecutive pages that continue the conversation,** so a reader can walk a conversation after the queue that held it moved on. Upstream keeps a `pre_page` id string and a `next_page` id string on each page dict. It writes both ends, and it clears them when it drops a page.
- **One `LineageDAG` edge replaces all of that.** It is one write. A reader can read it from either end. It refuses an edge that would close a loop. Genie removes it when either page is deleted.
- **It is also the only relationship here that crosses a session.** The lamp can file 2 consecutive exchanges under different topics, and the chain still links them. The lamp tests check this with a chain across 2 sessions, and then they evict one end of it.
- **The chain is the reason why the updater remembers the last page that it wrote.** With a queue of 3, exchanges page out one at a time, so separate calls write consecutive pages. Upstream solves the same problem with `last_evicted_page_for_continuity`.

## What promotion leaves behind

2 things stay after a promotion here, and they do not stay upstream. Genie
structures give both:

- **Profile history.** Upstream calls `update_user_profile(user_id, text, merge=False)`. So each analysis replaces the profile, and the old profile is lost. Here the profile is a `VersionedRecords` identity. So each analysis appends a revision, and the question "what did it think before this?" has an answer. The demo ends at revision 2, and revision 1 is still readable.
- **Provenance.** A promoted fact keeps the pages that it came from, as ids in its own record and as `memoryos.extracted_from` edges. Upstream stores a knowledge entry as text, a timestamp, and an embedding. So upstream cannot tell where a fact came from. The demo answer uses this provenance: it quotes the exchange that the seat-preference fact came from, and it does not state the preference without a source.

**The record and the graph can disagree, and that difference is useful.** When
mid-term memory evicts a page, the edges of the page go away, but the ids stay
in the record. So the ids tell what the analysis read, and the edges tell which
pages a reader can still read.

## What the hierarchy forgets

A memory system that only remembers is a log. So this section shows the 3
places where this hierarchy removes data, and which of them is a loss.

| Rule | What goes | Recoverable from |
| --- | --- | --- |
| Queue at capacity | The oldest exchange leaves short-term memory | Its page, in a session |
| Session at capacity | The least used session, and its pages | Long-term memory, if the session was ever hot |
| Knowledge base at capacity | Its oldest fact | Nothing |

- **The demo makes the middle row uncomfortable on purpose.** Alice tells the assistant about her Austin hotel. That exchange pages into an `Austin hotel choices` session, and the conversation never comes back to it. 2 sessions later, the tier has no more room. The hotel session and the meal session have the lowest access count, and the hotel session is older. So the lamp evicts the hotel session with both its pages, before an analysis read it. The exchange declared the hotel preference, and the lamp stored it and then lost it, because heat decides and usefulness does not. The seat session stays after the same eviction, only because a retrieval visited it.
- **The demo also shows the third row.** In the demo, each knowledge base holds 2 facts. So the fact from the second promotion pushes out `Alice prefers aisle seats on flights`. That looks like a decision, but it is only FIFO.

## Ids are addresses

- **Upstream, `get_page_by_id` walks the page list of each session** until it finds a match. Here a page id is a `KeyedCollection` address, so the lookup is one read. That read does not become slower when the tier fills. The same is true for session ids.
- **Eviction cleans up these addresses by hand.** A delete of a block removes its links, but not a binding that refers to it. `KeyedCollection.get` raises `MemoryNotFound` for a binding whose block does not exist. It does not return `None`.
- **So the eviction of a session unbinds the ids of its pages and its own id before the subtree delete.** It also removes the session from the `TemporalCollection` by hand. This is the opposite of upstream, where upstream cleans up the links by hand and the ids are only strings.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_memory` | Record one exchange, and run all steps that it starts |
| `retrieve_memory` | Read all 4 tiers, and make each session that the query reaches hotter |
| `get_user_profile` | Read the current profile and each old profile that it replaced |
| `inspect_sessions` | Read the heat table that promotion uses for its decision |
| `analyze_now` | Promote the hottest session now, as `force_mid_term_analysis` does |
| `trace_knowledge` | Ask which pages one promoted fact came from |
| `read_dialogue_chain` | Walk forward along the exchanges that continued one page |
| `memory_stats` | Read the quantity in each tier |

- **The tool set is bound to the hierarchy of one user when it is created.** So no tool argument can name the tiers of another user. The lamp tests check this with 2 hierarchies in one store.
- **Each tool returns what the tiers did, not a success flag.** One `add_memory` call can page an exchange out, open a session, evict a colder session, promote the hottest session, and remove a fact from a knowledge base. The caller asked for none of these effects.

The demo runs a travel assistant through 10 exchanges with small capacities, so
it reaches each boundary:

- **Capacities:** a queue of 3, a paged tier of 3 sessions, and knowledge bases of 2 facts each.
- **Heat threshold:** 5.0, the upstream value.
- **Results:** one session promotes from heat. One session leaves by eviction before it was ever hot. One forced analysis brings the profile to its second revision. The answer comes from all 4 tiers.

| Tool | Calls |
| --- | ---: |
| `add_memory` | 10 |
| `retrieve_memory` | 2 |
| `inspect_sessions` | 2 |
| `analyze_now` | 1 |
| `trace_knowledge` | 1 |
| `read_dialogue_chain` | 1 |
| `get_user_profile` | 1 |
| `memory_stats` | 1 |

## Limits

**Left out:** the MemoryOS embeddings and each model that MemoryOS uses with
them.

- **Encoders and search:** the BGE and MiniLM encoders, and FAISS lookups.
- **4 language-model calls:** one call writes the themed summaries and keywords of a batch. One call decides if 2 exchanges continue each other. One call writes the user profile again. One call splits what a session showed into user knowledge and assistant knowledge.
- **Other parts:** the generation module and its prompt templates, the persistence files and their locks, the retrieval thread pool, the heat heap, the propagation of page `meta_info` along a chain, the MCP server, the Docker and Chromadb deployments, and the multi-user manager.

**These omissions are deliberate:**

- **What a lamp shows:** the Genie blocks, links, and structure facades can hold the memory model of another system.
- **What Genie leaves to applications:** upstream features that are only model inference, ranking, or provider integration. Genie leaves these features to applications by design. If the lamp wrote them, it would add a hand-written algorithm and show nothing about Genie.

**Declared input replaces them:**

| Upstream | Here |
| --- | --- |
| A model's themed summary and keywords | Declared with each exchange |
| A model that decides continuation | A flag on the exchange |
| A model's profile and knowledge analysis | Declared traits and facts |
| Cosine distance | Token overlap |
| A model's keyword list | The content words of a summary |
| A clock | Timestamps that the caller supplies |

- **Promotion collects those contributions** from the pages that it reads.
- **Token overlap does no stemming,** so `seat` and `seats` are different terms. The demo shows this. The query term `seat` matches the page `The window seat turned out better, so keep window from now on.` (score 0.4). It does not match `seats` in the fact `Alice switched from aisle seats to window seats.`, which says the same thing. That fact matches only on `window` (score 0.2).

**3 modelling notes are important when you read the results:**

- **`L_interaction` comes from the pages and is not stored.** The lamp also calculates heat at each read, and does not keep it in a heap. The sections above describe both.
- **The user profile is a mapping of traits,** not the free text that the upstream analysis returns. With a mapping, "merge the new profile over the old one" is an operation with a clear definition, and it needs no model.
- **`inspect_sessions` has no upstream equivalent.** `get_memory_stats` returns counts, not the heat table. `inspect_sessions` reads stored state and adds no algorithm. Without it, only the tests would show the promotion rule.

A check on 2026-08-23 compared the mappings with the linked project and its
documentation.

References: [documentation](https://bai-lab.github.io/MemoryOS/docs),
[repository](https://github.com/BAI-LAB/MemoryOS),
[short-term memory](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/short_term.py),
[mid-term memory](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/mid_term.py),
[long-term memory](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/long_term.py),
[the updater](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/updater.py),
[the retriever](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/retriever.py),
and [the public class](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/memoryos.py).

The [lamp guide](../README.md) describes the shared output format of the agent
demos.
