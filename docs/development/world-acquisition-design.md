# Bounded world acquisition and intrinsic attention

## Module sequence and safety boundaries

1. M1: RSS/Atom entry-version fingerprints and durable poll heartbeat in one atomic storage boundary.
2. M2: separately reviewed injection classification; retain untrusted-data isolation and goal validation.
3. M3: bounded agenda discovery before metacognitive selection, independent of model invocation.
4. M4: authenticated administration diagnostics; public timeline remains a safe activity summary.

Each module is reviewed and tested before its commit. Model responses and HTTP responses in regression
fixtures are synthetic. Production observations, credentials and diagnostics are not publication inputs.
No historical observations or proposals are rewritten or replayed. No extra feeds, forced goals,
forced searches or paid model calls are introduced by the implementation or its tests.

## M1 acquisition contract

RSS 2.0 and Atom entries have a versioned structured identity and a separate normalized content hash.
GUID and resolved link identify an entry; missing both uses a structured content-based fallback.
Title, summary and body participate in the content revision; channel timestamps and entry ordering do
not. Unicode NFC and whitespace normalization precede hashing. Links remain data, not authorizations.
DTD/entity declarations, excessive XML depth/nodes/items/field size and oversized responses fail closed.
No XML entity resolution or external XML loading is enabled.

One successful poll emits at most one observation containing the unseen entry versions. An empty or
unchanged feed emits none but updates last-success time. Failure updates last-attempt without claiming
success. Selection uses last-attempt, independently of immutable observation timestamps. The exact
observation and seen-set/poll update share a transaction. Seen versions survive restart and are capped
at 4096 per source, evicted by first appearance; a version returning after eviction can be seen again.
This is a bounded horizon, not an unbounded lifetime uniqueness guarantee. Conditional HTTP requests
are not required for correctness and are not introduced by this change.

Schema 108 is an additive, fingerprinted migration. A missing poll table is an incompatible contract,
not a reason to fall back to parallel legacy scheduling. Older runtimes must not open the upgraded
schema. Keep a pre-upgrade backup for rollback, rather than pointing old code at a newer database.

Legacy flattened feed observations do not preserve reliable entry boundaries. No guessed baseline is
inferred from them. The first bounded current feed is processed once with `legacy_unavailable` shown
in diagnostics, preserving genuinely new entries rather than silently suppressing them. This can
reinterpret current old entries once, but it does not replay saved historical observations or goals.
Future fingerprint formats must have a separately reviewed migration policy; no cross-version hash
comparison is valid.

Acceptance: repeated 200 responses, changed channel timestamps, reordering and restart create no new
observations for retained entry versions; new and revised items still enter cognition; unchanged checks
advance scheduling; crashes roll back all ingestion state; existing observation evidence stays intact.
There is no quota requiring a particular number of daily observations or new goals.

## M2 external-text boundary

Classifier version 2 keeps override/impersonation/tool signals and replaces the bare secret-word
match with directed extraction/runtime-file signals. It also recognizes boundary spoofing and
concrete credential-shaped values coupled to outbound URLs. These are conservative heuristics,
not a semantic security proof; legitimate quotations can remain flagged. All extracted fields
are classified together after bounded Unicode/HTML normalization. Removed scripts/comments never
enter model input; arbitrary opaque Base64/hex content is not recursively decoded. No model is used
as a classifier. Observation events record classifier version for future explanation.

The world prompt keeps its system policy separate from source data. Source names, titles, recalled
memory text and memory blocks are explicitly untrusted text fields in context JSON; observation
body is JSON-quoted inside data markers, not interpolated as an instruction block. Existing goal
motive/evidence validation, capability restrictions and per-call budgets are unchanged. Historical
signals and filtered proposals are not reclassified or replayed. Tests use a frozen synthetic
positive/negative control set, title-only and split-field attacks, and a motivated benign-topic
integration case. Expanding sources still requires separate review and controlled deployment.

## M3 independent attention discovery

Before metacognitive selection, discovery scans bounded internal state at most once per five minutes
per process; it invokes no model. Existing stable source identities plus candidate-field comparison
coalesce unchanged relevant state. Do not key on the global subject version: unrelated ticks would
create new items and bypass cooldown. Restart can rescan once; the durable source identity prevents
duplicate insertion. Existing recurrence revisions, no-change streaks, cooldowns and daily call/goal
budgets remain in force.

An absence of current intrinsic goals is a factual, optional reflection question, not an imposed goal
or instruction to search. Waiting and not forming a goal are valid. Its agenda is resolved when the
absence condition clears. The existing optional goal-candidate path is retained, including affect
and evidence validation; no second goal-generation mechanism or forced call each tick is added.
