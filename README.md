# Genie

Typed building blocks for developers who build their own memory systems: memory
blocks, parent/child ownership, typed relationships, scoped access, and optional
structure views. Genie does not depend on any agent framework, database, memory
taxonomy, retrieval strategy, or stored value type.

## What it provides

- **`MemoryBlock` values** — your application chooses the type, value, metadata, policy, and version.
- **Parent/child ownership** — controls containment, subtree scope, and lifecycle.
- **`MemoryLink` relationships** — directed and typed, and they do not imply ownership.
- **`MemoryHandle` objects** — give agents and services only the access they need.
- **Structure facades** — `OwnershipTree`, `PropertyGraph`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, and `VersionedRecords`.
- **Storage protocols** — `InMemoryBackend` and `SQLiteBackend` need no extra package; `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` are optional; `RoutedBackend` combines several of them.
- **Separate extension points** — policies, hooks, queries, and codecs.

Genie does not run agents, plan their work, create embeddings, host a database,
or define one memory schema for everyone.

## Install

- **Python 3.11 or newer**, with no required runtime dependencies.
- **From a cloned repository**, in a fresh virtual environment:

```bash
python -m venv .venv --prompt genie
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e .
```

- **Optional backend adapters** come from extras: `python -m pip install -e ".[postgres]"`, and the same for `neo4j`, `mongodb`, and `redis`.

## Quick start

```python
from genie import InMemoryBackend, Memory


memory = Memory(
    backend=InMemoryBackend(),
    principal="agent:planner",
)
workspace = memory.put(type="project", value={"name": "Write proposal"})
requirement = memory.put(
    parent=workspace,
    type="requirement",
    value="Keep the proposal concise.",
)

agent_memory = memory.get_handle(root=workspace, principal="agent:planner")
agent_memory.put(type="observation", value="Use short sections.")

results = agent_memory.search("concise")
assert results[0].block.id == requirement.id
```

- **`Principal`** is whoever is acting: a user, an agent, or a service. Genie checks it against each block's `MemoryPolicy` on every operation.
- **Default policy** is `MemoryPolicy.owner_private()`, so only the creator can `read`, `write`, `search`, `traverse_up`, and `traverse_down` a new block.
- **Trust boundary:** keep the full `Memory` object and the backend inside trusted application code. Give agents handles rooted at one block, or tools that you write yourself.

## Storage

- **`InMemoryBackend`** is the reference backend. No extra packages, so it suits tests, examples, and prototypes.
- **`SQLiteBackend`** adds local persistent storage through the driver that ships with Python. You do not need the SQLite command-line program.
- **Optional adapters** need their extra: `postgres`, `neo4j`, `mongodb`, or `redis`.
- **Every persistent adapter** supports all seven structure facades.

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The [backend guide](lib/src/genie/backend/README.md) explains the storage contracts, how
each adapter works, and what to think about in production.

## Model and structures

```text
Memory
  project
    requirement
    observation

MemoryLink
  requirement --supports--> observation
```

- **Ownership** decides scope and lifecycle.
- **Links** describe relationships without ownership.
- **Meaning** of block types, values, metadata, and links is your application's choice.

| Facade | Purpose |
| --- | --- |
| `OwnershipTree` | Rooted ownership hierarchies |
| `PropertyGraph` | Typed relationships and traversal |
| `KeyedCollection` | Namespaced key lookup |
| `EventLog` | Stable append order |
| `TemporalCollection` | Event time and validity periods |
| `LineageDAG` | Derivation history, protected from cycles |
| `VersionedRecords` | Stable identities and ordered revisions |

All facades point at the same blocks. Combine them freely; they keep the normal
handle scope and policy checks.

```python
from genie import EventLog, OwnershipTree
```

- **Every public name imports from `genie`,** and also from its focused
  subpackage, so `from genie.structures import OwnershipTree` reaches the same
  class. Pick whichever reads better in your code.

## Architecture

```text
 Application or agent tools
              |
      Structure facades     optional views over the same blocks
              |
        MemoryHandle        optional root, principal, and capability limits
              |
           Memory           policy, hooks, codecs, and operation flow
              |
        MemoryBackend       blocks, ownership, links, and search
              |
     Stores and indexes     chosen by the backend
```

- **Required layers:** `Memory` and one backend object.
- **Optional layers:** handles and structure facades, which are views over the same blocks. A facade accepts either a `Memory` or a `MemoryHandle`.
- **One backend object** may run several databases or indexes behind the same contract.
- **Keep trusted:** `Memory`, backends, policy engines, hooks, and codec registries. Give agents rooted handles, or smaller tools that you define.

## Learn more

- [Overview](design/01-overview.md)
- [Architecture](design/03-high-level-architecture.md)
- [Memory and handles](lib/src/genie/memory/README.md)
- [Structures](lib/src/genie/structures/README.md)
- [Backends](lib/src/genie/backend/README.md)
- [Queries](lib/src/genie/query/README.md)
- [Policies](lib/src/genie/policy/README.md)
- [Hooks](lib/src/genie/hooks/README.md)
- [Codecs](lib/src/genie/codecs/README.md)
- [Examples](examples/README.md) and [Lamps](examples/lamps/README.md)

## Develop

Install the development extra, then run the checks from the repository root:

```bash
python -m pip install -e ".[dev]"
python -m pytest
python -m ruff check .
python -m compileall -q lib/src/genie lib/tests examples
```

A plain `pytest` run skips the live tests of the server-backed adapters, so
report it as partial. `./lib/scripts/test_full.sh` runs the whole suite against
throwaway containers for those adapters and needs Docker. Extra arguments are
passed on to pytest.

See [CONTRIBUTING.md](CONTRIBUTING.md) for the repository layout, the design
boundaries a change should respect, and how to send one.
[SECURITY.md](SECURITY.md) explains how to report a vulnerability privately and
which parts of the scoped-access model are in scope.

```bash
python lib/benchmarks/run.py
python lib/benchmarks/run.py --workers 4
```

The [benchmark suite](lib/benchmarks/README.md) measures what one operation
costs on each backend, and how much of that cost Genie itself adds over the raw
store call. `--workers` adds a second question: what changes when several
callers share one `Memory`, including the throughput they reach together and
the races a hot block loses. `./lib/scripts/benchmark_full.sh` runs it against
throwaway containers for the server-backed adapters.

## Disclosures

This software may include, incorporate, or access open source software (OSS) components,
datasets and other third party components, including those identified below. The license terms
respectively governing the datasets and third-party components continue to govern those
portions, and you agree to those license terms may limit any distribution, use, and copying.
You may use any OSS components under the terms of their respective licenses, which may
include BSD 3, Apache 2.0, and other licenses. In the event of conflicts between Megagon Labs,
Inc. (“Megagon”) license conditions and the OSS license conditions, the applicable OSS
conditions governing the corresponding OSS components shall prevail.
You agree not to, and are not permitted to, distribute actual datasets used with the OSS
components listed below. You agree and are limited to distribute only links to datasets from
known sources by listing them in the datasets overview table below. You agree that any right to
modify datasets originating from parties other than Megagon are governed by the respective
third party’s license conditions.
You agree that Megagon grants no license as to any of its intellectual property and patent rights.
THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS (INCLUDING
MEGAGON) “AS IS” AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED
TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR
PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE
LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED
AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
(INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE OF THIS
SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE. You agree to cease using,
incorporating, and distributing any part of the provided materials if you do not agree with the
terms or the lack of any warranty herein.
While Megagon makes commercially reasonable efforts to ensure that citations in this
document are complete and accurate, errors may occur. If you see any error or omission, please
help us improve this document by sending information to contact_oss@megagon.ai.