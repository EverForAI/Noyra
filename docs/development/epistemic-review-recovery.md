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
legacy result remains held; it is not permission for a new paid call.

A per-subject process lock serializes review workers. Existing lifecycle,
unknown-outcome, active-attempt, and one-shot retry fences still apply. No
unknown attempt or fee reservation is automatically released or replayed.
Old business-run identities and append-only validation remain compatible;
there is no database migration or history rewrite.

Integrity checks validate both the original business records and new input
receipts, including receipts read through encrypted event archives. Detailed
security audits and raw verification output remain outside the public tree.
