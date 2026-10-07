# Historical Schema 71 Upgrade Repair

## Production Failure Reproduction

The server runs GitHub commit `4d988c5af19f72e3751755ca7331d5e1a0e1b4cd`
(schema 71), previously upgraded from `f564c5b6a0b62d9a2112592f55fe907d612d7a89`
(schema 63). Its failed schema-80 startup reported structural fingerprint
`f20b36b9f492146624f4795dc10a9184a42b45365cb0fa17c2a1d16dfc969937`.

Running those two complete historical source trees in sequence on Ubuntu 24.04
with SQLite 3.45.1 reproduces that exact fingerprint. Starting from schema 62
(`f094666ae321576fe84448231f847a70bcad9c47`) produces the same result.
A fresh schema-71 installation has a different failing fingerprint, so testing
only a fresh old installation would not cover the server's upgrade history.

## Root Causes

| Defect | Impact | Correction |
| --- | --- | --- |
| `unknown_count` was added to the already-passed schema-69 repair hook. | An existing schema-71 database skips that hook and fails structural validation. | Reconcile provider metric columns within migration 80. |
| Schema-63 wallet policy columns appended by `ALTER TABLE` have a different order from fresh installations. | The complete schema fingerprint differs even though the field definitions match. | Rebuild only the exact recognized historical table layout, copying values by column name. |
| The old full provider bucket hash does not include `unknown_count`. | Legitimate old statistics fail integrity checks after adding the column. | Verify the historical hash before adding the zero unknown count to its hash payload. |

These are upgrade-path defects. The earlier installer, backup ownership and
activation fixes exposed this next blocker; they did not introduce these
database layouts.

## Safety Boundaries

- The expected schema-80 structural and complete DDL fingerprints are unchanged.
- The wallet transformation recognizes the exact historical DDL, including
  constraints and defaults. Unknown columns, constraints, dependent indexes,
  triggers or foreign keys are rejected before rebuilding the table.
- Wallet values, policy versions, recipient lists, pause state and automation
  state are copied unchanged. This change does not activate payments or migration.
- Provider hashes are converted only after authenticating all fields covered by
  the historical full hash and verifying the current counter invariants.
- All three repairs and the evidence counter migration share one transaction.
  The existing verified pre-migration backup also covers subsequent startup
  failure, restoring the old database version and history.
- Existing current-format provider rows remain unchanged. Same-version schema
  drift still fails closed.

## Regression Evidence

Frozen fixtures in `tests/fixtures/historical/manifest.json` record source commits
and compressed/uncompressed SHA-256 checksums. Their data is created by the actual
old identity, event, lifecycle, provider health, model ledger and wallet APIs.
One fixture represents a fresh schema-71 installation; another represents the
schema-63-to-71 upgrade history.

`tests/test_legacy_schema71_upgrade.py` checks the exact production failure
fingerprint, complete repaired schema, retained history and wallet policies,
evidence counter backfill, provider integrity, repeated open, injected failure
rollback, malformed structures, altered statistics and service readiness.
The historical migration suite also exercises these fixtures alongside schemas
6, 16, 21, 27 and 31.

Final fixture and historical migration validation passed 43 tests on Windows
Python 3.12 and 43 tests on Ubuntu 24.04 Python 3.12. Related wallet, provider,
counter, export and deployment regressions also passed. Ruff check/format and
mypy passed for the changed Python sources. Full supported-platform CI remains
required before merging and attempting server acceptance.

## Server Acceptance

Do not delete the existing installation or database. Keep the healthy old release
until the repair has passed required GitHub CI and merged into `main`. Then use
the normal installer upgrade, retaining encrypted backup verification and automatic
rollback. Acceptance still requires checking the actual release pointer, source
commit marker, systemd recovery unit, service logs and both health endpoints.
Local reproduction establishes the code repair; it does not substitute for the
server's final LUKS/systemd acceptance.
