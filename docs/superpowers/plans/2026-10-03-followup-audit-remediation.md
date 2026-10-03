# Follow-up audit remediation implementation plan (2026-10-03)

## Goal

Repair every confirmed issue in `docs/audit/2026-10-03-followup-full-readonly-audit.md` while preserving fail-closed migration defaults, signer/KMS preference, explicit local-wallet enablement, and operator approval boundaries. Each module starts with a regression test, is verified independently, and is committed before the next module.

## Safety boundaries

- Migration remains disabled by default; no fix may silently enable it.
- A target becomes active only after restore, health, ownership, and service-start evidence are bound to the task, source epoch, manifest, and target identity.
- Source fencing must use the same admission/write boundary as cognition, HTTP mutation, and wallet side effects. Existing leases must be invalidated or drained before a snapshot is accepted.
- Secrets, private keys, provider API keys, and wallet material stay in credential files/KMS; receipts contain digests and identifiers only.
- Local wallet migration remains opt-in and separately approved. External signer rebind is the default when configured.
- Every failure path must leave a durable, inspectable state and must not report success.

## Module order

### 1. Restore quality-gate confidence (A10/A11) — completed

Root causes: booted test kernels are not explicitly closed before Windows temporary-directory cleanup; the release workflow test still asserts the old `continue-on-error` contract. Add a `SubjectKernel` context-manager/close contract, make the kernel fixture close all created kernels, and update the workflow contract test to require fail-closed artifact download behavior. Run the focused tests, then the full kernel/workflow subset, and commit. Completed in `3bcbc53`; 23 focused tests passed, Ruff, mypy, and diff checks passed.

### 2. Make source ownership and migration fencing one boundary (A01/A03) — completed

Root causes: the installer created `migration/source` as `root:noyra 0750` while the service atomically writes epochs as `noyra`; active database epochs were checked by ownership but did not coordinate admission drain. The installer now gives only the service account access to the private source-epoch directory. The gate now has a process-owned migration-control scope, invalidates normal leases, waits for drain before artifact creation, blocks ordinary mutation while fenced, and clears the in-memory fence only after durable epoch revocation. Rollback remains reachable as an authenticated migration control while ordinary mutations remain denied. Restart recovery stays fail-closed through the durable epoch and filesystem markers. Completed with 20 migration fencing/cutover/service tests, 23 kernel/release contract tests, both migration shell contract tests, Ruff, mypy, compileall, and diff checks passing. No live Linux systemd ownership test was available in the Windows workspace; deployment audit remains an external release gate.

### 3. Make activation perform real target service handoff (A04)

Root cause: target activation writes only `activations/{task}.json`; no service manager consumes it. Add a target activation controller with an explicit restored database path, systemd unit/service identity, readiness probe, and ownership marker. Activation must start or switch the target instance, prove it is serving the requested subject/database and that the source is fenced, then return a signed receipt. Deactivation must stop the target and be idempotent. Test start failure, wrong database, readiness timeout, duplicate activation, rollback, and source/target single-active behavior. Commit.

### 4. Unify migration bundle, credentials, wallet binding, and at-rest proof (A05/A06)

Root causes: the service currently snapshots raw SQLite while proposals claim encrypted backup; `WalletMigration` is not in the executor receipt; `require_encrypted_storage` is metadata only. Define a manifest version that references encrypted backup/config bundles by digest, inject credentials through managed files/KMS, bind external signer rebind evidence, require a second approval for local wallet transfer, and require target volume attestation before receive/restore. Never include secret values in artifacts or audit rows. Test missing keyring, unencrypted target, secret omission, signer mismatch, local-wallet disabled/default, and rollback. Commit.

### 5. Repair management proof UX and capacity/resource behavior (A02/A07/A08)

Root causes: the admin cutover button sends `{}` although the endpoint requires proof; quotas are not negotiated before transfer; hashing reads entire files into memory. Add a server-issued short-lived task-bound cutover ticket and have the UI submit only that ticket, with evidence summaries and stable error states. Add manifest preflight for target quota/free space and stream SHA-256 over bounded chunks, reusing the digest. Test UI request shape, expiry/replay, quota rejection before fencing, large artifacts, and bounded memory behavior. Commit.

### 6. External release gate and final audit (A09)

Keep external evidence separate from local tests. Verify same-SHA signed gates for Ubuntu/systemd, encrypted storage, backup restore, two-host fence/cutover/rollback, signer/KMS, chain reorg/nonce, HTTPS proxy, and soak. Update the audit report only with fresh evidence; leave unknown gates explicitly blocked. Run the full repository quality suite and document any remaining environment-only skips. Commit documentation and code only when the final gates are green.

## Verification commands

- `.venv/Scripts/python.exe -m pytest <focused tests> -q -p no:cacheprovider`
- `.venv/Scripts/python.exe -m ruff check src scripts tests`
- `.venv/Scripts/python.exe -m mypy src tests`
- `.venv/Scripts/python.exe -m compileall -q src scripts`
- `git diff --check`
- POSIX-only shell/deployment tests on a Linux runner before production claims.

## Commit policy

Each module gets one focused commit after its red-green verification. No module is considered complete because a neighboring module happens to pass; the module's root-cause regression tests and relevant quality gates must be fresh.
