# Ubuntu 24/7 deployment

M9 packages Noyra as a long-running Python service with a local public dashboard, health endpoint,
graceful signal handling, persistent SQLite state, automatic restart, and a bounded autonomous
heartbeat. The service does not grant itself network, filesystem, publishing, messaging, or wallet
capabilities. Those grants remain operator-owned records.

## Host requirements

- Ubuntu 24.04 LTS or another currently supported Ubuntu release.
- Python 3.11 or 3.12 and `python3-venv` for systemd installation.
- A dedicated dm-crypt LUKS disk or volume for `/var/lib/noyra`, or the single-disk LUKS2 file
  container described below. A normal filesystem on an unencrypted block-device chain is rejected
  when the production at-rest policy is enabled.
- Outbound HTTPS access only to explicitly configured model and world-source hosts.
- An inbound firewall that does not expose port 8765 directly to the public internet.

Keep the service bound to `127.0.0.1`. The public site is available at `/`; the creator management
surface is `/admin`. The management surface uses the configured operator/admin/break-glass token
only at login, then keeps an in-memory HttpOnly session with a CSRF token for writes. Sessions are
intentionally invalidated on service restart. Access both surfaces through an SSH tunnel or a TLS
reverse proxy with its own authentication; the `/admin` path is not a security boundary. The public
site deliberately exposes only public state, public diary entries, redacted behavior logs, and
interactions sent on a `public:*` channel. Incoming messages and private outgoing messages are never
returned by the public API.

If a reverse proxy fronts the public site, set `NOYRA_TRUSTED_PROXY_CIDRS` to the smallest network
that contains only that proxy (for a same-host proxy, normally `127.0.0.1/32` and/or `::1/128`).
Noyra ignores `X-Forwarded-For` from every other peer. Leaving this setting empty is safe for an
SSH tunnel, but behind a reverse proxy it intentionally places all visitors in the proxy's shared
rate-limit bucket. Never enter a broad client or internet CIDR: that would let clients spoof the
source identity used by public-post and CAPTCHA abuse controls. Forwarded chains are bounded and
parsed from the trusted proxy backwards.

Public submissions always enter moderation first. The default limit is 10 posts per client bucket
per hour, with independent CAPTCHA issuance, global issuance, pending-queue, content-byte, row-count,
and free-disk backstops. Configure the exposed limits in `/admin` under Runtime Guardrails; the
environment values are initialization defaults. CAPTCHA is a human-friction layer, not the sole
security boundary. See `docs/implementation/m45-public-post-moderation.md` for the trust and proxy
model before publishing the site.

## Single-disk encrypted storage

When a second data disk is not practical, the supported personal-deployment option is a LUKS2
container file on the existing system filesystem. It gives the Noyra data tree an explicit encrypted
boundary without changing the runtime or formatting the host disk. The default container is a
10 GiB sparse file at `/var/lib/noyra-data.img`; its logical size is a hard upper bound, while the
backing filesystem consumes blocks as data is written. Leave at least 2 GB free outside the
container for upgrades and operating-system recovery.

Initialize it before the first service start, as root:

```bash
cd /path/to/Noyra
sudo bash ./scripts/setup-ubuntu-single-disk-storage.sh --size 10G
sudo ./scripts/install-ubuntu.sh --profile base
```

The initializer is deliberately separate from the installer. It refuses to overwrite an existing
image, non-empty mount point, active mapper, or existing helper/configuration path. It asks
`cryptsetup` for the passphrase interactively, never writes that passphrase to disk, and installs a
systemd drop-in that prevents Noyra from starting while `/var/lib/noyra` is not a mount point.
`--dry-run` performs only read-only validation; `--yes` skips only the explicit `CREATE` confirmation,
not the passphrase prompt.

After each reboot, unlock the container over an SSH session before using the service:

```bash
sudo /usr/local/sbin/noyra-storage-unlock
```

For maintenance, stop the service and lock the container with:

```bash
sudo /usr/local/sbin/noyra-storage-unlock --lock
```

This mode intentionally does not implement unattended boot unlock. Do not put the LUKS passphrase
in `/etc`, a shell command, an environment file, or the same server. A single-disk container does
not protect against disk loss, filesystem corruption, or a root compromise; losing the passphrase
means losing access to the container. Keep a separately stored encrypted backup and record a
restore procedure before production use. Monitor both the container quota and free space on the
system filesystem.

## Systemd installation

Install OS packages and run the repository installer as root:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
cd /path/to/Noyra
sudo ./scripts/install-ubuntu.sh
sudoedit /etc/noyra/noyra.env
sudo systemctl enable --now noyra
```

The installer creates `/var/lib/noyra` as `noyra:noyra` mode `0700`, installs a separate backup
keyring at `/etc/noyra/backup-keyring.json` as `root:noyra` mode `0640`, and enables
`NOYRA_AT_REST_MODE=required`. It does not format or encrypt a disk. Mount the intended LUKS volume
at `/var/lib/noyra` first and verify the device-mapper chain with `findmnt`, `lsblk`, and
`cryptsetup status`. The runtime independently walks the Linux sysfs block-device ancestry and
fails before database creation when it cannot find a `CRYPT-LUKS` mapping.

Generate secrets on the server instead of moving them through chat or source control:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
python3 -c 'import hashlib; print(hashlib.sha256(b"operator-recorded-genesis").hexdigest())'
```

Set separate `NOYRA_READ_TOKEN`, `NOYRA_OPERATOR_TOKEN`, `NOYRA_EXPORT_TOKEN` and an offline
`NOYRA_BREAK_GLASS_TOKEN`, each at least 32 random characters. The legacy `NOYRA_ADMIN_TOKEN`
remains a compatibility all-access token and should be empty in new deployments. Set
`NOYRA_GENESIS_HASH` once and keep a secure record of it. Changing the subject id or genesis hash
does not migrate an existing identity.

Generate a 32-byte archive key outside SQLite and back it up separately:

```bash
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

Set it as `NOYRA_ARCHIVE_ENCRYPTION_KEY`. Cold event payload segments are encrypted before local
or cloud archival; without this key the runtime deliberately refuses to read archived payloads.
`NOYRA_EVENT_PAYLOAD_RETENTION_DAYS` defaults to 90.

Autonomous cognition is disabled by default. Before enabling it, configure the remote model budget
and a compact JSON array of HTTPS sources. Environment files require the JSON to stay on one line:

```ini
NOYRA_COGNITION_ENABLED=true
NOYRA_WORLD_SOURCES_JSON=[{"name":"World RSS","url":"https://example.com/world.xml","source_type":"rss","trust_score":0.5}]
NOYRA_WEB_READ_PUBLIC_BY_DEFAULT=true
```

Replace the example URL with sources you have selected and reviewed. Configured sources remain
required to start cognition. With `NOYRA_WEB_READ_PUBLIC_BY_DEFAULT=true` (the default), Noyra
creates a managed capability for public HTTPS reads, so it can research and activate new public
sources without a host-by-host approval step. The source registry and safe reader still enforce
canonical HTTPS URLs, public-address checks, response limits, auditing, and trust/status checks.
Set the option to `false` in a high-assurance deployment to constrain that managed grant to the
configured world-source hosts. Set daily call, token, and cost limits before enabling cognition.

Autonomous research limits can be tuned independently:

```ini
NOYRA_RESEARCH_INTERVAL_SECONDS=7200
NOYRA_MAX_RESEARCH_MODEL_CALLS_PER_DAY=6
NOYRA_MAX_RESEARCH_CONTEXT_CHARS=36000
NOYRA_MAX_SEARCH_ROUNDS_PER_RUN=2
NOYRA_MAX_SEARCH_RESULTS_PER_ROUND=8
NOYRA_MAX_DISCOVERED_SOURCES_PER_RUN=4
NOYRA_MAX_BROWSER_SEARCHES_PER_HOUR=12
NOYRA_OUTCOME_EVALUATION_INTERVAL_SECONDS=60
NOYRA_MAX_GOAL_PROGRESS_DELTA_PER_OUTCOME=0.05
NOYRA_MAX_EPISTEMIC_REVIEW_MODEL_CALLS_PER_DAY=4
NOYRA_MAX_BELIEF_CONFIDENCE_DELTA=0.15
NOYRA_DEVELOPER_LOG_EXPORT_ENABLED=true
NOYRA_MEMORY_CONSOLIDATION_INTERVAL_SECONDS=86400
NOYRA_MEMORY_STALE_AFTER_DAYS=30
NOYRA_MEMORY_ARCHIVE_AFTER_DAYS=180
NOYRA_MINIMUM_ACTIVE_MEMORIES=24
NOYRA_SOCIAL_REVIEW_INTERVAL_SECONDS=21600
NOYRA_MAX_SOCIAL_MODEL_CALLS_PER_DAY=4
NOYRA_MAX_SOCIAL_CONTEXT_CHARS=32000
NOYRA_SELF_MODEL_REVIEW_INTERVAL_SECONDS=86400
NOYRA_MAX_SELF_MODEL_CALLS_PER_DAY=2
NOYRA_MAX_SELF_MODEL_CONTEXT_CHARS=48000
NOYRA_THOUGHT_INTERVAL_SECONDS=1800
NOYRA_MAX_THOUGHT_MODEL_CALLS_PER_DAY=8
NOYRA_MAX_THOUGHT_CONTEXT_CHARS=32000
NOYRA_MAX_THOUGHT_NO_CHANGE_STREAK=3
NOYRA_THOUGHT_COOLDOWN_SECONDS=21600
NOYRA_MAX_THOUGHT_GOALS_PER_DAY=1
NOYRA_METACOGNITIVE_SLEEP_THRESHOLD=0.82
NOYRA_METACOGNITIVE_SLEEP_SECONDS=3600
NOYRA_METACOGNITIVE_FIXATION_PENALTY=0.2
NOYRA_METACOGNITIVE_PENDING_TIMEOUT_SECONDS=1800
NOYRA_MOTIVATION_REVIEW_INTERVAL_SECONDS=172800
NOYRA_MAX_MOTIVATION_MODEL_CALLS_PER_DAY=1
NOYRA_MAX_MOTIVATION_CONTEXT_CHARS=56000
NOYRA_MAX_VALUE_WEIGHT_DELTA=0.15
NOYRA_MINIMUM_MISSION_VALUE_COUNT=2
NOYRA_MINIMUM_MISSION_SLEEP_COUNT=2
NOYRA_MINIMUM_MISSION_ADOPTION_SLEEP_COUNT=5
```

Configure Brave, Bing, Tavily, or Serper credentials after startup through the authenticated
dashboard. Keys are stored under `/var/lib/noyra/secrets/search`, not in the systemd environment or
SQLite. Back up the whole `/var/lib/noyra` tree so the database and secret references remain
consistent, and use encrypted storage when disk access is in the threat model.

The configured remote provider receives the bounded observation text, source metadata, current
affect labels, and goal summaries needed for world cognition. When interaction cognition is
enabled, it may also receive one current human invitation inside an untrusted-data boundary so it
can decide whether to engage. During reflective sleep, the provider receives a bounded selection
of private memory, interaction, goal, belief, affect, claim, prediction, and failed-action state.
Event payload values, API keys, filesystem file contents, action inputs, and capability tokens are
not included in the reflection context. Choose a provider whose data handling and retention terms
are acceptable for this private-state exposure; operator-facing privacy does not make the remote
provider blind to cognition inputs.

```bash
sudo systemctl status noyra
sudo journalctl -u noyra -f
curl --fail http://127.0.0.1:8765/health
```

The unit runs without root privileges and applies a read-only system filesystem, private temporary
directory, no-new-privileges, kernel protection, and an explicit write exception only for
`/var/lib/noyra`. `NOYRA_REQUEST_TIMEOUT_SECONDS` bounds slow or incomplete HTTP request bodies;
keep an equivalent or shorter timeout at the reverse proxy as well.

## Docker Compose

Docker cannot normally inspect encryption below the host volume. Compose therefore requires two
read-only bind mounts outside the data volume: the backup keyring and a short-lived, root-owned host
attestation. The attestation JSON uses this exact schema and scopes the claim to the container path:

```json
{"format":"noyra-volume-attestation/v1","data_root":"/var/lib/noyra","encrypted":true,"provider":"host-luks","volume_id":"operator-recorded-volume-id","expires_at":"2026-08-19T00:00:00+00:00"}
```

Create both host files under a root-controlled directory, make the attestation non-writable by the
container identity, and renew it before `expires_at`. Set `NOYRA_BACKUP_KEYRING_HOST_PATH` and
`NOYRA_VOLUME_ATTESTATION_HOST_PATH` in `.env`; the example `.runtime/at-rest` paths are development
placeholders only. Then fill the immutable identity and other secret values and run:

```bash
docker compose build
docker compose up -d
docker compose ps
curl --fail http://127.0.0.1:8765/health
```

Compose explicitly forces the container listener to `0.0.0.0:8765` so the container network can
reach it, but publishes the host port only on `127.0.0.1`. The standalone image defaults to
loopback and rejects non-loopback plaintext; changing either binding is an explicit deployment
decision that must be paired with a TLS reverse proxy and authentication. Compose also uses a
read-only root filesystem, drops Linux capabilities, disables privilege escalation, limits process
creation, and stores the subject database in the `noyra-data` volume.

## Backup and recovery

Noyra backups are offline, bounded, and encrypted with the separate versioned backup keyring. The
command acquires the same process lock as the service, uses SQLite's backup API for a consistent WAL
snapshot, packages the persistent data tree, and writes chunked AES-256-GCM with an authenticated
file manifest. It refuses to run while the service owns the subject:

```bash
sudo systemctl stop noyra
sudo install -d -o noyra -g noyra -m 0700 /secure-backups
sudo -u noyra env \
  NOYRA_DATA_DIR=/var/lib/noyra \
  NOYRA_AT_REST_MODE=required \
  NOYRA_VOLUME_ENCRYPTION_BACKEND=auto \
  NOYRA_BACKUP_KEYRING_PATH=/etc/noyra/backup-keyring.json \
  /opt/noyra/current/.venv/bin/python -m noyra backup \
  --output "/secure-backups/noyra-$(date -u +%Y%m%dT%H%M%SZ).noyra-backup"
sudo systemctl start noyra
```

Rotate the backup key deliberately with `sudo /opt/noyra/current/.venv/bin/python -m noyra backup-key rotate
--path /etc/noyra/backup-keyring.json`, then restore `root:noyra` ownership and mode `0640`. Rotation
retains old material. Never remove a retired key while any retained backup names it; key loss makes
that backup intentionally unavailable.

Restore only while the service is stopped and only to an absent or empty child directory on an
encrypted volume. Do not use the mount point itself: plaintext staging is created beside the target,
so the target and its parent must be the same encrypted filesystem. The restore authenticates every
chunk, rejects links/path traversal, verifies every file
hash and SQLite `quick_check`, and publishes the target only after all validation passes:

```bash
sudo systemctl stop noyra
sudo -u noyra env \
  NOYRA_AT_REST_MODE=required \
  NOYRA_VOLUME_ENCRYPTION_BACKEND=auto \
  NOYRA_BACKUP_KEYRING_PATH=/etc/noyra/backup-keyring.json \
  /opt/noyra/current/.venv/bin/python -m noyra restore-backup \
  --input /secure-backups/noyra-YYYYMMDDTHHMMSSZ.noyra-backup \
  --target /mnt/noyra-restore/data
```

Review the restored identity on a separate host or swap directories while stopped. Never start two
processes against the same copied identity. Keep the backup keyring in a separate protected/offline
backup; it is deliberately excluded from subject backups.

## Subject storage and training export

The Windows desktop build and Ubuntu service use the same runtime and storage contract. Set
`NOYRA_DATA_DIR` to a dedicated writable volume. Noyra creates `subject/`, `training_raw/`,
`workspace/`, `cache/`, and `exports/` beneath it; project artifacts are kept outside the subject
data boundary. The SQLite database remains at the historical top-level path for upgrade
compatibility.

Training provenance records event metadata without copying payloads. The authenticated dashboard
provides `/api/config/training-policy` and the asynchronous `/api/admin/export-jobs` API with
`kind: training`; poll the returned job and download the artifact only after it is completed. The
legacy `/api/admin/training-export` GET route remains a compatibility shim that returns the same
`202` job response. Export packages contain eligible `events.jsonl` plus manifest and schema
metadata. Private psychology, credentials, model I/O, conversations, and workspace files are
excluded unless explicitly enabled. Keep downloaded training packages encrypted.

Training-policy updates must include the `expected_version` returned by the latest policy read. A
stale update returns `409` and the current policy. Long-running exports revalidate that exact consent
version and all policy fields at the atomic publication boundary; a committed revocation destroys the
pending archive and prevents the job from becoming downloadable.

Export cancellation is cooperative. Runtime and training workers check cancellation while copying
the database, streaming rows/workspace files, writing ZIP chunks, hashing, and before publication.
Graceful shutdown joins all export workers before releasing the subject process lock; restart cleanup
runs only after the next process owns that lock and preserves completed artifacts. Training export
work, deduplication databases, and pending archives stay below the managed
`$NOYRA_DATA_DIR/exports` tree; same-volume SQLite snapshot images stay at the data root and remain
included in quota accounting. The exporter enforces row, episode, shard, work-byte, archive-byte,
subject-quota, and minimum-free-space limits while writing. Normal exits remove all owned partials;
ownership-gated restart removes work/pending orphans, while P2-16's lock-aware stale scavenger
removes abandoned snapshots after its grace period. The v3 manifest records per-shard row counts,
byte counts, and hashes. These P1-07 contracts are verified on Windows; this host has no Ubuntu/WSL
or systemd runtime, so the Ubuntu and systemd execution remains a CI/release gate.

The bounded integrity audit now treats event cold segments as archived evidence rather than hashing
their `{}` hot-table tombstones. It reconciles manifest counts/timestamps and restored payload hashes
under one SQLite snapshot. A missing or ambiguous archive key, temporarily unavailable local object,
or exhausted synchronous verification budget is reported as degraded; proven manifest, ciphertext,
tombstone, or payload corruption is reported as P0 without overwriting a successful SQLite check.
This P1-03 verifier is available to the audit harness, but P1-02 production startup/periodic scheduling
and automatic safe pause remain release blockers. Key rotation and recovery still require the open
P1-11 keyring work.

## Updates and rollback

The installer uses versioned releases under `/opt/noyra/releases`. Each invocation creates a fresh
profile-specific virtualenv, validates imports before publication, and atomically replaces the
`/opt/noyra/current` symlink. When a current release exists, `/opt/noyra/previous` is recorded before
the switch. An install lock prevents concurrent upgrades. An already-active service is stopped and
restarted only after the new release passes both `systemctl is-active` and `/health/ready`; an
inactive or first-time service is left stopped. Code rollback is not a data rollback: database
migrations are forward-only, so a failed code deployment may still require restoring the recorded
pre-update backup before the old release can safely run.

Run the deployment audit before an update. The installer also creates an encrypted cold backup
outside `/var/lib/noyra` before replacing an existing release:

```bash
./scripts/audit-deployment.sh
sudo ./scripts/install-ubuntu.sh --profile base --release-id "$(git rev-parse --short=12 HEAD)-$(date -u +%Y%m%d%H%M%S)"
```

The installer is intentionally run as root for release and systemd changes, but it runs the cold
backup itself through `runuser` as `noyra`. The backup keyring therefore remains `root:noyra` mode
`0640`; do not change it to a root-only `0600` file or make it globally readable. A per-upgrade
staging directory is temporarily `root:noyra` mode `1770` so the service account can create the
encrypted file and fsync its parent directory; root-owned marker and lock files remain mode `0600`.
Root validates the artifact, publishes it as `root:root` mode `0600`, and restores
`/var/backups/noyra` to root-only mode `0700` before continuing.
The staging directory carries root-owned marker and lock files and is accepted only with the
expected UID/GID, mode, link count, and filesystem device. The lock is held across the `runuser`
backup process so a hard-killed parent cannot be mistaken for an orphan while its child is still
running. If cleanup cannot restore the root-only boundary, the
installer leaves Noyra stopped. If `runuser` is unavailable, the installer refuses to stop the
service or begin the upgrade.

The default `/var/backups/noyra` directory may be created by the installer under the root-owned
`/var/backups` parent. A custom `--backup-dir` or `NOYRA_UPGRADE_BACKUP_DIR` must already exist;
every path component must be a real root-controlled directory with no group/other write
permission, and the final directory must be `root:root` mode `0700`.
Provision it before upgrading, for example:

```bash
sudo install -d -o root -g root -m 0700 /secure-backups/noyra
```

Use `--profile cloud` when S3 archive support is required. `base` and `cloud` are never installed
into the same virtualenv. A dependency, backup, pointer, or readiness failure leaves the old
release selected; if the service was active, the failure handler restores the old code and starts
it again. The data backup is intentionally retained for operator review. Code rollback is not a
data rollback: database migrations are forward-only, so if the new process has already migrated
the database, stop the service and restore the pre-update backup to an empty child directory on an
encrypted volume, then re-point/start the known-good release only after checking identity and
integrity.

To select the recorded previous code release after a failed deployment:

```bash
sudo ./scripts/install-ubuntu.sh --rollback
sudo systemctl status noyra
curl --fail http://127.0.0.1:8765/health/ready
```

Do not delete `/opt/noyra/releases/<id>` while it is selected by `current` or `previous`. Keep at
least one verified backup keyring copy separate from the subject data; losing retired key material
makes the corresponding encrypted backup intentionally unavailable.

`SIGTERM` and `SIGINT` stop the service loop, close the HTTP listener, cooperatively cancel and join
export workers, and only then release the subject lock. Systemd waits up to 30 seconds before
escalating; forced process termination ends every worker before the operating-system lock can be
acquired by a replacement process.

## Operational boundaries

For a remote staging deployment, use the reproducible runbook and read-only evidence collector in
[`remote-validation.md`](remote-validation.md). It keeps port 8765 on loopback, uses an SSH tunnel,
and separates service soak, deterministic synthetic soak, fault injection, and restore evidence.

- `/health` proves the process can read public lifecycle state; it is not proof that a remote model
  provider or every external source is reachable.
- The default six-hour deep-sleep interval can be changed with `NOYRA_DEEP_SLEEP_SECONDS`.
- Economy and deep cognition providers are configured independently with
  `NOYRA_ECONOMY_MODEL_GROUPS_JSON` and `NOYRA_DEEP_MODEL_GROUPS_JSON` or through the
  authenticated configuration panel. Provider API keys are stored under the runtime data
  directory's `secrets/models` path and must remain writable only by the service account.
- HTTP messages are communication invitations. Successful POST delivery never creates a goal or an
  action. The web endpoint fixes its channel and counterparty identity; callers cannot select a
  public or external transport channel.
- `GET /api/mailbox` requires the read token; `POST /api/interactions` requires the operator token. The mailbox returns
  web-channel transmission records only, never private rationale, appraisal, affect, model prompts,
  or idempotency keys. Keep it behind the same authenticated local access path as the dashboard.
- `NOYRA_INTERACTION_COOLDOWN_SECONDS` limits how frequently invitations receive a decision, and
  `NOYRA_MAX_INTERACTION_MODEL_CALLS_PER_DAY` sets a separate global daily cap for interaction
  deliberation. Reaching this cap does not stop world cognition.
- `NOYRA_MAX_SLEEP_MODEL_CALLS_PER_RUN` bounds reflection attempts for one sleep and
  `NOYRA_MAX_SLEEP_CONTEXT_CHARS` caps its serialized private context. Invalid proposals are rejected
  locally; provider failure or budget denial eventually commits a conservative no-change reflection
  so the lifecycle can still enter deep sleep.
- `NOYRA_GOAL_GOVERNANCE_INTERVAL_SECONDS` spaces awake goal reviews,
  `NOYRA_MAX_GOAL_GOVERNANCE_MODEL_CALLS_PER_DAY` caps their daily remote calls, and
  `NOYRA_MAX_ACTIVE_GOALS` limits simultaneous active directions. Goal governance receives no raw
  human message text and cannot invoke a tool or external channel.
- `NOYRA_ACTION_DELIBERATION_INTERVAL_SECONDS` spaces autonomous investigations,
  `NOYRA_MAX_ACTION_DELIBERATION_MODEL_CALLS_PER_DAY` caps their remote deliberation calls, and
  `NOYRA_MAX_WEB_ACTIONS_PER_GOAL_PER_DAY` prevents one goal from monopolizing authorized reads.
  M14 permits only read-only fetches of capability-authorized HTTPS sources. By default the
  environment-managed capability covers public HTTPS; set
  `NOYRA_WEB_READ_PUBLIC_BY_DEFAULT=false` to limit it to configured source hosts.
- `NOYRA_RESEARCH_INTERVAL_SECONDS` spaces autonomous source discovery,
  `NOYRA_MAX_RESEARCH_MODEL_CALLS_PER_DAY` limits research planning, assessment and fallback calls,
  and the round/result/source settings bound each run. Search API resources have their own hourly
  limits configured in the dashboard. Browser search has a separate hourly limit and uses a public
  Bing RSS search surface without an API key or extra model call. With no configured API, Noyra
  chooses model or browser fallback; discovered URLs remain candidates until normal source
  activation and observation.
- Web content remains untrusted data. Cognition and action proposals are locally validated before
  claims, predictions, affect, goals, observations, or action records are committed.
- Outcome evaluation runs before new goal governance. It consumes no model token and uses only
  durable action, observation and research records. Candidate search sources never count as goal
  progress; one verified new observation advances a goal by no more than
  `NOYRA_MAX_GOAL_PROGRESS_DELTA_PER_OUTCOME`.
- Epistemic review uses only analyzed observations to revise beliefs or settle due predictions.
  Confidence changes are bounded by `NOYRA_MAX_BELIEF_CONFIDENCE_DELTA`; insufficient evidence
  leaves a prediction open. Its daily model calls have a separate limit.
- `GET /api/runtime-logs` requires the read token; exports require the export token. The export
  contains private psychology, communications, observations, model outputs, actions, sleep and
  recovery records. Secret files and environment variables are excluded and credential-shaped
  fields are redacted, but the resulting archive must still be stored encrypted. Disable the
  endpoint with `NOYRA_DEVELOPER_LOG_EXPORT_ENABLED=false` after development introspection is no
  longer required.
- Daily budget boundaries use UTC. The dashboard and exported manifests label timestamps with
  their UTC offset; do not assume a local-midnight reset when reconciling provider invoices.
- Telegram, Feishu, QQ and WeChat are configured as operator-owned HTTPS adapters; email uses an
  SMTP endpoint. A message is only an intent until its delivery record reaches `delivered`.
- Long-term memory recall is local and consumes no model token. Recall access history and
  consolidation decisions are included in the developer export. Consolidation archives weak
  inactive memories instead of deleting them; tune its interval, age thresholds and minimum active
  set with the `NOYRA_MEMORY_*` settings.
- Proactive social cognition considers one known human relationship at a time, only after a channel
  has already been established by an interaction. It may record a contact or help-request delivery
  intent in the durable mailbox, or choose to wait. The built-in web mailbox is a local transport
  record, not proof that an external service delivered the message. No-contact, cooldown and severe
  conflict boundaries are enforced before any social model call. Tune its interval, daily calls and
  private context size with the `NOYRA_SOCIAL_*` settings.
- The operational self-model is formed only after one completed sleep and is stored as an immutable
  revision history. It can cite only supplied events, memories, beliefs, goals, relationships and
  sleep-derived personality candidates. The remote provider receives this bounded private context;
  the public state exposes only version, status and update time. Tune review cadence, daily calls and
  context size with the `NOYRA_SELF_MODEL_*` settings.
- Intrinsic attention converts durable goal tension, unresolved sleep questions, strong affect,
  relationship tension and self-model uncertainty into a private agenda. Each tick processes at
  most one agenda item, cannot execute tools or communicate, and uses independent interval and daily
  call limits. Repeated no-change thoughts enter cooldown, identical conclusions are rejected, and
  internally generated goals require current affect plus a separate daily limit. Tune this behavior
  with the `NOYRA_THOUGHT_*` settings.
