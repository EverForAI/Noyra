# Sleep failure and recovery

An abnormal sleep exit is a failed run, not a completed reflection or completed
restoration. The protocol can terminate `winding_down`, `reflective_sleep`,
`deep_sleep` or `waking` in one writer transaction. It verifies the owned subject's
sleep history, current version, matching lifecycle and fatigue integrity before
writing a failed terminal state, cancelling that run's cognitive waits, appending
a private deterministic `sleep_failed` receipt and transitioning to `paused`.

The receipt binds the previous phase/version/state hash, terminal state hash,
reason and completion timestamp. Validation checks its payload digest and phase
binding, including after supported event archiving. A missing new-format receipt
is an integrity error. Historical failed runs without this protocol remain
readable. Repeating the accepted original version is idempotent; a stale snapshot
cannot terminate a later run. A generic public lifecycle pause cannot bypass the
sleep protocol.

Failure preserves fatigue, prior reflection integrations, snapshots and model
accounting. It does not fabricate reflection, release unknown reservations,
resend a request or automatically resume active operation. Cancelling a cognitive
wait records that the parent sleep failed; it does not settle its model call.
All phase writes verify the subject and matching lifecycle, so a late reflection
cannot commit after failure. Invalid or damaged evidence remains an integrity
error; recovery is not a way to suppress it.

Sleep and fatigue operations accept a shared clock. Sleep timestamps are
normalized to UTC milliseconds. No schema migration or historical data rewrite
is needed for this protocol.

## Loop advancement and persistent failures

Each sleep tick has a tracked lease, including while its reflection callback
awaits a model. Restarted sleep may obtain a dedicated `sleep_tick` lease while
ordinary cognition admission is closed. The kernel must still own the subject,
the durable lifecycle must still be a sleep phase, and quarantine, migration,
shutdown and epoch fencing remain effective. Completed waking reopens ordinary
admission only while the runtime is still active and the control boundary permits
it. Cancellation releases the lease without manufacturing a reflection failure.

Stage failures are scoped to the sleep ID and exact run version. Private
`sleep_stage_failed` events retain the count across restarts. Existing generic
error backoff and circuit cooldown remain; when the configured consecutive
failure threshold is reached, the run enters the atomic failure protocol before
another callback. If the failure exit itself is interrupted, the next tick can
complete it before waiting for a circuit cooldown. Integrity/ownership/epoch
errors cannot be converted into a benign sleep failure.

An absent optional reflection callback does not block already committed
reflection, deep sleep or waking. A run that actually needs a missing callback
reports `sleep_state_wait` and records bounded failure evidence; persistent
absence reaches `sleep_failed_paused`, without inventing a plan. The existing
model-call cap, validated successful-response recovery and no-change local
fallbacks remain unchanged.

The loop, sleep engine, fatigue tracker, scheduled wake calculation and error
cooldown use the injected timezone-aware clock. Wake deadlines and budget sleep
day changes are evaluated in UTC. A clock rollback cannot cause an early wake;
this does not authorize a budget reset or resolve a general wall-clock anomaly.
