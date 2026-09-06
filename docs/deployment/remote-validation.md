# Remote Ubuntu validation

A separate Ubuntu server is suitable for the remaining runtime evidence, provided it is treated as
a disposable staging host. Do not use the production subject database for the first deployment,
fault injection, disk-pressure test, or restore drill. The server must have a dedicated test data
volume and a backup destination that can be discarded independently.

## Scope and evidence

Remote execution can produce evidence for:

- P2-13: real systemd install, staged upgrade, readiness failure handling, and code rollback;
- P2-14: encrypted-volume and backup/restore drill on the intended host class;
- P3-03: multi-day affect calibration/soak with resource and drift samples;
- P3-04: memory benchmark runs with real corpus revisions and answer-level results;
- P3-05: only the Ubuntu side of the operational matrix; Windows VM and signing remain separate;
- P3-08: only the runtime's release inputs; a real GitHub tag, Sigstore certificate, and offline
  provenance check still require a release account and an external verifier.

The result is staging evidence, not production certification. Record the host OS image, kernel,
commit id, dependency lock hashes, data-volume device chain, UTC start/end times, and every fault
injection action. Never put bearer tokens, model keys, backup keyrings, or private health output in
an issue or chat transcript.

## Safe server layout

Use a host or VM that can be rebuilt without affecting other services. The intended layout is:

```text
/srv/noyra                 source checkout at a pinned commit
/var/lib/noyra             dedicated encrypted test data volume, mode 0700
/var/backups/noyra         separate encrypted backup destination
/var/log/journal            systemd journal retained for the test window
```

Before installation, verify the data boundary and encryption chain. Do not format a device from a
copy-pasted command; the volume must already be provisioned by the operator:

```bash
findmnt /var/lib/noyra
lsblk -o NAME,TYPE,FSTYPE,MOUNTPOINTS
cryptsetup status <mapped-device>
df -h /var/lib/noyra
```

The installer expects Ubuntu 24.04 (22.04 is also acceptable if the supported Python toolchain is
available), Python 3.11/3.12, `python3-venv`, systemd, curl, and outbound HTTPS only to the
explicitly configured providers. Keep port 8765 on loopback and use an SSH tunnel for inspection.

## Deploy through SSH

Clone or copy the repository to the server, then pin the exact commit under test:

```bash
sudo install -d -o "$USER" -g "$USER" -m 0755 /srv/noyra
git clone <repository-url> /srv/noyra
cd /srv/noyra
git checkout <commit-under-test>
git rev-parse HEAD
sha256sum requirements*.lock
```

Generate all tokens and the genesis value on the server. Put them in `/etc/noyra/noyra.env` with
mode `0600` through `sudoedit`; do not place secrets in shell history. Keep cognition disabled for
the first smoke and restart test, then enable it only with bounded budgets and test-provider keys.

Install the base profile and start the service:

```bash
cd /srv/noyra
sudo ./scripts/audit-deployment.sh
sudo ./scripts/install-ubuntu.sh --profile base \
  --release-id "$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
sudoedit /etc/noyra/noyra.env
sudo systemctl enable --now noyra
sudo systemctl status noyra --no-pager
```

The installer creates a versioned release and requires `/health/ready` after a live upgrade. If the
service is already active, keep the installer-created encrypted cold backup until the validation
report is signed off. An old code release is not a data rollback; restore the recorded pre-update
backup separately if a forward migration must be undone.

## Inspect remotely without exposing the dashboard

Open the tunnel from the operator workstation and leave the server listener on loopback:

```bash
ssh -N -L 8765:127.0.0.1:8765 user@server
```

In a second local terminal, use `curl http://127.0.0.1:8765/health/live` and
`curl http://127.0.0.1:8765/health/ready`. `/health/live` only proves that the process can answer;
`/health/ready` includes startup integrity, lifecycle, at-rest, and recorded cloud readiness. The
authenticated `/api/v1/admin/health` endpoint is for operators and must stay inside the tunnel.

## Repeatable evidence collection

The repository includes a read-only collector. It never restarts, kills, reconfigures, or fills the
host. Run it inside `tmux` or `systemd-run --scope` so an SSH disconnect does not terminate a soak:

```bash
cd /srv/noyra
sudo install -d -o "$USER" -g "$USER" -m 0700 /var/backups/noyra/evidence
sudo -v
tmux new -s noyra-validation
bash ./scripts/remote-acceptance.sh preflight /var/backups/noyra/evidence/preflight
bash ./scripts/remote-acceptance.sh smoke /var/backups/noyra/evidence/smoke
bash ./scripts/remote-acceptance.sh soak 259200 /var/backups/noyra/evidence/soak-72h
```

The 72-hour run samples both liveness and readiness every 30 seconds, records systemd properties,
disk/mount information, and the last 24 hours of journal output, and aborts after three consecutive
probe failures. Evidence directories are created with mode `0700`; inspect them for secrets before
copying them off the host.

For deterministic offline data-scale evidence, run the existing synthetic profile separately:

```bash
cd /srv/noyra
bash ./scripts/audit-m41.sh --scope targeted --profile soak
```

That profile creates 100,000 synthetic events/memories and is not a substitute for the service
soak. Run it in a separate window or on a separate test checkout so it cannot compete with the
subject service for the same data directory.

## Controlled fault injection

Run one fault at a time, record UTC start/end and the expected invariant, and collect evidence
after each action. Never combine a process kill with a disk-full or network partition on the first
run.

### Process crash and graceful restart

On a disposable staging host, from a second SSH session:

```bash
date -u
sudo systemctl kill --kill-who=main --signal=SIGKILL noyra
sleep 15
sudo systemctl show noyra -p ActiveState -p SubState -p Result -p NRestarts
curl --fail http://127.0.0.1:8765/health/ready
sudo journalctl -u noyra --since "-5 minutes" --no-pager
```

Expected result: the process is replaced by systemd, the readiness probe returns 200, the subject
lock is reacquired by one process, and there is no second service process. Then test the graceful
path with `sudo systemctl stop noyra` followed by `sudo systemctl start noyra`; verify that the
journal contains a clean drain and no orphan export worker.

### Provider/network failure

Use a test provider or a temporary firewall rule scoped to the service user and one exact provider
address. Do not run a blanket `iptables -F`, `nft flush ruleset`, interface shutdown, or DNS change
on a shared server. Save the current ruleset first and remove only the rule created for this test.
The expected invariant is bounded timeout/backoff, no token leakage, and a conservative no-change
cycle; readiness should reflect only the recorded cloud state and must not cause probe I/O storms.

The deterministic provider failure contracts are already covered by the repository tests. A host
network fault is supplementary operational evidence, not a reason to weaken those contracts.

### Disk pressure

Perform this only when `/var/lib/noyra` is a dedicated disposable filesystem. Record `df -P` first,
create a temporary root-owned file while preserving the configured minimum-free-space reserve, and
remove that file immediately after the observation. Never fill `/`, `/var`, or a shared volume.
The expected invariant is a bounded storage-pressure result, no partial archive publication, and a
healthy service after the reserve is restored.

### Backup, restore, and upgrade rollback

Create an encrypted backup to a separate destination, restore it into an absent child directory,
run the restore integrity checks, and inspect the subject identity before deleting the copy. For an
upgrade, install a second pinned commit, capture the installer-created backup and `current`/`previous`
symlinks, then exercise `sudo ./scripts/install-ubuntu.sh --rollback`. Do not point two processes at
the same copied identity, and do not restore over the live mount point.

## Stop conditions and reporting

Stop the experiment immediately if the service loses readiness for three consecutive samples, the
data volume approaches its reserve, a backup key is unavailable, an unexpected process owns the
subject lock, or a fault rule affects traffic outside the test provider. Preserve the evidence
directory and journal before cleanup.

The final report should separate:

1. reproducible code/test evidence;
2. staging-host runtime evidence;
3. production-only gates still outstanding.

Only the second category can move P3-03/P3-04 toward closure, and it must include multi-day
calibration/quality/cost curves rather than a single successful start.
