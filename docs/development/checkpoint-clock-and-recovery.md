# Checkpoint clocks and temporary recovery

Checkpoint versions define logical sequence. For new checkpoints, `created_at`
is a nondecreasing UTC high-water time. Equal times are valid. If the observed
wall clock regresses, the write retains that high-water and atomically appends a
private `workflow-clock/v1` event containing the actual observed time, high-water,
and hashes of the original current and previous rows. Normal timestamps remain
unchanged. The workflow store and recovery-budget writer share this append
protocol. There is no schema migration or historical timestamp rewrite.

Full integrity verification retains subject, type, version continuity, state hash
and transition validation. Clock receipts must match the exact rows and expected
high-water calculation. Supported encrypted cold archiving preserves these
receipts. Version order must not be inferred from wall-clock timestamps.

## Existing histories with reversed timestamps

Old inverted timestamps are not automatically accepted. An operator can inspect
`GET /api/v1/admin/workflow-clock?workflow_id=<id>` and, after checking independent
clock/backup evidence, submit exactly `workflow_id`, `expected_fingerprint`,
`evidence_sha256` and `reason` to
`POST /api/v1/admin/workflow-clock/acknowledge`. Unversioned `/api/admin/...`
aliases use the same contract. The evidence digest is an operator attestation to
independently retained evidence; supplying a digest alone does not prove that
an old row was authentic. Do not acknowledge when the cause remains uncertain.

The operation requires operator authentication, CSRF for cookie sessions,
exclusive runtime ownership, durable manual pause and no other active operation.
It is allowed during integrity quarantine specifically to repair clock evidence,
but remains blocked during migration fencing or unavailable protected storage.
It appends private acknowledgements and an audit record in one transaction,
bound to the original complete workflow snapshot. It cannot edit timestamps,
state hashes or versions, waive structural/hash/ownership errors, resume the
runtime, accept an execution, settle an unknown call or release costs.

The catalog bounds a workflow to 50,000 checkpoints and 16 MiB of state JSON, and returns at most 100
unexplained version numbers plus a total/truncation flag. The snapshot covers the
whole workflow. Larger histories require a separately reviewed maintenance
procedure; they are not partially accepted. If several workflows have reversed
times, acknowledge them separately; unresolved workflows continue to fail full
integrity verification. Repeat with the same unchanged snapshot is idempotent.
Run the normal integrity check before any separately authorized resume.

## Temporary recovery admission and exit

Temporary recovery retains the existing subject-wide limits: 6 attempts per
rolling hour, 24 per rolling day, 400,000 reserved input tokens and 80,000 reserved
output tokens per rolling day. These are conservative reservations, not supplier
billing. No new per-lane allowance or refund is introduced. Reservations survive
restart and new incidents. A wall clock earlier than any checked/reserved
high-water prevents new recovery attempts rather than reopening an earlier day.

Health evidence evaluates project coordination and its actual dependencies,
all active source heartbeats, bounded observation backlog and recent model
failures. Recorded budget waits are distinguished from provider failures.
An arbitrary source sample size cannot block exit. Missing current execution
coordination and unacknowledged dependencies still block exit; recognized,
validated waits remain observable waits.

Exit requires an hour of continuous sampled health, with no sample gap greater
than ten minutes. The gap is measured between successful evaluations, not between
persisted heartbeats. Process-local sample evidence is bound to the durable
incident and health fields, so normal five-minute scheduling jitter does not
reset the window when a ten-minute checkpoint is written slightly late. A
restart, a changed health record or a failed clock/database evaluation discards
that local continuity and begins a new sampled window. Checkpoint writes caused
only by model-budget reservations neither count as health samples nor discard
valid observations in the current process. Samples become usable only after
their database transaction commits.

Clock regression, including a regression behind an unpersisted local sample,
clears continuous-health evidence while retaining cost reservations. Forward
jumps beyond the sample-gap bound also begin a new window rather than claiming
unobserved healthy time. The management summary shows each condition,
when it was checked and when an unmet condition began. Model admission time
is not presented as a health evaluation. Health checkpoints are written on
condition changes, clock transitions or at most once per ten-minute heartbeat,
rather than on every normal tick.

## Reset ownership boundary

An operator reset requires the current process lock, a manually paused and
fully drained runtime, and no in-memory or durable migration fence. The control
holds admission and process ownership through the transaction and reopening of
admission. It rechecks the original paused lifecycle version and the existing
migration mutation guard inside the SQLite write transaction, before any reset
edge or audit record. A target that legitimately owns its configured migration
epoch retains the normal target ownership exception. A rejected reset never
changes subject history or reopens source admission; it cannot repair missing
ownership by acquiring the process lock itself.

## Integrity checker failures

The pre-tick integrity checker has its own durable failure count and retry
schedule. Failures use capped exponential backoff, one initial event, one
threshold escalation and one recovery event. Only error types and bounded status
metadata are retained; exception text and private paths are excluded. In-process
retry intervals use a monotonic deadline; restart reconstructs the remaining
wall-clock interval, bounded by the persisted retry duration. Due hints from other
subsystems cannot shorten checker-failure backoff.

Normal integrity quarantine is not a checker crash. A successful checker result
can clear its own error counter without marking cognition as having progressed.
Cancellation propagates. If watchdog persistence or evidence is unavailable,
in-memory retry control remains fail-closed and reports that it is not persisted.
It never converts unavailable evidence into permission to run cognition. The HTTP
summary overlays the main service's current memory snapshot, without an extra
HTTP-side disk read, so a failed watchdog write is not mistaken for a clean
stored history. The database observation cache is not modified by that overlay.

Error, escalation and recovery checkpoints require a fresh diagnostic control
lease, the already-held process lock and the existing migration mutation guard
inside the write transaction. Ownership remains pinned until commit, and the
lease participates in shutdown draining. A lost owner, a migration-fenced source
or a closing runtime keeps only unpersisted in-process diagnostics; it cannot
append to another owner's history. An owner that is merely manually paused or
in integrity quarantine can still record checker health, without reopening
ordinary cognition admission. A legitimate target retains the existing
exception for its own configured migration epoch.

## World polling uses ordered observed evidence

World poll times continue to mean actual observed checks, not checkpoint logical
time. An in-flight wall-clock regression rolls the poll/observation transaction
back atomically; it does not claim a successful check, delete committed evidence
or move a source high-water backwards. Scheduling reports `world_poll_clock_wait`
and yields to other work. Polling resumes after the wall clock catches up.
The checkpoint exception protocol is intentionally not applied as a single-field
clamp to poll success records.
