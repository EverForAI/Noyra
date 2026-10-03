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

### 3. Make activation perform real target service handoff (A04) — local implementation verified; trusted-source proof remains a release blocker

Root cause: target activation writes only `activations/{task}.json`; no service manager consumes it. A target activation controller now uses a fixed Noyra service identity, restored database staging, readiness probe, and root-owned ownership marker. It verifies staged artifact bytes against the manifest digest, task/epoch binding, and source fence before cutover; deactivation is idempotent, task-bound, and restores the prior owner/runtime. Regression tests cover crashes, rollback, subsequent activations, stale deactivation, and database changes while active. Commits: `21c5862`, `735da74`, plus this follow-up. Local focused Python, quality, and shell-contract checks pass. The request still carries the expected artifact digest authenticated with a key readable by the agent process, so a compromised agent can make an untrusted digest assertion. Root-verifiable source authorization or root-owned target attestation key separation is not present yet; migration stays disabled by default and A04 must not be treated as production-ready against a compromised-agent threat until a trusted-source/runner identity channel is designed and verified.

### 4. Unify migration bundle, credentials, wallet binding, and at-rest proof (A05/A06)

Root causes: the service currently snapshots raw SQLite while proposals claim encrypted backup; `WalletMigration` is not in the executor receipt; `require_encrypted_storage` is metadata only. Define a manifest version that references encrypted backup/config bundles by digest, inject credentials through managed files/KMS, bind external signer rebind evidence, require a second approval for local wallet transfer, and require target volume attestation before receive/restore. Never include secret values in artifacts or audit rows. Test missing keyring, unencrypted target, secret omission, signer mismatch, local-wallet disabled/default, and rollback. Commit.

#### Follow-up status (2026-10-03)

The active HTTP/CLI target-agent dispatch now refuses persistent `receive` and
`restore` operations with `recipient-encrypted migration bundle support is
unavailable`. The source executor already refuses before target lookup or
source fencing. Migration proposals now state that execution is blocked while
recipient encryption and wallet/credential binding remain unverified. This
closes the old raw-SQLite/legacy-backup remote path and the misleading
"encrypted backup" claim; it is a fail-closed containment step, **not completion
of A05/A06**. The standalone X25519/HKDF/AES-GCM file envelope helper is tested
for round-trip, context binding, tamper rejection, and source mutation, but is
not wired to an enrolled recipient key, allowlisted database/config package,
target decryption, wallet/signer evidence, or the cutover receipt. The
encrypted-volume checks remain mandatory and non-disableable; live LUKS
validation remains an external gate. Keep this module open until those protocol
contracts have end-to-end tests. Do not remove either source or target
fail-closed barrier in an intermediate commit.

The bundle helper now rejects non-canonical encodings and symlink/reparse-point
destination paths, uses exclusive private temporary files with atomic publish,
and streams source/ciphertext hashing. The resource-only A08 slice is complete
(`fb197ae`, `aa49ae6`); provider and target-agent digest/assembly paths now
stream fixed-size chunks. This does not change the A05/A06 status or reopen the
executor, and A07 quota preflight is still open.

#### Code closure for items 1-3 (2026-10-04)

The remaining code boundary is now implemented. Target registration and
recipient PoP are required before the source executor fences or snapshots a
task. The encrypted bundle path is connected end to end: recipient-encrypted
manifest, bounded chunk transfer, target assembly, recipient decryption,
context/plaintext verification, restore, health, activation, and rollback.
The target now signs a durable task-bound binding record covering credential
references/fingerprints, wallet mode, external signer identity or one-time
local-wallet approval, encrypted-volume evidence, generation, and recipient
fingerprint. The source verifies it and binds its digests to activation and the
execution receipt. Requests containing secret material, mismatched context,
wrong recipient keys, expired or replayed approvals, and binding-record
tampering fail closed; identical binding retries are idempotent.

This is completion of the code planned for items 1-3, with focused and full
migration tests covering the protocol and failure cases. The migration default
remains disabled. Real Linux/LUKS/systemd, two-host, KMS/signer, restart,
rollback, chain, and soak evidence remain external release gates; they are
verification of this code rather than a reason to add placeholder behavior to
the protocol.

### 5. Repair management proof UX and capacity/resource behavior (A02/A07/A08)

Root causes: the admin cutover button sends `{}` although the endpoint requires proof; quotas are not negotiated before transfer; hashing reads entire files into memory. Add a server-issued short-lived task-bound cutover ticket and have the UI submit only that ticket, with evidence summaries and stable error states. Add manifest preflight for target quota/free space and stream SHA-256 over bounded chunks, reusing the digest. Test UI request shape, expiry/replay, quota rejection before fencing, large artifacts, and bounded memory behavior. Commit.

#### Follow-up status (2026-10-03)

The admin UI no longer submits an empty cutover proof: the action is visibly
disabled until recipient-encrypted transfer and target restore evidence are
available (`f1546d5`). The target agent now exposes an authenticated `preflight`
operation that checks the live encrypted-volume probe and incoming file/byte
quota before any artifact is accepted (`b6e4786`). Provider and target-agent
hash/assembly paths stream fixed-size chunks (`fb197ae`, `aa49ae6`). The server
task-bound cutover ticket, recipient-bundle executor wiring, and source-side
quota negotiation remain open; these changes intentionally do not enable
migration.

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
