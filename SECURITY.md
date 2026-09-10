# Security Policy

## Reporting a vulnerability

**Report privately through GitHub, not in a public issue.** Open the
repository's **Security** tab and choose **Report a vulnerability**. That opens a
private advisory visible only to the maintainers.

Please do not open a public issue, pull request, or discussion for a suspected
vulnerability until it has been addressed.

A useful report includes:

- **What an attacker gains,** such as reading a block they should not reach or
  writing through a handle that should have blocked them.
- **The smallest script that shows it,** ideally against `InMemoryBackend` so it
  needs no server.
- **The Genie version or commit,** the Python version, and the backend adapter
  and driver versions if a server is involved.

Genie is alpha software with no released versions yet, so fixes land on the
default branch. There are no maintenance branches and no backports.

## What is in scope

Genie's security-relevant job is **scoped access**: deciding which `Principal`
may perform which operation on which `MemoryBlock`. Reports about that are in
scope, including:

- **Policy bypass** — an operation succeeding when the block's `MemoryPolicy`
  should have raised `AccessDenied`. The default is
  `MemoryPolicy.owner_private()`, so only the creator can `read`, `write`,
  `search`, `traverse_up`, and `traverse_down` a new block.
- **Handle scope escape** — reaching a block outside a `MemoryHandle` root, or
  performing an operation the handle's capabilities exclude.
- **Leaks through a side channel** — search results, traversal, error messages,
  or hook payloads revealing a block the principal cannot read.
- **Injection in a shipped adapter** — block ids, types, metadata, keys, or
  query filters escaping their parameter and altering a SQL statement, a Cypher
  query, a Mongo command, or a Redis command.
- **Recursive delete removing more than it should,** beyond the target block and
  its descendants.

## What is not in scope

These are design boundaries, not defects. Genie is a library of building blocks,
so the application around it owns the rest.

- **Genie does not authenticate anyone.** The application supplies the
  `Principal`, and Genie trusts it. Proving who the caller is belongs to the
  application.
- **`Memory`, backends, policy engines, hook registries, and codec registries
  are trusted objects.** Hand an agent a `MemoryHandle` rooted at one block, or
  a tool you wrote yourself. Code that already holds the full `Memory` object is
  inside the trust boundary, so reaching further is expected, not a bypass.
- **A block's policy is chosen when the block is created and never changes.**
  That is deliberate. Requests to add a policy mutation path are a feature
  discussion, not a vulnerability.
- **`MemoryQuery.limit` is a backend candidate budget,** applied before policy
  filtering. A page shorter than `limit` is expected behavior.
- **Stored values are the application's concern.** Genie does not inspect,
  validate, or sanitize what you store, and a codec you register runs with the
  privileges of your process.
- **Operational hardening of a database** you point an adapter at, such as its
  authentication, network exposure, and encryption.
- **Third-party driver vulnerabilities.** Report those upstream. Tell us if
  Genie's own use of a driver makes an upstream issue reachable when it otherwise
  would not be.
