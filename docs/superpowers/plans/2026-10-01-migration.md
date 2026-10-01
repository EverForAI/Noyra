# Noyra Subject Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a disabled-by-default, operator-governed migration system that can discover trusted registered targets, propose or execute a migration, preserve identity and encrypted state, prevent double-active writers, and support safe wallet and credential handling.

**Architecture:** Add a focused `noyra.migration` domain with policy, trust, proposal, transfer, fencing, and wallet-mode boundaries. Reuse the existing encrypted backup/restore and process-lock primitives, but run privileged host changes through a root-owned systemd runner and a target migration agent. Keep the HTTP layer as an authenticated coordinator and projection, and keep all migration decisions auditable and fail-closed.

**Tech Stack:** Python 3.11+, SQLite append-only migrations, existing `EncryptedBackupManager`, existing process locks and integrity checks, Python standard-library TLS/HTTP, systemd on Ubuntu, native HTML/CSS/JavaScript admin UI, pytest, shell contract tests, Ruff, and mypy.

**Spec:** `docs/superpowers/specs/2026-10-01-migration-design.md`

## Global Constraints

- Migration is disabled by default.
- Enabling migration defaults to `manual` approval.
- `policy_auto` and `emergency_recovery` are explicit independent settings.
- Targets must be registered and nonce-authenticated; no public Internet scanning.
- External signer/KMS rebinding is preferred; local wallet transfer requires separate opt-in.
- Secrets never enter manifests, task status, logs, audit payloads, or ordinary exports.
- Only one subject epoch may accept mutations.
- Every durable mutation uses an idempotency key and append-only audit event.
- Existing native Ubuntu, Docker, Windows, and setup-mode contracts remain compatible.

---

### Task 1: Freeze the public contracts and test fixtures

**Files:**
- Create: `tests/migration_fixtures.py`
- Create: `tests/test_migration_contracts.py`
- Modify: `src/noyra/service_contract.py`
- Modify: `docs/api/openapi.yaml`

**Interfaces:**
- Produces route names, status matrices, error codes, and fixture builders used by later tasks.

- [ ] **Step 1: Write contract tests for default-disabled policy, route inventory, and stable error codes.**

  Add tests that assert the migration routes are versioned, operator-protected, and expose only the documented statuses. Include fixtures for `disabled`, `manual`, `policy_auto`, and `emergency_recovery`.

- [ ] **Step 2: Run the focused contract tests and verify they fail because the routes and domain types do not exist.**

  Run: `python -m pytest tests/test_migration_contracts.py -q`

- [ ] **Step 3: Add the route contracts and OpenAPI operation inventory.**

  Define policy, target, candidate, proposal, approval, task-status, cutover, rollback, and emergency-recovery operations with exact `401`, `400`, `409`, `413`, `415`, `429`, and `503` responses where applicable.

- [ ] **Step 4: Re-run the contract tests and the existing API contract suite.**

  Run: `python -m pytest tests/test_migration_contracts.py tests/test_m42_p3_07_api_contract.py -q`

- [ ] **Step 5: Commit the contract boundary.**

  Run: `git add tests/migration_fixtures.py tests/test_migration_contracts.py src/noyra/service_contract.py docs/api/openapi.yaml; git commit -m "feat: define migration contracts"`

### Task 2: Add schema, settings, and audit primitives

**Files:**
- Create: `src/noyra/migration/types.py`
- Create: `src/noyra/migration/policy.py`
- Modify: `src/noyra/core/database.py`
- Modify: `src/noyra/service.py`
- Modify: `deploy/noyra.env.example`
- Test: `tests/test_migration_policy.py`
- Test: `tests/test_database_migration.py`

**Interfaces:**
- `MigrationPolicy.from_record(row) -> MigrationPolicy`
- `MigrationPolicy.validate() -> None`
- `MigrationPolicy.approval_mode -> Literal["disabled", "manual", "policy_auto", "emergency_recovery"]`
- `MigrationPolicy.rejection_cooldown_seconds: int`
- `MigrationPolicy.wallet_mode -> Literal["external_signer_rebind", "local_wallet_transfer", "disabled"]`
- `MigrationPolicy.revision: int`
- `MigrationStore.read_policy(subject_id) -> MigrationPolicy`
- `MigrationStore.update_policy(subject_id, expected_revision, patch, actor) -> MigrationPolicy`

- [ ] **Step 1: Write tests for safe defaults and bounded policy updates.**

  Assert a fresh database has migration disabled, manual approval is selected when enabled, emergency recovery is off, local wallet transfer is off, cooldown values are bounded, policy revisions are monotonic, and stale `expected_revision` updates return `409` semantics.

- [ ] **Step 2: Run the policy tests and confirm missing tables/types fail.**

  Run: `python -m pytest tests/test_migration_policy.py tests/test_database_migration.py -q`

- [ ] **Step 3: Add append-only schema objects.**

  Add tables for `migration_policies`, `migration_targets`, `migration_proposals`, `migration_tasks`, `migration_epochs`, `migration_rejections`, and `migration_audit_events`. Add unique subject/idempotency indexes and transition guards. Add a schema migration with a verified pre-migration backup using the existing database migration pattern.

- [ ] **Step 4: Implement `MigrationPolicy` validation and store operations.**

  Reject unknown fields, negative limits, unbounded cooldowns, invalid windows, empty target allowlists in auto mode, and local-wallet mode without its explicit enable flag. Store only normalized, non-secret policy data.

- [ ] **Step 5: Add settings defaults and environment documentation.**

  Add `NOYRA_MIGRATION_ENABLED=false`, `NOYRA_MIGRATION_APPROVAL_MODE=manual`, bounded cooldown and expiry defaults, and protected target-registry paths. Keep existing installations compatible by treating absent variables as disabled.

- [ ] **Step 6: Run migration and policy regression tests, then commit.**

  Run: `python -m pytest tests/test_migration_policy.py tests/test_database_migration.py tests/test_wallet.py -q`

  Commit: `git add src/noyra/migration src/noyra/core/database.py src/noyra/service.py deploy/noyra.env.example tests/test_migration_policy.py tests/test_database_migration.py; git commit -m "feat: add migration policy persistence"`

### Task 3: Implement target enrollment and deterministic trust admission

**Files:**
- Create: `src/noyra/migration/trust.py`
- Create: `src/noyra/migration/targets.py`
- Create: `src/noyra/migration/agent_protocol.py`
- Create: `tests/test_migration_trust.py`
- Create: `tests/test_migration_targets.py`

**Interfaces:**
- `TargetRegistration(target_id, public_key, generation, endpoint, capabilities)`
- `TargetChallenge(nonce, expires_at, source_epoch)`
- `TargetAttestation.verify(challenge, signature) -> TrustEvidence`
- `TargetRegistry.register(...) -> TargetRegistration`
- `TargetRegistry.revoke(target_id, reason, actor) -> None`
- `TargetTrustEvaluator.evaluate(candidate, policy) -> TrustDecision`

- [ ] **Step 1: Write tests for registration, nonce freshness, signature mismatch, revocation, and incompatible capabilities.**

  Include rejection of unencrypted storage, stale enrollment generations, incompatible release SHAs, duplicate target IDs, and an already-active target epoch.

- [ ] **Step 2: Run the trust tests and verify the protocol is absent.**

  Run: `python -m pytest tests/test_migration_trust.py tests/test_migration_targets.py -q`

- [ ] **Step 3: Implement bounded enrollment and challenge verification.**

  Use a target public key and one-time nonce. Store fingerprints and generations, never private keys. Bound request sizes, timestamps, capabilities, and resource observations.

- [ ] **Step 4: Implement deterministic trust gates.**

  Require registration, valid signature, freshness, approved region/provider, encrypted target volume, compatible release, enough free space, target-agent health, and no conflicting epoch. Return machine-readable failure reasons.

- [ ] **Step 5: Run focused tests and commit.**

  Run: `python -m pytest tests/test_migration_trust.py tests/test_migration_targets.py -q`

  Commit: `git add src/noyra/migration/trust.py src/noyra/migration/targets.py src/noyra/migration/agent_protocol.py tests/test_migration_trust.py tests/test_migration_targets.py; git commit -m "feat: enroll and verify migration targets"`

### Task 4: Add resource discovery and cognitive proposal evaluation

**Files:**
- Create: `src/noyra/migration/discovery.py`
- Create: `src/noyra/migration/proposals.py`
- Create: `tests/test_migration_discovery.py`
- Create: `tests/test_migration_proposals.py`

**Interfaces:**
- `ResourceObservation(target_id, observed_at, capacity, latency, cost, region, evidence_hash)`
- `MigrationNeed(source_health, workload, storage_pressure, provider_health) -> NeedAssessment`
- `MigrationProposalBuilder.build(source, candidate, need, trust, policy) -> MigrationProposal | None`
- `MigrationProposalStore.reject(proposal_id, reason, cooldown_seconds, actor) -> None`
- `MigrationProposalStore.next_eligible_at(subject_id, target_id, reason_code) -> datetime | None`

- [ ] **Step 1: Write tests proving resource availability alone never creates a proposal.**

  Add cases for resource-only, need-only, untrusted-target, cooldown-active, payment-in-flight, and maintenance-window failures. Add a passing case with all hard gates and a positive benefit decision.

- [ ] **Step 2: Implement registered-target discovery.**

  Read resource observations only from registered targets and explicit provider adapters. Do not add public Internet scanning, arbitrary SSH, or model-controlled credentials.

- [ ] **Step 3: Implement the need/benefit/risk evaluator.**

  Keep model output explanatory and bounded. The returned proposal must include reason code, evidence hashes, benefit score, risk score, hard-gate results, estimated downtime, data/key plan, expiry, and rollback plan.

- [ ] **Step 4: Implement rejection cooldowns.**

  Enforce cooldown by target and reason category, independent of proposal text or idempotency key. Record denial reason and policy revision in append-only audit data.

- [ ] **Step 5: Run focused proposal tests and commit.**

  Run: `python -m pytest tests/test_migration_discovery.py tests/test_migration_proposals.py -q`

  Commit: `git add src/noyra/migration/discovery.py src/noyra/migration/proposals.py tests/test_migration_discovery.py tests/test_migration_proposals.py; git commit -m "feat: evaluate trusted migration proposals"`

### Task 5: Build the migration task state machine and encrypted transfer protocol

**Files:**
- Create: `src/noyra/migration/manager.py`
- Create: `src/noyra/migration/transfer.py`
- Create: `tests/test_migration_manager.py`
- Create: `tests/test_migration_transfer.py`

**Interfaces:**
- `MigrationManager.create_proposal(...) -> MigrationProposal`
- `MigrationManager.approve(proposal_id, approval) -> MigrationTask`
- `MigrationManager.start(task_id) -> MigrationTask`
- `MigrationManager.cancel(task_id, reason) -> MigrationTask`
- `MigrationManager.status(task_id) -> MigrationTaskStatus`
- `TransferSession.send(artifact, target) -> TransferReceipt`
- `TransferSession.resume(receipt) -> TransferReceipt`
- `TransferSession.verify(receipt) -> None`

- [ ] **Step 1: Write state-transition tests.**

  Cover manual approval, policy auto approval, expiry, stale policy revision, duplicate idempotency, cancellation, restart recovery, and invalid transitions. Assert every transition appends an audit event.

- [ ] **Step 2: Write transfer tests.**

  Cover chunk hashing, interruption/resume, duplicate chunks, corrupted chunks, oversized manifests, path traversal, symlinks, and missing backup-key material.

- [ ] **Step 3: Implement the task state machine.**

  Enforce the state graph in one domain module. Store bounded redacted logs and stable error codes. Never store request bodies containing credentials.

- [ ] **Step 4: Reuse the existing encrypted backup manager through a privileged boundary.**

  Create a migration artifact containing the encrypted data backup, manifest digest, schema version, identity hash, event-chain tip, release SHA, and key references. Keep backup keys external and never include them in the artifact.

- [ ] **Step 5: Implement resumable authenticated transfer.**

  Use a mutually authenticated target channel and fixed-size chunks with per-chunk hashes plus a final artifact hash. Discard incomplete or invalid artifacts.

- [ ] **Step 6: Run manager, transfer, backup, restore, and integrity tests, then commit.**

  Run: `python -m pytest tests/test_migration_manager.py tests/test_migration_transfer.py tests/test_gate1_archive_keyring.py tests/test_m42_p2_14_at_rest.py -q`

  Commit: `git add src/noyra/migration/manager.py src/noyra/migration/transfer.py tests/test_migration_manager.py tests/test_migration_transfer.py; git commit -m "feat: add encrypted migration tasks"`

### Task 6: Add single-active fencing and cutover/rollback

**Files:**
- Create: `src/noyra/migration/fencing.py`
- Create: `src/noyra/migration/cutover.py`
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/core/operator_controls.py`
- Create: `tests/test_migration_fencing.py`
- Create: `tests/test_migration_cutover.py`

**Interfaces:**
- `EpochLease.acquire(target_id, expected_source_epoch) -> EpochLease`
- `EpochLease.assert_current() -> None`
- `EpochLease.revoke(reason, actor) -> None`
- `CutoverCoordinator.prepare(task_id) -> CutoverPlan`
- `CutoverCoordinator.commit(task_id) -> MigrationTask`
- `CutoverCoordinator.rollback(task_id, reason) -> MigrationTask`

- [ ] **Step 1: Write tests for stale epochs and duplicate active instances.**

  Assert an old source rejects mutation after a new epoch is acquired, a target cannot acquire a second active lease, and a restarted source cannot regain authority without an explicit newer epoch.

- [ ] **Step 2: Implement durable epoch leases and mutation checks.**

  Include epoch IDs in runtime state and mutation admission. Fail closed when the lease is missing, expired, revoked, or owned by another target.

- [ ] **Step 3: Implement cutover preparation, commit, and rollback.**

  Pause cognition, search, communications, exports, and wallet execution before final backup. Require target validation before epoch acquisition. Keep the source read-only during the rollback window.

- [ ] **Step 4: Integrate fencing with service admission and operator controls.**

  Expose clear health states such as `migration_preparing`, `migration_cutover`, `migration_read_only`, and `migration_rollback`. Ensure emergency pause remains available.

- [ ] **Step 5: Run fault-injection tests and commit.**

  Run: `python -m pytest tests/test_migration_fencing.py tests/test_migration_cutover.py tests/test_m42_p3_06_operator_controls.py -q`

  Commit: `git add src/noyra/migration/fencing.py src/noyra/migration/cutover.py src/noyra/service.py src/noyra/core/operator_controls.py tests/test_migration_fencing.py tests/test_migration_cutover.py; git commit -m "feat: fence migration cutover"`

### Task 7: Implement the target migration agent and privileged systemd runner

**Files:**
- Create: `src/noyra/migration/agent.py`
- Create: `scripts/noyra-migration-agent.py`
- Create: `scripts/noyra-migration-runner.sh`
- Create: `deploy/systemd/noyra-migration-agent.service`
- Create: `deploy/systemd/noyra-migration-runner.service`
- Modify: `scripts/install-ubuntu.sh`
- Modify: `scripts/audit-deployment.sh`
- Create: `tests/test_migration_agent.py`
- Create: `tests/shell/test-migration-runner.sh`

**Interfaces:**
- `MigrationAgent.enroll(request) -> EnrollmentReceipt`
- `MigrationAgent.challenge(request) -> TargetAttestation`
- `MigrationAgent.receive(artifact_manifest) -> ReceiveReceipt`
- `MigrationAgent.restore(receipt) -> RestoreReport`
- `MigrationAgent.validate(report) -> TargetHealthReport`

- [ ] **Step 1: Write agent protocol and shell-boundary tests.**

  Assert root ownership, private directories, no environment-file secret inheritance, bounded request parsing, no symlink traversal, and no arbitrary command execution from migration input.

- [ ] **Step 2: Implement the target agent with mutual authentication.**

  Restrict the agent to enrollment, challenge, receive, restore, health, and lease operations. Use a private target data root and refuse unencrypted storage when production at-rest policy is required.

- [ ] **Step 3: Implement the root-owned runner.**

  The runner stops or fences the source, invokes the existing backup/restore primitives, writes redacted status, verifies health, and performs no work from unvalidated user-provided paths.

- [ ] **Step 4: Install and audit systemd units.**

  Apply `NoNewPrivileges`, private temporary storage, restrictive paths, `UMask=0077`, bounded restart behavior, and explicit read/write paths. Add installer rollback for failed unit publication.

- [ ] **Step 5: Run shell, deployment, and agent tests, then commit.**

  Run: `bash tests/shell/test-migration-runner.sh; bash scripts/audit-deployment.sh`

  Commit: `git add src/noyra/migration/agent.py scripts/noyra-migration-agent.py scripts/noyra-migration-runner.sh deploy/systemd/noyra-migration-agent.service deploy/systemd/noyra-migration-runner.service scripts/install-ubuntu.sh scripts/audit-deployment.sh tests/test_migration_agent.py tests/shell/test-migration-runner.sh; git commit -m "feat: add migration target agent"`

### Task 8: Add wallet and credential migration modes

**Files:**
- Create: `src/noyra/migration/credentials.py`
- Create: `src/noyra/migration/wallet.py`
- Modify: `src/noyra/wallet/config.py`
- Modify: `src/noyra/wallet/execution.py`
- Create: `tests/test_migration_credentials.py`
- Create: `tests/test_migration_wallet.py`

**Interfaces:**
- `CredentialRebinder.plan(source, target) -> CredentialBindingPlan`
- `CredentialRebinder.apply(plan) -> CredentialBindingReceipt`
- `WalletMigration.plan(mode, source, target) -> WalletMigrationPlan`
- `WalletMigration.apply_external_signer(plan) -> WalletBindingReceipt`
- `WalletMigration.apply_local_transfer(plan, one_time_approval) -> WalletBindingReceipt`

- [ ] **Step 1: Write tests proving secrets are absent from artifacts and status.**

  Include model, search, tunnel, operator, export, archive, signer, and local-wallet values. Assert only references, fingerprints, and binding metadata are serialized.

- [ ] **Step 2: Implement protected-file/system-credential rebinding.**

  Require target-side credential references or explicit re-enrollment. Never copy ordinary environment files or private credential files inside the data backup.

- [ ] **Step 3: Implement external signer/KMS rebinding.**

  Verify signer identity and wallet address on the target. Do not export private keys. Reject an address mismatch before cutover.

- [ ] **Step 4: Implement separately approved local-wallet transfer.**

  Require migration policy opt-in, a second approval bound to the exact wallet address and task ID, a one-time encrypted channel, destination address verification, and source-key retention until commit. Emit high-risk audit events.

- [ ] **Step 5: Run wallet, credential, redaction, and restart tests, then commit.**

  Run: `python -m pytest tests/test_migration_credentials.py tests/test_migration_wallet.py tests/test_wallet_execution.py tests/test_service.py -q`

  Commit: `git add src/noyra/migration/credentials.py src/noyra/migration/wallet.py src/noyra/wallet/config.py src/noyra/wallet/execution.py tests/test_migration_credentials.py tests/test_migration_wallet.py; git commit -m "feat: secure migration credential and wallet modes"`

### Task 9: Expose authenticated APIs and admin UI

**Files:**
- Modify: `src/noyra/service.py`
- Modify: `src/noyra/web/admin.html`
- Modify: `src/noyra/web/admin.js`
- Modify: `src/noyra/web/admin.css`
- Create: `tests/test_migration_service.py`
- Create: `tests/test_migration_admin_ui.py`

**Interfaces:**
- `GET /api/v1/admin/migration/policy`
- `PUT /api/v1/admin/migration/policy`
- `GET /api/v1/admin/migration/targets`
- `POST /api/v1/admin/migration/targets`
- `POST /api/v1/admin/migration/targets/{targetId}/revoke`
- `GET /api/v1/admin/migration/candidates`
- `GET /api/v1/admin/migration/proposals`
- `GET /api/v1/admin/migration/proposals/{proposalId}`
- `POST /api/v1/admin/migration/proposals/{proposalId}/approve`
- `POST /api/v1/admin/migration/proposals/{proposalId}/reject`
- `POST /api/v1/admin/migration/tasks/{taskId}/cancel`
- `GET /api/v1/admin/migration/tasks/{taskId}`
- `POST /api/v1/admin/migration/tasks/{taskId}/cutover`
- `POST /api/v1/admin/migration/tasks/{taskId}/rollback`

- [ ] **Step 1: Write HTTP tests for authentication, CSRF, validation, cooldown, stale approvals, and redaction.**

  Assert disabled mode returns a stable unavailable response, manual mode creates proposals only, policy auto requires all gates, and all mutation endpoints append audit records.

- [ ] **Step 2: Implement service handlers as thin domain adapters.**

  Parse bounded JSON, enforce operator authorization, call domain methods, map stable errors to documented statuses, and return projections without secrets.

- [ ] **Step 3: Build the Chinese admin workflow.**

  Add a “迁移与庇护所” card showing mode, policy revision, trust failures, cooldown, active epoch, proposals, target details, reason, key mode, risk, and rollback status. Require explicit confirmations for policy auto and local-wallet transfer.

- [ ] **Step 4: Run service and browser-contract tests and commit.**

  Run: `python -m pytest tests/test_migration_service.py tests/test_migration_admin_ui.py tests/test_web_contract.py -q`

  Commit: `git add src/noyra/service.py src/noyra/web/admin.html src/noyra/web/admin.js src/noyra/web/admin.css tests/test_migration_service.py tests/test_migration_admin_ui.py; git commit -m "feat: add migration management console"`

### Task 10: Add emergency recovery and provider adapter boundaries

**Files:**
- Create: `src/noyra/migration/providers.py`
- Create: `src/noyra/migration/recovery.py`
- Create: `tests/test_migration_providers.py`
- Create: `tests/test_migration_recovery.py`
- Modify: `src/noyra/service.py`
- Modify: `docs/deployment/setup-modes.md`

**Interfaces:**
- `TargetProvider.list_candidates(policy) -> list[TargetCandidate]`
- `TargetProvider.provision(candidate, policy) -> TargetRegistration`
- `RecoveryCoordinator.restore_standby(task_id) -> MigrationTask`

- [ ] **Step 1: Write tests for provider isolation and emergency boundaries.**

  Assert provider adapters cannot receive wallet or operator secrets, cannot provision outside policy, and emergency recovery accepts only pre-registered standby targets with verified backups.

- [ ] **Step 2: Implement the provider interface and static registered-target provider.**

  Make the static provider the default. Keep provider-specific cloud creation optional and explicitly configured; no generic SSH or public scanning is permitted.

- [ ] **Step 3: Implement emergency recovery.**

  Require source failure evidence, a verified backup, a standby registration, a new epoch, and a recovery audit event. Keep ordinary migration approval rules separate from recovery rules.

- [ ] **Step 4: Document setup, trust enrollment, policy modes, wallet choices, and recovery.**

  Add a deployment runbook that explains prerequisites, dry-run checks, protected files, rollback, and how to disable migration immediately.

- [ ] **Step 5: Run provider, recovery, documentation, and full service tests, then commit.**

  Run: `python -m pytest tests/test_migration_providers.py tests/test_migration_recovery.py tests/test_deployment_setup.py -q`

  Commit: `git add src/noyra/migration/providers.py src/noyra/migration/recovery.py src/noyra/service.py docs/deployment/setup-modes.md tests/test_migration_providers.py tests/test_migration_recovery.py; git commit -m "feat: add migration recovery boundaries"`

### Task 11: Complete verification, fault injection, and release gates

**Files:**
- Modify: `scripts/audit-deployment.sh`
- Modify: `scripts/audit-deployment.ps1`
- Create: `tests/test_migration_end_to_end.py`
- Create: `tests/shell/test-migration-install.sh`
- Modify: `docs/security/threat-model.md`
- Modify: `docs/deployment/ubuntu.md`

- [ ] **Step 1: Add end-to-end tests for every required failure mode.**

  Cover source restart, target restart, interrupted transfer, corrupted backup, stale approval, changed policy, revoked target, target health failure, cutover failure, rollback, duplicate request, cooldown bypass attempt, local-wallet approval omission, and double-active fencing.

- [ ] **Step 2: Run the migration matrix from a fresh database and historical fixtures.**

  Run: `python -m pytest tests/test_migration_end_to_end.py tests/test_database_migration.py tests/test_gate2_archive_integrity.py -q`

- [ ] **Step 3: Run static and deployment checks.**

  Run: `python -m compileall src tests; python -m ruff check src tests; python -m ruff format --check src tests; python -m mypy src tests; bash -n scripts/*.sh; bash scripts/audit-deployment.sh`

- [ ] **Step 4: Run a real Ubuntu restore and cutover rehearsal.**

  Use a disposable encrypted target volume and two isolated hosts. Record backup hash, target attestation, epoch transitions, health results, rollback result, and proof that the old source rejected mutations after fencing.

- [ ] **Step 5: Update threat model and release checklist.**

  Document the no-scan boundary, provider credential boundary, local-wallet risk, target enrollment lifecycle, epoch fencing, operator emergency pause, and evidence required before enabling policy auto.

- [ ] **Step 6: Review the complete diff and commit the release gates.**

  Run: `git diff --check; git status --short`

  Commit: `git add scripts/audit-deployment.sh scripts/audit-deployment.ps1 tests/test_migration_end_to_end.py tests/shell/test-migration-install.sh docs/security/threat-model.md docs/deployment/ubuntu.md; git commit -m "test: verify subject migration safety gates"`

## Completion Gate

The migration feature is not ready for production until the following are all true:

- Fresh installations remain disabled and manual approval is the enabled default.
- The complete migration test matrix passes on Python 3.11 and 3.12 on Ubuntu and Windows where applicable.
- A real encrypted Ubuntu target restore and rollback rehearsal has recorded evidence.
- No secrets appear in artifacts, logs, status, exports, or audit projections.
- A stale source cannot mutate after target epoch acquisition.
- Local-wallet mode is separately disabled unless explicitly enabled and approved.
- Emergency recovery is independently disabled unless a verified standby and backup exist.
- The admin UI shows the exact target, reason, policy revision, trust evidence, wallet mode, cooldown, and rollback state.
- Deployment documentation includes disable, revoke, rollback, and restore procedures.
