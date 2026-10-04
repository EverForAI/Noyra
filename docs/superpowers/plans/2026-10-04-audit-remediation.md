# 2026-10-04 Audit Remediation Implementation Plan

> For agentic workers: execute this plan inline with review checkpoints; keep the modules isolated and commit each verified module.

**Goal:** Resolve the code findings in the 2026-10-04 Noyra audit without weakening data ownership, migration, or audit guarantees.

**Architecture:** Address correctness and build gates first, then destination security, then long-term retention. Each module gets a focused failing test, a minimal root-cause fix, its own regression gate, and a separate local commit. External deployment evidence remains a release gate.

**Tech Stack:** Python 3.12, SQLite, pytest, Ruff, mypy, git.

**Spec:** `docs/audit/2026-10-04-full-project-readonly-audit.md`.

## Global Constraints

- Keep strict schema/export ownership checks and CI type checking enabled.
- Preserve protected audit, payment, recovery, and migration evidence.
- Preserve pre-existing untracked Pelican files and generated output.
- Commit locally after each module; do not push.
- Never represent local tests as real-host release-gate evidence.

## Goal and boundaries

Resolve code findings N01-N04 from `docs/audit/2026-10-04-full-project-readonly-audit.md` in dependency and risk order. Keep the current branch local; commit each verified module separately and do not push. Preserve pre-existing untracked Pelican files and generated output. N05 is an external release-evidence gate and cannot be closed by local code or fabricated evidence.

## Module 1: Schema 79 runtime export ownership graph (N01)

- Root cause: schema 79 is the current database contract, but the exporter has an explicit graph only through schema 78. Strict inventory validation correctly fails closed.
- Security boundary: classify every schema-79 table according to ownership; subject data must be selected by `subject_id` or an audited parent edge, control/global data must be explicitly skipped or classified, and unrecognized tables must continue to fail closed. Never weaken coverage/reconciliation checks.
- Work: add failing current-schema/export isolation coverage, inspect schema-79 DDL and table ownership, add the explicit versioned graph, then test current and historical exports, row reconciliation, and cross-subject exclusion.
- Gate: focused exporter/schema tests, then full pytest, Ruff, and compileall. Commit only this module after all pass.

## Module 2: Strict mypy failures (N02)

- Root cause: one invalid standard-library exception reference in migration source and stale/incomplete annotations in tracked migration tests.
- Security boundary: keep the CI strictness and runtime behavior unchanged; use precise types rather than ignores/casts that hide mistakes. Do not modify the untracked Pelican test.
- Work: reproduce tracked-only diagnostics, fix source and tracked tests, then run the exact CI mypy command plus Ruff and relevant migration tests.
- Gate: `mypy src tests` on a clean tracked-file view. Since the worktree contains a user-owned untracked test that currently contributes one mypy error, validate both the complete current tree and tracked-only CI input without editing or deleting that file; record the distinction.

## Module 3: Migration endpoint destination and identity binding (N04)

- Root cause: registration and request execution validate URL syntax but do not constrain DNS/IP destinations or bind the enrolled target identity to the HTTPS origin.
- Security boundary: preserve HTTPS and certificate validation, forbid redirects, reject loopback/link-local/multicast/unspecified and cloud metadata destinations, prevent DNS rebinding between validation and connect, and require an explicit administrator allowlist for private deployment networks. Bind the normalized origin into the target enrollment/signature challenge; fail closed on endpoint changes. Do not expose the HMAC secret or permit credentials in URLs.
- Work: trace target registration, challenge, persisted target schema, and transport connection lifecycle; add tests first for prohibited addresses, allowed explicit private destinations, endpoint tampering, DNS changes, and redirects; implement validation at the point of connection using the same resolved address that is connected while retaining TLS hostname verification.
- Gate: focused target enrollment/HTTP executor tests, all migration tests, static checks, and review of backward compatibility. Commit only after verification.

## Module 4: Runtime record lifecycle and bounded retention (N03)

- Root cause: the existing retention registry protects several high-volume evidence tables indefinitely without per-table lifecycle rationale or a compacted/archived representation.
- Security boundary: never delete unresolved/in-flight model ledger rows, payment/security/migration evidence, records referenced by durable foreign keys, or data needed for budgets/idempotency/recovery. Retention must be bounded and batched, preserve audit integrity, be restartable, and be opt-in/configurable only where deletion semantics are safe. Do not silently drop per-call records before proving dependencies.
- Work: inventory foreign keys, triggers, readers, export and integrity contracts for each named table; distinguish indispensable evidence from rebuildable or expirable details; add a durable retention contract and test exact cutoff, protected references, batch restart, and summary/aggregate behavior before enabling cleanup.
- Gate: focused retention, integrity, export, model ledger, and recovery tests; full pytest and static checks. Commit only after no protected references or required audit paths are broken.

## Module 5: External release gates (N05)

- Required evidence: same-SHA Ubuntu/systemd, encrypted-volume and backup/restore, dual-host migration/fencing, signer/KMS, chain/reorg/nonce, HTTPS proxy, and soak results, reviewed by the configured external-gate process.
- Boundary: local tests can prepare runbooks or improve evidence validation, but cannot claim real-host acceptance. Keep high-risk capabilities gated until the required evidence is produced in the real environment.

## Per-module workflow

For each code module: reproduce the failure, document the precise root cause and boundary, add a focused failing test, implement the smallest coherent fix, run focused tests, run adjacent regression checks, run repository static checks, inspect the final diff for unrelated changes, then make one local commit. Re-run any broader test suite only when the change or a failure warrants it. Update the audit report to distinguish resolved code findings from external evidence still outstanding.
