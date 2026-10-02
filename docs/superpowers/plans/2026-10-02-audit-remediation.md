# Noyra Audit Remediation Implementation Plan

> For agentic workers: use superpowers:subagent-driven-development for this plan. Each task ends with focused verification, review, and a commit.

**Goal:** Resolve every code-addressable finding in the 2026-10-02 audit, restore local quality gates, and leave external-only release gates with executable evidence checks.

**Architecture:** Work in independent modules. Each module starts with a failing regression test, changes the boundary that owns the invariant, runs focused and cross-module tests, then commits only after review. Secrets, provider attempts, retention records, migration epochs, and release evidence remain separate durable contracts.

**Tech Stack:** Python 3.12, SQLite/WAL, Pydantic, pytest, Ruff, mypy, shell/systemd scripts, GitHub Actions, Ed25519, ChaCha20-Poly1305.

**Spec:** docs/audit/2026-10-02-full-readonly-audit.md and the approved repair order in the user conversation.

## Global Constraints

- Do not weaken or skip CI gates.
- Production secrets come only from protected files or systemd credentials.
- Unknown outcomes never become ordinary failures or new logical retries.
- Migration stays disabled by default and cutover requires durable task-bound proofs.
- Local wallet approval consumption is durable and CAS protected.
- Every change follows failing test -> minimal implementation -> focused regression -> broader regression -> review -> commit.
- Real KMS, reorg, backup/restore, HTTPS and soak evidence cannot be claimed from local simulation.

---

### Task 1: Restore schema-75 runtime export and local quality gates

**Files:** src/noyra/core/runtime_export.py; scripts/check-site.py; production files reported by mypy; tests/test_gate1_redaction.py; tests/test_m42_p1_04_runtime_export.py; new tests/test_runtime_export_schema75.py.

**Contract:** Preserve RuntimeLogExporter.export and strict rejection of unknown future schemas. Add a reviewed schema-75 ownership graph covering the current database inventory.

- [ ] Add a regression that boots CURRENT_SCHEMA_VERSION 75 and exports a manifest whose ownership graph version is 75.
- [ ] Run .venv\Scripts\python.exe -m pytest -q tests/test_runtime_export_schema75.py tests/test_gate1_redaction.py::test_runtime_export_and_runtime_log_projection_share_secret_redaction and observe the current missing-graph failure.
- [ ] Implement the graph from the current migration/table inventory; do not silently fall back to an older graph.
- [ ] Fix the two scripts/check-site.py E501 lines, the unformatted deployment plan, and only confirmed production-code mypy errors.
- [ ] Run runtime export tests, Ruff, format and targeted mypy; run the broader suite; request review.
- [ ] Commit with: git add ... && git commit -m "fix: restore schema 75 runtime export and quality gates"

### Task 2: Enforce managed production secret sources

**Files:** src/noyra/service.py; src/noyra/model/resources.py; scripts/preflight-production.py; service/model/preflight tests.

**Contract:** Development/test retain inline compatibility. Production rejects inline-only operator token and every model-group api_keys entry; file and systemd credential references remain valid.

- [ ] Add failing fixtures for inline-only/file/credential operator token, group JSON api_keys, file references, credential references, mixed sources and malformed JSON.
- [ ] Run the fixtures and verify they fail for the existing fallback/blind spot.
- [ ] Implement production source enforcement and group-aware validation with provider/group-specific safe errors.
- [ ] Run service security, model resource and preflight suites; review; commit with git commit -m "fix: require managed production secret sources".

### Task 3: Make provider recovery permits and unknown outcomes durable

**Files:** src/noyra/core/provider_health.py; src/noyra/model/resources.py; src/noyra/research/search.py; provider/search/model tests.

**Contract:** route_available returns a typed RoutePermit containing provider identity, probe token, state revision and expiry. Only the permit owner can finish a half-open probe. Projection separates success, known failure and unknown.

- [ ] Add failing tests for concurrent probes, an old completion after a new probe, timeout/5xx unknown results and known failures.
- [ ] Run focused tests and observe the current boolean API/statistics failure.
- [ ] Implement RoutePermit and thread it through model/search attempt completion.
- [ ] Split breaker counters and projection fields; run provider, failover, model and integrity suites; review; commit with git commit -m "fix: bind provider recovery probes to attempt permits".

### Task 4: Make retention and optional schema contracts self-describing

**Files:** src/noyra/core/retention.py; src/noyra/core/integrity.py; src/noyra/core/database.py; retention/migration/integrity tests.

**Contract:** Diagnostics compare sqlite_master to RETENTION_REGISTRY. Cursor payloads bind cutoff, registry version and data epoch. The final durable run payload supplies return data and state hash. Every optional persistent structure has a feature marker/fingerprint.

- [ ] Add failing tests for an unregistered table, changed-cutoff cursor reset, old-time backfill, history-prune hash consistency and missing feature marker.
- [ ] Run focused tests and confirm current failures.
- [ ] Implement dynamic inventory, conservative unclassified handling, cursor reset, final-payload hashing and feature validation.
- [ ] Run retention, schema replay, integrity and storage suites; review; commit with git commit -m "fix: make retention and optional schema contracts durable".

### Task 5: Complete release evidence and upgrade verification

**Files:** .github/workflows/release.yml; scripts/verify_external_gates.py; scripts/build-release-evidence.py; upgrade manager/runner only where a test demonstrates a missing invariant; release/upgrade tests.

**Contract:** external-gates.json is an independently produced, signed, same-SHA artifact. The release job only downloads and verifies it, with distinct failure codes for missing, stale, unsigned and mismatched records.

- [ ] Add failing tests for missing artifact, wrong SHA, stale time, wrong reviewer, missing gate ID and bad signature.
- [ ] Run them against the current validator/workflow.
- [ ] Implement the protected artifact handoff and strict verifier; never generate a pass record inside the release job.
- [ ] Run external gate, upgrade and shell runner tests; review; commit with git commit -m "fix: make release evidence and upgrade verification complete".

### Task 6: Finish migration execution and wallet approval durability

**Files:** src/noyra/migration/wallet.py; agent/runner scripts and systemd unit; cutover/fencing/recovery modules; migration service routes; database migrations; migration tests.

**Contract:** Migration remains disabled by default. A cutover step advances only when task-bound manifest, restore, health, target signature, source epoch and policy revision match durable records. Local wallet approval is a durable CAS operation. Agent receive/restore requires authenticated source-target session and bounded quota.

- [ ] Add failing tests for approval replay after a new process/store, unauthenticated agent receive, quota exhaustion, stale proof, partial restore, old-epoch writes, cutover failure and rollback.
- [ ] Run focused tests and observe current stubs/in-memory approval behavior.
- [ ] Add schema and durable approval consume event; add signed session, nonce replay protection, quota and TTL cleanup.
- [ ] Implement restore/health proof verification, prepare/commit/fence/rollback state transitions and epoch CAS.
- [ ] Run all migration and shell runner tests; inspect durable transitions; review; commit with git commit -m "feat: complete durable migration execution controls".

### Task 7: Harden public management controls and public contract

**Files:** service session/rate boundary; src/noyra/mind/benchmarks.py; src/noyra/interaction/projection.py; service/transport/public tests.

**Contract:** Public management uses shared TTL failure/session state. Benchmark downloads use the common safe transport policy. Public fields and limits are versioned as public-contract-v1.

- [ ] Add failing tests for rate/session behavior across service instances, benchmark SSRF/redirect/private-IP rejection and public contract field/size limits.
- [ ] Implement shared state and transport reuse; add public contract snapshots and privacy tests.
- [ ] Run service security, transport, projection and site suites; review; commit with git commit -m "fix: harden public controls and projection contract".

### Task 8: Final verification and audit evidence

**Files:** docs/audit/2026-10-02-full-readonly-audit.md; all test/tooling surfaces.

- [ ] Run .venv\Scripts\python.exe -m pytest -q --disable-warnings.
- [ ] Run .venv\Scripts\python.exe -m ruff check ., .venv\Scripts\python.exe -m ruff format --check ., .venv\Scripts\python.exe -m mypy src tests, .venv\Scripts\python.exe -m compileall -q src scripts tests, bash -n scripts/install-ubuntu.sh, migration shell tests and upgrade shell tests.
- [ ] Review every F01-F18 and mark only code-addressable items closed; retain external-only gates with exact evidence requirements.
- [ ] Request a final whole-branch review before integration options.

## Commit Boundaries

Each task has its own commit. A task is incomplete until focused tests, broader regression, review and git diff --check pass. No task pushes to GitHub or changes a server.

