# MemoryOS-inspired lamp

This lamp shows how an application can build a compact MemoryOS-like hierarchy
with Genie blocks, bounded access, metadata, and links. It is an explanatory
example, not a MemoryOS compatibility layer.

It demonstrates:

- short-term FIFO storage of recent user/agent QA pairs;
- automatic overflow into summarized mid-term dialogue pages;
- page visits and transparent heat scores;
- a long-term user-profile dictionary plus separate user and assistant
  knowledge collections;
- temporal retrieval across tiers and graph traversal across page chronology
  and promotion links; and
- an agent that adds, reflects on, consolidates, and queries memory through
  named tools.

## Stored memory structure

### Short-term FIFO QA

Each `memoryos.short_term_qa` block stores:

```python
{
    "key": "seat-fix",
    "user_input": "The window-seat change worked well.",
    "agent_response": "Great, I'll use that successful correction as guidance.",
    "timestamp": "2025-04-11T10:30:00Z",
}
```

The demo capacity is two. Adding the third and subsequent pairs moves the
oldest pair into a mid-term page before deleting it from the FIFO queue.

### Mid-term dialogue page

Each `memoryos.mid_term_page` block stores:

```python
{
    "page_id": "page_...",
    "source_qa_key": "aisle-problem",
    "summary": "A prior aisle-seat booking required a correction.",
    "dialogue": {
        "user_input": "...",
        "agent_response": "...",
        "timestamp": "2025-04-09T09:30:00Z",
    },
    "heat": 2.0,
    "visit_count": 1,
}
```

Pages are connected in dialogue order with `memoryos.next_page` links. Relevant
retrieval adds one visit and one heat point. Once a page reaches the configured
threshold, it can support a deterministic long-term update.

### Long-term memory

The current `memoryos.long_term_profile` block value is a plain dictionary:

```python
{
    "seat_preference": "window",
    "austin_hotel": "Riverside Inn",
    "planning_strategy": "confirm current preferences before booking",
}
```

This profile is the demo's synthesized mental model. User and assistant facts
are separate bounded `memoryos.long_term_knowledge` collections whose entries
contain `owner`, `knowledge`, `timestamp`, and `source_page_ids`.

## Mapping to Genie

| MemoryOS-inspired concept | Implementation in this lamp |
| --- | --- |
| Short-term FIFO QA | Owned `memoryos.short_term_qa` blocks ordered by sequence metadata |
| FIFO overflow | Oldest QA becomes a page, then is deleted from short-term storage |
| Mid-term pages/segments | `memoryos.mid_term_page` blocks with summaries, dialogue, heat, and visits |
| Dialogue chain | `memoryos.next_page` Genie links |
| Heat accumulation | Matching page retrieval increments explicit `heat` and `visit_count` fields |
| User mental model | One updateable long-term profile dictionary |
| User facts | Bounded long-term knowledge blocks with `owner="user"` |
| Assistant facts | Bounded long-term knowledge blocks with `owner="assistant"` |
| Mid-to-long update | Heated pages link to profile/knowledge through `memoryos.promoted_to` |
| Multi-tier retrieval | One result combines current FIFO, matching pages, profile, and both knowledge stores |
| Temporal retrieval | Event/timestamp filtering across QA, pages, and knowledge |
| Agent access | `MemoryOSTools` wraps a bounded hierarchy; the agent never receives a Genie handle |

Parent/child structure owns the hierarchy. Links express chronological and
derivation relationships without implying ownership. Capacity policies,
summaries, heat, profile meaning, extraction decisions, ranking, and agent
behavior remain outside Genie core.

## Agent workflow

The deterministic travel agent:

1. calls `add_memory` five times, leaving two QA pairs in short-term memory and
   promoting three into linked pages;
2. calls `retrieve_memory` to reflect on the pages and raise their heat;
3. calls `update_long_term` with one profile update, two user facts, and one
   assistant fact;
4. retrieves the profile and all relevant memory tiers; and
5. queries a five-day timeline and a two-hop chronology/promotion graph.

The explicit summaries and long-term candidates stand in for MemoryOS's LLM
summarization and analysis, keeping the example deterministic and dependency
free.

## Run it

From the repository root:

```bash
conda run -n "genie" python -m lamps.memoryos.demo
```

The output uses the shared lamp format: tool definitions, agent answer,
numbered calls, and per-tool usage counts.

Run its co-located tests with:

```bash
conda run -n "genie" python -m pytest lamps/memoryos/tests -q
```

## Deliberate limits

This lamp does not reproduce MemoryOS's LLM summarization, dialogue-continuity
classification, embeddings, keyword extraction, similarity thresholds, heat
formula, LFU mid-term eviction, persistence files, parallel retrieval, or
response-generation pipeline. Lexical overlap replaces embedding search, and
page heat uses an intentionally simple `+1` per matching retrieval.

`retrieve_temporal` and `retrieve_graph` are explicit demo APIs. Page chronology
is based on MemoryOS's `pre_page`/`next_page` structure, but this lamp exposes
that structure as Genie link traversal. The profile is called a mental model to
match this demo series; MemoryOS describes it as a long-term user profile.

References:

- [MemoryOS documentation](https://bai-lab.github.io/MemoryOS/docs)
- [MemoryOS repository](https://github.com/BAI-LAB/MemoryOS)
- [Short-term implementation](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/short_term.py)
- [Mid-term implementation](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/mid_term.py)
- [Long-term implementation](https://github.com/BAI-LAB/MemoryOS/blob/main/memoryos-pypi/long_term.py)
