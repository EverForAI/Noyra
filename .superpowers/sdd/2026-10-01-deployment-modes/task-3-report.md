# Task 3 Report: Public HTTPS Mode with Caddy

## Status

Implemented Task 3 and committed the public HTTPS setup changes.

## Changes

- Added `SetupRunner.run_public()` with domain validation, dry-run planning, and a structured result with a stable `exit_code` property.
- Updated the environment atomically with the public HTTPS origin, loopback trusted proxy CIDRs, and secure admin session cookies.
- Rendered and atomically published separate public and admin Caddy HTTPS blocks targeting `127.0.0.1:8765`.
- Refused replacement of an existing Caddyfile unless `--replace` is set; symlink and non-file paths are rejected.
- Created SHA-256 backup records for both files before mutation, restored both files on Caddy, restart, or health-check failure, and restarted the known-good services after rollback.
- Added Caddy validation, Caddy reload, Noyra restart, local health checks, and HTTPS health checks for both origins.
- Added `X-Real-IP` forwarding and generated setup documentation to the Caddy example.
- Extended setup and contract tests for generated security settings, both origins, replacement refusal, and rollback.

## Validation

- `python -m pytest tests/test_deployment_setup.py tests/test_deployment_contract.py -q`: 19 passed.
- `python -m ruff check src/noyra/deployment_setup.py tests/test_deployment_setup.py`: passed.
- `git diff --check`: passed.

## Limits

- HTTPS checks run during non-dry setup and therefore require both DNS names and the public proxy to be reachable before setup can complete.
- Rollback errors are suppressed after the original setup failure so the stable setup error remains the reported failure; operators should inspect service logs if the recovery restart itself fails.
