# Stage 4-A-8 Wallet Observation Health Breakdown

Date: 2026-09-02
Branch: `codex/candidate-20260822`
Base: `3b86df4 fix: harden wallet observation diagnostics`

## Baseline

The working tree was clean before this module. Focused baseline:

- `python -m pytest tests/test_wallet.py tests/test_wallet_observation_health.py tests/test_wallet_acquisition.py tests/test_m42_p3_07_api_contract.py -q`
- Result: **55 passed in 111.14s**.

## Scope

Pending implementation: bounded, subject-scoped read-only balance-observation
health breakdowns by network or latest observation source. This module must not
introduce private-key custody, signing, transaction construction, nonce/gas,
broadcast, transfer, approval, swap, or payment behavior.


## Completed implementation

- Added bounded `WalletStore.observation_health_breakdown` plus explicit
  `observation_health_by_network` and `observation_health_by_source` wrappers.
- The projection is subject-scoped, active-lifecycle-only, and consumes the
  ordered history through one SQLite cursor. It caps active pairs and groups,
  validates persisted relationships/timestamps, and returns aggregate counters
  only. Source grouping uses each pair's newest source while counting all active
  history snapshots; never-observed source pairs use a final `null` bucket.
- Added authenticated `GET /api/v1/config/wallet-observation-health` (and the
  existing unversioned compatibility form), strict query validation, explicit
  200/400/401/404/503 behavior, service projection redaction, route inventory,
  OpenAPI schema, and admin UI controls for network/source views.
- Added focused tests for empty/never states, source/history semantics,
  filters, pagination bounds, subject isolation, malformed projections, HTTP
  errors, and sensitive-value redaction.

## Verification

- Focused wallet/API/UI regression including the new breakdown suite:
  **64 passed**.
- Static gates passed: `ruff check`, `ruff format --check`, `mypy`,
  `compileall`, `node --check src/noyra/web/admin.js`, `python -m pip check`,
  and `git diff --check` (only existing CRLF normalization warnings).

The changes remain uncommitted pending final freeze review.
