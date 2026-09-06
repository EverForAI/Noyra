# M36: Controlled Self-Modification

Noyra may adapt only an explicit allowlist of bounded cognitive parameters. Identity,
experiences, audit records, permissions, provenance, integrity checks, and rollback logic are never
self-modifiable.

Every change follows `proposal -> local validation -> deterministic simulation -> risk scoring ->
apply -> observation -> accept/rollback`. Proposals cite existing events. Only low-risk candidates
inside both hard ranges and relative-delta limits may be applied. Each application and rollback is
append-only and hash-verified. A harmful observation automatically restores the previous value.

This stage deliberately does not permit source-code rewriting, prompt replacement, schema changes,
credential access, or privilege changes.
