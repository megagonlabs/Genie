# MemoryOS-inspired tiered agent memory

MemoryOS takes its design from an operating system. Memory is a hierarchy of
tiers rather than one store.

- **Short-term memory** is a fixed-size queue that holds the last few exchanges word for word and drops the oldest first, which is called first-in, first-out (FIFO).
- **Mid-term memory** is a paged tier where exchanges are grouped into topic *sessions*, which it also calls segments.
- **Long-term memory** holds a profile of the user, plus two knowledge bases with a size cap: one for what is known about the user, and one for what the assistant learned about doing its job.
- **Heat, not age, decides what moves up:**

```text
H_segment = alpha * N_visit + beta * L_interaction + gamma * R_recency
```

- **`N_visit` rises** when a retrieval reaches a session.
- **`L_interaction` rises** when more dialogue is paged into it.
- **`R_recency` falls** with time.
- **Crossing the threshold triggers promotion.** The session's pages are analyzed and what they revealed is written into long-term memory. The session then cools down and another one can rise.
- **A session nobody comes back to is eventually evicted** when the tier fills up.
- **So the questions an agent asks are part of what its memory decides to keep,** and dialogue that never got hot is dropped before it ever reaches the profile.

This lamp rebuilds that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
python -m examples.lamps.memoryos.demo
python -m pytest examples/lamps/memoryos/tests -q
```

- **`demo.py` is the entry point.** Run the module path from the repository root so that both `examples` and `genie` can be found.
- **What it does:** talks a travel assistant through every tier boundary and prints the answer.
- **Deterministic.** Every timestamp comes from the scenario and not from a clock, so each run prints the same output.

## Modules

The lamp is split the way upstream splits itself. Each module depends on the
ones above it:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, the records each tier hands back, and the heat formulas |
| `structures.py` | Genie facades wired to the four tiers |
| `short_term.py` | The FIFO queue: append, read, report full, pop the oldest |
| `mid_term.py` | Sessions and pages: group by topic, search, visit, cool, evict |
| `long_term.py` | The profile's revisions and the two capped knowledge bases |
| `updater.py` | The two tier crossings: paging out, and promotion driven by heat |
| `retriever.py` | One read across all four tiers, bounded as upstream bounds it |
| `memoryos.py` | MemoryOS's public surface over those pieces |
| `agent.py` | JSON-compatible tools and the deterministic example agent |
| `demo.py` | One user, ten exchanges, one promotion, one eviction, one answer |

## Mapping

| MemoryOS concept | Genie representation |
| --- | --- |
| The four memory modules | Four `OwnershipTree` tiers below one system root |
| One user's whole memory | A bounded `MemoryHandle`; no tool argument leaves it |
| Short-term FIFO of QA pairs | `EventLog` on the short-term tier; arrival order is the queue |
| `is_full` / `pop_oldest` | The log's length, and deleting the block at its oldest end |
| A dialogue page | One `memoryos.page` block owned by its session |
| A mid-term session | One `memoryos.segment` block that owns its pages |
| `page_id` / `session_id` | `KeyedCollection` addresses in two namespaces |
| `get_page_by_id` | One keyed read instead of a scan across every session |
| `pre_page` / `next_page` | `LineageDAG` edges on `memoryos.next_page`, protected from cycles |
| Topic grouping of a paged-out batch | Summary overlap plus MemoryOS's keyword-overlap (Jaccard) term |
| `L_interaction` | The pages a session owns that no analysis has read |
| `N_visit` / `access_count_lfu` | Counters on the session block, raised by retrieval |
| `last_visit_time` | The session's entry in a `TemporalCollection` |
| `R_recency` | `time_decay` over that entry, at MemoryOS's 24-hour decay constant (tau) |
| `H_segment` | `segment_heat` over the three terms, computed on read |
| Least-frequently-used (LFU) eviction at `mid_term_capacity` | `OwnershipTree.delete_subtree` on the least-used session |
| Mid-term page search | `MemoryHandle.search` inside the mid-term subtree, one query per term |
| Promotion driven by heat | Read the hottest session, write long-term, reset its terms |
| `update_user_profile` | A `VersionedRecords` identity with one revision per analysis |
| User / assistant knowledge base | Two `EventLog`s, each capped and evicted oldest first |
| Where a promoted fact came from | `PropertyGraph` edges on `memoryos.extracted_from` |
| `retrieve_context` | One read per tier, bounded by `retrieval_queue_capacity` |
| `get_memory_stats` | Counts read from each tier |
| Agent access | Named JSON-compatible tools |

Four tiers sit below one system root. That mirrors the fact that upstream these
are four independent stores with four different eviction rules:

```text
memoryos.system                   one user, paired with one assistant
  short_term                      the FIFO queue of recent exchanges
  mid_term                        sessions, and the pages each one owns
  user_long_term                  the profile's revisions, and the user's facts
  assistant_long_term             what the assistant learned about the job
```

## A tier is an owner

Making each tier an owner rather than a label is what keeps the rest of the lamp
short, because three things that upstream maintains by hand follow directly from
ownership:

- **`L_interaction` is a read of the store,** because a session owns its pages.
- **Evicting a session is one `delete_subtree`,** and its pages go with it. Upstream has to walk the pages it is about to drop and clear the `pre_page` and `next_page` fields that point at them, while here Genie removes a deleted block's links itself.
- **One user's memory is a bounded handle,** where upstream separates users by writing to different files.

**The one thing ownership does not give you is a move.** Genie has no reparent
operation, so paging an exchange out is a write under its new owner followed by
a delete under the old one. Upstream does the same thing, because the queue and
the paged tier are two different files: the QA pair is popped, and a page is
built from it.

## Length is a query, not a counter

- **Upstream keeps `L_interaction` on the session,** adds to it when pages are inserted, and sets it to zero at the same moment that it marks every page `analyzed`. Those two writes always happen together, which means the counter is a cache of something that is already stored: the number of pages nobody has analyzed yet.
- **`Segment.length` reads that instead of keeping it:**

```python
@property
def length(self) -> int:
    return sum(1 for page in self.pages if not page.analyzed)
```

- **So promotion does not need to reset it.** Marking the pages *is* the reset. Later pages count again because they arrive unanalyzed, and heat cannot drift away from the pages it describes.
- **`R_recency` comes out of the temporal index the same way.** That leaves `N_visit` as the only heat term the lamp actually writes down.
- **The cost:** upstream keeps a heap of sessions ordered by heat, while this lamp recomputes heat on read. With a few sessions that is the right trade. At upstream's default of two thousand it would not be. It is named here because it is a real difference and not a hidden one.

## A read is a write

- **Upstream, `search_sessions` raises `N_visit`,** moves `last_visit_time`, bumps the LFU counter, and recomputes heat for every session that a query reached. So retrieval is the mechanism by which memory decides what to keep. It is not a read-only view of it.
- **The lamp keeps that exactly, and the demo turns on it.** Three exchanges about seat preferences page into one session. One retrieval visits it. The third page takes its heat to `1 + 3 + 1 = 5.0`, which is exactly the upstream default threshold, and the next `add_memory` promotes it.
- **Nothing in the agent asked for that.** The heat table read just before shows the session at `3.99`, one page short.

| When | `N_visit` | `L_interaction` | `R_recency` | `H_segment` |
| --- | ---: | ---: | ---: | ---: |
| Two pages in, after one retrieval | 1 | 2 | 0.99 | 3.99 |
| Third page paged in | 1 | 3 | 1.00 | 5.00 |
| Promoted and cooled | 0 | 0 | 1.00 | 1.00 |

## The chain follows the dialogue, not the storage

- **MemoryOS links consecutive pages that continue one topic,** so a conversation stays walkable after the queue that held it has moved on. Upstream keeps this as a `pre_page` and a `next_page` id string on each page dict, writes both ends itself, and clears them when a page is dropped.
- **One `LineageDAG` edge replaces all of that.** It is a single write, it can be read from either end, it refuses an edge that would close a loop, and it disappears when either page does.
- **It is also the one relationship here that crosses a session.** Two consecutive exchanges can be filed under different topics and still be linked. The lamp's tests check that with a chain that spans two sessions, and then evict one end of it.
- **The chain is why the updater remembers the last page it wrote.** With a queue of three, exchanges page out one at a time, so consecutive pages are written by separate calls. Upstream solves the same problem with `last_evicted_page_for_continuity`.

## What promotion leaves behind

Two things survive a promotion here that do not survive one upstream, and both
are free:

- **Profile history.** Upstream calls `update_user_profile(user_id, text, merge=False)`, so each analysis overwrites the profile and the reading it replaced is gone. The profile is a `VersionedRecords` identity here, so every analysis appends, and "what did it think before this?" has an answer. The demo ends at revision 2, with revision 1 still readable.
- **Provenance.** A promoted fact keeps the pages it came from, both as ids in its own record and as `memoryos.extracted_from` edges. Upstream stores a knowledge entry as text, a timestamp, and an embedding, so it cannot answer where a fact came from at all. The demo's answer uses this: it quotes the exchange that the seat preference was read out of, instead of asserting the preference on its own authority.

**The record and the graph can disagree, and that difference is the useful
part.** A page evicted from mid-term memory takes its edges with it while the
ids stay written down. So the ids say what the analysis read, and the edges say
what can still be read back.

## What the hierarchy forgets

A memory system that only remembers is a log. So it is worth being explicit
about the three places where this one drops things, and which of them is a loss.

| Rule | What goes | Recoverable from |
| --- | --- | --- |
| Queue at capacity | The oldest exchange leaves short-term memory | Its page, in a session |
| Session at capacity | The least-used session, and its pages | Long-term memory, if it was ever hot |
| Knowledge base at capacity | Its oldest fact | Nothing |

- **The middle row is the one the demo makes uncomfortable on purpose.** Alice mentions her Austin hotel, that exchange pages into an `Austin hotel choices` session, and nothing ever brings the conversation back to it. Two sessions later the tier is full, that session has the lowest access count, and it is evicted with both its pages, before any analysis read it. The hotel preference was declared, stored, and lost, because heat and not usefulness is what decides. Meanwhile the seat session survives the same eviction, only because a retrieval had visited it.
- **The third row runs at the same time.** The knowledge base holds two facts in the demo, so the fact from the second promotion pushes out `Alice prefers aisle seats on flights`. That reads like a decision, and it is only FIFO.

## Ids are addresses

- **Upstream, `get_page_by_id` walks every session's page list** until it finds a match. A page id is a `KeyedCollection` address here, so the answer is one read that does not get slower as the tier fills up. The same holds for session ids.
- **That is also the one thing eviction has to clean up by hand.** Deleting a block removes its links, but not a binding that names it, and `KeyedCollection.get` raises on a binding whose block is gone rather than quietly returning nothing.
- **So evicting a session unbinds its pages' ids and its own id before the subtree delete.** That is the opposite of upstream, where the links are the manual cleanup and the ids are just strings.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_memory` | Record one exchange, and run whatever that sets in motion |
| `retrieve_memory` | Read all four tiers, heating every session the query reaches |
| `get_user_profile` | Read the current profile and every reading it replaced |
| `inspect_sessions` | Read the heat table that promotion decides from |
| `analyze_now` | Promote the hottest session immediately, as `force_mid_term_analysis` |
| `trace_knowledge` | Ask which pages one promoted fact was read from |
| `read_dialogue_chain` | Walk forward along the exchanges that continued one page |
| `memory_stats` | Read how much each tier holds |

- **The tool set is bound to one user's hierarchy at construction,** so no tool argument can name another user's tiers. The lamp's tests check that by building two hierarchies in one store.
- **Every tool returns what the tiers did rather than a success flag,** because one `add_memory` can page an exchange out, open a session, evict a colder one, promote the hottest one, and drop a fact from a knowledge base — and none of that was asked for.

The demo runs a travel assistant through ten exchanges with the capacities
turned down, so that every boundary is reached: a queue of three, a paged tier
of three sessions, knowledge bases of two facts each, and upstream's own heat
threshold of 5.0. One session promotes on its own. One is evicted before it ever
got hot. One forced analysis takes the profile to its second revision. And the
answer is assembled from all four tiers.

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

**Left out:** MemoryOS's embeddings and every model it uses them with — the BGE
and MiniLM encoders, FAISS lookups, and the four language-model calls that write
a batch's themed summaries and keywords, judge whether two exchanges continue
each other, rewrite the user profile, and split what a session revealed into
user and assistant knowledge. Also left out: the generation module and its
prompt templates, the persistence files and their locks, the retrieval thread
pool, the heat heap, propagating page `meta_info` along a chain, the MCP server,
the Docker and Chromadb deployments, and the multi-user manager.

- **These omissions are on purpose.** A lamp exists to show that Genie's blocks, links, and structure facades can carry another system's memory model. Upstream features that are pure model inference, ranking, or provider integration are exactly what Genie leaves to applications by design, so writing them here would add a hand-written algorithm and demonstrate nothing.

**What replaces them is declared input:**

| Upstream | Here |
| --- | --- |
| A model's themed summary and keywords | Declared with each exchange |
| A model judging continuation | A flag on the exchange |
| A model's profile and knowledge analysis | Declared traits and facts |
| Cosine distance | Token overlap |
| A model's keyword list | The content words of a summary |
| A clock | Supplied timestamps |

- **Promotion is then the collection of those contributions** from the pages it reads.
- **Token overlap does no stemming,** so `seat` and `seats` are different terms. You can see that in the demo, where a query matches a page but misses a knowledge entry that says the same thing in the plural.

**Three modelling notes matter when you read the results:**

- **`L_interaction` is derived from the pages rather than stored,** and heat is computed on read rather than kept in a heap. Both are described above.
- **The user profile is a mapping of traits** rather than the free text that upstream's analysis returns, because a mapping is what makes "merge the new reading over the old one" a definable operation without a model.
- **`inspect_sessions` has no upstream counterpart.** `get_memory_stats` returns counts, not the heat table. It is a read over stored state that adds no algorithm, and without it the promotion rule would only be visible in the tests.

The mappings were checked against the linked project and its documentation on
2026-08-23.

References: [documentation](https://bai-lab.github.io/MemoryOS/docs),
[repository](https://github.com/BAI-LAB/MemoryOS),
[short-term memory](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/short_term.py),
[mid-term memory](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/mid_term.py),
[long-term memory](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/long_term.py),
[the updater](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/updater.py),
[the retriever](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/retriever.py),
and [the public class](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/memoryos.py).

The shared agent-demo output format is described in the [lamp
guide](../README.md).
