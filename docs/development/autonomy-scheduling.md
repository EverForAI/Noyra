# Autonomous scheduling and acquisition boundaries

Autonomous work retains its existing capability, resource and lease checks. Scheduler
service is an opportunity to consider a workflow, not an obligation to think, publish,
contact somebody, or create a goal. Public reading stays open by default.

## Acquisition cost and evidence

Public URL validation checks credential parameter names and delimited credential words
instead of arbitrary substrings: ordinary `author` and `keyword` queries are usable,
while encoded token, API-key, password and signature parameters remain private.

The public DNS transport passes the remaining connection budget through its bounded
worker into the killable system resolver. Gate waiting does not grant a new DNS budget.
SafeWebReader uses this path by default; custom resolvers remain enclosed by the real
request and connect deadlines. Model request timeouts are a separate configuration.

Feed ingestion keeps the existing per-poll observation limit and lossless fragmentation.
Seen identities are probed by the complete indexed key in small batches, instead of
loading the entire retained cache for an unchanged feed. Cache pruning is needed only
when new identities are written. No observation, fingerprint or historical counter is
rewritten by this optimization.

Acquisition diagnostics explicitly distinguish acknowledged identities (including partial
entry identities), retained deduplication identities, observation batches, and character
budgets. These counters are not complete article counts or daily activity totals.
