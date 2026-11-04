# Scenarios

This note shows the situations that Genie is for. Each scenario is a short story
with real values. Each scenario gives:

- **The questions** that the scenario raises.
- **The call** that answers each question.
- **What the answers show** about the design.
- **What stays in the application.**

The answers in the tables come from runs of the code on `InMemoryBackend`.

## At a glance

| Scenario | Who acts | Main question | Genie parts |
| --- | --- | --- | --- |
| [1. An assistant that knows one traveler](#1-an-assistant-that-knows-one-traveler) | An agent that plans trips | What can this agent see? | `MemoryHandle`, ownership, search |
| [2. Private drafts and shared notes](#2-private-drafts-and-shared-notes) | 3 people in a newsroom | Who finds what in search? | `MemoryPolicy`, `AccessRule`, `Principal` roles |
| [3. Working memory for one task](#3-working-memory-for-one-task) | An agent that fixes a build | What is the plan now, and what happened last? | `KeyedCollection`, `EventLog` |
| [4. Facts that stop being true](#4-facts-that-stop-being-true) | An on-call assistant | Who was on call when the incident started? | `TemporalCollection` |
| [5. A policy with editions](#5-a-policy-with-editions) | A support agent | What did the refund policy say before? | `VersionedRecords` |
| [6. Where a conclusion came from](#6-where-a-conclusion-came-from) | A research assistant | What depends on a withdrawn source? | `LineageDAG`, `PropertyGraph` |
| [7. Who looked at what](#7-who-looked-at-what) | A compliance team | Who read the passport, and who got a refusal? | `MemoryHook` |
| [8. Forgetting a traveler](#8-forgetting-a-traveler) | The travel service | What stays after the service deletes the data of a traveler? | `Memory.delete` |
| [9. Application types in and out](#9-application-types-in-and-out) | The travel service | Is a `Booking` still a `Booking` after a read? | `CodecRegistry`, `DataclassCodec` |
| [10. Same code, different storage](#10-same-code-different-storage) | The team that runs the service | What changes when the database changes? | `SQLiteBackend`, the optional adapters, `RoutedBackend` |

## 1. An assistant that knows one traveler

- **Situation:** a travel service (`service:trips`) keeps one memory root for
  each traveler. An agent that plans trips (`agent:planner`) works for Ana. A
  hotel-search sub-task works on one trip of Ana.
- **Goal:** the agent sees the memory of Ana and nothing else. The sub-task sees
  one trip.

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
| What does the memory of Ana hold? | `planner.children()` | The `preference`, `document`, and `trip` blocks |
| What does the agent know about seats? | `planner.search("window")` | `"Window seat. No flights before 8:00."` |
| Can the agent open the memory of Ben? | `planner.get(ben)` | `AccessDenied`: the block is outside the handle root |
| Can the hotel sub-task find the passport? | `hotels.search("passport")` | `[]`, an empty list and no error |
| What if the sub-task asks for the passport by id? | `hotels.get(passport)` | `AccessDenied`: the block is outside the handle root |
| Can the sub-task get a wider view? | `hotels.get_handle(root=ana)` | `AccessDenied`: a child handle can only narrow the scope |
| Can an agent that the policy does not list get a handle? | `memory.get_handle(root=ana, principal="agent:ads")` | `AccessDenied`: `agent:ads` cannot `read` the root |

**What this shows about the design**

- **Access needs 2 checks.** The handle decides *where* the agent can go (the
  subtree under `ana`). The `MemoryPolicy` decides *who* can act on each block.
  `for_planner` gives access to `agent:planner`. If the blocks have the default
  `owner_private()` policy, `get_handle` itself raises `AccessDenied`.
- **Parent/child is the scope.** `ana` owns the preference, the passport, and
  the trip. So a handle rooted at `ana` reaches exactly those blocks, and the
  booking below the trip.
- **A list call gives fewer results. A single lookup refuses.** Search returns
  fewer results and gives no information about hidden blocks. A `get` of a
  known id raises `AccessDenied`.
- **The sub-task keeps the principal of the planner.** `get_handle` on a handle
  narrows the root. It cannot change the principal.
- **The trust boundary is below the handle.** `memory` stays in service code.
  The agent receives `planner`, or smaller tools that the service writes over
  it.
- **The backend decides how text matches.** `InMemoryBackend` uses a
  case-insensitive substring match. For this reason, `"window"` finds
  `"Window seat"`.

**Left to the application:** the traveler that a session belongs to, the
principal that an agent acts as, and the tools that the agent gets.

## 2. Private drafts and shared notes

- **Situation:** a newsroom keeps its notes in one backend. Maya and Leo are
  reporters. Ines is an editor, with the role `editor`.
- **Goal:** the draft of Maya stays private. Editors can read the source note
  of Maya, but they cannot edit it. The style guide is open to all principals.

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
| Can Leo open the draft if Leo knows its id? | `leo.get(draft)` | `AccessDenied`: `user:leo` cannot `read` it |
| Can Ines read the source note? | `ines.get(source)` | `"City election office: turnout 61%, up from 54%."` |
| Can Ines correct the source note? | `ines.set(source, "...")` | `AccessDenied`: `user:ines` cannot `write` it |
| Does Ines see a correction that Maya makes? | `maya.set(source, "... up from 54% in 2022.")`, then `ines.search("2022")` | Yes. The block is then at `version` `2` |

**What this shows about the design**

- **`owner_private()` is the default.** `draft` has no `policy` argument. So
  only `user:maya` passes the checks on it.
- **One rule for each operation.** A `MemoryPolicy` holds a separate
  `AccessRule` for `read`, `write`, `search`, `traverse_up`, and
  `traverse_down`. So "read but not edit" is one policy value, not a separate
  feature.
- **A field that the caller leaves out stays owner-only.** Each `MemoryPolicy`
  field has the default `AccessRule.owner()`. So a forgotten field never opens
  a block to all principals. `editors_may_read` still names all 5 fields, so
  each grant is visible.
- **Roles come from the `Principal`.** Genie reads `roles` from the principal
  that acts. Genie does not know who is an editor.
- **Search filters results and does not tell.** The search of Leo returns one
  result. Nothing in the reply tells that 2 more blocks matched.
- **A policy is fixed at `put`.** No call changes the policy of `draft`. To
  share the draft later, the application stores a new block with the wider
  policy.
- **3 `Memory` objects, one set of blocks.** A `Memory` holds configuration and
  no block data. So each person is a different principal over the same backend.

**Left to the application:** user accounts, roles, and the policy that each kind
of note gets.

## 3. Working memory for one task

- **Situation:** a build agent (`agent:builder`) fixes a build that fails on the
  branch `release/2.4`. The agent keeps named facts that it can find by key. It
  also keeps an ordered list of the steps that it did.
- **Goal:** find the current plan by name, and read the last steps in order. If
  2 workers race, replace the plan safely. Delete the task memory in one call.

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

2 workers of the same agent read the plan at the same time. Both workers try to
replace the plan:

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
| What were the last 2 steps? | `steps.tail(limit=2)` | The step about the babel 2.15 change, then the pin step, with the oldest step first |
| Which position did each step get? | `steps.append(...).entry.sequence` | `0`, `1`, `2` |
| 2 workers replace the plan after the same read. Which worker wins? | `facts.rebind(PLAN, ..., expected=...)` | The first call wins. The second call raises `MemoryConflict`, because `PLAN` no longer refers to the block that the second call expected |
| Is the first plan gone? | `memory.get(first_plan)` | No. `rebind` moves the key. The old block stays until a caller deletes it |
| The task is done. How does the agent remove it? | `memory.delete(task)` | Each block below `task` is gone. Its key bindings and log entries are gone too |

**What this shows about the design**

- **Membership is not ownership.** A key binding or a log entry refers to a
  block by id. The fact and step blocks are under `facts_root` and
  `steps_root` because the agent put them there. For this reason,
  `memory.delete(task)` removes them.
- **The backend makes contested writes atomic.** `bind`, `rebind`, and
  `unbind` check and change the key in one `KeyedStore` step. So 2 workers that
  race cannot both win.
- **`MemoryConflict` means "read again".** Genie does not retry. The worker
  that loses reads the new plan and decides what to do.
- **The store allocates sequences.** Sequences start at `0`, and the store
  never uses a sequence again.

**Left to the application:** the meaning of a fact, the key names, and the next
action of the worker that loses.

## 4. Facts that stop being true

- **Situation:** an ops assistant (`agent:ops`) keeps the on-call rotation for
  the payments team. The assistant also keeps the incidents that occur during
  the rotation.
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
oncall.add(tomas, validity=ValidityInterval(utc(16, 9)))       # no end time yet
oncall.add(incident, occurred_at=utc(14, 2, 10))
```

| Question | Call | Answer |
| --- | --- | --- |
| What happened in March? | `oncall.events(start=utc(1), stop=utc(31))` | `INC-2207`, at `2026-03-14T02:10:00+00:00` |
| Who was on call at that moment? | `oncall.valid_at(utc(14, 2, 10))` | Priya |
| Who was on call at the handover, 16 March 09:00? | `oncall.valid_at(utc(16, 9))` | Tomas only |
| Who is on call on 20 March? | `oncall.valid_at(utc(20, 12))` | Tomas |
| The shift of Tomas changes to end on 18 March. Who is on call on 20 March? | `oncall.replace(tomas, validity=ValidityInterval(utc(16, 9), utc(18, 9)))`, then `oncall.valid_at(utc(20, 12))` | `[]`: no person is on call. The gap in cover is visible |
| Who covered a part of 15 to 17 March? | `oncall.overlapping(ValidityInterval(utc(15), utc(17)))` | Priya and Tomas |
| What if a time has no time zone? | `oncall.valid_at(datetime(2026, 3, 20, 12))` | `ValueError`: `valid_at must be timezone-aware.` |

**What this shows about the design**

- **2 time axes.** `occurred_at` is the time when an event happened. A
  `ValidityInterval` is the period in which a fact was true. One collection
  holds both. An entry can have one, the other, or both.
- **Intervals are half-open.** An interval includes `valid_at` and does not
  include `invalid_at`. So the 2 shifts meet at 16 March 09:00 and do not
  overlap. `invalid_at=None` means that the fact is still true.
- **An empty answer is information.** After the `replace`, the empty list for
  20 March shows a real gap, not an error.
- **All times are timezone-aware.** Genie stores them in UTC.

**Left to the application:** the time when a fact is no longer true. Genie
stores the intervals that it gets, and it never ends an interval by itself.

## 5. A policy with editions

- **Situation:** a support service quotes the refund policy of the company to
  customers. The policy changed, and older orders use the old text.
- **Goal:** read the current text, read an earlier edition by number, and list
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
| The customer placed order `A-1093` under revision 1. What did revision 1 say? | `policies.revision(REFUNDS, 1)` | `"Refunds within 30 days of delivery."` |
| How did the refund window change? | `policies.history(REFUNDS)` | Revision `1`: `30` days. Revision `2`: `14` days |
| The service fixes a typo in revision 2 with `memory.update`. Is that a new revision? | `memory.update(current.block, value={"text": "..."})` | No. The block changes to `version` `2`. `current(REFUNDS)` is still revision `2` |

**Which kind of change is it?**

| The application wants to | Use | Does Genie keep the old value? |
| --- | --- | --- |
| Correct a value in place | `Memory.set` or `Memory.update` | No. `version` increases by `1`. A write that races another write raises `MemoryConflict` |
| Keep each edition and read any edition later | `VersionedRecords` | Yes. Each revision is a separate block |
| Know when something was true in the world | `TemporalCollection` | Yes. Each fact has its validity period |

**What this shows about the design**

- **One stable identity, many revision blocks.** `refunds` is the identity at
  `REFUNDS`, and each revision is a separate block. Here the identity owns its
  revisions. So a delete of `refunds` also removes the history.
- **Revision numbers start at `1`** and follow the order of `append`.
- **A revision block is an ordinary block.** `append` does not freeze it. For
  this reason, the typo fix changes revision 2 in place.
- **Composed, not new storage.** `VersionedRecords` is a composition of keyed
  identity and an event log. So it needs a backend with `KeyedStore` and
  `SequenceStore`.

**Left to the application:** the revision that applies to an order, and if a
change is a correction or a new edition.

## 6. Where a conclusion came from

- **Situation:** a research assistant (`agent:research`) writes a retention
  summary for Q2 2026. The summary is based on claims, and each claim comes
  from a source.
- **Goal:** trace the summary back to its sources. Find all blocks that depend
  on a withdrawn source.

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

memory.protect_relation("derived_into")
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
| What is the summary based on? | `lineage.walk_predecessors(summary, max_depth=2)` | Summary ← churn claim ← earnings call transcript, and summary ← seats claim ← analyst note |
| The analyst note is withdrawn. What does it affect? | `lineage.walk_successors(note, max_depth=3)` | The seats claim, and through that claim, the summary |
| Can the agent record the summary as a source of the churn claim? | `lineage.add(summary, churn)` | `MemoryConflict`: the new link makes a cycle in relation `derived_into`, so `lineage.add` refuses it |
| Does a block contradict the seats claim? | `graph.neighbors(seats, relation="contradicts", direction="in")` | The June customer survey, with its `note` metadata |
| Is the survey part of the lineage? | `lineage.predecessors(seats)` | No. Only the analyst note is a predecessor |

**What this shows about the design**

- **Links are relationships, not ownership.** Each block here is a child of
  `research`. The lineage is in links. So a delete of the summary removes its
  links, and the claims and sources stay.
- **`LineageDAG` is a `PropertyGraph` on one relation, with a cycle check.**
  The check and the insert occur in one `AcyclicLinkStore` step. So 2
  concurrent inserts cannot close a cycle together.
- **Trusted code protects the relation once.** `memory.protect_relation`
  comes before the first `lineage.add`. After it, a plain `add_link` with
  `derived_into` raises `MemoryConflict`. So no caller can close a cycle
  around the check.
- **Other relations share the same blocks.** `contradicts` goes through
  `PropertyGraph`. It stays outside the `derived_into` walk.
- **Walks are bounded.** `max_depth` and `limit` set their limits. Walks return
  simple paths in breadth-first order.

**Left to the application:** the relation names, the meaning of "derived", and
the action for a claim that is based on a withdrawn source.

## 7. Who looked at what

- **Situation:** the compliance team of the travel service from
  [Scenario 1](#1-an-assistant-that-knows-one-traveler) wants a record of each
  passport read and each refused call.
- **Goal:** one trail that lists allowed reads and refused attempts. Each item
  shows the principal that tried and the handle that it used.

```python
from genie import MemoryHook


class AuditTrail(MemoryHook):
    def __init__(self):
        self.events = []
        self.deleted = []                                          # Scenario 8 uses this list

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

The service then runs the code and the calls of Scenario 1. It also does one
allowed read: `planner.get(passport)`. The trail holds block ids. The table
shows the variable names from Scenario 1 in place of those ids.

| Principal | Handle root | Operation | Block | Result |
| --- | --- | --- | --- | --- |
| `agent:planner` | `ana` | `get` | `passport` | allowed |
| `agent:planner` | `ana` | `get` | `ben` | refused by `handle.subtree` |
| `agent:planner` | `lisbon` | `get` | `passport` | refused by `handle.subtree` |
| `agent:planner` | `lisbon` | `handle.create` | `ana` | refused by `handle.subtree` |
| `agent:ads` | none | `handle.create` | `ana` | refused by `read` |

**What this shows about the design**

- **Genie reports each refusal** to `on_denied`, with the check that refused
  it. For a handle boundary, the check is a `HandleCapability`, for example
  `handle.subtree`. For a policy, the check is an `Operation`, for example
  `read`.
- **The trail shows which handle made each call.** The planner and the hotel
  sub-task share one principal. But `context.attributes` has `handle.id` and
  `handle.root`. So each row shows the handle that made the call.
- **A filtered result is not a refusal.** The empty
  `hotels.search("passport")` of the sub-task is not in the trail. The reason
  is that the passport was never inside the search scope of the sub-task.
- **Hooks see only calls through `Memory` and handles.** A direct backend call
  reports nothing. This is one reason that the backend stays in trusted code.
- **Genie has more audit tools.** `on_operation_end` runs exactly once for each
  operation that starts. `hooks.scope(...)` puts calls into a group under one
  label, for example an agent turn. `register(hook, mandatory=True)` makes a
  hook that no caller can remove or silence.
  [`hook_audit.py`](../examples/basic/hook_audit.py) shows each tool.

**Left to the application:** the place where the trail goes, the time that the
application keeps it, and who can read it.

## 8. Forgetting a traveler

- **Situation:** Ana closes the account with the travel service from
  [Scenario 1](#1-an-assistant-that-knows-one-traveler). The service runs with
  the `AuditTrail` hook from [Scenario 7](#7-who-looked-at-what). The booking
  of Ana links to a shared partner-hotel record. A directory maps the email
  address of Ana to the root block of Ana.
- **Goal:** remove the memory of Ana in one call, and know what stays.

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
| Which blocks did the delete remove? | `audit.deleted`, which `after_delete` fills | `traveler`, `preference`, `document`, `trip`, `booking` |
| Is the passport still readable? | `memory.get(passport)` | `None` |
| Does the hotel record still have a link back to the booking? | `memory.list_links(casa, direction="in")` | `[]`: the delete removed the `stays_at` link with the booking |
| Does the email address still resolve? | `directory.get(EMAIL)` | `None`: the delete removed the key binding with `ana` |
| Is the hotel record still there? | `memory.get(casa)` | Yes. The booking had a link to it, but did not own it |
| Can a search still find "Ana Souza"? | `memory.search("Ana Souza")` | Yes, the `report` block. Its text names Ana, but `ana` does not own it |

**What this shows about the design**

- **Delete follows ownership.** `memory.delete(ana)` removes `ana` and each
  block below it in one backend step. In the same step, the backend removes
  each link, key binding, log entry, and temporal entry that names a removed
  block.
- **`write` on the root is enough.** Delete checks `write` on `ana` only, not
  on each block below it. The reason is that authority over a block covers the
  existence of all blocks that it owns.
- **Linked and keyed blocks outside the subtree stay.** `ana` does not own
  `casa` or the collection block of the directory.
- **Genie does not read values.** The `report` names Ana in its text, and
  Genie cannot know that.
- **A concurrent change stops the delete.** Recursive delete sends the exact
  subtree that it saw. If another writer changed that subtree first, the call
  raises `MemoryConflict` and deletes nothing.

**Left to the application:** the search for personal data in values outside the
subtree, backups, and a legal record of the delete.

## 9. Application types in and out

- **Situation:** the travel service describes bookings with a `Booking`
  dataclass.
- **Goal:** store and read a `Booking` as it is. The application does not
  convert it to a dict by hand.

```python
from dataclasses import dataclass

from genie import CodecRegistry, DataclassCodec, InMemoryBackend, JsonCodec, Memory


@dataclass(frozen=True)
class Booking:                                                     # the module trips/models.py defines this class
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
| Is the value still a `Booking` after a read? | `memory.get(booking).value` | `Booking(hotel='Casa do Rio', check_in='2026-05-12', nights=4)` |
| What does the backend store? | `backend.get_block(booking.id)` | Content type `application/vnd.genie.dataclass+json`, data `{"type": "trips.models:Booking", "fields": {"hotel": "Casa do Rio", "check_in": "2026-05-12", "nights": 4}}` |
| What if another service reads it without `DataclassCodec`? | `Memory(backend=backend, principal="service:trips").get(booking)` | `TypeError`: `No Genie codec registered for 'application/vnd.genie.dataclass+json'.` |

**What this shows about the design**

- **Codecs work at the storage boundary.** The backend stores plain data and a
  content type. The application sees its own type. For a decode, Genie selects
  the codec only by the stored content type.
- **A registry replaces the default.** So `JsonCodec()` stays in the list for
  plain dict and string values.
- **`DataclassCodec` decodes only registered types.** It never imports a class
  by a name that it finds in stored data.

**Left to the application:** its value types, and the codecs that each service
registers.

## 10. Same code, different storage

- **Situation:** the travel service uses `InMemoryBackend` for tests, a SQLite
  file on a developer laptop, and PostgreSQL in production. The data of EU
  travelers is in a separate EU store.
- **Goal:** keep one version of the application code, and keep EU data in the
  EU store.

```python
from genie import InMemoryBackend, Memory, PostgresBackend, SQLiteBackend

backend = InMemoryBackend()                                        # tests
backend = SQLiteBackend("trips.db")                                # a laptop, no extra package
backend = PostgresBackend("postgresql://trips@db.internal/trips")  # production, needs the postgres extra

memory = Memory(backend=backend, principal="service:trips")        # the same line in each case
```

For EU data, a `RoutedBackend` selects a store from the block metadata:

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
| Do blocks stay after a restart on SQLite? | Close the first `SQLiteBackend`, open a new `SQLiteBackend("trips.db")`, then call `memory.get(ana_id)` | Yes: `{"name": "Ana Souza"}`, with its trip as a child |
| What occurs if the PostgreSQL driver is not installed? | `PostgresBackend(...)` | `UnsupportedOperation`: `PostgresBackend requires the optional 'psycopg' package. In the Genie clone, run: python -m pip install -e '.[postgres]'` |
| Where is the trip of Ana stored? | `backend.route_for_block(trip.id)` | `eu`, the route of its parent, although the trip has no `region` |
| Can the application send a child of Ana to the US store? | `memory.put(parent=ana, ..., metadata={"region": "us"})` | `MemoryConflict`: `Child route 'us' does not match parent route 'eu'.` |
| Does one search still see both stores? | `memory.search(types=["traveler"])` | Yes: Ana, then Ben. They have the same score, so they come in creation order |

**What this shows about the design**

- **Only the backend line changes.** `Memory`, handles, facades, policies,
  hooks, and codecs do not know which store is behind the contract.
- **Each shipped adapter supports all 7 facades,** and the core imports no
  driver. Each driver is part of its own extra.
- **A subtree stays on one route.** A child always goes to the route of its
  parent. So ownership, traversal, and recursive delete never cross stores.
- **Each adapter documents its own options.** Durability, timeouts, and
  construction-time server checks are in the README of each adapter under
  `lib/src/genie/backend/`.

**Left to the application:** the database choice, the rule that selects a
route, deployment, backups, and monitoring.

## Outside Genie in every scenario

- **Decisions about what to remember.** Application code extracts facts from a
  conversation, merges duplicates, and drops old memories.
- **Retrieval strategy.** Search returns what the backend matched. Embeddings,
  re-ranking, and the combination of several searches stay in the application.
- **Agent behavior.** The plan, the tool choice, and the use of each result
  stay in the agent.
- **Meaning.** Block types, relation names, key names, and value shapes belong
  to the application.

## Larger scenarios

- **Lamps** under `examples/lamps/` build complete memory systems in the style
  of known projects. Each lamp has a demo, tests, and a guide that maps its
  concepts onto Genie. See [Lamps](../examples/lamps/README.md).
- **Single-topic examples** for each feature are in the
  [examples guide](../examples/README.md).

## Related reading

- [Overview](01-overview.md) — the model and the reasons for it
- [High-level architecture](03-high-level-architecture.md) — components, boundaries, and the path of one call
- [API interface spec](09-api-interface-spec.md) — each call above, with its return values and exceptions
- [Data model](10-data-model.md) — the records that these scenarios store
- [Testing strategy](11-testing-strategy.md) — the contract suites that each adapter runs
