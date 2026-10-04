# Post-Remediation Audit Closure Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task with verification and a commit after each module.

**Goal:** Close the remaining code-addressable findings M05-M08 from the 2026-10-02 post-remediation audit and turn M09 external evidence into an executable release gate without fabricating external results.

**Architecture:** Each module owns one invariant at its existing boundary. Retention binds cursors to the exact cutoff and data epoch; migration cutover uses one transaction or a durable reconciliation record; the migration agent reads identity through a verified file descriptor; Windows packaging emits a clear prerequisite diagnostic and remains gated on a standard runner; release evidence is validated as an independently produced same-SHA artifact.

**Tech Stack:** Python 3.12, SQLite/WAL, pytest, Ruff, mypy, PowerShell, shell/systemd, signed JSON evidence.

**Spec:** ``docs/audit/2026-10-02-post-remediation-readonly-audit.md``

## Global Constraints

- Migration remains disabled by default and all cutover proofs remain task-, subject-, epoch-, and policy-bound.
- Unknown outcomes never become successful or ordinary retryable outcomes.
- No secrets are written to manifests, status records, audit events, or evidence artifacts.
- Every module follows failing regression test -> minimal implementation -> focused tests -> broader regression -> diff check -> independent commit.
- External Ubuntu/KMS/RPC/HTTPS/soak results must be recorded as missing until a real environment produces signed evidence.

---

### Task 1: Bind retention cursors to the current cutoff

**Files:**
- Modify: ``src/noyra/core/retention.py``
- Test: ``tests/test_retention.py`` and the nearest retention/integrity regression file

**Invariant:** A cursor can be resumed only when its table, registry version, data epoch, sort-key version, and exact per-table cutoff match the current run. A cutoff change resets scanning to the first key; a previous cursor must never overwrite a newly computed cutoff.

- [x] Add regression tests for bounded batches, time advancement, backfilled expired records, and cutoff changes.
- [x] Include a canonical per-table cutoff map and sort-key version in cursor metadata/hash; reject incompatible metadata.
- [x] Ensure the current cutoff is always written into the run payload and cannot be replaced by a copied prior entry.
- [x] Run retention, storage-pressure, integrity and schema-replay tests; run Ruff and diff checks.
- [x] Commit: ``9091fd2 fix: bind retention cursors to current cutoffs``.

### Task 2: Make migration cutover crash-consistent

**Files:**
- Modify: ``src/noyra/migration/cutover.py``, ``src/noyra/migration/manager.py``, ``src/noyra/migration/fencing.py``
- Test: ``tests/test_migration_cutover.py``, ``tests/test_migration_fencing.py``, migration end-to-end tests

**Invariant:** A successful commit has task=committed and epoch=complete in one durable transition. A process stop or SQLite failure cannot leave an unclassified mixed state; retries are idempotent and concurrent commit/revoke are rejected or reconciled deterministically.

- [x] Add failure-injection and idempotency coverage for task/epoch transition.
- [x] Add a shared transaction API that accepts an existing SQLite connection for task CAS, epoch CAS, audit append and state hash update.
- [x] Change coordinator commit to execute all transition writes under one transaction guarded by the admission lease; preserve the existing proof checks before opening the write transaction.
- [x] The shared transaction removes the mixed-state window; legacy mixed states remain fail-closed and are not silently promoted.
- [x] Test duplicate commit, concurrent commit/revoke and injected interruption; run all migration focused tests.
- [x] Commit: ``8952c46 fix: make migration cutover crash consistent``.

### Task 3: Harden migration identity file loading

**Files:**
- Modify: ``scripts/noyra-migration-agent.py``, installer/systemd identity setup if needed
- Test: ``tests/test_migration_agent.py`` or a new focused identity-security test module

**Invariant:** The agent consumes the exact file it validated. On POSIX the file must be regular, owner-correct, mode-private, single-link and opened with no-follow semantics; contents are read from the descriptor, preventing path replacement between validation and parsing. Windows uses a documented compatible branch and keeps package tests deterministic.

- [x] Add tests for wrong owner, hardlink count >1, symlink, over-broad mode and replacement during load.
- [x] Implement ``os.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)`` where available, ``fstat()`` checks for regular file, expected owner, private mode and ``st_nlink == 1``, then read/parse from the open descriptor.
- [x] Keep a safe Windows fallback that checks regular file and private ACL contract where available; return a stable diagnostic when the platform cannot prove the contract.
- [x] Update installer/systemd setup to create the parent directory and identity file with root:noyra ownership and private permissions.
- [x] Run migration agent, runner, package and shell tests; run Ruff and diff checks.
- [x] Commit: ``253db90 fix: harden migration identity file loading``.

### Task 4: Restore the Windows packaging gate with explicit prerequisites

**Files:**
- Modify: ``scripts/build-windows-package.ps1``, Windows package tests/diagnostics
- Test: ``tests/test_m42_p3_05_windows_package.py`` and a new prerequisite diagnostic test if needed
- Docs: ``docs/release/windows-package-gate.md``

**Invariant:** A standard Windows PowerShell 7 runner can execute the packaging script; a broken host fails early with an actionable prerequisite message, never a misleading package result. No fake pass is accepted.

- [x] Reproduce the current failure with the provided PowerShell runtime and capture the missing ``Microsoft.PowerShell.Management`` module/cmdlet.
- [x] Add a preflight check for ``Resolve-Path``, ``Test-Path``, ``Copy-Item``, ``Get-FileHash`` and the Management module; emit stable exit code 78 and remediation text.
- [x] Keep the package workflow using standard cmdlets; do not replace security checks with weaker custom parsing.
- [ ] Run tests on a standard Windows runner; the current environment remains an external gate failure.
- [x] Commit: ``ad7b476 fix: make Windows package prerequisites explicit``.

### Task 5: Make external release evidence an executable gate

**Files:**
- Inspect/modify: ``.github/workflows/release.yml``, ``scripts/verify_external_gates.py``, ``scripts/build-release-evidence.py``
- Test: release evidence and workflow tests
- Docs: ``docs/release/external-gates.md``

**Invariant:** A release cannot claim M09 closed unless an independently generated, signed, same-commit evidence artifact contains every required gate, reviewer, time window and evidence reference. Missing, stale, unsigned, wrong-SHA, wrong-reviewer and secret-bearing records fail closed.

- [x] Add tests for missing artifact, wrong SHA, stale timestamp, missing required gate, wrong reviewer, invalid signature and secret keys.
- [x] Require explicit gate IDs for Ubuntu/systemd, encrypted volume, backup/restore, two-host migration/fence, signer/KMS, reorg/nonce, HTTPS/proxy and soak.
- [x] Ensure the release job downloads and verifies the artifact rather than generating a pass record in CI.
- [x] Document the exact evidence-producing commands and the state of any still-missing external gate; do not mark unrun gates passed.
- [x] Run verifier, workflow, shell and broad regression tests; commit: ``2f00035 fix: enforce signed external release evidence``.

### Task 6: Final verification and audit update

**Files:**
- Modify: ``docs/audit/2026-10-02-post-remediation-readonly-audit.md``
- All relevant test/tooling surfaces

- [x] Run focused suites after every module and a broad suite at the end (`1732 passed, 24 skipped, 259 subtests passed`).
- [x] Run Ruff check, mypy, compileall, git diff --check and shell syntax tests.
- [x] Update the audit with exact commit IDs and evidence; leave M09 open because real external evidence is unavailable.
- [x] Verify the worktree contains only the intentionally untracked audit and plan documents before their documentation commit.
