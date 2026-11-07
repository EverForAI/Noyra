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


Automatic stagnation candidates preserve their existing integer/float type and
are projected into the setting's current hard envelope before proposal
validation. A projection equal to the current value is no work. This applies
only to generated candidates: explicit `propose()` requests still reject
out-of-range values, protected settings and excessive relative changes.

Automatic evaluations bind the setting, old/new values, selected strategy
profile revision, selected event identities/hashes, effective settings and their
revisions, rule definitions and the bounded deterministic replay history into a
versioned fingerprint. After a verified deterministic rejection, the same
fingerprint yields without rerunning the replay or appending another proposal.
Time passing, a calendar-day change or a restart does not make that input new.
Changed evidence, profile, settings, rules or replay history may be reconsidered
immediately; no new global waiting interval is introduced.

Candidate selection, rejection lookup, simulation and proposal append share one
writer transaction. Concurrent ticks cannot each append the same rejection.
The fingerprint is covered by the existing proposal hash; no schema migration,
historical proposal rewrite, model call, new approval step or permission change
is required. Application, observation, acceptance and rollback keep their
existing contracts.

Older rejections lack a complete evaluation fingerprint. Compatibility handling
reuses one only after validating its original hash and exactly matching the
setting, values, evidence, reason, local validation and a deterministic replay
under current inputs. That bounded compatibility comparison may rerun the local
replay; the old row is never relabeled with an invented fingerprint. Explicit
requests remain explicit and are not silently deduplicated. When the replay
algorithm or automatic evaluation semantics change, update their protocol
version so a previous rejection cannot suppress evaluation under new semantics.
