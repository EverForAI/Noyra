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
