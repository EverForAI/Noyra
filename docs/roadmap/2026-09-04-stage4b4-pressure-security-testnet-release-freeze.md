# Stage 4-B-4 Wallet Pressure, Security, Testnet, and Release Freeze

Date: 2026-09-04  
Status: local release gates implemented; external testnet and production gates remain open  
Base: `6c6b384 feat: integrate autonomous reward workflows`

## Purpose

Stage 4-B-4 is the final wallet readiness boundary after the reward workflow
integration. It verifies that the autonomous reward path remains bounded under
contention, fails closed under safety controls, preserves append-only evidence,
and does not move signer secrets into the runtime database, API, or exports.

This stage does not claim that a live chain transfer or a production signer has
been validated. Those checks require an independently operated signer and an
external staging environment.

## Local gates

The repeatable entry point is:

```text
# Windows
scripts\\audit-wallet-stage4b4.ps1 --scope targeted --profile smoke

# Ubuntu
scripts/audit-wallet-stage4b4.sh --scope targeted --profile smoke
```

`targeted` covers the release-gate suite plus the high-risk existing contracts:

- concurrent reward-slot acceptance produces one order and one reservation;
- concurrent daily spending limits cannot be exceeded;
- an emergency pause blocks both new reservations and execution of an existing
  reservation without creating an execution row;
- economy integrity and balance projections consume ledger history incrementally;
- signer failures persist only fixed classifications and exports contain no key
  material;
- unknown broadcast, receipt failure, restart recovery, refund, lease expiry,
  acquisition budget, redaction, and RPC response bounds;
- workflow chain reorganization and manual-intervention paths;
- no-signer HTTP behavior and bounded diagnostics.

`--profile pressure` and `--profile soak` increase the synthetic ledger history
used by the streaming test. They are deterministic local checks, not a
substitute for a long-running production soak.

`--scope full` additionally runs the repository test suite, Ruff, mypy,
compileall, `pip check`, and `git diff --check`.

### Local gate acceptance record

Each run is accepted only when all thresholds below are met on one immutable
commit. A non-zero test, lint, type, compile, dependency, or diff-check exit
code is a blocking failure; skipped tests must be listed with their reason.

| Gate | Passing threshold | Required evidence |
| --- | --- | --- |
| Targeted | 100% pass; 0 failures; 0 unexpected skips | `targeted.txt` with command, commit SHA, UTC start/end, exit code, and complete pytest summary |
| Pressure | 100% pass at the repository pressure profile; 0 failures; bounded process RSS and WAL/disk samples recorded | `pressure.txt` plus `pressure-metrics.json` containing profile, history size, peak RSS, peak WAL bytes, and peak database bytes |
| Full | 100% pass; Ruff, mypy, compileall, `pip check`, and `git diff --check` all exit 0 | `full.txt` with each command, version, exit code, and summary |

The execution owner records the evidence under
`artifacts/release/stage4b4/<commit-sha>/<utc-run-id>/`. A second operator
reviews the files and records `review.json` with `executed_by`, `reviewed_by`,
`started_at`, `finished_at`, `window`, and a boolean `approved`. The evidence
directory is immutable after approval; corrections require a new run ID.
Local gate evidence is valid for 72 hours and must be regenerated after any
source, dependency, schema, or configuration change.

The runner rejects staged, unstaged, and untracked changes before executing
tests and checks the clean commit again between commands and at completion.
Dirty-tree runs produce a failed `run.json`, never passing release evidence.
`run.json` records tool versions, final status, errors, and all completed
commands. Each command writes its full stdout/stderr directly to its own log,
with a matching JSON record saved before launch and finalized after exit.
`full.txt` aggregates the completed full-gate logs; a failing command retains
its own log even if aggregation is interrupted. Process termination during a
run leaves `status: running`, which is incomplete and blocks acceptance.
Pressure metrics are persisted before checking thresholds, including on test
failure. Missing RSS/database samples fail the local gate. Samples cover the
current pytest PID and only that run's isolated temporary directory, not old
pytest directories or other applications. Windows sampling includes the venv
launcher's descendants and sums their peak working sets conservatively; sampling
the launcher alone is invalid. Linux sampling uses the target PID's VmHWM.
An actual 64 MiB allocation regression verifies the Python worker is measured.
Windows run directories use the temporary drive's short `ng` root with a unique
random subdirectory. A regression creates and removes the deepest prototype
fixture path to check the MAX_PATH budget. The run record preserves this path
and any cleanup error separately, without replacing the original gate failure.

Smoke and pressure runs use separate run IDs on the same clean commit. Collect
both, then run `--scope full --profile pressure`; review all three directories
together. `review.json` and `external-gates.json` are independent operator
records, not approvals generated by the local runner.

## Schema 60 upgrade boundary

Schema 60 is introduced by the freeze repair candidate and is not yet approved
for production publication. It adds complete policy and journal/entry hashes, including
timestamps, without rewriting published migrations 1-59. New journals and
entries share their creation timestamp and hash every durable identity field.

Old custom policy hashes omitted limits and allow-lists; old ledger hashes
omitted timestamps. A matching old hash is NOT proof of those omitted fields.
Startup therefore rejects these rows by default instead of silently rehashing
them. An unchanged disabled bootstrap is automatically upgraded only when all
its defaults match and its timestamp equals the subject's creation timestamp.
Older migration-generated bootstrap timestamps that cannot be anchored this
way also require explicit operator review. Invalid timestamps always fail.

For a legitimate schema-59 database, stop the runtime and preserve an offline
backup first. The following inspection holds the runtime ownership lock and
reads a consistent, read-only snapshot without migrating or approving it:

```text
.venv\Scripts\python.exe scripts\authorize-wallet-schema60.py PATH_TO_DATABASE
```

Independently review the actual policy limits, allow-lists, modes, versions,
and all ledger timestamps against trusted configuration/backups/receipts.
The printed fingerprint only binds the reviewed database state; it does not
establish historical authenticity. Do not automatically pipe inspection output
into authorization. After review, the responsible operator explicitly runs:

```text
.venv\Scripts\python.exe scripts\authorize-wallet-schema60.py PATH_TO_DATABASE --expected-fingerprint REVIEWED_DIGEST --actor OPERATOR_ID --reason REVIEW_REFERENCE
```

The approval fingerprint binds schema, subject identity, policies, orders,
journals, and entries. Any change to those rows invalidates it. Legacy hashes
and current integrity invariants, including the normal policy input validation,
must still pass. Operator approval does not authorize malformed configuration.
In one migration transaction,
the upgrade installs full hashes, restores append-only guards, records the
operator/reason/fingerprint with `historical_authenticity_proven: false`, and
advances the marker. It never changes policy settings or enables a disabled
wallet. Any failure restores the pre-migration image, including hashes,
indexes, marker and audit state. Approval cannot be replayed on schema 60.
Do not manually change schema markers or invoke this tool against a running
runtime. Interrupted earlier development candidates already marked schema 60
must be restored from their schema-59 backup before this approval workflow;
current-schema integrity damage is never auto-repaired.

## Local implementation evidence

- `WalletEconomyStore.ledger_balances` now consumes ledger rows from a cursor
  instead of materialising the complete entry history.
- `WalletEconomyStore.verify_integrity` validates each journal's two entries
  incrementally and obtains final counts with bounded aggregate queries.
- Concurrent SQLite writers are covered with a barrier-controlled test so the
  policy and reward-slot decisions are checked at the transaction boundary.
- The release suite uses only `MockSigner`; no private key, seed, mnemonic, or
  live RPC credential is accepted by the test boundary.

## Required external gates

These gates must be completed and recorded outside the local deterministic
suite before a production release:

1. Use a disposable staging wallet and an independent signer/KMS deployment.
2. Perform one small native-asset testnet transfer through the fixed transfer
   route, then confirm the receipt from a second observer.
3. Exercise signer timeout, response loss, rejected signing, duplicate request,
   and process restart while preserving nonce and request identity.
4. Verify that the signer service is the only process with key access, that key
   material is absent from logs and backups, and that network egress is limited
   to the approved signer and chain endpoints.
5. Run a 24-hour acquisition and payment soak with bounded queue, WAL, disk,
   and memory telemetry; perform backup restore and rollback rehearsal.
6. Record operator approval for emergency pause, incident resolution, refund,
   and production signer rotation procedures.

### External gate evidence and blocking rules

The release owner stores one signed `external-gates.json` beside the local
evidence. It must contain the staging wallet identifier (redacted), network and
chain ID, signer/KMS deployment identifier, observer identity, UTC test window,
transaction/request identifiers, receipt evidence, timeout/retry outcomes,
backup-restore and rollback results, telemetry summary, and operator approvals.
Secret values, private keys, seeds, mnemonics, and raw signer credentials are
never included. Each gate entry includes `executed_by`, `reviewed_by`,
`started_at`, `finished_at`, `status`, `evidence_refs`, and `failure_reason`.

The following are hard blocking thresholds: the testnet transfer must have a
receipt independently observed on the expected chain; duplicate requests must
not create a second nonce or transfer; signer timeout/response loss must end in
an explicitly classified unknown state; no key material may appear in logs or
backups; the 24-hour soak must remain within the configured queue, WAL, disk,
and memory caps with zero unreconciled incidents; and backup restore plus
rollback must complete with integrity checks passing. Missing, expired, or
unreviewed evidence blocks publication. A failed gate blocks release until a
new run produces complete evidence and receives a fresh review.

## Freeze decision

The local wallet implementation is ready for freeze review when targeted and
full gates pass on the exact source commit. Production publication remains
blocked until the external testnet, signer/KMS isolation, backup/restore,
soak, and operator approval evidence above is attached to the release record.
