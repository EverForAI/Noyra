# Task 1 report: upgrade manager and HTTP contract

## TDD evidence

RED: `pytest -q tests/test_upgrade_manager.py` failed during collection with `ModuleNotFoundError: noyra.core.upgrade`; the failure was caused by the intentionally absent production module.

GREEN: focused manager and service tests pass after implementation: `10 passed, 53 deselected` for the upgrade and existing admin-session tests. The manager-only suite passes `7 passed`.

## Changed files

- `src/noyra/core/upgrade.py`: explicit-path `UpgradeManager`, bounded GitHub metadata projection, clean-source checks, recent-check SHA admission, atomic JSON state/request writes, process lock, idempotent start, unavailable runner gating, and redaction.
- `src/noyra/service.py`: authenticated `/api/v1/admin/upgrade/check`, `/status`, and POST routes. The existing `/api/v1` normalization, CSRF, admission, and mutation gates remain in use. POST only creates the pending request file and records a secret-free audit event.
- `src/noyra/service_contract.py`: route inventory and response statuses.
- `tests/test_upgrade_manager.py`, `tests/test_service.py`: RED/GREEN coverage for manager and HTTP behavior.

## Self-review

The HTTP layer never invokes a privileged command. Requests are written to the fixed pending JSON protocol and the manager is unavailable unless the runner trigger path exists. Only a SHA returned by a recent successful check is accepted. Status and request documents use bounded fields and redact secret-shaped text.

## Verification

- Ruff: all focused files pass.
- Mypy: upgrade/service modules pass with no issues.
- `git diff --check`: clean (Git reports only its normal CRLF conversion warning for the existing contract file).
- Focused tests: `10 passed, 53 deselected`; manager suite: `7 passed`.

## Concerns

The default production paths intentionally require the root-owned systemd trigger installed by Task 2; local development must inject explicit paths or receive `upgrade_unavailable`.

## Deployment-boundary follow-up

Review found that installed releases are not Git checkouts and that the application user must not write the root-owned runner status. The manager now checks the dedicated source checkout at `/opt/noyra/upgrade/source`, projects the current release from `/opt/noyra/current`'s resolved release-directory name, reads `/var/lib/noyra/upgrade/status.json`, and writes only `/var/lib/noyra/upgrade/requests/pending.json`. Its process lock lives beside the service-owned request file. Missing root status reads as `idle`; unreadable or malformed status fails closed. The runner request retains the existing fixed keys `task_id`, `target_sha`, and `requested_at`.

TDD RED: updated tests first to construct the manager with separate source/current/status paths and prove a symlink-backed versioned release without `.git`, read-only status behavior, and pending-request exclusion. The first focused run failed all 11 selected tests at construction because `UpgradeManager` did not yet accept the separated paths (`unexpected keyword argument 'source_path'`).

RED/GREEN follow-up: an additional test first failed because an idempotent POST projected root status logs without redaction (`token=super-secret` escaped). The status projection was centralized and bounded; final relevant selection passes `14 passed, 53 deselected`, including HTTP authentication/CSRF and the filesystem-boundary cases.

Self-review: `UpgradeManager.start()` no longer creates or updates the status file. It only performs atomic replacement of the request file while the lock is held in that service-writable request directory. A different request is rejected while a pending request remains; repeated submissions resolve by deterministic task ID from the root status or pending request. Service defaults now point at the deployment source checkout and current symlink instead of deriving a Git root from installed package files.
