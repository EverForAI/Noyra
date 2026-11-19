# Online migration artifact staging

Online migration measures a repeatable subject-state v1 tar, then emits the same bytes directly through recipient-bound AES-GCM encryption. Size and SHA-256 must match before publication. The v1 bundle, authenticated metadata, inventory and target decoder stay compatible.

## Material boundaries

| Material | Source staging |
| --- | --- |
| SQLite snapshot | Exclusive private file under the source data root in migration/source/snapshots |
| Wallet keystore/password | Original approved files; no complete plaintext tar copy |
| Wallet RPC configuration | Bytes in memory; no temporary RPC file |
| Complete tar | Measured and encrypted through bounded writes; no plaintext spool |
| Encrypted output | Private temporary file, published without replacing an existing destination after verification |

Snapshot names are a dot, the artifact ID, and the .sqlite suffix. The directory rejects links and cross-device placement and is hardened before file creation. The file is created exclusively with POSIX 0600 or the private Windows directory ACL. SQLite backup uses page batches and a bounded cache without full-database serialization or an in-memory database copy. Hashing and encryption use at most 1 MiB chunks. Existing 100,000-file, 32 MiB inventory and 10 GiB payload/bundle limits remain. Metadata limits are separate from content buffers.

**SQLite scratch remains plaintext in SQLite format.** Same-filesystem placement and private permissions do not prove physical encryption. Production must retain its verified encrypted-volume at-rest boundary. A direct library call does not establish that boundary. Process exit or power loss may retain a private partial snapshot or encrypted partial bundle. This change does not eliminate every plaintext file, provide secure deletion, retract delivered bytes or prove physical power-loss durability. Target-side plaintext restore staging is unchanged.

Wallet approval is checked before the snapshot, before material discovery, and before and after each emission pass. Checks retain task, policy, target, recipient, source epoch, expiry, revocation and one-artifact authorization. Repeated checks do not issue extra approvals. Revocation observed during either pass aborts publication. Existing cutover/reconciliation rules still apply to bytes already published or delivered.

## Failure and recovery

Ordinary prepublication failure removes only this invocation’s snapshot after checking its identity. A replacement path or hard link is preserved and reported as cleanup failure. Existing snapshots and legacy output files with the same artifact ID and .sqlite, .tar or .rpc.json suffix return artifact_snapshot_recovery_required; they are not consumed or deleted. An exporter losing exclusive creation does not delete another exporter’s file.

A post-publication filesystem error can leave a complete encrypted destination without a returned result, as in the existing publication protocol. Treat that as ambiguous rather than overwriting it. Retained snapshots and encrypted temporary files are not committed or resumable migration artifacts.

1. Establish that no exporter is writing the task; inspect its source fence, authorization and any target acceptance/activation evidence.
2. Inspect the exact artifact’s snapshot and partial output in their private roots. Preserve ambiguous material for review. Never remove the live database, keystore, password or unrelated artifacts.
3. Apply the operator’s retention/recovery policy to those exact files. There is no startup glob deletion or unbounded recursive sweep.
4. Retry only after that evidence is resolved and the task and permit remain valid. Expired/failed tasks require the existing recovery protocol; changing artifact identity cannot bypass one-artifact approval.

The standalone create_payload compatibility API still writes a plaintext tar when explicitly called. Online migration no longer calls it. This change does not redesign offline export, backup restore or wallet approval.
