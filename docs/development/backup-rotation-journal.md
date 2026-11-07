# Backup rotation preparation journal

`BackupRotationJournal` adds an administrative preparation and inspection API.
Preparation records a proposed v1 keyring privately; it leaves the active
keyring bytes, inode, generation and service-group permissions unchanged. This
module does not publish that proposal or create an acceptance receipt.

## Explicit request and expected source

The local Python API lives in `noyra.core.backup_rotation`. Create a stable
128-bit request ID before preparing. A read-only `status(request_id=...)` returns
the observed keyring generation and `observed_keyring_hash`, together with any
matching recorded intent. Pass both observed values to `prepare`:

```python
from uuid import uuid4
from noyra.core.backup_rotation import BackupRotationJournal

journal = BackupRotationJournal(keyring_path)
request_id = uuid4().hex
observed = journal.status(request_id=request_id)
assert observed.observed_keyring_hash is not None
prepared = journal.prepare(
    request_id=request_id,
    expected_generation=observed.generation,
    expected_source_hash=observed.observed_keyring_hash,
)
```

Callers must retain the request ID. Another operation may advance the source
between inspection and preparation; the explicit generation and complete byte
hash then cause rejection. Retrying the same prepared request validates and
synchronizes the same immutable intent instead of generating another key.
An unresolved request prevents preparing a different request. Errors and result
objects contain IDs, hashes and states, never key snapshots.

## Private storage contract

The journal is the sibling directory `.<keyring-name>.rotation-journal`. The
header binds a random slot to the verified target location. Immutable intents
bind that header, request, exact source and target bytes and their hashes. Strict
validation requires exactly one new active key, an increase of one generation,
and unchanged retained keys marked retired. Acceptance records have a separate
strict format and must refer to an existing intent. Complete recorded history
must form a consecutive source/target chain; a predecessor must be accepted
before a successor may be prepared.

Intents contain backup keys. They require the same protected storage boundary
as the keyring, with an owner-only directory and files (`0700`/`0600` on POSIX).
They are not audit logs, public receipts, source exports or ordinary artifacts.
Repository ignore rules exclude these directories, and the publication boundary
also rejects them if they are forcibly staged.
Retained snapshots also retain their keys; future key destruction must account
for them. This module does not automatically prune records.

The reader accepts at most 63 operations, 127 directory entries, 2 MiB per
intent, 4 KiB per header/acceptance record and 8 MiB in total. Unknown, nested,
linked, incomplete, malformed or over-budget entries cause rejection. A private
orphan is preserved for explicit inspection rather than silently deleted.

Writes require the existing stable sibling rotation lock. Journal creation and
its parent entry are synchronized before preparation completes. Records are
written privately, file-synchronized and published create-only; an unexpected
existing file is never replaced. Directory synchronization and protected
readback follow publication. An I/O exception can leave a visible preparation:
`RotationPreparationUnknownError` preserves that uncertainty, and a same-request
retry verifies the existing evidence. The active keyring is not published by
this preparation API, including on error paths.

## Original paths, revisions and inspection

`BackupRotationLocation` retains original path, ancestor and held-lock checks.
`BackupRotationBoundary` additionally pins one source revision. A fresh target
revision cannot silently reset that pinned boundary. Journal reads validate
original components, ownership, link counts and descriptor/path identities, and
inspection rejects a changing catalog.

`status` creates no directory or lock, generates no entropy, and performs no
fsync or write. It describes observed records: `absent`, `request_missing`,
`prepared_recorded`, `target_visible_unconfirmed`, `accepted_recorded`,
`accepted_superseded` or `conflict`. A read-only result never sets
`durability_confirmed`. A successful `prepare` returns `prepared` and confirms
only the preparation evidence, not publication of the main keyring.

Once a journal path exists, legacy `BackupKeyring.rotate` refuses publication
under the same lock. This also applies to incomplete or linked journal paths;
it prevents a legacy writer from bypassing a recorded proposal. Ordinary
rotation without a journal, key initialization, reading, backup and restoration
retain their existing behavior. Do not delete preparation evidence merely to
re-enable a legacy command.

## Platform and rollout scope

Preparation requires POSIX file and directory durability primitives. Other
platforms reject it before opening the keyring or creating a lock/journal.
Windows inspection still enforces the existing private-ACL policy. A no-op
Windows directory synchronization is not reported as durable preparation.
Native process-exit tests demonstrate process recovery, not hardware power-loss
behavior. Publication, resume, acceptance writing and CLI migration belong to
subsequent modules. No runtime or database migration is required for preparation.
