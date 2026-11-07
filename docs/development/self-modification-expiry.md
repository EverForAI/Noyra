# Self-modification proposal expiry

A simulated proposal is based on the setting value observed when it was created.
If an upgrade or configuration change produces a different valid effective value
before application, the proposal is obsolete. Applying the old value or rolling
back the new configuration would misrepresent that evidence.

The apply boundary now reads and verifies the proposal and current setting in
one writer transaction. A drifted proposal reaches the existing `rejected`
terminal state with a private, deterministic `self_modification_expired` event.
That event binds the original simulated-state hash, expected/observed values,
setting revision when present, reason and completion time. Original validation,
simulation and autonomous evidence remain intact. No setting or application
revision is written for expiry, and the scheduler reports
`self_modification_expired` once instead of repeatedly throwing and keeping
the slot occupied. A new proposal may evaluate the new baseline separately.

Concurrent attempts serialize at this boundary: one proposal is applied once
or expired once. An already applied proposal and an already expired proposal
are idempotent reads; rejected unsafe proposals remain ineligible for apply.
A stored effective setting still takes precedence over a changed configuration
baseline. Corrupt proposal/setting data remains an integrity error and cannot
be converted into a benign expiry. Faults while publishing expiry roll back
the terminal state and event together. Restoring the old configuration does
not revive an expired proposal.

The existing database status vocabulary and historical migrations are unchanged.
Expiry is represented by an explained terminal rejection rather than an extra
schema state. Integrity verification validates expiry evidence when present.
No model request, provider retry, setting rollback or operator approval is
introduced by this local lifecycle change.

Completion and event timestamps share the UTC millisecond representation,
including when an injected clock supplies another offset or submillisecond
precision. Receipt reads check the stored payload digest as well as its semantic
binding to the proposal.
