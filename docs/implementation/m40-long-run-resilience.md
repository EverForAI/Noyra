# M40: Long-Run Resilience

The resilience harness provides an unattended-operation watchdog with timeout,
bounded retry, cancellation preservation, SQLite integrity checks, event and
snapshot hash verification, storage-boundary checks, and a signed-by-hash JSON
report. It records recovery failures without exposing credentials. Deployment
acceptance should run this harness together with injected API failures, network
loss, database lock contention, process restart, exhausted budgets, and full
disk tests. P0/P1 findings remain a release gate.
