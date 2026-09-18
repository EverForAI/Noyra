# Core audit quarantine compatibility review

## Findings

The initial schema-60 patch recognized `wallet_legacy_state_authorized`, but
ordinary wallet registrations and policy updates still failed `core.actions`.
Its test had been reduced from a configured wallet to an empty one, hiding the
second failure. The final test edit also left an unused import. Migration audit
validation accepted actors and reason lengths that `WalletLegacyApproval`
rejects when authorizing a migration.

The same shared-table error affected operator pause/resume/reset/reconciliation
and management HTTP audit events. The HTTP writer uses
`json.dumps(payload, sort_keys=True)` rather than the compact canonical format;
even valid session audit records therefore failed the core JSON check.

These are reproduced code defects. They do not establish that every record in
the cloud database is healthy. The particular reported audit ID has not been
read locally, and its action must not be inferred from the error text alone.

## Boundary and correction

Only `core.actions` audit interpretation changes. Explicit known wallet and
management actions are routed to validators; unknown actions, including unknown
`wallet_*` names, remain P0 errors. No SQL migration, data rewrite, report
deletion, signing change, recipient restriction, or quarantine bypass is needed.

- Migration authorization reuses `WalletLegacyApproval` validation and checks
  exact payload keys, target schema 60, and boolean false for historical
  authenticity. The historical fingerprint is not compared to current wallet
  contents: migration and subsequent operations legitimately change them.
- Wallet audits must reference records belonging to the same subject. Registry
  registration/revocation audits retain the existing exact payload and actor
  validators and must be bound to the resource's audit ID. Policy audit versions
  cannot exceed the current policy, and wallet audits reject the reserved
  `subject` actor. `wallet.state` continues to check payment
  hashes, ledger consistency, acquisition evidence, execution and workflow state.
- Operator records validate reason hashes, payload fields, result types and
  ownership of reconciled actions/calls. Management records validate their known
  payloads and referenced post/inbound records.
- Only known management HTTP actions accept the writer's exact legacy JSON
  encoding in addition to canonical JSON. Duplicate keys and malformed values
  remain invalid. Raw payloads and exception messages are not added to reports.

This is not a complete redesign of historical wallet audit cryptography or
event-chain linkage. The existing domain validators retain their scope; the
patch adds action routing and ownership checks rather than treating every
shared-table row as a core training-policy audit.

## Verification and rollout

Regression tests cover empty/configured schema-59 upgrades, malformed migration
evidence, wallet setup/acquisition/payment and reward settlement using the mock
signer, unknown/orphan/cross-subject audits, wallet state corruption, management
operations and historical HTTP JSON, and persistence of quarantine findings.
The recovery test reproduces the old error, reloads the controller, and verifies
that a successful rerun of `core.actions` clears the finding while retaining the
original audit and failure report.

Validation on the Windows development environment:

- Wallet migration, registry, acquisition, economy, execution, reward workflow,
  release gates, remediation and integration suites: 143 passed.
- Integrity hardening, runtime and operator controls: 138 passed, 2 subtests passed.
- Additional wallet policy/payment evidence cases and management audit suite:
  10 passed (4 wallet cases and 6 management cases).
- `ruff check .`, `mypy src tests` (269 files), repository-wide formatting
  (404 files),
  byte compilation, dependency consistency and `git diff --check` passed.

The full Windows development test run completed with 1,377 passed tests, 22
platform-specific skips and 251 passed subtests. This is not a Linux deployment
validation. Existing formatting failures in
`wallet/config.py`, `wallet/local.py`, `wallet/local_rpc.py`,
`test_service_wallet_modes.py` and `test_wallet_local.py` were corrected with
Ruff. Their Python syntax trees are identical before and after formatting.

Deploy through the normal cloud release installer after validation. Keep the
encrypted backup and existing evidence. A clean standalone read-only diagnostic
does not update the running controller: the controller must rerun `core.actions`
successfully (normally its periodic shard) before this active finding clears.
Other active findings must be resolved independently. HTTP liveness/readiness
200 alone is not proof that quarantine has cleared. Check the integrity summary
before resuming wallet setup; keep the payment policy disabled until the planned
test is ready.

All execution tests here use temporary databases and fake chain responses; no
server deployment or real transfer is performed by this review.
