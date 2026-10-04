# Migration Audit Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the four migration blockers identified in the 2026-10-03 read-only audit so that a migration cannot report completion without real source fencing, authenticated artifact transfer, target restore/health evidence, and a reversible cutover.

**Architecture:** Keep the existing database state machine and target-agent trust model, but add explicit execution ports around it. The service-side coordinator will call a bounded migration executor and will fail closed when no executor is configured; source fencing will be represented by the same runtime admission gate used by cognition and write paths, with a durable fence record for restart recovery. The target agent will receive artifacts through authenticated resumable chunks and will be started by systemd with an isolated restore root and managed backup credential.

**Tech Stack:** Python 3.12, SQLite, `http.server`, Ed25519/HMAC, ChaCha20-Poly1305, systemd, pytest, ruff, mypy, shell tests.

**Spec:** `docs/audit/2026-10-03-full-readonly-audit.md`

## Global Constraints

- Migration remains disabled by default and automatic approval remains disabled unless an operator explicitly enables it.
- No migration request, report, log, or evidence may contain private keys, passwords, API keys, bearer tokens, or wallet secrets.
- Every external operation is bounded by a task ID, source epoch, target ID, manifest digest, timeout, quota, and idempotency key.
- A database `committed` state is valid only after target restore/health and source fence evidence have been durably recorded.
- Any missing executor, missing credential, stale epoch, incomplete transfer, or failed health check must fail closed.
- Existing public and management API contracts remain backward compatible unless a new field is required to prevent a false success.
- Real two-host, signer/KMS, chain, HTTPS, and soak evidence remains an external release gate; tests must not fabricate it.

---

### Task 1: Define the migration execution contract and test the false-success boundary

**Files:**
- Create: `src/noyra/migration/executor.py`
- Modify: `src/noyra/migration/cutover.py`
- Test: `tests/test_migration_executor.py`
- Test: `tests/test_migration_cutover.py`

**Interfaces:**
- Consumes: `MigrationTask`, `TargetHealthReport`, `EpochLease`, and the existing `RuntimeAdmissionGate`.
- Produces: `MigrationExecutionResult`, `MigrationExecutionError`, and a `MigrationExecutor` protocol with `prepare`, `fence_source`, `transfer_and_restore`, `verify_target`, `activate_target`, and `rollback` methods.

- [ ] **Step 1: Write the failing tests**

Add tests that construct a coordinator without an executor and assert `commit()` raises `migration_executor_unavailable` without changing the task or epoch. Add a fake executor test proving a successful result includes task ID, source epoch, target ID, manifest digest, target health digest, and a source fence receipt.

- [ ] **Step 2: Run the focused tests and verify the expected failure**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_executor.py tests/test_migration_cutover.py -q -p no:cacheprovider`

Expected: the new tests fail because the executor contract and coordinator dependency do not exist; existing cutover tests continue to identify the current direct database commit path.

- [ ] **Step 3: Implement the minimal contract**

Define typed immutable receipts and a protocol. Change `CutoverCoordinator` to accept an optional executor. Require a non-null executor before `prepare` can allocate an epoch or `commit` can transition to `committed`; map missing executor and executor errors to stable `ValueError` codes. Keep the existing proof signature verification before invoking the executor.

- [ ] **Step 4: Run focused tests and the migration service tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_executor.py tests/test_migration_cutover.py tests/test_migration_service.py -q -p no:cacheprovider`

Expected: all focused tests pass and an unavailable executor cannot create a committed task or active epoch.

- [ ] **Step 5: Commit the module**

```powershell
git add src/noyra/migration/executor.py src/noyra/migration/cutover.py tests/test_migration_executor.py tests/test_migration_cutover.py
git commit -m "fix: require a real migration executor before cutover"
```

### Task 2: Connect source fencing to runtime admission and restart recovery

**Files:**
- Modify: `src/noyra/core/admission.py`
- Modify: `src/noyra/core/runtime.py`
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/migration/fencing.py`
- Modify: `scripts/noyra-migration-runner.py`
- Modify: `scripts/install-ubuntu.sh`
- Test: `tests/test_migration_fencing.py`
- Test: `tests/test_migration_runner.py`
- Test: `tests/test_service.py`

**Interfaces:**
- Consumes: the executor contract from Task 1 and the existing lifecycle database.
- Produces: durable source-fence state, `RuntimeAdmissionGate.fence_for_migration()`, and restart recovery that opens admission only when no active source fence exists.

- [ ] **Step 1: Write failing fencing tests**

Add a test that creates a runtime, calls the migration fence, then asserts new `admission.begin("active_tick")` calls fail while an existing lease becomes invalid. Add a restart test that persists an active fence and asserts runtime startup remains quarantined/closed. Add a runner test proving `fence` without the runtime-owned source state fails and never writes a success marker.

- [ ] **Step 2: Run tests to confirm the current marker-only behavior fails the contract**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_fencing.py tests/test_migration_runner.py tests/test_service.py -q -p no:cacheprovider`

Expected: new tests fail because the in-memory gate has no durable migration fence and the root runner marker is not consumed by runtime admission.

- [ ] **Step 3: Implement durable fencing at the admission boundary**

Add a small durable fence record keyed by subject and source epoch. `RuntimeAdmissionGate` receives a `fence_check` callback and rejects new work when the durable record is active. Add `fence_for_migration()` to invalidate current leases, drain active operations, and atomically record the fence. Startup reads the record before opening normal admission; rollback clears it only after the source epoch is still current. The runner must call a fixed database-backed fence operation or fail with `source_runtime_not_fenceable`; it must not treat a JSON marker as proof.

- [ ] **Step 4: Wire installer and systemd paths**

Create the fence storage under the existing encrypted data root with root/noyra ownership and `0700`/`0600` permissions. Ensure the runner unit can access only the fixed migration database path. Add shell assertions that the install layout contains the durable fence path and no unbounded request execution path.

- [ ] **Step 5: Run focused tests and static checks**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_fencing.py tests/test_migration_runner.py tests/test_service.py -q -p no:cacheprovider`; `.venv\\Scripts\\python.exe -m ruff check src/noyra scripts tests`; `.venv\\Scripts\\python.exe -m mypy src tests`.

Expected: all pass, and no migration fence success can be produced without changing the same admission boundary used by runtime writes.

- [ ] **Step 6: Commit the module**

```powershell
git add src/noyra/core/admission.py src/noyra/core/runtime.py src/noyra/service.py src/noyra/migration/fencing.py scripts/noyra-migration-runner.py scripts/install-ubuntu.sh tests/test_migration_fencing.py tests/test_migration_runner.py tests/test_service.py
git commit -m "fix: bind migration fencing to runtime admission"
```

### Task 3: Add authenticated resumable artifact transfer and target restore wiring

**Files:**
- Modify: `src/noyra/migration/transfer.py`
- Modify: `src/noyra/migration/agent.py`
- Modify: `scripts/noyra-migration-agent.py`
- Modify: `deploy/systemd/noyra-migration-agent.service`
- Modify: `scripts/install-ubuntu.sh`
- Test: `tests/test_migration_transfer.py`
- Test: `tests/test_migration_agent.py`
- Test: `tests/test_migration_agent_cli.py`
- Test: `tests/shell/test-migration-install.sh`

**Interfaces:**
- Consumes: task-bound manifest, session HMAC, target identity, and managed backup keyring.
- Produces: `/v1/receive/chunk` and `/v1/receive/complete` bounded protocol handlers, an agent `restore_root`, and a real `EncryptedBackupManager` construction path without exposing key material.

- [ ] **Step 1: Write failing transfer and deployment tests**

Add tests for a 2 MB artifact transferred in multiple authenticated chunks, out-of-order or duplicate chunk rejection, resume after an interrupted transfer, final hash mismatch rejection, and quota exhaustion. Add CLI tests proving a received artifact can be restored only when a private restore root and managed backup manager are configured. Add a systemd contract test asserting the unit has `ReadWritePaths` for the isolated restore root and `LoadCredential` for the backup keyring.

- [ ] **Step 2: Run the tests and verify they fail at the current 1 MB JSON boundary**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_transfer.py tests/test_migration_agent.py tests/test_migration_agent_cli.py -q -p no:cacheprovider`; `bash tests/shell/test-migration-install.sh` on POSIX.

Expected: large artifact and deployment wiring tests fail because the current endpoint accepts only one Base64 body and the CLI does not construct a restore manager.

- [ ] **Step 3: Implement the bounded chunk protocol**

Add a task-bound upload session containing artifact ID, manifest digest, expected size, chunk size/count, per-chunk SHA-256, final SHA-256, expiry, and reserved quota. Store chunks under the private incoming root with no symlink traversal; accept only one bounded chunk per request, make an identical retry idempotent, reject conflicting duplicates, and atomically finalize only after all chunks and the final hash match. Keep HMAC timestamp/nonce authentication on every chunk request.

- [ ] **Step 4: Wire restore root and managed backup credentials**

Add explicit CLI arguments/environment names for a private restore root and a credential path. Construct `EncryptedBackupManager` from the managed credential without placing its contents in process arguments, JSON, or logs. Make restore fail closed when encrypted storage, keyring, subject identity, schema, or SQLite quick check is invalid. Start the target service only after the health witness is generated.

- [ ] **Step 5: Run focused tests, shell checks, Ruff, and mypy**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_transfer.py tests/test_migration_agent.py tests/test_migration_agent_cli.py -q -p no:cacheprovider`; `bash tests/shell/test-migration-install.sh`; `.venv\\Scripts\\python.exe -m ruff check src/noyra scripts tests`; `.venv\\Scripts\\python.exe -m mypy src tests`.

Expected: large artifacts complete through chunking, no secret appears in returned reports, and the shipped systemd contract supplies restore dependencies.

- [ ] **Step 6: Commit the module**

```powershell
git add src/noyra/migration/transfer.py src/noyra/migration/agent.py scripts/noyra-migration-agent.py deploy/systemd/noyra-migration-agent.service scripts/install-ubuntu.sh tests/test_migration_transfer.py tests/test_migration_agent.py tests/test_migration_agent_cli.py tests/shell/test-migration-install.sh
git commit -m "fix: support authenticated resumable migration restore"
```

### Task 4: Connect orchestration, rollback, and external release evidence

**Files:**
- Create: `src/noyra/migration/http_executor.py`
- Modify: `src/noyra/migration/executor.py`
- Modify: `src/noyra/migration/cutover.py`
- Modify: `src/noyra/service.py`
- Modify: `scripts/verify_external_gates.py`
- Modify: `.github/workflows/release.yml`
- Test: `tests/test_migration_http_executor.py`
- Test: `tests/test_migration_end_to_end.py`
- Test: `tests/test_release_evidence.py`

**Interfaces:**
- Consumes: source fence, chunk transport, target restore/health witness, target signature, and service lifecycle controls from Tasks 1–3.
- Produces: one idempotent source→transfer→restore→health→fence→activate state machine, rollback that reverses only completed steps, and an explicit external-gate status that cannot be mistaken for local test evidence.

- [ ] **Step 1: Write failing orchestration tests**

Add a fake HTTP target test covering the complete happy path and asserting the final source fence, target health digest, activation receipt, and task/epoch state are committed together. Add interruption tests for transfer failure, health timeout, source fence failure, duplicate commit, stale target signature, and rollback after each completed phase. Add a release test proving a missing or stale external-gates artifact fails the verifier.

- [ ] **Step 2: Run tests to verify current coordinator cannot satisfy them**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_http_executor.py tests/test_migration_end_to_end.py tests/test_release_evidence.py -q -p no:cacheprovider`

Expected: the end-to-end executor tests fail because there is no HTTP client/orchestration implementation.

- [ ] **Step 3: Implement the bounded HTTP executor**

Use the existing bounded transport conventions: HTTPS-only target origin, explicit connect/read timeouts, response-size limits, task/manifest binding, HMAC nonce signing, and no redirects. Run target enrollment/attestation before transfer, send chunks with retry only for idempotent chunk receipts, request restore and health, verify target signature locally, fence the source through the runtime admission boundary, and only then activate the target. Record receipts and digests in the same durable task transition.

- [ ] **Step 4: Implement failure-safe rollback and idempotency**

Persist the last completed phase and receipt digests. On any pre-commit failure, revoke the target epoch, clear the source fence only if the original source epoch is still current, and leave the task retryable with a stable error code. Replaying a completed request returns the existing result; replaying an old epoch or different manifest is rejected.

- [ ] **Step 5: Tighten release evidence checks and workflow diagnostics**

Keep external evidence separate from local tests, require the exact SHA and all gate IDs, and make missing artifact download fail with a direct diagnostic instead of relying on `continue-on-error`. Add a test that verifies the workflow’s required artifact path and verifier inputs without fabricating a passed external gate.

- [ ] **Step 6: Run the migration suite and quality gates**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_migration_*.py tests/test_release_evidence.py -q -p no:cacheprovider`; `.venv\\Scripts\\python.exe -m ruff check src/noyra scripts tests`; `.venv\\Scripts\\python.exe -m mypy src tests`; `.venv\\Scripts\\python.exe -m compileall -q src scripts`; `git diff --check`.

Expected: all local migration and release tests pass. The report must still state that real two-host, KMS, chain, HTTPS, and soak evidence requires the external gate workflow.

- [ ] **Step 7: Commit the module**

```powershell
git add src/noyra/migration/http_executor.py src/noyra/migration/executor.py src/noyra/migration/cutover.py src/noyra/service.py scripts/verify_external_gates.py .github/workflows/release.yml tests/test_migration_http_executor.py tests/test_migration_end_to_end.py tests/test_release_evidence.py
git commit -m "fix: execute migration cutover with verified rollback"
```

### Task 5: Final audit, documentation, and external gate handoff

**Files:**
- Modify: `docs/audit/2026-10-03-full-readonly-audit.md`
- Create: `docs/audit/2026-10-03-migration-release-gate-runbook.md`
- Test: all repository tests and release audit scripts

- [ ] **Step 1: Run the complete local verification set**

Run: `.venv\\Scripts\\python.exe -m pytest -q -p no:cacheprovider`; `.venv\\Scripts\\python.exe -m ruff check .`; `.venv\\Scripts\\python.exe -m ruff format --check .`; `.venv\\Scripts\\python.exe -m mypy src tests`; `.venv\\Scripts\\python.exe -m compileall -q src scripts`; `git diff --check`.

- [ ] **Step 2: Update the audit with evidence and remaining external gates**

Mark only code findings proven by fresh tests as repaired. Add a runbook for Ubuntu/systemd, encrypted volume, backup restore, two-host fence/cutover/rollback, signer/KMS, reorg/nonce, HTTPS proxy, and 24/72-hour soak. Do not invent evidence or mark G01 closed without a signed artifact from the protected workflow.

- [ ] **Step 3: Verify repository state and commit documentation**

Run: `git status --short`; confirm only intended documentation remains uncommitted, then commit the audit/runbook changes with `git diff --check` passing.

```powershell
git add docs/audit/2026-10-03-full-readonly-audit.md docs/audit/2026-10-03-migration-release-gate-runbook.md
git commit -m "docs: record migration remediation and release gates"
```

