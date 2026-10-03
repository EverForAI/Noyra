# Module 3 implementation report — A04 target service handoff

## Result

Implemented target runtime activation and deactivation as a real systemd-managed handoff. The target controller accepts a validated restored database path and fixed Noyra service identity, snapshots the prior database/drop-in state, switches the service to the restored database, starts it, and waits for readiness bound to the expected subject and target. It returns a signed receipt binding task, source fence/epoch, manifest, target identity, database digest, and activation time. The activation marker is durable and idempotent. Failure paths record inspectable status and restore the prior selection; restart recovery handles interrupted activation and deactivation.

Root-owned controller state is kept under `/var/lib/noyra/migration/target-activation/state`, outside the agent-writable request/status tree. Linux ownership and mode checks, symlink rejection, and no-follow privileged JSON reads fail closed. Systemd is invoked through a fixed service boundary; request data cannot supply commands. The installer installs the runner and activation, path, and recovery units, prepares protected state/request/status directories, enables the path unit, and includes the components in upgrade rollback snapshots. Migration remains opt-in/default-off.

Source integration checks and drains/invalidate existing migration leases before resolving and snapshotting the artifact. The source fence digest is verified at target activation against the restored task/epoch, and successful activation requires target readiness for the bound subject and identity. Wallet/secrets handling remains outside receipts; wallet migration remains separately opt-in.

## TDD and verification

Focused tests were first run against the new behavior and failed while the controller/service handoff and fencing integration were absent. After implementation, the following checks passed:

- `python -m pytest tests/test_migration_activation.py tests/test_migration_agent.py tests/test_migration_agent_cli.py tests/test_migration_http_executor.py tests/test_migration_fencing.py` — 46 passed.
- `python -m pytest tests/test_gate3_contract_ops.py` — 7 passed.
- `bash tests/shell/test-migration-install.sh` — passed.
- `bash tests/shell/test-migration-components-rollback.sh` — passed.
- Ruff on `activation.py`, `agent.py`, and `http_executor.py` — passed.
- Mypy on those three modules — passed.
- `git diff --check` — passed (only Git CRLF normalization warnings).

Coverage includes actual database handoff, invalid subject/fence rejection, staged-copy revalidation, start failure and rollback, readiness failure, duplicate activation, idempotent deactivation, persistent deactivation recovery state, invalid signatures, systemd unit wiring, installation wiring, and source fencing/lease behavior.

## Limits

This work ran on Windows. The tests exercise controller logic and static unit/installer contracts; actual Linux ownership enforcement, systemd activation/path/recovery ordering, and runtime readiness against a live Linux service could not be exercised here. No deployment or push was performed.

## Post-commit verification follow-up

Corrected the import ordering in `scripts/noyra-migration-agent.py` after a fresh Ruff run identified I001. Reverification after the fix:

- Gate 3 contract tests: 7 passed.
- Focused activation, agent, CLI, HTTP executor, and fencing tests: 46 passed.
- Ruff: passed.
- Mypy over the seven modified runtime/runner modules: passed.
- `compileall`: passed.
- Migration install and component rollback shell contracts: passed.
- `git diff --check`: passed.

## Independent review remediation follow-up

Added regressions first; they failed on the activating-without-backup crash window, stale deactivation after a later activation, task artifact ID mismatch, and agent-side source substitution. The fixes now:

- Recover an `activating` journal when the old active database still matches the recorded digest and no backup exists, so startup recovery does not fail permanently in the journal-before-move window.
- Record root-owned active task/database ownership after activation and require the task, target, and current database digest to match before deactivation mutates service or files. A stale deactivation now fails before stopping the service.
- Compare the restored migration task's `artifact_id` with the activation request.
- Carry the manifest artifact SHA-256 into the privileged activation request, copy the restored SQLite database into root-only staging, and compare the staged bytes to the expected digest before semantic validation, finalization, or cutover. The journal records the staged digest; the signed activation receipt continues to bind the finalized active database digest.
- Preserve the preceding root-owned runtime-owner record across failed/recovered re-activation and deactivation. This also allows a running target database to change after activation without making an authorized rollback impossible; ownership is tied to the committed task and target stored in the database, while stale tasks still fail before stopping the service.

Verification (fresh after the runtime-owner recovery fix): Gate 3 tests 7 passed; focused activation/agent/CLI/HTTP/fencing tests 56 passed; Ruff passed; mypy on the changed activation module passed (the earlier seven-module run also passed before this final activation-only delta); compileall passed; migration install and component rollback contracts passed; `git diff --check` passed.

### Remaining trust boundary

The artifact digest reaches the root runner inside a request signed by the target agent key, which is readable by agent code. Root independently hashes the staged database and rejects mismatches, but it has no source-authenticated manifest/signature to establish that the agent-provided expected digest is the source-approved artifact digest if the agent itself is compromised. The current protocol has no source signing key/trust channel at the root runner. Closing that threat requires an architectural addition such as a source-signed artifact authorization verified by root, or moving artifact receipt and verification behind the root boundary. No such source signature was available to wire in this remediation, so this specific compromised-agent authenticity guarantee remains unverified.
