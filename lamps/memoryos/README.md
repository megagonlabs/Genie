# MemoryOS-inspired lamp

This lamp builds a compact MemoryOS-like hierarchy with Genie blocks, scoped
access, metadata, and links. It is an explanatory example, not a compatibility
layer.

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

Its demo capacity is two. Before deleting the oldest item, overflow turns it
into a mid-term page:

```python
{
    "page_id": "page_...",
    "source_qa_key": "aisle-problem",
    "summary": "A prior aisle-seat booking required a correction.",
    "dialogue": {"user_input": "...", "agent_response": "...", "timestamp": "..."},
    "heat": 2.0,
    "visit_count": 1,
}
```

`memoryos.next_page` links preserve page order. A relevant retrieval adds one
visit and one heat point; a configured threshold allows a deterministic
long-term update.

Long-term memory contains an updateable profile dictionary plus separate user
and assistant knowledge collections. Knowledge entries store `owner`,
`knowledge`, `timestamp`, and `source_page_ids`. The profile acts as this demo's
mental model.

## Genie mapping

| Behavior | Genie implementation |
| --- | --- |
| Memory tiers | Owned subtrees through `OwnershipTree` |
| Short-term FIFO | QA blocks appended to an `EventLog` |
| Overflow | Oldest QA becomes a page, then leaves short-term storage |
| Mid-term pages | Separate `EventLog` |
| Page order | Cycle-protected `LineageDAG` edges |
| Heat | Explicit fields updated after matching retrieval |
| Stable conversation key | `KeyedCollection` binding transferred to the page |
| Profile | One updateable keyed record |
| User and assistant facts | Separate bounded `EventLog` collections |
| Mid-to-long promotion | `PropertyGraph` edges to derived knowledge |
| Time retrieval | One `TemporalCollection` across tiers |
| Agent access | `MemoryOSTools` wraps a bounded hierarchy |

Ownership defines the tier hierarchy; links represent chronology and
derivation. Capacity, summaries, heat, profile meaning, extraction, ranking,
and agent behavior remain application code.

## Demo workflow

The travel agent adds five QA pairs, leaving two in short-term memory and three
as linked pages. It retrieves matching pages to raise their heat, updates the
profile plus two user facts and one assistant fact, retrieves all tiers, then
queries a five-day timeline and a two-hop chronology/promotion graph.

See [the lamp guide](../README.md) for run commands and the shared output
format.

## Limits

The lamp does not reproduce MemoryOS's LLM summarization, dialogue-continuity
classification, embeddings, keyword extraction, similarity thresholds, heat
formula, LFU mid-term eviction, persistence files, parallel retrieval, or
response pipeline. It uses lexical overlap and a simple `+1` heat rule.

Temporal and graph retrieval are explicit demo APIs. Page links adapt
MemoryOS's `pre_page`/`next_page` chronology to Genie traversal. MemoryOS calls
the synthesized state a user profile; this demo calls it a mental model for
consistency with the lamp series.

References: [documentation](https://bai-lab.github.io/MemoryOS/docs),
[repository](https://github.com/BAI-LAB/MemoryOS),
[short-term code](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/short_term.py),
[mid-term code](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/mid_term.py),
and [long-term code](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/long_term.py).
