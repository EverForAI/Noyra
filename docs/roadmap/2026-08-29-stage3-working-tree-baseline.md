# Stage 3 Working-Tree Baseline

Date: 2026-08-29
Purpose: Preserve a durable checkpoint before the 3-C bounded-read and scan work.

## Repository checkpoint

- Branch: `codex/candidate-20260822`
- HEAD: `e2f55b0 fix: harden native channels and public moderation`
- Working tree: intentionally dirty; all existing changes are retained.
- `git diff --check`: passed (Git emitted only the existing CRLF conversion warning for `src/noyra/service.py`).
- No reset, checkout, clean, or file removal was performed.

## Existing changes

The working tree contains 29 modified tracked files and 2 untracked test files. The untracked files are part of the current work and must be retained.

### Modified tracked files

```text
.env.example
deploy/noyra.env.example
docs/api/openapi.yaml
docs/deployment/ubuntu.md
src/noyra/capability/integrity.py
src/noyra/capability/store.py
src/noyra/cognition/cycle.py
src/noyra/cognition/interaction.py
src/noyra/cognition/settings.py
src/noyra/core/database.py
src/noyra/core/locking.py
src/noyra/model/__init__.py
src/noyra/model/resources.py
src/noyra/service.py
src/noyra/service_contract.py
src/noyra/web/admin.css
src/noyra/web/admin.html
src/noyra/web/admin.js
tests/test_capability.py
tests/test_cognition.py
tests/test_gate1_secret_intents.py
tests/test_gate1_unknown_model.py
tests/test_inbound.py
tests/test_m41_verification_foundation.py
tests/test_m42_p2_14_at_rest.py
tests/test_model_gateway.py
tests/test_public_posts.py
tests/test_service.py
tests/test_transports.py
```

### Untracked files

```text
tests/test_locking.py
tests/test_resource_groups_env.py
```

Diff summary at checkpoint: 2,910 insertions and 322 deletions across the tracked changes (excluding the two untracked files).

## Test baseline

Commands were run read-only against this working tree. Results below are the evidence to use when evaluating subsequent 3-C edits.

| Command | Result | Duration |
|---|---:|---:|
| `python -m pytest -q tests/test_resource_groups_env.py tests/test_locking.py` | **14 passed** | 4.62 s |
| `python -m pytest -q tests/test_storage.py tests/test_storage_lifecycle.py tests/test_gate2_storage_pressure.py` | **37 passed** | 47.60 s |
| `python -m pytest -q tests/test_archive.py tests/test_archive_s3.py tests/test_m42_p1_11_archive_lifecycle.py tests/test_m42_p1_03_archive_integrity.py` | **53 passed** | 116.37 s |
| `python -m pytest -q tests/test_embedding_resources.py tests/test_m42_p1_10_embedding_resilience.py` | **14 passed** | 12.66 s |
| `python -m pytest -q tests/test_m42_p1_02_integrity_runtime.py` | **124 passed** | 340.37 s |
| `python -m pytest -q tests/test_service.py` | **37 passed** | 316.87 s |

The unique focused baseline above is **279 passed**. The integrity-runtime and service suites are slow on this machine; run them once per checkpoint and do not start duplicate sessions.

A full-suite attempt was intentionally stopped after it showed no progress rate suitable for this checkpoint run: **169 passed, 14 subtests passed in 318.05 s**, then `KeyboardInterrupt` while executing `src/noyra/core/database.py:6624`. This is not a full-suite pass and must not be reported as one. The later focused suites completed successfully.

## 3-C starting point

No 3-C implementation was added by this checkpoint. The next work item is to make the existing resource, integrity, storage, archive, and diagnostics reads bounded and streamable while preserving public return shapes. New tests should cover hard limits, timeout/degraded outcomes, invalid timestamps, and large-history scans before the 3-D gate sequence is run.

## Next checkpoint protocol

1. Record the exact files and tests changed for one bounded subtask.
2. Run only that subtask's focused tests once.
3. Run `git diff --check` and record the result.
4. Continue to the next subtask only after the focused result is captured.
5. Run the full 3-D gate sequence only after all 3-C subtasks are complete.

## 3-C checkpoint 1: bounded cognitive resource access

Implemented after the initial baseline:

- Added bounded `limit`/`offset` pagination to cognitive resource group and key views, with a hard page size of 1,000 and deterministic tie-break ordering.
- Changed key selection to let SQLite select one eligible key (`LIMIT 1`) instead of loading every candidate into Python.
- Changed group revocation to iterate key rows rather than calling `fetchall()`.
- Replaced the unbounded duplicate-fingerprint materialization in `add_keys` with a bounded 64-key batch/total guard and per-fingerprint existence checks inside the writer transaction.
- Hardened resource timestamp parsing against overlong values and timezone/parser overflow/type failures.
- Changed the model and embedding integrity secret loops to consume the budgeted cursor incrementally instead of `fetchall()`/record-list materialization.
- Added regression coverage for resource pagination/order and oversized key batches.

Checkpoint validation:

| Command | Result |
|---|---:|
| `python -m pytest -q tests/test_model_gateway.py tests/test_gate1_unknown_model.py tests/test_gate1_secret_intents.py tests/test_m42_p1_02_integrity_runtime.py` | **204 passed, 172 subtests passed** |
| `ruff check src/noyra/model/resources.py src/noyra/core/integrity.py tests/test_model_gateway.py` | **passed** |
| `git diff --check` | **passed** (existing `service.py` CRLF warning only) |

The remaining resource integrity history and routing-history lists are intentionally the next 3-C subtask; this checkpoint does not claim the full 3-C boundary work is complete.

## 3-C checkpoint 2: streaming cognitive-resource integrity history

Implemented the remaining resource-history boundary work:

- Reworked `CognitiveResourceStore.verify_integrity` so groups, keys, group revisions, and key events are consumed row-by-row from the shared budgeted cursor.
- Replaced full revision/event history lists with compact per-group/per-key state machines that retain first, previous, and latest state plus lifecycle counters.
- Reworked `verify_routing_integrity` so resource groups, decisions, attempts, outcomes, waiting tasks, and wait revisions are all validated incrementally.
- Preserved the existing return counts, lifecycle rules, ownership checks, hash checks, and integrity error messages while allowing healthy histories to exceed `max_rows_per_check` without materializing them.

Checkpoint validation:

| Command | Result |
|---|---:|
| `python -m pytest -q tests/test_model_gateway.py` | **39 passed, 172 subtests passed** |
| `python -m pytest -q tests/test_m42_p1_02_integrity_runtime.py` | **124 passed** |
| `python -m ruff check src/noyra/model/resources.py` | **passed** |
| `python -m mypy src/noyra/model/resources.py` | **passed** |
| `git diff --check` | **passed** (existing `service.py` CRLF warning only) |

## 3-C checkpoint 3: bounded storage, archive, diagnostics, and integrity reads

Implemented the storage/archive/diagnostics read boundary work:

- Storage and lifecycle scans now consume SQLite cursors incrementally; cache cleanup uses post-order traversal and export pruning uses a stable keyset with a 1,000-candidate budget.
- Archive keyring history, transfer lease recovery, queue cleanup/orphan GC, cold-archive staging, and cloud coordinator inventories no longer materialize unbounded query results. Explicit small worklists retain hard limits.
- Event cold-archive selection and staging reconciliation use bounded cursors/keyset replay while preserving segment row/byte limits.
- Diagnostics aggregates are built while iterating cursors, resource pressure and interaction lists have explicit caps, and the delivery/self-modification endpoints use bounded fetches.
- Integrity provenance, transport, delivery, and common-knowledge checks consume budgeted cursors; two-row tail/ownership probes use bounded fetches.

Checkpoint validation:

| Command | Result |
|---|---:|
| `python -m pytest -q` | **1052 passed, 8 skipped, 251 subtests passed** |
| `python -m ruff check src/noyra/core/archive.py src/noyra/core/event_archive.py src/noyra/core/integrity.py src/noyra/service.py` | **passed** |
| `python -m mypy src/noyra/core/archive.py src/noyra/core/event_archive.py src/noyra/core/integrity.py src/noyra/service.py` | **passed** |
| `python -m compileall -q src` | **passed** |
| `git diff --check` | **passed** (existing `service.py` CRLF warning only) |
