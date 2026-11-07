# Genie

Typed building blocks for developers who build their own memory systems.

Genie gives you memory blocks, parent/child ownership, typed relationships,
scoped access, and optional structure views, with no dependency on an agent
framework, database, memory taxonomy, retrieval strategy, or stored value type.
Each memory system runs and plans its agents, creates embeddings, hosts a
database, and defines a memory schema in its own way, so Genie leaves these
decisions to your application.

## What it provides

- **`MemoryBlock` values** — your application chooses the type, value, metadata, policy, and version.
- **Parent/child ownership** — controls containment, subtree scope, and lifecycle.
- **`MemoryLink` relationships** — directed and typed, and they do not imply ownership.
- **`MemoryHandle` objects** — give agents and services only the access they need.
- **Structure facades** — `OwnershipTree`, `PropertyGraph`, `KeyedCollection`, `EventLog`, `TemporalCollection`, `LineageDAG`, and `VersionedRecords`.
- **Storage protocols** — `InMemoryBackend` and `SQLiteBackend` need no extra package; `PostgresBackend`, `Neo4jBackend`, `MongoDBBackend`, and `RedisBackend` are optional; `RoutedBackend` combines several of them.
- **Separate extension points** — policies, hooks, queries, and codecs.

## Install

- **Python 3.12 or 3.13**, with no required runtime dependencies.
- **From a cloned repository**, in a fresh virtual environment:

```bash
python -m venv .venv --prompt genie
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python -m pip install -e .
```

- **Optional backend adapters** come from extras: `python -m pip install -e ".[postgres]"`, and the same for `neo4j`, `mongodb`, and `redis`.

## Quick start

```python
from genie import AccessDenied, InMemoryBackend, Memory, MemoryHandle, MemoryPolicy

# Scenario: an application runs an LLM agent that writes a proposal.
# The application owns the memory. The agent gets a handle to one project only.

# --- Application side (trusted code) ---
memory = Memory(backend=InMemoryBackend(), principal="app")

# New blocks use `owner_private` by default: only their creator can use them.
# `owner_or` also lets "agent:writer" use the blocks of the proposal project.
writer_access = MemoryPolicy.owner_or(principals={"agent:writer"})
proposal = memory.put(type="project", value={"name": "Write proposal"}, policy=writer_access)
requirement = memory.put(
    parent=proposal,
    type="requirement",
    value="Keep the proposal concise.",
    policy=writer_access,
)
budget = memory.put(type="project", value={"name": "Budget review"})  # owner_private: "app" only


# --- Agent side: it sees only the handle that it gets ---
class Agent:
    def __init__(self, memory: MemoryHandle) -> None:
        self.memory = memory

    # Tools that an LLM can call
    def memorize(self, text: str) -> str:
        self.memory.put(type="observation", value=text)
        return "Saved."

    def recall(self, query: str) -> str:
        results = self.memory.search(query)  # SearchPage, a sequence of SearchResult
        return "\n".join(f"- [{r.block.type}] {r.block.value}" for r in results)


agent = Agent(memory.get_handle(root=proposal, principal="agent:writer"))
print(agent.memorize("Use short sections."))  # Saved.
print(agent.recall("concise"))  # - [requirement] Keep the proposal concise.

# Handle scope: the budget project is outside the handle root.
try:
    agent.memory.get(budget)
except AccessDenied:
    print("Agent reads budget: AccessDenied")

# Policy: the budget project is owner_private, so "agent:writer" cannot get a handle to it.
try:
    memory.get_handle(root=budget, principal="agent:writer")
except AccessDenied:
    print("Handle on budget: AccessDenied")
```

- **Runnable copy:** `python examples/basic/quick_start.py` runs this code. [`examples/expected/basic/quick_start.out`](examples/expected/basic/quick_start.out) shows its output.
- **`Principal`** is whoever is acting: a user, an agent, or a service. Genie checks it against each block's `MemoryPolicy` on every operation.
- **Default policy** is `MemoryPolicy.owner_private()`, so only the creator can `read`, `write`, `search`, `traverse_up`, and `traverse_down` a new block.
- **`MemoryPolicy.owner_or(principals=..., roles=...)`** allows these operations to the creator, the listed principals, and the listed roles.
- **`memory.get_handle(root=..., principal=...)`** returns a `MemoryHandle`. All operations through the handle act as that principal. The handle reaches only its root block and the blocks below the root.
- **Two checks:** a handle operation succeeds only if the block is inside the handle root and the block policy allows the principal. Each check raises `AccessDenied` when it fails.
- **Blocks that the agent creates:** a handle cannot set a policy (`allow_policy_override=False` by default). So a new block gets the `default_policy` of `Memory`, with the agent as its creator. In the example, `app` cannot read the observation. To let `app` read agent blocks, give `Memory` a `default_policy` such as `MemoryPolicy.owner_or(principals={"app"})`.
- **`SearchResult`:** `search` returns a `SearchPage`, a read-only sequence of `SearchResult` values, higher `score` first. Each one has the matching `block`, a `score` that the backend defines, and an optional `reason` text. The `next_cursor` of the page gives the next page, and it is `None` at the end.
- **Trust boundary:** keep the full `Memory` object and the backend inside trusted application code. Give agents handles rooted at one block, or tools that you write yourself.

## Storage

- **`InMemoryBackend`** is the reference backend. No extra packages, so it suits tests, examples, and prototypes.
- **`SQLiteBackend`** adds local persistent storage through the driver that ships with Python. You do not need the SQLite command-line program.
- **Optional adapters** need their extra: `postgres`, `neo4j`, `mongodb`, or `redis`.
- **Every persistent adapter** supports all seven [structure facades](#structure-facades).

```python
from genie import Memory, SQLiteBackend


with SQLiteBackend("memory.db") as backend:
    memory = Memory(backend=backend, principal="agent:planner")
    memory.put(type="note", value={"text": "Persist this"})
```

The [backend guide](lib/src/genie/backend/README.md) explains the storage contracts, how
each adapter works, and what to think about in production.

## Model

The blocks of the Quick start, with one link added:

```text
Ownership (parent/child)
  project "Write proposal"
    requirement "Keep the proposal concise."
    observation "Use short sections."
  project "Budget review"

Relationship (MemoryLink)
  observation --supports--> requirement
```

- **`MemoryBlock`** is one stored value. Each `put` creates one, with an `id`, a `type`, a `value`, and a `MemoryPolicy`.
- **Parent/child ownership** decides scope and lifecycle. Each child has one parent. A handle with a block as its root reaches the blocks below it, and a delete of a block also deletes them.
- **`MemoryLink`** is a directed, typed relationship from a source block to a target block. It does not make the target a child, and it does not give access to the target. Use links for references, provenance, and relationships across the tree.
- **Meaning** of block types, values, metadata, and relation names such as `"supports"` is your application's choice.

```python
observation = agent.memory.search("sections")[0].block  # the block that memorize created
link = agent.memory.add_link(observation, requirement, "supports")
assert agent.memory.list_links(requirement, direction="in") == [link]
```

## Structure facades

- **A structure facade** is an optional class that you build over a `Memory` or a `MemoryHandle`. It organizes blocks that already exist into one shape, such as a graph, a keyed map, or a log. It adds the operations for that shape.
- **References, not copies:** a facade refers to blocks by id. It does not copy or change their values. So one block can be in several facades at the same time.
- **Same access checks:** a facade checks access through its `Memory` or `MemoryHandle`. So the handle scope and the policy checks still apply.
- **Not required:** `Memory` and handles work without facades. Use none, one, or several of them on the same blocks.

| Facade | Organizes blocks as |
| --- | --- |
| `OwnershipTree` | A tree of parents and children below one root block |
| `PropertyGraph` | A graph: blocks are nodes, and `MemoryLink` values are typed edges |
| `KeyedCollection` | A map from namespaced keys to blocks |
| `EventLog` | A list of blocks in the order that you append them |
| `TemporalCollection` | Blocks with an event time and a validity period |
| `LineageDAG` | A derivation history for one relation, with no cycles |
| `VersionedRecords` | One stable identity with an ordered list of revisions |

`PropertyGraph` reads the link from the Model example as a graph edge:

```python
from genie import PropertyGraph


graph = PropertyGraph(agent.memory)
path = graph.walk(observation, relation="supports")[0]
assert path.block_ids == (observation.id, requirement.id)
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

Install the development extra and the 4 adapter extras. Then run the checks from
the repository root. Pyright needs the adapter extras, because it checks the
adapter code against the types of each driver.

```bash
python -m pip install -e ".[dev,mongodb,neo4j,postgres,redis]"
python -m pytest
python -m ruff check .
python -m pyright
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

### Datasets

Genie uses no datasets. The benchmark suite and the examples make their own data.

### Open Source Software (OSS) Components

All OSS components used within the product are listed below (including their copyright holders and the license information).

For OSS components having different portions released under different licenses, please refer to the included Upstream link(s) specified for each of the respective OSS components for identifications of code files released under the identified licenses.

The core package has no third-party runtime dependencies. Each component below is a database driver for one backend adapter. Only the `mongodb`, `neo4j`, `postgres`, or `redis` extra installs it.

| ID  | OSS Component Name | Modified | Copyright Holder | Upstream Link | License  |
|-----|----------------------------------|----------|------------------|-----------------------------------------------------------------------------------------------------------|--------------------|
| 1 | neo4j | No | Neo4j Sweden AB | [link](https://github.com/neo4j/neo4j-python-driver) | Apache Software License (some files: Python Software Foundation License) |
| 2 | psycopg (with psycopg-binary) | No | The Psycopg Team | [link](https://github.com/psycopg/psycopg) | GNU Lesser General Public License v3 (LGPL) |
| 3 | pymongo (includes bson) | No | MongoDB, Inc. | [link](https://github.com/mongodb/mongo-python-driver) | Apache Software License |
| 4 | redis | No | Redis, Inc. | [link](https://github.com/redis/redis-py) | MIT License |