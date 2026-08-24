# MemoryOS-inspired tiered agent memory

MemoryOS borrows its design from an operating system. Memory is a hierarchy of
tiers rather than one store: short-term memory is a fixed-size FIFO holding the
last few exchanges verbatim, mid-term memory is a paged tier where exchanges are
grouped into topic *sessions* (which it also calls segments), and long-term
memory holds a profile of the user plus two capped knowledge bases, one for what
is known about the user and one for what the assistant learned about doing its
job. What decides whether something moves up is not age but heat:

```text
H_segment = alpha * N_visit + beta * L_interaction + gamma * R_recency
```

A retrieval that reaches a session raises `N_visit`. Paging more dialogue into it
raises `L_interaction`. Time lowers `R_recency`. When a session crosses a heat
threshold its pages are analyzed and what they revealed is written into long-term
memory, after which the session cools and something else gets a turn. A session
nobody comes back to is eventually the one evicted when the tier fills. So the
questions an agent asks are part of what its memory decides to keep, and the
dialogue that never got hot is dropped without ever reaching the profile.

This lamp reproduces that architecture on Genie building blocks. It is an
architectural example, not a drop-in implementation.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.memoryos.demo
conda run -n "genie" python -m pytest lamps/memoryos/tests -q
```

`demo.py` is the entry point; the module path must be run from the repository
root so both `lamps` and `genie` resolve. It talks a travel assistant through
every tier boundary and prints the answer. Every timestamp is supplied by the
scenario rather than read from a clock, so each run prints the same output.

## Modules

The lamp is split the way upstream splits itself, and the dependency order runs
downward:

| Module | Contents |
| --- | --- |
| `model.py` | Block types, the records each tier hands back, and the heat formulas |
| `structures.py` | Genie facades wired to the four tiers |
| `short_term.py` | The FIFO queue: append, read, report full, pop the oldest |
| `mid_term.py` | Sessions and pages: group by topic, search, visit, cool, evict |
| `long_term.py` | The profile's revisions and the two capped knowledge bases |
| `updater.py` | The two tier crossings: paging out, and heat-triggered promotion |
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
| `pre_page` / `next_page` | `LineageDAG` edges on `memoryos.next_page`, cycle-protected |
| Topic grouping of a paged-out batch | Summary overlap plus MemoryOS's keyword Jaccard term |
| `L_interaction` | The pages a session owns that no analysis has read |
| `N_visit` / `access_count_lfu` | Counters on the session block, raised by retrieval |
| `last_visit_time` | The session's entry in a `TemporalCollection` |
| `R_recency` | `time_decay` over that entry, at MemoryOS's 24-hour tau |
| `H_segment` | `segment_heat` over the three terms, computed on read |
| LFU eviction at `mid_term_capacity` | `OwnershipTree.delete_subtree` on the least-used session |
| Mid-term page search | `MemoryHandle.search` inside the mid-term subtree, one query per term |
| Heat-triggered promotion | Read the hottest session, write long-term, reset its terms |
| `update_user_profile` | A `VersionedRecords` identity with one revision per analysis |
| User / assistant knowledge base | Two `EventLog`s, each capped and evicted oldest-first |
| Where a promoted fact came from | `PropertyGraph` edges on `memoryos.extracted_from` |
| `retrieve_context` | One read per tier, bounded by `retrieval_queue_capacity` |
| `get_memory_stats` | Counts read from each tier |
| Agent access | Named JSON-compatible tools |

Four tiers sit below one system root, which mirrors the fact that upstream these
are four independent stores with four different eviction rules:

```text
memoryos.system                   one user, paired with one assistant
  short_term                      the FIFO queue of recent exchanges
  mid_term                        sessions, and the pages each one owns
  user_long_term                  the profile's revisions, and the user's facts
  assistant_long_term             what the assistant learned about the job
```

## A tier is an owner

Making each tier an owner rather than a label is what lets the rest of the lamp
stay short, because three things upstream maintains by hand fall out of
ownership.

A session owns its pages, so `L_interaction` is a read of the store. Evicting a
session is one `delete_subtree`, and its pages go with it — upstream has to walk
the pages it is about to drop and clear the `pre_page`/`next_page` fields that
point at them, while here Genie removes a deleted block's links itself. And one
user's memory is a bounded handle, where upstream separates users by writing to
different files.

The one thing ownership does not give is a move. Genie has no reparent
operation, so paging an exchange out is a write under its new owner followed by a
delete under the old one. That is what upstream does too, since the queue and the
paged tier are two different files: the QA pair is popped and a page is built
from it.

## Length is a query, not a counter

Upstream keeps `L_interaction` on the session, adds to it when pages are
inserted, and sets it to zero at the same moment it marks every page `analyzed`.
Those two writes always happen together, which means the counter is a cache of
something already stored: the number of pages nobody has analyzed yet.

`Segment.length` reads that instead of keeping it:

```python
@property
def length(self) -> int:
    return sum(1 for page in self.pages if not page.analyzed)
```

So promotion does not need to reset it. Marking the pages *is* the reset, later
pages count again because they arrive unanalyzed, and heat cannot drift away from
the pages it describes. `R_recency` comes out of the temporal index the same way,
which leaves `N_visit` as the only heat term the lamp actually writes down.

The cost is that upstream keeps a heap of sessions by heat and this lamp
recomputes heat on read. At a few sessions that is the right trade; at upstream's
default of two thousand it would not be. It is named here because it is a real
difference and not a hidden one.

## A read is a write

`search_sessions` upstream raises `N_visit`, moves `last_visit_time`, bumps the
LFU counter, and recomputes heat for every session a query reached. Retrieval is
therefore the mechanism by which memory decides what to keep, not a read-only
view of it.

The lamp keeps that exactly, and the demo turns on it. Three exchanges about seat
preferences page into one session; one retrieval visits it; the third page takes
its heat to `1 + 3 + 1 = 5.0`, precisely the upstream default threshold, and the
next `add_memory` promotes it. Nothing in the agent asked for that. The heat
table read just before shows the session at `3.99`, one page short.

| When | `N_visit` | `L_interaction` | `R_recency` | `H_segment` |
| --- | ---: | ---: | ---: | ---: |
| Two pages in, after one retrieval | 1 | 2 | 0.99 | 3.99 |
| Third page paged in | 1 | 3 | 1.00 | 5.00 |
| Promoted and cooled | 0 | 0 | 1.00 | 1.00 |

## The chain follows the dialogue, not the storage

MemoryOS links consecutive pages that continue one topic, so a conversation stays
walkable after the queue that held it has moved on. Upstream keeps this as a
`pre_page` and a `next_page` id string on each page dict, writes both ends
itself, and clears them when a page is dropped.

One `LineageDAG` edge replaces all of that. It is a single write, readable from
either end, it refuses an edge that would close a loop, and it disappears when
either page does. It is also the one relationship here that crosses a session: two
consecutive exchanges can be filed under different topics and still be linked,
which the lamp's tests check with a chain that spans two sessions and then evict
one end of.

The chain is also why the updater remembers the last page it wrote. With a queue
of three, exchanges page out one at a time, so consecutive pages are written by
separate calls — the same problem upstream solves with
`last_evicted_page_for_continuity`.

## What promotion leaves behind

Two things survive a promotion here that do not survive one upstream, and both
are free.

Upstream calls `update_user_profile(user_id, text, merge=False)`, so each
analysis overwrites the profile and the reading it replaced is gone. The profile
is a `VersionedRecords` identity here, so every analysis appends and the question
"what did it think before this?" has an answer. The demo ends at revision 2 with
revision 1 still readable.

And a promoted fact keeps the pages it came from, both as ids in its own record
and as `memoryos.extracted_from` edges. Upstream stores a knowledge entry as
text, a timestamp, and an embedding, so it cannot answer where a fact came from
at all. The demo's answer uses it: it quotes the exchange the seat preference was
read out of rather than asserting the preference on its own authority.

The record and the graph can disagree, and the difference is the useful part. A
page evicted from mid-term memory takes its edges with it while the ids stay
written down — so the ids say what the analysis read, and the edges say what can
still be read back.

## What the hierarchy forgets

A memory system that only remembers is a log, so it is worth being explicit about
the three places this one drops things, and which of them is a loss.

| Rule | What goes | Recoverable from |
| --- | --- | --- |
| Queue at capacity | The oldest exchange leaves short-term memory | Its page, in a session |
| Session at capacity | The least-used session, and its pages | Long-term memory, if it was ever hot |
| Knowledge base at capacity | Its oldest fact | Nothing |

The middle row is the one the demo makes uncomfortable on purpose. Alice mentions
her Austin hotel, that exchange pages into an `Austin hotel choices` session, and
nothing ever brings the conversation back to it. Two sessions later the tier is
full, that session has the lowest access count, and it is evicted with both its
pages — before any analysis read it. The hotel preference was declared, stored,
and lost, because heat and not usefulness is what decides. Meanwhile the seat
session survives the same eviction, purely because a retrieval had visited it.

The third row runs at the same time: the knowledge base holds two facts in the
demo, so the second promotion's fact pushes out `Alice prefers aisle seats on
flights` — which reads like intent, and is only FIFO.

## Ids are addresses

Upstream's `get_page_by_id` walks every session's page list until it finds a
match. A page id is a `KeyedCollection` address here, so the answer is one read
that does not get slower as the tier fills, and the same holds for session ids.

That is also the one thing eviction has to clean up by hand. Deleting a block
removes its links but not a binding that names it, and `KeyedCollection.get`
raises on a binding whose block is gone rather than quietly returning nothing —
so evicting a session unbinds its pages' ids and its own before the subtree
delete. Which is the inverse of upstream: there, the links are the manual cleanup
and the ids are just strings.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_memory` | Record one exchange, and run whatever that sets in motion |
| `retrieve_memory` | Read all four tiers, heating every session the query reaches |
| `get_user_profile` | Read the current profile and every reading it replaced |
| `inspect_sessions` | Read the heat table promotion decides from |
| `analyze_now` | Promote the hottest session immediately, as `force_mid_term_analysis` |
| `trace_knowledge` | Ask which pages one promoted fact was read from |
| `read_dialogue_chain` | Walk forward along the exchanges that continued one page |
| `memory_stats` | Read how much each tier holds |

The tool set is bound to one user's hierarchy at construction, so no tool
argument can name another user's tiers — which the lamp's tests check by building
two hierarchies in one store. Every tool returns what the tiers did rather than a
success flag, because one `add_memory` can page an exchange out, open a session,
evict a colder one, promote the hottest one, and drop a fact from a knowledge
base, and none of that was asked for.

The demo runs a travel assistant through ten exchanges with the capacities turned
down so every boundary is reached: a queue of three, a paged tier of three
sessions, knowledge bases of two facts each, and upstream's own heat threshold of
5.0. One session promotes on its own, one is evicted before it ever got hot, one
forced analysis takes the profile to its second revision, and the answer is
assembled from all four tiers.

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

The lamp omits MemoryOS's embeddings and every model it uses them with — BGE and
MiniLM encoders, FAISS lookups, and the four language-model calls that generate a
batch's themed summaries and keywords, judge whether two exchanges continue each
other, rewrite the user profile, and split what a session revealed into user and
assistant knowledge. It also omits the generation module and its prompt
templates, the persistence files and their locks, the retrieval thread pool, the
heat heap, page `meta_info` propagation along a chain, the MCP server, the Docker
and Chromadb deployments, and the multi-user manager.

Those omissions are deliberate. A lamp exists to show that Genie's blocks, links,
and structure facades can carry another system's memory model, so upstream
features that are pure model inference, ranking, or provider integration — which
Genie leaves to applications by design — would add hand-written algorithm without
adding any demonstration.

What replaces them is declared input. Each exchange arrives with the themed
summary a model would have written for it, a flag saying whether it continues the
previous one, and the profile traits and facts an analysis of it would conclude;
promotion is then the collection of those contributions from the pages it reads.
Similarity is token overlap rather than cosine distance, keyword sets are the
content words of a summary rather than a model's list, and supplied timestamps
replace a clock. Token overlap does no stemming, so `seat` and `seats` are
different terms — visible in the demo, where a query matches a page and misses a
knowledge entry that says the same thing in the plural.

Three modelling notes matter when reading the results. `L_interaction` is derived
from the pages rather than stored, and heat is computed on read rather than kept
in a heap, both described above. The user profile is a mapping of traits rather
than the free text upstream's analysis returns, because a mapping is what makes
"merge the new reading over the old one" a definable operation without a model.
And `inspect_sessions` has no upstream counterpart: `get_memory_stats` returns
counts, not the heat table. It is a read over stored state that adds no
algorithm, and without it the promotion rule would only be visible in the tests.

Mappings were checked against the linked project and documentation on 2026-08-23.

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
