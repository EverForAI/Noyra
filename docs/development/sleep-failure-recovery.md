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
