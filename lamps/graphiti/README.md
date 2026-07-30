# Graphiti-inspired temporal knowledge graph

This lamp shows how Genie primitives can support a small temporal knowledge
graph with provenance and effective-time validity. It is an architectural
example, not a drop-in implementation of Graphiti.

## Concept mapping

| Temporal graph concept | Genie representation |
| --- | --- |
| Graph boundary | One root block and a bounded `MemoryHandle` |
| Entity node | `graphiti.entity` memory block |
| Source episode | `graphiti.episode` memory block |
| Temporal fact | First-class `graphiti.temporal_fact` memory block |
| Subject and object | Typed links from a fact to entity blocks |
| Provenance | `graphiti.provenance` links from a fact to episodes |
| Validity period | `valid_from` and `valid_to` fact metadata |
| Supersession | Close the old interval and link it to the new fact |

Validity intervals are half-open: `[valid_from, valid_to)`. An absent
`valid_to` means that the fact remains valid. When an exclusive relationship
such as `lives_in` changes, the example closes the previous fact at the new
fact's `valid_from`. The update episode is also attached to the previous fact
because it is the evidence for ending that validity period.

Run the demo from the repository root:

```bash
conda run -n "genie" python -m lamps.graphiti
```

The demo builds a connected graph around Alice, Bob, Acme, San Francisco, and
Seattle. It queries Alice before and after her move, inspects Acme's temporal
neighborhood, and shows multiple sources supporting Alice's employment:

```text
Alice as of 2024-12-01:
- Alice -[lives_in]-> San Francisco
- Alice -[works_at]-> Acme
Alice as of 2025-07-01:
- Alice -[lives_in]-> Seattle
- Alice -[works_at]-> Acme
Acme neighborhood as of 2025-07-01:
- Acme -[headquartered_in]-> San Francisco
- Alice -[works_at]-> Acme
- Bob -[works_at]-> Acme
Alice location history:
- Alice -[lives_in]-> San Francisco; valid [2024-01-10, 2025-06-01); provenance: chat/2024-01-10, chat/2025-06-02
- Alice -[lives_in]-> Seattle; valid [2025-06-01, open); provenance: chat/2025-06-02
Alice employment provenance: chat/2024-01-10, directory/2024-02-01
```

## Deliberate limits

The example does not reproduce Graphiti's LLM extraction, entity resolution,
ontology management, embeddings, hybrid ranking, or graph-database query
engine. Those are application and backend concerns. The lamp focuses on the
portable model Genie can provide: owned blocks, scoped access, typed
relationships, provenance, and updateable temporal facts.
