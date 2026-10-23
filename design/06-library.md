# Library

This note shows how an application builds its own memory on Genie. It tells
what the library supplies, what the application writes, and how the 2 parts work
together at runtime. The signatures are in
[API interface spec](09-api-interface-spec.md). The packages and extras are in
[Software stack](05-software-stack.md).

## A worked example

A billing support desk keeps its tickets and notes after a restart. The
application puts all Genie calls in one small class, `DeskMemory`. The rest of
the service sees only tickets and notes.

```python
from genie import EventLog, KeyedCollection, Memory, MemoryAddress, MemoryBlock, SQLiteBackend


class DeskMemory:
    """The tickets and notes of one support desk. The class stores values and makes no decisions."""

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
    """Return the block with this id. Create the block if it does not exist."""

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

- **The second run finds the same desk.** `_open` gets each container by a stable id (`desk:billing`, `desk:billing/tickets`, `desk:billing/notes`). It creates a container only if the container does not exist. Genie has no call that lists all roots, so the application keeps the ids that it needs.
- **The stable ids come from application constants.** For ids that come from untrusted input, the [policy guide](../lib/src/genie/policy/README.md#security-guidance) prefers generated `mem_` ids.
- **`DeskMemory` hides all Genie details.** A caller gives a ticket number and gets a `dict`, a `list[str]`, or `None`. Block ids, `MemoryAddress`, and the 2 facades stay inside the class.
- **Ownership and membership are 2 separate steps.** `open_ticket` creates the ticket under the `tickets` container (ownership). Then it binds the ticket number in `KeyedCollection` (membership). Without `parent`, a `scope="subtree"` search with its root at `desk:billing` does not find the ticket.
- **The class makes no decisions.** The caller decides if an open ticket now waits for the customer. `set_status` only stores the answer.
- **The class runs in trusted code.** It holds a `Memory` for `service:helpdesk`. A `Memory` for `agent:triage` on the same file gets `AccessDenied` on `desk:billing`. The cause is that each block keeps the default `owner_private()` policy. An agent does not get the class itself. It gets tools that call `DeskMemory`, for example `lookup_ticket(number)`.
- **The application closes the backend.** The `with` block closes the SQLite connection. `Memory` holds no connection and has no `close`.

## Who owns what

| Concern | Genie | Application |
| --- | --- | --- |
| Ids, versions, timestamps | Generates `mem_` ids, increases `version` on each write, stores UTC times | Can give its own stable `id` to `put` |
| Block types, value shapes, relation names | Stores them as the application gives them | Chooses all of them |
| Tree layout | Parent/child ownership, subtree scope, recursive delete | Decides which block owns which block |
| Access | Checks handle scope and `MemoryPolicy` on each call | Chooses principals, writes policies, gives handles to callers |
| Values | `JsonCodec` by default, and `CodecRegistry` | Adds codecs for other value types, and decides what values mean |
| Storage | Backend contracts and the shipped adapters | Chooses the backend, operates the server, owns backups and capacity |
| Search | `MemoryQuery`, scope rules, a portable candidate scan | The rank order of results, embeddings, or a dedicated `SearchIndex` |
| Stale writes | Raises `MemoryConflict` | Decides if it reads the block again and retries |
| Connections | `close()` on each backend that holds a connection | Opens and closes the backend |
| Agent behavior | None | Task interpretation, plans, extraction, consolidation, rank policy, and action choice |

## How applications are built on it

The examples and lamps use the same 4 layers. Genie needs only the setup code.
The repository uses the other 3 layers to keep Genie details out of agent code.

| Layer | Holds | Trust | In the repository |
| --- | --- | --- | --- |
| Setup code | Backend, `Memory`, policy engine, hooks, codecs | Trusted | The `with SQLiteBackend(...)` block above |
| Application memory class | Facades, container roots, stable ids, value-oriented reads and writes | Trusted | `TaskMemory` in `examples/advanced/agent_memory.py`, and `MemoryStore` in the `mem0` and `langmem` lamps |
| Tools | Named functions with JSON-compatible arguments and results | The agent boundary | `Mem0Tools` in `examples/lamps/mem0/agent.py`, and the `agent.py` of each other lamp |
| Agent or service | Decisions about what to store, read, and do next | Outside the boundary | `TaskAgent.choose_next_action` in `examples/advanced/agent_memory.py` |

- **The memory class returns stored values and relationships.** It does not rank, merge, or choose. `TaskMemory` returns preconditions, recent experiments, and graph neighbors. `TaskAgent` decides what to do with them.
- **A named class outside the memory class holds the decision logic.** `MemoryStore` in the `langmem` lamp does not decide when a memory changes. Its `MemoryManager` makes that decision. `MemoryStore` in the `mem0` lamp does not decide if a fact adds, replaces, or removes a record. `MemoryUpdater` in its `pipeline.py` makes that decision.
- **The memory class takes a `Memory` or a `MemoryHandle`.** The 2 lamp `MemoryStore` classes take a handle with its root at one block. So the class itself cannot reach blocks outside that subtree.
- **The memory class checks capabilities before it writes.** `TaskMemory.create` raises `UnsupportedOperation` if the backend is not both a `KeyedStore` and a `SequenceStore`. It does this check before it creates a container block.
- **Tools are the agent boundary.** An agent that calls tools never holds `Memory`, a backend, or a hook registry. So the agent cannot skip a policy check. The lamps keep tool arguments and results JSON-compatible. So an agent that uses a model can call the same tools.

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

- **Any subset of facades works on the same blocks.** `DeskMemory` uses `KeyedCollection` and `EventLog`. `TaskMemory` uses `OwnershipTree`, `KeyedCollection`, `EventLog`, and `PropertyGraph`.
- **Each shipped adapter supports all 7 facades.** If a facade gets a `RoutedBackend`, the facade binds to the route that owns its container block. `LineageDAG` writes through `Memory`. `RoutedBackend` sends each acyclic link to the store that holds the links.
- **A missing capability fails at facade construction.** The facade constructor raises `UnsupportedOperation`. The failure does not wait for the first write.

### Backend by deployment

| Situation | Backend |
| --- | --- |
| Tests, examples, prototypes | `InMemoryBackend` |
| One process with a local file | `SQLiteBackend` |
| A database server that the application already operates | `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, or `RedisBackend`, each through its extra |
| Different subtrees in different stores | `RoutedBackend` on top of any backend above |

- **A different backend changes only the setup code.** The memory class, the tools, and the agent call the same `Memory` API.
- **Throughput and deployment work are different for each adapter.** [Backends](../lib/src/genie/backend/README.md#small-production-deployments) gives the good use case and the deployment responsibility of each adapter.

## Extending from application code

| Extension point | The application writes | The application gives it through |
| --- | --- | --- |
| Storage | A class with the `MemoryBackend` methods, and any optional capability protocols | `Memory(backend=...)` |
| Access rules | `MemoryPolicy` values made from `AccessRule`, and custom operation names through `PolicyEngine(operations=...)` | `put(policy=...)`, `Memory(default_policy=..., policy_engine=...)`, or a `before_put` hook |
| Observation | A `MemoryHook` subclass | `Memory(hooks=[...])` or a `HookRegistry` |
| Value types | A `MemoryCodec` | `Memory(codecs=CodecRegistry([JsonCodec(), ...]))` |
| Structures | A class that composes the Genie facades | Genie does not receive the class. The class takes a `Memory` or a `MemoryHandle` |

- **Genie has no plugin system.** It has no entry points, registry files, or discovery step. Each extension is an object that the application gives to a constructor.
- **A backend claims a capability when it implements the methods.** The protocols are `runtime_checkable`. So a backend with the `KeyedStore` methods is a `KeyedStore`. It needs no base class and no registration.
- **The contract suites are not in the installed package.** They are in `lib/tests/backends/test_backend_contracts.py`. An application-owned backend uses them from a clone of the repository. Its test class subclasses the contracts for the capabilities that the backend claims. The test class also implements `make_backend()`.
- **A `codecs` registry replaces the default registry.** If the registry does not contain `JsonCodec()`, Genie cannot encode JSON values.
- **A new structure composes facades.** It does not add its own storage. For example, `LineageDAG` wraps `PropertyGraph`, and `VersionedRecords` wraps a keyed collection and an event log.

## Runtime contract

- **Genie is in-process and synchronous.** Each call runs on the thread of the caller, and the hooks run there too. Genie itself starts no thread, timer, or event loop.
- **Genie has no async API.** An `asyncio` application runs Genie calls outside the event loop, for example with `asyncio.to_thread`.
- **Threads can share one `Memory` and one backend.** The concurrency tests and the benchmark workers use this configuration. `SQLiteBackend`, `PostgresBackend`, and `Neo4jBackend` make a second writer wait. `RedisBackend` and `MongoDBBackend` retry the writer that lost.
- **Genie has no global state.** An import of `genie` configures nothing, and the package reads no environment variable. One process can hold 2 `Memory` objects with different principals, policies, or hooks. They work together on one backend or on several backends.
- **Genie reports through hooks, not logs.** The `genie.hooks.registry` logger writes the only log record, at `ERROR` level. It writes it when an observer callback of a hook with `fail_open = True` fails, and the registry contains the failure.
- **A backend that holds a connection closes explicitly.** `SQLiteBackend`, `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` have `close()`, and each one works as a context manager. After `close`, their calls raise `RuntimeError`. `InMemoryBackend` has nothing to close. `RoutedBackend` does not close its routed backends. The application closes each one.
- **Server checks run at construction.** For example, `PostgresBackend`, `MongoDBBackend`, and `RedisBackend` refuse a server that reports that it does not accept writes, for example a replica. Each `verify_*` flag disables one check. On the server adapters, `initialize_schema=False` uses the layout that is already in the store and does not create it.
- **The application finds data again by id.** The application keeps a generated `mem_` id in its own configuration, or it gives a stable id to `put(id=...)`. A second `put` with the same id raises `MemoryConflict`.
- **A search can call the backend more than one time.** `MemoryQuery.limit` counts visible results. If many matching blocks are hidden, `Memory` asks the backend for more pages until it has `limit` visible results. There is no cap.

### Errors a caller handles

- **`except GenieError` catches all Genie failures except `AccessDenied`.** `AccessDenied` is a `PermissionError`, so a caller catches it by name.
- **Setup errors:** `UnsupportedOperation` (a missing extra, a missing capability, or a stored schema that the adapter does not accept), and also `TypeError` and `ValueError` (bad constructor arguments).
- **Errors that a service handles at runtime:** `AccessDenied`, `MemoryNotFound`, `MemoryConflict` (a stale write that Genie never retries), `BackendBusy` (a retry can succeed), `BackendUnavailable`, `BackendReadOnly`, `BackendCapacityExceeded`, and `BackendCorruption` (a stored record that the backend cannot read safely).
- **Each `BackendError` keeps the driver error** as `__cause__`.
- **Full table:** [API interface spec](09-api-interface-spec.md#errors).

## Public surface and stability

- **The public API is the root `__all__`** in `lib/src/genie/__init__.py`. Each name also imports from its subpackage. So `from genie import KeyedCollection` and `from genie.structures import KeyedCollection` give the same class. `lib/tests/test_public_imports.py` checks the 2 import styles.
- **Underscore modules are private.** Genie does not re-export `genie.structures._hooks` and `genie.structures._routing`.
- **The package is typed.** The package contains `py.typed`, so a type checker in the application reads the Genie annotations.
- **The version is `1.0.0`.**
- **The application installs Genie from the repository.** It uses `pip install -e .` from a clone, or `pip install` with a git URL. Adapter drivers come from extras, for example `genie[postgres]`.
- **The module guides are part of the package.** Each subpackage `README.md` is package data. So an installed version contains the guide that matches it.

## Left out on purpose

| Left out | Why |
| --- | --- |
| A ready-made memory class | Taxonomy, layout, and retrieval belong to the application. `DeskMemory` and `TaskMemory` are examples, not library code |
| Agent framework integrations | The core stays independent of how agents run. Integrations are in `examples/`, and they never become core dependencies |
| A default or global `Memory` | The application builds each `Memory` and gives it explicitly. So 2 services or 2 tests never share hidden state |
| Configuration files and environment variables | Each setting is a constructor argument |
| An async API | All calls are synchronous |
| Automatic retries | Genie gives `MemoryConflict` to the caller. The caller knows if it still needs the work |
| A call that lists all roots | The backend contracts have no method that lists all blocks |

## Related reading

- [Overview](01-overview.md) — the model and the design philosophy
- [High-level architecture](03-high-level-architecture.md) — layers, trust boundary, and extension points
- [Software stack](05-software-stack.md) — Python version, extras, and driver version ranges
- [API interface spec](09-api-interface-spec.md) — all signatures, return values, and errors
- [Examples](../examples/README.md) and [Lamps](../examples/lamps/README.md) — runnable application memory classes and tools
- [Backends](../lib/src/genie/backend/README.md), [Policies](../lib/src/genie/policy/README.md), [Hooks](../lib/src/genie/hooks/README.md), [Codecs](../lib/src/genie/codecs/README.md)
