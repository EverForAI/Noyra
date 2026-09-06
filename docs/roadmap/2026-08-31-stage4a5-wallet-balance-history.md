# Stage 4-A-5 Wallet Balance History

Date: 2026-08-31
Branch: codex/candidate-20260822

## Scope

This module adds a read-only, subject-scoped history view for immutable wallet
balance observations. It remains outside private-key custody, signing,
transaction construction, broadcasting, transfers, approvals, swaps, and
payments.

## Completed

- Added `WalletStore.balance_history_page` with bounded keyset pagination over
  `observed_at DESC, snapshot_id DESC`.
- Cursors are URL-safe canonical JSON and bind the subject, all optional
  filters, a high-water SQLite rowid, and the final page key.
- A high-water mark excludes newer or backdated observations inserted while a
  caller is walking an existing history, preventing duplicates and gaps.
- Enforced a maximum page size of 1,000 and a maximum cursor length of 8,192;
  malformed, cross-subject, and filter-mismatched cursors fail closed.
- Added `/api/v1/config/wallet-balance-history` and its unversioned alias with
  operator authentication, strict query validation, and a page-shaped response.
- Added a subject/time keyset index and an idempotent repair path for existing
  schema-54 databases.

## Verification

- Wallet and API contract tests cover cursor stability, concurrent writes,
  subject/filter binding, malformed cursors, page limits, authorization, and
  query rejection.
- The existing wallet migration and integrity contracts remain at schema 54;
  the new index is additive and does not rewrite wallet history.

## Next-step rule

Keep the next change to one medium-sized read-only wallet module. Do not enter
signer material, transaction construction, or payment execution until the
read-only surface is separately reviewed and frozen.
