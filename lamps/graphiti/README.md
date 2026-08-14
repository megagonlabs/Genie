# Graphiti-inspired temporal graph

This lamp builds a small temporal knowledge graph with provenance and
effective-time validity. It demonstrates an architectural subset of Graphiti,
not a drop-in implementation.

## Mapping

| Temporal graph concept | Genie representation |
| --- | --- |
| Graph boundary | Root block and bounded handle |
| Entity | `graphiti.entity` block |
| Source episode | `graphiti.episode` block |
| Relationship fact | `graphiti.temporal_fact` block used as an edge record |
| Subject and object | `PropertyGraph` edges from fact to entities |
| Provenance | Graph edges from fact to episodes |
| Episode time | `TemporalCollection` event membership |
| Validity | `ValidityInterval` membership |
| Supersession | Closed old interval plus `LineageDAG` edge |
| Agent access | Named JSON-compatible tools |

Intervals are half-open: `[valid_from, valid_to)`, with no `valid_to` meaning
still valid. When an exclusive relation such as `lives_in` changes, the new
fact's `valid_from` closes the previous interval. The update episode supports
both the new fact and the end of the old one.

Graphiti stores facts as relationships between entity nodes. This lamp
materializes each relationship as a block because Genie temporal membership
indexes blocks; the subject/object links preserve the same triplet. Graphiti's
bi-temporal distinction maps to `MemoryBlock.created_at` for ingestion time and
`ValidityInterval` for effective time. Episode `observed_at` is source event
time, not another fact-validity field.

## Tools and workflow

| Tool | Capability |
| --- | --- |
| `add_episode` | Store source material for provenance |
| `add_fact` | Store a dated relationship supported by an episode |
| `query_entity` | Find facts valid for an entity at one time |
| `query_neighborhood` | Inspect incoming and outgoing valid facts |
| `get_history` | Return relationship intervals and sources |

The agent learns from three observations, then answers where Alice lives now
and previously and who works with her. As of 2025-07-01, Alice lives in Seattle
and works at Acme; Bob also works at Acme, whose headquarters are in San
Francisco. Her `lives_in` history is:

- San Francisco, valid `[2024-01-10, 2025-06-01)`, supported by the January and
  June chat episodes.
- Seattle, valid `[2025-06-01, open)`, supported by the June episode.

| Tool | Calls |
| --- | ---: |
| `add_episode` | 3 |
| `add_fact` | 6 |
| `query_entity` | 1 |
| `query_neighborhood` | 1 |
| `get_history` | 1 |

## Limits

The lamp omits Graphiti's LLM extraction, entity resolution, prescribed and
learned ontology, entity summaries, communities, embeddings, hybrid ranking,
group management, and graph-database engine. Structured fact candidates
replace natural-language extraction. The focus is portable owned blocks,
scoped access, graph relationships, provenance, temporal membership, and
cycle-protected succession.

References: [Graphiti repository](https://github.com/getzep/graphiti) and
[adding episodes](https://help.getzep.com/graphiti/core-concepts/adding-episodes).

Run and test commands plus the shared agent format are in the [lamp
guide](../README.md).
