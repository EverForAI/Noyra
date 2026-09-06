# Stage 4-A-6 Wallet Integrity and Diagnostics

Date: 2026-09-01
Branch: codex/candidate-20260822

## Scope

This module hardens the read-only wallet boundary for long-lived histories. It
keeps private-key custody, signing, transaction construction, broadcasting,
transfers, approvals, swaps, and payments out of the wallet surface.

## Completed

- Changed wallet network, asset, address, and balance integrity scans to consume
  SQLite cursors incrementally instead of materializing table histories with
  `fetchall()`.
- Changed lifecycle revision verification to validate one ordered resource at a
  time, retaining only the small resource registry required for relationship and
  audit checks.
- Added `WalletStore.status_summary`, a constant-size aggregate projection of
  resource lifecycle counts and balance-history timestamps.
- Added the wallet projection to `/api/v1/diagnostics` and its unversioned alias;
  it contains no resource identifiers, addresses, balances, RPC origins, claim
  tokens, or lease owners.
- Added an explicit degraded wallet projection when the summary is unavailable,
  without making the remaining diagnostics response unusable.
- Preserved the existing `verify_integrity` counters and all cross-subject,
  lifecycle, audit, state-hash, and append-only failure semantics.

## Verification

- Focused wallet, acquisition, API-contract, and operations regression: 40 passed.
- Service regression: 38 passed.
- Wallet-only independent review: 16 passed; no high- or medium-severity defect.
- Static gates: Ruff check, Ruff format check, Mypy, compileall, JavaScript syntax,
  and `git diff --check` passed.
- Full regression: 1092 passed, 8 skipped, and 251 subtests passed. Six tests in
  `test_m42_p1_12_project_execution_integrity.py` failed because their fixed
  `2026-09-01T00:00:00Z` prediction target had become historical while the suite
  was running. Neither that fixture nor `PredictionStore` is changed by 4-A-6;
  this pre-existing calendar-sensitive baseline requires a separate test-clock
  repair and is not a wallet regression.

## Review conclusion

The stream conversion preserves subject ownership, parent relationships,
lifecycle evidence, revision ordering and hashes, audit evidence, balance
references, and historical counters. The resource registries intentionally
remain proportional to registered networks/assets/addresses because cross-resource
validation requires them; unbounded revision and balance histories are no longer
materialized.

## Next-step rule

Freeze this read-only wallet difference as its own commit. Before another wallet
feature, repair the calendar-sensitive full-suite fixture in a separate commit.
Then select one medium-sized read-only wallet module; do not enter signer material,
transaction construction, nonce/gas policy, broadcasting, transfers, approvals,
or payments.