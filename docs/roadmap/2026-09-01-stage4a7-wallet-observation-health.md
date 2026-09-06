# Stage 4-A-7 Wallet Observation Health

Date: 2026-09-01
Branch: `codex/candidate-20260822`

## Scope

This module adds a constant-size, read-only health projection for durable wallet
balance observations. It does not add private-key custody, signing, transaction
construction, nonce/gas policy, broadcasting, transfers, approvals, swaps,
payments, arbitrary RPC methods, or arbitrary RPC URLs/calldata.

## Contract

`WalletStore.observation_health(subject_id, max_age_seconds=86400, now=None)`
examines active asset/address/network pairs only. The configured age window is
bounded to 30 days (2,592,000 seconds), and the active registry is capped at
100,000 pairs. Exceeding that registry cap fails closed with `IntegrityError`
before history scanning; the implementation counts the bounded cursor rather
than materializing the asset/address cross-product. The result contains exactly:

- `status`, `evaluated_at`, and `stale_after_seconds`;
- `observed_pairs`, `fresh_pairs`, `near_expiry_pairs`, `stale_pairs`,
  `never_pairs`, and `future_pairs`;
- `anomalous_pairs` and `max_age_seconds`.

Times are canonical UTC ISO-8601 strings with millisecond precision. A latest
observation at exactly the evaluation time is fresh. The final quarter of the
window, including exactly 75% and exactly the stale threshold, is near-expiry;
only an age strictly greater than the threshold is stale. Future observations
are counted separately and are excluded from fresh/stale counts. An active pair
without a valid snapshot is `never`, not stale. Invalid persisted timestamps or
relationships fail closed with `IntegrityError`; snapshot relationships are
validated before legitimate revoked history is excluded. Invalid caller
clocks/windows raise a validation error.

History is consumed through one ordered SQLite cursor. Pairs are isolated by
`(subject_id, network_id, asset_id, address_id)`. Adjacent snapshots are compared
newest-first with `snapshot_id DESC` as the equal-time tie-break. A differing
balance at one timestamp is anomalous; otherwise a non-zero change greater than
100% is anomalous, and any zero/non-zero transition is anomalous. Only counts
leave the store.

`WalletStore.status_summary` embeds this projection. The HTTP wallet diagnostics
projection validates a fixed allowlist, counter invariants, canonical timestamps,
and severity (`degraded > attention > ok`) before returning it. Missing, unknown,
malformed, or unavailable observation/acquisition data returns only
`{status: degraded, reason: unavailable}` and never exposes IDs, addresses,
balances, RPC origins, lease owners, or claim tokens.

## Verification

The focused Stage 4-A-7 suite covers empty history, never/fresh/near-expiry/
stale/future boundaries, continuous jumps and normal changes, equal-time
tie-breaking/conflicts, subject/network/lifecycle isolation, malformed clocks,
canonical persisted timestamps and creation times, malformed relationships,
active-pair cap enforcement, concurrent appends, cursor streaming without
`fetchall`, index query plans without temporary sorting, summary integration,
cross-field fail-closed service behavior (including acquisition lease counters),
and redaction.

- Focused wallet, acquisition, RPC, and API-contract regression: 68 passed.
- Full repository regression: 1119 passed, 8 skipped, and 251 subtests passed.
- Static gates passed: `ruff check`, `ruff format --check`, `mypy`,
  `compileall`, `node --check`, `python -m pip check`, and `git diff --check`.

## Next-step rule

Keep subsequent work to one medium-sized read-only wallet module. Do not enter
signer or payment semantics until this module is reviewed, tested, and frozen.
