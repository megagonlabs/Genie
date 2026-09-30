# Library

How an application builds its own memory on Genie: what the library provides,
what the application writes, and how the two meet at runtime. Signatures are in
[API interface spec](09-api-interface-spec.md). Packages and extras are in
[Software stack](05-software-stack.md).

## A worked example

A billing support desk keeps its tickets and notes across restarts. The
application wraps Genie in one small class, `DeskMemory`, and the rest of the
service sees only tickets and notes.

```python
from genie import EventLog, KeyedCollection, Memory, MemoryAddress, MemoryBlock, SQLiteBackend


class DeskMemory:
    """Tickets and notes for one support desk. It stores values and decides nothing."""

    def __init__(self, memory: Memory, desk: str) -> None:
        root = _open(memory, f"desk:{desk}", type="desk")
        tickets = _open(memory, f"desk:{desk}/tickets", type="collection", parent=root)
        notes = _open(memory, f"desk:{desk}/notes", type="log", parent=root)
        self._memory = memory
        self._tickets = KeyedCollection(memory, memory.backend, collection=tickets)
        self._notes = EventLog(memory, memory.backend, log=notes)

    def open_ticket(self, number: str, subject: str) -> None:
        ticket = self._memory.put(parent=self._tickets.collection, type="ticket", value={"subject": subject, "status": "open"})
        self._tickets.bind(MemoryAddress((), number), ticket)

    def ticket(self, number: str) -> dict | None:
        block = self._tickets.get(MemoryAddress((), number))
        return None if block is None else block.value

    def set_status(self, number: str, status: str) -> None:
        self._memory.update(self._tickets.get(MemoryAddress((), number)), value={"status": status})

    def add_note(self, text: str) -> None:
        self._notes.append(self._memory.put(parent=self._notes.log, type="note", value=text))

    def recent_notes(self, limit: int = 3) -> list[str]:
        return [item.block.value for item in self._notes.tail(limit=limit)]


def _open(memory: Memory, block_id: str, *, type: str, parent: MemoryBlock | None = None) -> MemoryBlock:
    """Return the block with this id, and create it on first use."""

    return memory.get(block_id) or memory.put(id=block_id, type=type, value=block_id, parent=parent)
```

```python
with SQLiteBackend("helpdesk.db") as backend:
    desk = DeskMemory(Memory(backend=backend, principal="service:helpdesk"), "billing")
    desk.open_ticket("T-4471", "Refund email never arrived")
    desk.add_note("T-4471: customer asked again from a second address")

with SQLiteBackend("helpdesk.db") as backend:  # a later run of the same service
    desk = DeskMemory(Memory(backend=backend, principal="service:helpdesk"), "billing")
    desk.ticket("T-4471")   # {'status': 'open', 'subject': 'Refund email never arrived'}
    desk.recent_notes()     # ['T-4471: customer asked again from a second address']
    desk.ticket("T-4490")   # None

    if desk.ticket("T-4471")["status"] == "open":  # the caller decides
        desk.set_status("T-4471", "waiting on customer")
```

- **The second run finds the same desk.** `_open` looks up each container by a stable id (`desk:billing`, `desk:billing/tickets`, `desk:billing/notes`) and creates it only on first use. Genie has no call that lists every root, so the application keeps the ids it needs.
- **The stable ids come from application constants.** For ids built from untrusted input, the [policy guide](../lib/src/genie/policy/README.md#security-guidance) prefers generated `mem_` ids.
- **`DeskMemory` hides every Genie detail.** Callers pass a ticket number and get back a `dict`, a `list[str]`, or `None`. Block ids, `MemoryAddress`, and both facades stay inside the class.
- **Ownership and membership are set separately.** `open_ticket` creates the ticket under the `tickets` container (ownership), then binds its number in `KeyedCollection` (membership). Without `parent`, a `scope="subtree"` search rooted at `desk:billing` does not reach the ticket.
- **The class decides nothing.** Whether an open ticket now waits on the customer is the caller's decision. `set_status` only stores the answer.
- **It runs in trusted code.** It holds a `Memory` for `service:helpdesk`. A `Memory` for `agent:triage` over the same file gets `AccessDenied` on `desk:billing`, because every block keeps the default `owner_private()` policy. An agent gets tools that call `DeskMemory`, such as `lookup_ticket(number)`, instead of the class itself.
- **The application closes the backend.** The `with` block closes the SQLite connection. `Memory` holds no connection and has no `close`.

## Who owns what

| Concern | Genie | Application |
| --- | --- | --- |
| Ids, versions, timestamps | Generates `mem_` ids, raises `version` on each write, stores UTC times | May pass its own stable `id` to `put` |
| Block types, value shapes, relation names | Stores them as given | Chooses all of them |
| Tree layout | Parent/child ownership, subtree scope, recursive delete | Decides which block owns which |
| Access | Checks handle scope and `MemoryPolicy` on every call | Picks principals, writes policies, hands out handles |
| Values | `JsonCodec` by default, plus `CodecRegistry` | Adds codecs for other value types, and decides what values mean |
| Storage | Backend contracts and the shipped adapters | Picks the backend, runs the server, owns backups and capacity |
| Search | `MemoryQuery`, scope rules, a portable candidate scan | Ranking, embeddings, or a dedicated `SearchIndex` |
| Stale writes | Raises `MemoryConflict` | Decides whether to read again and retry |
| Connections | `close()` on each backend that holds one | Opens and closes the backend |
| Agent behavior | None | Task interpretation, planning, extraction, consolidation, ranking policy, action choice |

## How applications are built on it

The examples and lamps build in the same four layers. Only setup code is needed
to call Genie. The other three layers are how the repository keeps Genie
details out of agent code.

| Layer | Holds | Trust | In the repository |
| --- | --- | --- | --- |
| Setup code | Backend, `Memory`, policy engine, hooks, codecs | Trusted | The `with SQLiteBackend(...)` block above |
| Application memory class | Facades, container roots, stable ids, value-oriented reads and writes | Trusted | `TaskMemory` in `examples/advanced/agent_memory.py`; `MemoryStore` in the `mem0` and `langmem` lamps |
| Tools | Named functions with JSON-compatible arguments and results | The agent boundary | `Mem0Tools` in `examples/lamps/mem0/agent.py`, and the `agent.py` of every other lamp |
| Agent or service | Decisions about what to store, read, and do next | Outside the boundary | `TaskAgent.choose_next_action` in `examples/advanced/agent_memory.py` |

- **The memory class returns stored values and relationships.** It does not rank, merge, or choose. `TaskMemory` returns preconditions, recent experiments, and graph neighbors, and `TaskAgent` decides what to do with them.
- **The decision logic has a named home outside it.** `MemoryStore` in the `langmem` lamp never decides when a memory changes; its `MemoryManager` does. `MemoryStore` in the `mem0` lamp never decides whether a fact adds, replaces, or removes a record; `MemoryUpdater` in its `pipeline.py` does.
- **It takes a `Memory` or a `MemoryHandle`.** Both lamp `MemoryStore` classes take a handle rooted at one block, so the class itself cannot reach outside that subtree.
- **It checks capabilities before it writes.** `TaskMemory.create` raises `UnsupportedOperation` when the backend is not both a `KeyedStore` and a `SequenceStore`, before it creates any container block.
- **Tools are the agent boundary.** An agent that calls tools never holds `Memory`, a backend, or a hook registry, so it cannot skip a policy check. The lamps keep tool arguments and results JSON-compatible, so an agent backed by a model can call the same tools.

## Choosing the parts

### Structures by question

| The application asks | Facade | Store capability |
| --- | --- | --- |
| What is under this block? | `OwnershipTree` | None |
| What is linked to this, and how? | `PropertyGraph` | None |
| Where did this come from, with no cycles allowed? | `LineageDAG` | `AcyclicLinkStore` |
| Which block has this name? | `KeyedCollection` | `KeyedStore` |
| What happened, in order? | `EventLog` | `SequenceStore` |
| What was true on this date? | `TemporalCollection` | `TemporalStore`, optional `TemporalIndex` |
| What did this record say before? | `VersionedRecords` | `VersionedStore` (`KeyedStore` + `SequenceStore`) |

- **Any subset works over the same blocks.** `DeskMemory` uses `KeyedCollection` and `EventLog`. `TaskMemory` uses `OwnershipTree`, `KeyedCollection`, `EventLog`, and `PropertyGraph`.
- **Every shipped adapter supports all seven facades.** A facade given a `RoutedBackend` binds to the route that owns its container block.
- **A missing capability fails when the facade is constructed,** with `UnsupportedOperation`, not on its first write.

### Backend by deployment

| Situation | Backend |
| --- | --- |
| Tests, examples, prototypes | `InMemoryBackend` |
| One process with a local file | `SQLiteBackend` |
| A database server the application already runs | `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, or `RedisBackend`, each through its extra |
| Different subtrees in different stores | `RoutedBackend` over any of the above |

- **Changing the backend changes setup code only.** The memory class, the tools, and the agent call the same `Memory` API.
- **Throughput and deployment work differ by adapter.** See [Backends](../lib/src/genie/backend/README.md#small-production-deployments) for the good fit and the deployment responsibility of each.

## Extending from application code

| Seam | The application writes | Passed in through |
| --- | --- | --- |
| Storage | A class with the `MemoryBackend` methods, plus any optional capability protocols | `Memory(backend=...)` |
| Access rules | `MemoryPolicy` values built from `AccessRule`; custom operation names through `PolicyEngine(operations=...)` | `put(policy=...)`, `Memory(default_policy=..., policy_engine=...)`, or a `before_put` hook |
| Observation | A `MemoryHook` subclass | `Memory(hooks=[...])` or a `HookRegistry` |
| Value types | A `MemoryCodec` | `Memory(codecs=CodecRegistry([JsonCodec(), ...]))` |
| Structures | A class that composes existing facades | Takes a `Memory` or a `MemoryHandle` |

- **No plugin system.** No entry points, registry files, or discovery step. Every extension is an object the application passes to a constructor.
- **A capability is claimed by implementing it.** The protocols are `runtime_checkable`, so a backend with the `KeyedStore` methods is a `KeyedStore`, with no base class and no registration.
- **The contract suites do not ship.** They live in `lib/tests/backends/test_backend_contracts.py`, outside the installed package. An application-owned backend reuses them from a clone: a test class subclasses the contracts for the capabilities it claims and implements `make_backend()`.
- **A `codecs` registry replaces the default.** Without `JsonCodec()` in it, JSON values stop encoding.
- **A new structure composes facades** instead of adding its own storage, the way `LineageDAG` wraps `PropertyGraph` and `VersionedRecords` wraps a keyed collection and an event log.

## Runtime contract

- **In-process and synchronous.** Every call runs on the calling thread, hooks included. Genie itself starts no thread, timer, or event loop.
- **No async API.** An `asyncio` application runs Genie calls off the event loop, for example with `asyncio.to_thread`.
- **Threads share one `Memory` and one backend.** The concurrency tests and the benchmark workers run that way. `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend` make a second writer wait. `RedisBackend` and `MongoDBBackend` retry the writer that lost instead.
- **No global state.** Importing `genie` configures nothing, and the package reads no environment variable. Two `Memory` objects with different principals, policies, or hooks work side by side, over one backend or several.
- **Genie reports through hooks, not logs.** The only log record comes from the `genie.hooks.registry` logger at `ERROR` level, when an observing callback of a hook with `fail_open = True` fails and the failure is contained.
- **Connection-holding backends close explicitly.** `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` have `close()` and work as context managers. After `close`, their calls raise `RuntimeError`. `InMemoryBackend` has nothing to close, and `RoutedBackend` leaves each routed backend to the application.
- **Server checks run at construction.** For example, `PostgresBackend`, `MongoDBBackend`, and `RedisBackend` refuse a server that reports it does not accept writes, such as a replica. Each `verify_*` flag turns one check off, and on the server adapters `initialize_schema=False` uses an existing layout without creating it.
- **Data is found again by id.** A generated `mem_` id is kept in the application's own configuration, or a stable id is passed to `put(id=...)`. A second `put` with the same id raises `MemoryConflict`.
- **A search page can be short.** `MemoryQuery.limit` is a candidate budget spent before policy filtering. A caller that needs a full page asks for a larger limit and slices the result.

### Errors a caller handles

- **`except GenieError` catches every Genie failure except `AccessDenied`,** which is a `PermissionError` and is caught by name.
- **Setup errors:** `UnsupportedOperation` (a missing extra, a missing capability, or a stored schema the adapter does not accept), plus `TypeError` and `ValueError` (bad constructor arguments).
- **Errors a running service handles:** `AccessDenied`, `MemoryNotFound`, `MemoryConflict` (a stale write that Genie never retries), `BackendBusy` (a retry may succeed), `BackendUnavailable`, `BackendReadOnly`, `BackendCapacityExceeded`, and `BackendCorruption` (a stored record that cannot be read safely).
- **The driver error is kept** as `__cause__` on every `BackendError`.
- **Full table:** [API interface spec](09-api-interface-spec.md#errors).

## Public surface and stability

- **The public API is the root `__all__`** in `lib/src/genie/__init__.py`. Each name also imports from its subpackage, so `from genie import KeyedCollection` and `from genie.structures import KeyedCollection` reach the same class. `lib/tests/test_public_imports.py` checks both styles.
- **Underscore modules are private.** `genie.structures._hooks` and `genie.structures._routing` are not re-exported.
- **Typed.** `py.typed` ships with the package, so a type checker in the application reads Genie's annotations.
- **Version `1.0.0`.**
- **Installed from the repository.** `pip install -e .` from a clone, or `pip install` from a git URL. Adapter drivers come from extras, such as `genie[postgres]`.
- **The module guides ship with the code.** Every subpackage `README.md` is package data, so an installed version carries the guide that matches it.

## Left out on purpose

| Left out | Why |
| --- | --- |
| A ready-made memory class | Taxonomy, layout, and retrieval belong to the application. `DeskMemory` and `TaskMemory` are examples, not library code |
| Agent framework integrations | The core stays independent of how agents run. Integrations belong in `examples/` and never become core dependencies |
| A default or global `Memory` | Every `Memory` is built and passed in explicitly, so two services or two tests never share hidden state |
| Configuration files and environment variables | Every setting is a constructor argument |
| An async API | Every call is synchronous |
| Automatic retries | `MemoryConflict` goes to the caller, which knows whether the work is still wanted |
| A call that lists every root | The backend contracts have no method that lists every block |

## Related reading

- [Overview](01-overview.md) — the model and the design philosophy
- [High-level architecture](03-high-level-architecture.md) — layers, trust boundary, and extension seams
- [Software stack](05-software-stack.md) — Python version, extras, and driver ranges
- [API interface spec](09-api-interface-spec.md) — every signature, return value, and error
- [Examples](../examples/README.md) and [Lamps](../examples/lamps/README.md) — application memory classes and tools that run
- [Backends](../lib/src/genie/backend/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md), [Codecs](../lib/src/genie/codecs/README.md)
