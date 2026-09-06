# Remote validation preparation

Date: 2026-08-20

This note records the operational preparation for the remaining runtime evidence. No remote host
was contacted and no production data was changed in this worktree.

## Added assets

- `scripts/remote-acceptance.sh` provides read-only `preflight`, `smoke`, `soak`, and `collect`
  modes. It samples `/health/live` and `/health/ready`, captures systemd/disk/mount/journal
  evidence, uses a private evidence directory, and stops after three consecutive probe failures.
- `docs/deployment/remote-validation.md` defines the staging-host boundary, SSH tunnel, pinned
  deployment, 72-hour service soak, deterministic M41 synthetic soak, controlled fault classes,
  backup/restore checks, stop conditions, and evidence format.
- `docs/deployment/ubuntu.md` links the runbook from the Ubuntu operational section.

## Safety contract

The collector never stops, restarts, kills, reconfigures, or fills a host. Fault injection remains
an explicit operator action and is restricted to a disposable staging host, a dedicated test data
volume, and one fault at a time. Blanket firewall flushes, shared-volume disk filling, and direct
public exposure of port 8765 are prohibited.

## Closure impact

These changes make the remote experiment reproducible; they do not close P3-03, P3-04, P3-05,
P3-08, or the production-host portion of P2-14. Closure still requires actual server evidence,
with host identity, commit and lock hashes, UTC window, fault trace, resource samples, and
sanitized logs recorded separately from the code/test evidence.
