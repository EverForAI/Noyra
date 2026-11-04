# Epistemic review evidence and paid recovery

New epistemic model calls bind original input in the model preparation
transaction through a private immutable `epistemic_context_bound` event. The
receipt contains request/context hashes, the original allowed observation IDs
and evidence hashes, and fingerprints of permitted belief/prediction states.
It does not duplicate observation text or prompts.

A successful proposal is recovered locally before checking the allowance for
new model calls. New observations, a changed budget day, or a changed provider
configuration do not substitute different evidence for that result. The
response hash and proposal schema are verified; explicitly reconciled results
may contain only content, without invented billing metadata.

Recovery rechecks observation integrity and private-source availability. Any
proposed belief or prediction change must target an originally supplied,
unchanged state version. The local writer transaction checks evidence and
target versions again, preventing a result from overwriting concurrent work.
A stale target produces a business rejection and retains the paid result.

Legacy successes without an input receipt are accepted only if the current
prompt/schema/options prove the original persisted request hash. Missing
historical input is not reconstructed or silently replaced. An unprovable
legacy result remains held and receives a private local exit receipt. It is
not permission to repay that original request. A different analyzed observation
can become a new review trigger while the original result remains preserved.

A per-subject process lock serializes review workers. Preparation also checks
for a paid result written by another worker after the initial recovery read;
a new request rolls back instead of spending again. Existing lifecycle,
unknown-outcome, active-attempt, and one-shot retry fences still apply. No
unknown attempt or fee reservation is automatically released or replayed.
Old business-run identities and append-only validation remain compatible;
there is no database migration or history rewrite.

Integrity checks validate both the original business records and new input
receipts, including receipts read through encrypted event archives. Detailed
security audits and raw verification output remain outside the public tree.

## Determinate failures and local exits

An exhausted determinate provider failure closes that observation's trigger
through a private `epistemic_round_closed` event. It does not fabricate an
`epistemic_review_runs` proposal, rewrite model success/failure, or change the
observation from analyzed. The observation remains usable as evidence in later
reviews; the next distinct trigger still obeys daily model limits.

Known local admission deferrals such as exhausted budget do not retire the
trigger. They wait at least 60 seconds before another request generation and
remain bounded by daily review/model budgets. Prepared, executing, unknown, and
unclosed one-shot retries cannot be closed by a local exit. Their existing
reconciliation rules and historical fee reservations remain unchanged.

The terminal receipt binds the original call outcomes and request/response
hashes. The executor and metacognitive selector both verify it before excluding
a trigger. A subject-specific event-ID namespace uses the existing primary-key
range index, avoiding a scan of unrelated event history on every selection.
Work still grows with that subject's actual closed rounds; no arbitrary newest-N
cutoff silently makes older failed triggers return. Full integrity verification
also rejects closure records outside the reserved namespace.

The lifecycle and all unresolved calls/attempts are rechecked in the receipt
writer transaction. Corruption or a failed write does not authorize ignoring an
observation or sending a replacement paid request. The original calls, paid
successes, and reservations stay in their ledgers for audit and recovery.
