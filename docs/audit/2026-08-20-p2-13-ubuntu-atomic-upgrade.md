# P2-13 Ubuntu Atomic Upgrade and Rollback

Date: 2026-08-20

Branch: `codex/gate3-contract-ops`

## Finding

The native Ubuntu installer previously rebuilt `/opt/noyra/.venv` in place. A failed dependency
installation could leave a mixed environment, and an active service could continue importing from
that environment while it was being modified. The installer did not stop the service, retain code
releases, publish through an atomic pointer, verify readiness, or restore the prior code selection.
Changing from the cloud profile to base also retained cloud-only packages in the shared virtualenv.

## Remediation

- Each install now builds a new profile-specific virtualenv in
  `/opt/noyra/releases/.staging-*`, runs locked dependency installation, `pip check`, package/import
  checks, and only then renames the staging directory into a release.
- A root-owned `flock` serializes install and rollback operations. Release IDs and pointers are
  constrained to safe segments, and protected paths, release targets, the lock, keyring and systemd
  profile drop-in reject unsafe symlinks or non-regular entries.
- Systemd starts `/opt/noyra/current/.venv/bin/python`. `current` and `previous` are same-filesystem
  relative symlinks replaced with `mv -Tf`, followed by a directory sync.
- An active service is stopped and observed inactive before pointer changes. After publication it
  must be active and return HTTP 200 from `/health/ready` within a bounded deadline.
- Failure after publication stops the candidate, restores both original pointers and the prior
  install-profile drop-in, reloads systemd, and restarts the prior service only after pointer
  restoration succeeds. Failed staging trees are removed; old and failed published releases are
  retained for diagnosis rather than modified in place.
- Existing data upgrades require an encrypted cold backup outside the data and install roots before
  release publication. Backup or at-rest verification failure aborts before pointer changes.
- Base and cloud installs always use different fresh virtualenvs. A configured S3 archive rejects a
  base-profile install before the service is stopped.
- `--rollback` swaps the recorded current and previous code releases under the same lock and applies
  the same bounded readiness check.

## Data Boundary

Code rollback is not database rollback. Migrations are forward-only and may complete before a new
release fails readiness. The installer therefore retains the pre-upgrade encrypted backup and
reports its path, but it does not destructively replace `/var/lib/noyra`. An incompatible schema
requires an explicit stopped-service restore onto the encrypted volume, followed by identity and
integrity verification. This avoids presenting an unsafe automatic directory replacement as a
complete rollback.

## Verification

- WSL2 Ubuntu 24.04 `bash -n` passed for the installer and deployment audit scripts.
- The focused install-profile, at-rest and Gate 3 deployment suites passed: 34 passed, 1
  Windows-only filesystem-symlink test skipped.
- The dedicated atomic lifecycle contract passed together with the base/cloud lock contracts.
- Ruff and Ruff format passed for the new contract test; `git diff --check` passed.
- `systemd-analyze verify` accepted the unit after a temporary executable `current` target was
  supplied. It emitted only the expected DrvFs permission warnings for the source file.

The real `/opt/noyra` install/upgrade was not executed in this remediation run because the existing
WSL instance contains a prior installed state and its subject data must not be mutated for a script
test. A disposable Ubuntu/systemd VM upgrade, forced readiness failure and encrypted-data restore
remain release-environment acceptance evidence, not a reason to keep the code defect open.
