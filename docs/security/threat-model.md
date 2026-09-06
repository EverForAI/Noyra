# Noyra Threat Model

## Assets

- Subject identity, event chain, memory revisions, private psychology, and
  communication history.
- Provider credentials, capability grants, archive encryption keys, and export
  artifacts.

## Trust boundaries

- The subject database and secret directory are local trusted storage.
- Remote models, web pages, search providers, transports, and imported common
  knowledge are untrusted or externally controlled inputs.
- Operators can configure resources and stop the process, but cannot write
  subject-private memory through the public projection.

## Primary threats and controls

| Threat | Control |
|---|---|
| Credential leakage | Secrets live outside SQLite; export and model IO redaction; encrypted archives |
| Cross-subject evidence injection | Subject-scoped causal and ownership validation; append-only event chain |
| Repeated external side effects | Durable idempotency keys, unknown-result quarantine, bounded retries |
| Management-plane compromise | Read/operator/export/break-glass roles, request limits, loopback default |
| Storage exhaustion | Cold payload segments, quotas, cache cleanup, bounded exports and job queue |
| Malicious shared knowledge | Ed25519 signatures, trusted publishers, quarantine, scoped payload schema |
| Host file escape | Capability grants, symlink/TOCTOU checks, static prototype artifact boundary |

## At-rest boundary

The production at-rest contract is `noyra-at-rest/offline-media-v1`. It protects the hot SQLite
database, WAL/SHM files, local secret files, cold subject segments, and offline backups against an
attacker who obtains powered-off media, a detached disk/cloud-volume snapshot, or a copied backup
without the volume and backup keys. It does **not** claim to protect data from a live host root or
Windows Administrator, a compromised signed-in service account, process-memory inspection, or an
already unlocked encrypted volume. Those actors are inside the host trust boundary and can read the
same plaintext that Noyra must read while running.

The hot database stays in native SQLite format for migration, recovery, and WAL correctness. When
`NOYRA_AT_REST_MODE=required`, the service therefore fails before opening or creating the database
unless all of these controls are established:

- the data root, SQLite files, and secret tree are private to the service identity (`0700`/`0600`
  on Ubuntu, or a protected Windows DACL limited to the current identity, SYSTEM, and
  Administrators);
- the data root is on a fully protected BitLocker volume or below a dm-crypt LUKS mapping;
- a container has a read-only, root-owned, unexpired host-volume attestation scoped to the exact
  container data root when the host encryption layer is not visible in the container;
- a valid versioned backup keyring exists outside the data root.

The same checks run again at boot and are projected through `/health`. Permission drift, missing
keys, an expired attestation, disabled BitLocker protection, or an unverified Linux block-device
chain makes the required boundary unavailable; there is no plaintext fallback.

Offline backups use a separate keyring (`noyra-backup-keyring/v1`) rather than a key embedded in the
database or backup. Every backup is chunked AES-256-GCM with authenticated ordering and termination.
The encrypted payload contains a complete file hash manifest and a SQLite backup-API snapshot.
Restore decrypts only into a private staging tree on the target encrypted volume, rejects links and
path traversal, verifies every file and `PRAGMA quick_check`, and publishes only to an absent or
empty target after all checks pass. Rotation adds a new active key and retains old keys as retired;
operators must keep every key referenced by a retained backup. Losing or deleting a historical key
deliberately makes that backup unavailable and never causes an unauthenticated restore.

The model does not claim that remote providers, a compromised host, or a
malicious operator are trustworthy. Production deployments must protect the
data directory, archive key, bearer tokens, and reverse-proxy termination.
