# Scenarios

The situations Genie is built for, each told as a short story with real values.
Every scenario lists the questions it raises, the call that answers each one,
what the answer shows about the design, and what stays in the application. The
answers in the tables come from running the code on `InMemoryBackend`.

## At a glance

| Scenario | Who acts | Main question | Genie parts |
| --- | --- | --- | --- |
| [1. An assistant that knows one traveler](#1-an-assistant-that-knows-one-traveler) | A trip-planning agent | What may this agent see? | `MemoryHandle`, ownership, search |
| [2. Private drafts and shared notes](#2-private-drafts-and-shared-notes) | Three people in a newsroom | Who finds what in search? | `MemoryPolicy`, `AccessRule`, `Principal` roles |
| [3. Working memory for one task](#3-working-memory-for-one-task) | A build-fixing agent | What is the plan now, and what happened last? | `KeyedCollection`, `EventLog` |
| [4. Facts that stop being true](#4-facts-that-stop-being-true) | An on-call assistant | Who was on call when the incident started? | `TemporalCollection` |
| [5. A policy with editions](#5-a-policy-with-editions) | A support agent | What did the refund policy say before? | `VersionedRecords` |
| [6. Where a conclusion came from](#6-where-a-conclusion-came-from) | A research assistant | What depends on a withdrawn source? | `LineageDAG`, `PropertyGraph` |
| [7. Who looked at what](#7-who-looked-at-what) | A compliance team | Who read the passport, and who was refused? | `MemoryHook` |
| [8. Forgetting a traveler](#8-forgetting-a-traveler) | The travel service | What is left after a traveler's data is deleted? | `Memory.delete` |
| [9. Application types in and out](#9-application-types-in-and-out) | The travel service | Does a `Booking` come back as a `Booking`? | `CodecRegistry`, `DataclassCodec` |
| [10. Same code, different storage](#10-same-code-different-storage) | The team that runs the service | What changes when the database changes? | `SQLiteBackend`, the optional adapters, `RoutedBackend` |

## 1. An assistant that knows one traveler

- **Situation:** a travel service (`service:trips`) keeps one memory root per
  traveler. A planning agent (`agent:planner`) works for Ana, and a
  hotel-search sub-task works on one of her trips.
- **Goal:** the agent sees Ana's memory and nothing else. The sub-task sees one
  trip.

```python
from genie import InMemoryBackend, Memory, MemoryPolicy

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="service:trips")
for_planner = MemoryPolicy.owner_or(principals={"agent:planner"})

ana = memory.put(type="traveler", value={"name": "Ana Souza"}, policy=for_planner)
memory.put(parent=ana, type="preference", value="Window seat. No flights before 8:00.", policy=for_planner)
passport = memory.put(parent=ana, type="document", value={"kind": "passport", "expires": "2027-02-14"}, policy=for_planner)
lisbon = memory.put(parent=ana, type="trip", value={"city": "Lisbon", "from": "2026-05-12", "to": "2026-05-16"}, policy=for_planner)
booking = memory.put(parent=lisbon, type="booking", value={"hotel": "Casa do Rio", "nights": 4}, policy=for_planner)
ben = memory.put(type="traveler", value={"name": "Ben Okafor"}, policy=for_planner)

planner = memory.get_handle(root=ana, principal="agent:planner")
hotels = planner.get_handle(root=lisbon)
```

| Question | Call | Answer |
| --- | --- | --- |
| What does Ana's memory hold? | `planner.children()` | The `preference`, `document`, and `trip` blocks |
| What does the agent know about seats? | `planner.search("window")` | `"Window seat. No flights before 8:00."` |
| Can the agent open Ben's memory? | `planner.get(ben)` | `AccessDenied`: the block is outside the handle root |
| Can the hotel sub-task find the passport? | `hotels.search("passport")` | `[]`, an empty list and no error |
| And if it asks for the passport by id? | `hotels.get(passport)` | `AccessDenied`: outside the handle root |
| Can the sub-task widen its own view? | `hotels.get_handle(root=ana)` | `AccessDenied`: a child handle only narrows |
| Can an agent the policy does not list get a handle? | `memory.get_handle(root=ana, principal="agent:ads")` | `AccessDenied`: `agent:ads` cannot `read` the root |

**What this shows about the design**

- **Two checks, and access needs both.** The handle decides *where* the agent
  can reach (the subtree under `ana`). The `MemoryPolicy` decides *who* may act
  on each block. `for_planner` is what lets `agent:planner` in; with the
  default `owner_private()`, `get_handle` itself would raise `AccessDenied`.
- **Parent/child is the scope.** `ana` owns the preference, the passport, and
  the trip, so a handle rooted at `ana` reaches exactly those blocks and the
  booking below the trip.
- **A list call shrinks; a single lookup refuses.** Search returns fewer
  results and says nothing about hidden blocks. A `get` of a known id raises
  `AccessDenied`.
- **The sub-task keeps the planner's principal.** `get_handle` on a handle
  narrows the root and cannot change the principal.
- **The trust boundary sits under the handle.** `memory` stays in service code. The
  agent receives `planner`, or smaller tools the service writes over it.
- **Text matching belongs to the backend.** `InMemoryBackend` matches a
  case-insensitive substring, which is why `"window"` finds `"Window seat"`.

**Left to the application:** which traveler a session belongs to, which
principal an agent acts as, and which tools the agent gets.

## 2. Private drafts and shared notes

- **Situation:** a newsroom keeps its notes in one backend. Maya and Leo are
  reporters. Ines is an editor, with the role `editor`.
- **Goal:** Maya's draft stays private. Her source note is readable by editors
  but not editable by them. The style guide is open to everyone.

```python
from genie import AccessRule, InMemoryBackend, Memory, MemoryPolicy, Principal

backend = InMemoryBackend()
maya = Memory(backend=backend, principal="user:maya")
ines = Memory(backend=backend, principal=Principal("user:ines", roles=frozenset({"editor"})))
leo = Memory(backend=backend, principal="user:leo")

editors = AccessRule.only(roles={"editor"}, owner=True)
editors_may_read = MemoryPolicy(read=editors, search=editors, traverse_up=editors, traverse_down=editors, write=AccessRule.owner())

draft = maya.put(type="draft", value="Turnout in the city election rose in every district.")
source = maya.put(type="source_note", value="City election office: turnout 61%, up from 54%.", policy=editors_may_read)
guide = maya.put(type="style_guide", value="Write election turnout as a whole percent.", policy=MemoryPolicy.public())
```

| Question | Call | Answer |
| --- | --- | --- |
| What does Maya find for "election"? | `maya.search("election")` | `draft`, `source_note`, `style_guide` |
| What does Ines find? | `ines.search("election")` | `source_note`, `style_guide` |
| What does Leo find? | `leo.search("election")` | `style_guide` |
| Can Leo open the draft if he knows its id? | `leo.get(draft)` | `AccessDenied`: `user:leo` cannot `read` it |
| Can Ines read the source note? | `ines.get(source)` | `"City election office: turnout 61%, up from 54%."` |
| Can Ines correct it? | `ines.set(source, "...")` | `AccessDenied`: `user:ines` cannot `write` it |
| Does Ines see Maya's own correction? | `maya.set(source, "... up from 54% in 2022.")`, then `ines.search("2022")` | Yes. The block is now at `version` `2` |

**What this shows about the design**

- **`owner_private()` is the default.** `draft` has no `policy` argument, so
  only `user:maya` passes any check on it.
- **One rule per operation.** A `MemoryPolicy` holds a separate `AccessRule`
  for `read`, `write`, `search`, `traverse_up`, and `traverse_down`, so "read
  but not edit" is one policy value, not a separate feature.
- **A field left out allows everyone.** Every `MemoryPolicy` field defaults to
  `AccessRule.allow()`, which is why `editors_may_read` names all five.
- **Roles come from the `Principal`.** Genie reads `roles` from the acting
  principal. It does not know who is an editor.
- **Search filters without telling.** Leo's search returns one result, and
  nothing in the reply says that two more blocks matched.
- **A policy is fixed at `put`.** No call changes the policy of `draft`.
  Sharing the draft later means storing a new block with the wider policy.
- **Three `Memory` objects, one set of blocks.** A `Memory` holds
  configuration and no block data, so each person is a different principal
  over the same backend.

**Left to the application:** user accounts, roles, and which policy each kind
of note gets.

## 3. Working memory for one task

- **Situation:** a build agent (`agent:builder`) fixes a failing build on the
  branch `release/2.4`. It keeps named facts it can look up by key, and an
  ordered list of the steps it took.
- **Goal:** look up the current plan by name, read the last steps in order,
  replace the plan safely when two workers race, and clean up in one call.

```python
from genie import EventLog, InMemoryBackend, KeyedCollection, Memory, MemoryAddress

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="agent:builder")

task = memory.put(type="task", value={"goal": "Fix the failing build on release/2.4"})
facts_root = memory.put(parent=task, type="collection", value="facts")
steps_root = memory.put(parent=task, type="log", value="steps")
facts = KeyedCollection(memory, backend, collection=facts_root)
steps = EventLog(memory, backend, log=steps_root)

PLAN = MemoryAddress(("plan",), "current")
facts.bind(MemoryAddress(("build",), "failing_test"), memory.put(parent=facts_root, type="fact", value="test_invoice_totals"))
first_plan = memory.put(parent=facts_root, type="plan", value="Pin babel to 2.14")
facts.bind(PLAN, first_plan)

for text in (
    "Ran the test suite: 1 failure in test_invoice_totals",
    "babel 2.15 changed how currency amounts are rounded",
    "Pinned babel to 2.14; the suite passes",
):
    steps.append(memory.put(parent=steps_root, type="step", value=text))
```

Two workers of the same agent read the plan at the same time, and both try to
replace it:

```python
seen_by_a = facts.get(PLAN)
seen_by_b = facts.get(PLAN)
plan_b = memory.put(parent=facts_root, type="plan", value="Round with Decimal.quantize and unpin babel")
facts.rebind(PLAN, plan_b, expected=seen_by_b)
plan_a = memory.put(parent=facts_root, type="plan", value="Keep the pin and open an upstream issue")
facts.rebind(PLAN, plan_a, expected=seen_by_a)      # raises MemoryConflict
```

| Question | Call | Answer |
| --- | --- | --- |
| What is the current plan? | `facts.get(PLAN).value` | `"Pin babel to 2.14"` |
| What were the last two steps? | `steps.tail(limit=2)` | The rounding finding, then the pin, oldest first |
| Which position did each step get? | `steps.append(...).entry.sequence` | `0`, `1`, `2` |
| Two workers replace the plan from the same read. Who wins? | `facts.rebind(PLAN, ..., expected=...)` | The first call. The second raises `MemoryConflict`, because `PLAN` no longer points at the block it expected |
| Is the first plan gone? | `memory.get(first_plan)` | No. `rebind` moves the key; the old block stays until something deletes it |
| The task is done. How is it cleaned up? | `memory.delete(task)` | Every block below `task` is gone, and so are its key bindings and log entries |

**What this shows about the design**

- **Membership is not ownership.** A key binding or a log entry points at a
  block by id. The fact and step blocks sit under `facts_root` and
  `steps_root` because the agent put them there, and that is why
  `memory.delete(task)` removes them.
- **Contested writes are atomic in the backend.** `bind`, `rebind`, and
  `unbind` check and change the key in one `KeyedStore` step, so two racing
  workers cannot both win.
- **`MemoryConflict` means "read again".** Genie does not retry. The losing
  worker reads the new plan and decides what to do.
- **The store allocates sequences.** They start at `0` and are never reused.

**Left to the application:** what counts as a fact, the key names, and what
the losing worker does next.

## 4. Facts that stop being true

- **Situation:** an ops assistant (`agent:ops`) keeps the on-call rotation for
  the payments team, and the incidents that happen during it.
- **Goal:** answer "who was on call at that moment" for any past moment, and
  see gaps in cover.

```python
from datetime import UTC, datetime

from genie import InMemoryBackend, Memory, TemporalCollection, ValidityInterval

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="agent:ops")


def utc(day, hour=0, minute=0):
    return datetime(2026, 3, day, hour, minute, tzinfo=UTC)


rotation = memory.put(type="timeline", value="Payments on-call")
priya = memory.put(parent=rotation, type="shift", value="Priya Nair is on call for payments")
tomas = memory.put(parent=rotation, type="shift", value="Tomas Ruiz is on call for payments")
incident = memory.put(parent=rotation, type="incident", value="INC-2207: card payments fail for EU customers")

oncall = TemporalCollection(memory, backend, collection=rotation)
oncall.add(priya, validity=ValidityInterval(utc(9, 9), utc(16, 9)))
oncall.add(tomas, validity=ValidityInterval(utc(16, 9)))       # no end yet
oncall.add(incident, occurred_at=utc(14, 2, 10))
```

| Question | Call | Answer |
| --- | --- | --- |
| What happened in March? | `oncall.events(start=utc(1), stop=utc(31))` | `INC-2207`, at `2026-03-14T02:10:00+00:00` |
| Who was on call at that moment? | `oncall.valid_at(utc(14, 2, 10))` | Priya |
| Who was on call at the handover, 16 March 09:00? | `oncall.valid_at(utc(16, 9))` | Tomas only |
| Who is on call on 20 March? | `oncall.valid_at(utc(20, 12))` | Tomas |
| Tomas's shift now ends on 18 March. Who is on call on 20 March? | `oncall.replace(tomas, validity=ValidityInterval(utc(16, 9), utc(18, 9)))`, then `oncall.valid_at(utc(20, 12))` | `[]`: nobody. The gap in cover is visible |
| Who covered any part of 15 to 17 March? | `oncall.overlapping(ValidityInterval(utc(15), utc(17)))` | Priya and Tomas |
| What if a time has no time zone? | `oncall.valid_at(datetime(2026, 3, 20, 12))` | `ValueError`: `valid_at must be timezone-aware.` |

**What this shows about the design**

- **Two time axes.** `occurred_at` is when an event happened. A
  `ValidityInterval` is the period in which a fact was true. One collection
  holds both, and an entry may carry one, the other, or both.
- **Intervals are half-open.** `valid_at` is included and `invalid_at` is not,
  so the two shifts meet at 16 March 09:00 without overlapping.
  `invalid_at=None` means the fact is still true.
- **An empty answer is information.** After the `replace`, the empty list for
  20 March shows a real gap, not an error.
- **All times are timezone-aware** and are stored in UTC.

**Left to the application:** when a fact stops being true. Genie stores the
intervals it is given and never ends one by itself.

## 5. A policy with editions

- **Situation:** a support service quotes the company refund policy to
  customers. The policy has changed, and older orders fall under the old text.
- **Goal:** read the current text, read any earlier edition by number, and list
  the history.

```python
from genie import InMemoryBackend, Memory, MemoryAddress, VersionedRecords

backend = InMemoryBackend()
memory = Memory(backend=backend, principal="service:support")

policies_root = memory.put(type="collection", value="Customer policies")
policies = VersionedRecords(memory, backend, collection=policies_root)
REFUNDS = MemoryAddress(("policies",), "refunds")

refunds = memory.put(parent=policies_root, type="policy", value="Refund policy")
policies.register(REFUNDS, refunds)
policies.append(REFUNDS, memory.put(parent=refunds, type="revision", value={"window_days": 30, "text": "Refunds within 30 days of delivery."}))
policies.append(REFUNDS, memory.put(parent=refunds, type="revision", value={"window_days": 14, "text": "Refunds within 14 days of delivery, unopened items only."}))
```

| Question | Call | Answer |
| --- | --- | --- |
| What does the policy say now? | `policies.current(REFUNDS)` | Revision `2`: `"Refunds within 14 days of delivery, unopened items only."` |
| Order `A-1093` was placed under revision 1. What did it say? | `policies.revision(REFUNDS, 1)` | `"Refunds within 30 days of delivery."` |
| How has the refund window changed? | `policies.history(REFUNDS)` | Revision `1`: `30` days. Revision `2`: `14` days |
| A typo in revision 2 is fixed with `memory.update`. Is that a new revision? | `memory.update(current.block, value={"text": "..."})` | No. The block goes to `version` `2`, and `current(REFUNDS)` is still revision `2` |

**Which kind of change is it?**

| The application wants to | Use | Is the old value kept? |
| --- | --- | --- |
| Correct a value in place | `Memory.set` or `Memory.update` | No. `version` goes up by `1`, and a write that races another raises `MemoryConflict` |
| Keep each edition and read any of them later | `VersionedRecords` | Yes. Each revision is its own block |
| Know when something was true in the world | `TemporalCollection` | Yes. Each fact carries its validity period |

**What this shows about the design**

- **One stable identity, many revision blocks.** `refunds` is the identity at
  `REFUNDS`, and each revision is a separate block. Here the identity owns its
  revisions, so deleting `refunds` removes the history with it.
- **Revision numbers start at `1`** and follow the order of `append`.
- **A revision block is an ordinary block.** `append` does not freeze it, which
  is why the typo fix changes revision 2 in place.
- **Composed, not new storage.** `VersionedRecords` is built from keyed
  identity and an event log, so it needs a backend with `KeyedStore` and
  `SequenceStore`.

**Left to the application:** which revision an order falls under, and whether a
change is a correction or a new edition.

## 6. Where a conclusion came from

- **Situation:** a research assistant (`agent:research`) writes a retention
  summary for Q2 2026. The summary rests on claims, and each claim comes from a
  source.
- **Goal:** trace the summary back to its sources, and find everything that
  depends on a source that was withdrawn.

```python
from genie import InMemoryBackend, LineageDAG, Memory, PropertyGraph

memory = Memory(backend=InMemoryBackend(), principal="agent:research")

research = memory.put(type="workspace", value="Q2 2026 retention review")
transcript = memory.put(parent=research, type="source", value="Q2 2026 earnings call transcript")
note = memory.put(parent=research, type="source", value="Analyst note, 3 July 2026")
survey = memory.put(parent=research, type="source", value="Customer survey, June 2026")
churn = memory.put(parent=research, type="claim", value="Monthly churn fell from 3.4% to 2.1% in Q2")
seats = memory.put(parent=research, type="claim", value="Enterprise seats grew 18% in Q2")
summary = memory.put(parent=research, type="summary", value="Retention improved in Q2")

lineage = LineageDAG(memory, relation="derived_into")
lineage.add(transcript, churn)
lineage.add(note, seats)
lineage.add(churn, summary)
lineage.add(seats, summary)

graph = PropertyGraph(memory)
graph.add_edge(survey, seats, "contradicts", metadata={"note": "survey shows seat count flat"})
```

| Question | Call | Answer |
| --- | --- | --- |
| What does the summary rest on? | `lineage.walk_predecessors(summary, max_depth=2)` | Summary ← churn claim ← earnings call transcript, and summary ← seats claim ← analyst note |
| The analyst note was withdrawn. What does it affect? | `lineage.walk_successors(note, max_depth=3)` | The seats claim, and through it the summary |
| Can the summary be recorded as a source of the churn claim? | `lineage.add(summary, churn)` | `MemoryConflict`: it would create a cycle in relation `derived_into` |
| Does anything argue against the seats claim? | `graph.neighbors(seats, relation="contradicts", direction="in")` | The June customer survey, with its `note` metadata |
| Is the survey part of the lineage? | `lineage.predecessors(seats)` | No. Only the analyst note |

**What this shows about the design**

- **Links are relationships, not ownership.** Every block here is a child of
  `research`. The lineage lives in links, so deleting the summary removes its
  links and leaves the claims and sources in place.
- **`LineageDAG` is a `PropertyGraph` on one relation, with a cycle check.**
  The check and the insert happen in one `AcyclicLinkStore` step, so two
  concurrent inserts cannot close a cycle together.
- **Other relations share the same blocks.** `contradicts` goes through
  `PropertyGraph` and stays outside the `derived_into` walk.
- **Walks are bounded.** `max_depth` and `limit` cap them, and they return
  simple paths in breadth-first order.

**Left to the application:** the relation names, what counts as "derived",
and what to do with a claim that rests on a withdrawn source.

## 7. Who looked at what

- **Situation:** the compliance team of the travel service from
  [Scenario 1](#1-an-assistant-that-knows-one-traveler) wants a record of every
  passport read and every refused call.
- **Goal:** one trail that lists allowed reads and refused attempts, with who
  tried and from which handle.

```python
from genie import MemoryHook


class AuditTrail(MemoryHook):
    def __init__(self):
        self.events = []
        self.deleted = []                                          # used in Scenario 8

    def after_get(self, context, block):
        if block is not None and block.type == "document":
            self.events.append((context.principal_id, context.attributes.get("handle.root"), context.operation, block.id, "allowed"))

    def on_denied(self, context, operation, subject_id, reason):
        self.events.append((context.principal_id, context.attributes.get("handle.root"), context.operation, subject_id, f"refused by {operation}"))

    def after_delete(self, context, block):
        self.deleted.append(block.type)


audit = AuditTrail()
memory = Memory(backend=backend, principal="service:trips", hooks=[audit])
```

The service then runs the code and the calls of Scenario 1, plus one allowed
read: `planner.get(passport)`. The trail holds block ids; the table shows the
variable names from Scenario 1 instead.

| Principal | Handle root | Operation | Block | Result |
| --- | --- | --- | --- | --- |
| `agent:planner` | `ana` | `get` | `passport` | allowed |
| `agent:planner` | `ana` | `get` | `ben` | refused by `handle.subtree` |
| `agent:planner` | `lisbon` | `get` | `passport` | refused by `handle.subtree` |
| `agent:planner` | `lisbon` | `handle.create` | `ana` | refused by `handle.subtree` |
| `agent:ads` | none | `handle.create` | `ana` | refused by `read` |

**What this shows about the design**

- **Every refusal is reported** to `on_denied` with the check that refused it:
  a `HandleCapability` such as `handle.subtree` for a handle boundary, or an
  `Operation` such as `read` for a policy.
- **The trail tells handles apart.** The planner and the hotel sub-task share
  one principal, but `context.attributes` carries `handle.id` and
  `handle.root`, so each row shows which handle made the call.
- **A filtered result is not a refusal.** The sub-task's empty
  `hotels.search("passport")` is not in the trail, because the passport was
  never inside its search scope.
- **Hooks see calls through `Memory` and handles only.** A direct backend call
  reports nothing, which is one reason the backend stays in trusted code.
- **More audit tools exist.** `on_operation_end` runs exactly once for every
  operation that starts, `hooks.scope(...)` groups calls under one label such
  as an agent turn, and `register(hook, mandatory=True)` keeps a hook from
  being removed or silenced. [`hook_audit.py`](../examples/basic/hook_audit.py)
  shows each one.

**Left to the application:** where the trail is written, how long it is kept,
and who may read it.

## 8. Forgetting a traveler

- **Situation:** Ana closes her account with the travel service from
  [Scenario 1](#1-an-assistant-that-knows-one-traveler), which runs with the
  `AuditTrail` hook from [Scenario 7](#7-who-looked-at-what). Her booking links
  to a shared partner-hotel record, and a directory maps her email address to
  her root block.
- **Goal:** remove her memory in one call, and know what is left.

```python
from genie import KeyedCollection, MemoryAddress, MemoryPolicy

catalog = memory.put(type="catalog", value="Partner hotels", policy=MemoryPolicy.public())
casa = memory.put(parent=catalog, type="hotel", value={"name": "Casa do Rio", "city": "Lisbon"}, policy=MemoryPolicy.public())
memory.add_link(booking, casa, "stays_at")

directory = KeyedCollection(memory, backend, collection=memory.put(type="collection", value="Travelers by email"))
EMAIL = MemoryAddress(("travelers",), "ana.souza@example.com")
directory.bind(EMAIL, ana)

report = memory.put(type="report", value="May 2026 top travelers: Ana Souza, Ben Okafor")

memory.delete(ana)
```

| Question | Call | Answer |
| --- | --- | --- |
| Which blocks did the delete remove? | `audit.deleted`, filled by `after_delete` | `traveler`, `preference`, `document`, `trip`, `booking` |
| Is the passport still readable? | `memory.get(passport)` | `None` |
| Does the hotel record still point back at the booking? | `memory.list_links(casa, direction="in")` | `[]`: the `stays_at` link went with the booking |
| Does the email address still resolve? | `directory.get(EMAIL)` | `None`: the key binding went with `ana` |
| Is the hotel record still there? | `memory.get(casa)` | Yes. It was linked, not owned |
| Can anything still find "Ana Souza"? | `memory.search("Ana Souza")` | Yes, the `report` block. Its text names her, but `ana` does not own it |

**What this shows about the design**

- **Delete follows ownership.** `memory.delete(ana)` removes `ana` and every
  block below it in one backend step. Every link, key binding, log entry, and
  temporal entry that names a removed block goes in the same step.
- **`write` on the root is enough.** Delete checks `write` on `ana` only, not
  on each block below it, because authority over a block covers the existence
  of everything it owns.
- **Linked and keyed blocks outside the subtree stay.** `casa` and the
  directory's collection block are not owned by `ana`.
- **Genie does not read values.** The `report` names Ana in its text, and
  Genie has no way to know that.
- **A concurrent change stops the delete.** Recursive delete sends the exact
  subtree it saw. If another writer changed that subtree first, the call raises
  `MemoryConflict` and deletes nothing.

**Left to the application:** finding personal data in values outside the
subtree, backups, and any legal record of the deletion.

## 9. Application types in and out

- **Situation:** the travel service describes bookings with a `Booking`
  dataclass.
- **Goal:** store and read a `Booking` as it is, without converting it to a
  dict by hand.

```python
from dataclasses import dataclass

from genie import CodecRegistry, DataclassCodec, InMemoryBackend, JsonCodec, Memory


@dataclass(frozen=True)
class Booking:                                                     # defined in trips/models.py
    hotel: str
    check_in: str
    nights: int


backend = InMemoryBackend()
codecs = CodecRegistry([JsonCodec(), DataclassCodec([Booking])])
memory = Memory(backend=backend, principal="service:trips", codecs=codecs)
booking = memory.put(type="booking", value=Booking("Casa do Rio", "2026-05-12", 4))
```

| Question | Call | Answer |
| --- | --- | --- |
| Does it come back as a `Booking`? | `memory.get(booking).value` | `Booking(hotel='Casa do Rio', check_in='2026-05-12', nights=4)` |
| What does the backend store? | `backend.get_block(booking.id)` | Content type `application/vnd.genie.dataclass+json`, data `{"type": "trips.models:Booking", "fields": {"hotel": "Casa do Rio", "check_in": "2026-05-12", "nights": 4}}` |
| What if another service reads it without `DataclassCodec`? | `Memory(backend=backend, principal="service:trips").get(booking)` | `TypeError`: `No Genie codec registered for 'application/vnd.genie.dataclass+json'.` |

**What this shows about the design**

- **Codecs work at the storage boundary.** The backend stores plain data and a
  content type, and the application sees its own type. Decoding picks the
  codec by the stored content type only.
- **A registry replaces the default,** so `JsonCodec()` stays in the list for
  plain dict and string values.
- **`DataclassCodec` decodes registered types only.** It never imports a class
  by the name found in stored data.

**Left to the application:** its value types, and which services register which
codecs.

## 10. Same code, different storage

- **Situation:** the travel service runs its tests on `InMemoryBackend`, a
  developer laptop on a SQLite file, and production on PostgreSQL. EU travelers'
  data lives in a separate EU store.
- **Goal:** keep one version of the application code, and keep EU data in the
  EU store.

```python
from genie import InMemoryBackend, Memory, PostgresBackend, SQLiteBackend

backend = InMemoryBackend()                                        # tests
backend = SQLiteBackend("trips.db")                                # a laptop, no extra package
backend = PostgresBackend("postgresql://trips@db.internal/trips")  # production, needs genie[postgres]

memory = Memory(backend=backend, principal="service:trips")        # the same line in every case
```

For EU data, a `RoutedBackend` picks a store from block metadata:

```python
from genie import RoutedBackend


def by_region(block):
    region = block.metadata.get("region")
    return region if isinstance(region, str) else None


backend = RoutedBackend({"us": us_store, "eu": eu_store}, route_selector=by_region, default_route="us")
memory = Memory(backend=backend, principal="service:trips")

ana = memory.put(type="traveler", value={"name": "Ana Souza"}, metadata={"region": "eu"})
trip = memory.put(parent=ana, type="trip", value={"city": "Lisbon"})
ben = memory.put(type="traveler", value={"name": "Ben Okafor"}, metadata={"region": "us"})
```

| Question | Call | Answer |
| --- | --- | --- |
| Do blocks survive a restart on SQLite? | A new `SQLiteBackend("trips.db")` after the first one is closed, then `memory.get(ana_id)` | Yes: `{"name": "Ana Souza"}`, with its preference as a child |
| What happens when the PostgreSQL driver is missing? | `PostgresBackend(...)` | `UnsupportedOperation`: `PostgresBackend requires the optional 'psycopg' package; install 'genie[postgres]'.` |
| Where does Ana's trip live? | `backend.route_for_block(trip.id)` | `eu`, the route of its parent, although the trip has no `region` |
| Can a child of Ana be sent to the US store? | `memory.put(parent=ana, ..., metadata={"region": "us"})` | `MemoryConflict`: `Child route 'us' does not match parent route 'eu'.` |
| Does one search still see both stores? | `memory.search(types=["traveler"])` | Yes: Ana and Ben, in an order the backend chooses |

**What this shows about the design**

- **Only the backend line changes.** `Memory`, handles, facades, policies,
  hooks, and codecs do not know which store sits behind the contract.
- **Every shipped adapter supports all seven facades,** and the core imports no
  driver. Each driver arrives with its own extra.
- **A subtree stays on one route.** A child always lands on its parent's route,
  so ownership, traversal, and recursive delete never cross stores.
- **Adapter options live with each adapter.** Durability, timeouts, and
  construction-time server checks are in each adapter's README under
  `lib/src/genie/backend/`.

**Left to the application:** the database choice, the routing rule, deployment,
backups, and monitoring.

## Outside Genie in every scenario

- **Deciding what to remember.** Pulling facts out of a conversation, merging
  duplicates, and dropping old memories are application code.
- **Retrieval strategy.** Search returns what the backend matched. Embeddings,
  re-ranking, and combining several searches stay in the application.
- **Agent behavior.** Planning, tool choice, and what to do with a result stay
  in the agent.
- **Meaning.** Block types, relation names, key names, and value shapes belong
  to the application.

## Larger scenarios

- **Lamps** under `examples/lamps/` build whole memory systems in the style of
  known projects. Each has a demo, tests, and a guide that maps its concepts
  onto Genie. See [Lamps](../examples/lamps/README.md).
- **Single-topic examples** for each feature are listed in the
  [examples guide](../examples/README.md).

## Related reading

- [Overview](01-overview.md) — the model and the reasoning behind it
- [High-level architecture](03-high-level-architecture.md) — components, boundaries, and the path of one call
- [API interface spec](09-api-interface-spec.md) — every call used above, with what it returns and raises
- [Data model](10-data-model.md) — the records these scenarios store
- [Testing strategy](11-testing-strategy.md) — the contract suites every adapter runs
