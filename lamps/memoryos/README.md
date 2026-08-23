# MemoryOS-inspired lamp

This lamp builds a compact MemoryOS-like hierarchy with Genie blocks, scoped
access, metadata, and links. It is an explanatory example, not a compatibility
layer.

## Run

From the repository root, with no API key and no external service:

```bash
conda run -n "genie" python -m lamps.memoryos.demo
conda run -n "genie" python -m pytest lamps/memoryos/tests -q
```

`demo.py` is the entry point; the module path must be run from the repository
root so both `lamps` and `genie` resolve. It fills short-term memory, overflows
it into pages, promotes hot context, and prints every tier. Nothing is random
or clock-driven, so every run prints the same output.

## Memory shape

Short-term memory is a FIFO of `memoryos.short_term_qa` values:

```python
{
    "key": "seat-fix",
    "user_input": "The window-seat change worked well.",
    "agent_response": "I'll use that correction as guidance.",
    "timestamp": "2025-04-11T10:30:00Z",
}
```

Its demo capacity is two. Overflow turns the oldest item into a mid-term page.
MemoryOS normally groups pages into topic-similar sessions; to keep the mapping
small and visible, this lamp represents each promoted page as a one-page
session:

```python
{
    "page_id": "page_...",
    "session_id": "session_...",
    "source_qa_key": "aisle-problem",
    "summary": "A prior aisle-seat booking required a correction.",
    "dialogue": {"user_input": "...", "agent_response": "...", "timestamp": "..."},
    "H_segment": 2.0,
    "N_visit": 1,
}
```

`memoryos.next_page` links preserve page order. In MemoryOS, heat and visit
counts belong to the containing session, not to an individual page. A relevant
retrieval in this lamp adds one visit and one heat point to the page's one-page
session; a configured threshold allows a deterministic long-term update.

Long-term memory contains an updateable profile dictionary plus separate user
and assistant knowledge collections. Knowledge entries store `owner`,
`knowledge`, `timestamp`, and `source_page_ids`.

## Genie mapping

| Behavior | Genie implementation |
| --- | --- |
| Memory tiers | Owned subtrees through `OwnershipTree` |
| Short-term FIFO | QA blocks appended to an `EventLog` |
| Overflow | Oldest QA becomes a page, then leaves short-term storage |
| Mid-term sessions/pages | One-page-session blocks in a separate `EventLog` |
| Page order | Cycle-protected `LineageDAG` edges |
| Session heat | `H_segment` and `N_visit` updated after matching retrieval |
| Stable conversation key | `KeyedCollection` binding transferred to the page |
| Profile | One updateable keyed record |
| User and assistant facts | Separate bounded `EventLog` collections |
| Mid-to-long promotion | `PropertyGraph` edges to derived knowledge |
| Time retrieval | One `TemporalCollection` across tiers |
| Agent access | `MemoryOSTools` wraps a bounded hierarchy |

Ownership defines the tier hierarchy; links represent chronology and
derivation. Capacity, summaries, session heat, profile meaning, extraction, ranking,
and agent behavior remain application code.

## Demo workflow

The travel agent adds five QA pairs, leaving two in short-term memory and three
as linked pages. It retrieves matching pages to raise their heat, updates the
profile plus two user facts and one assistant fact, retrieves all tiers, then
queries a five-day timeline and a two-hop chronology/promotion graph.

See [the lamp guide](../README.md) for the shared output format.

## Limits

The lamp does not reproduce MemoryOS's LLM summarization, dialogue-continuity
classification, topic-based grouping of several pages into one session,
embeddings, keyword extraction, similarity thresholds, the recency/interaction
heat formula, LFU mid-term session eviction, persistence files, parallel
retrieval, or response pipeline. It uses lexical overlap, one-page sessions,
and a simple `+1` heat rule. The explicit `update_long_term` call stands in for
MemoryOS's automatic update of hot segments.

Temporal and graph retrieval are explicit demo APIs. Page links adapt
MemoryOS's `pre_page`/`next_page` chronology to Genie traversal. The long-term
dictionary is called a user profile here, matching MemoryOS terminology.

References: [documentation](https://bai-lab.github.io/MemoryOS/docs),
[repository](https://github.com/BAI-LAB/MemoryOS),
[short-term code](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/short_term.py),
[mid-term code](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/mid_term.py),
and [long-term code](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/long_term.py).
