# Publish and resume a prepared backup rotation

`BackupRotationJournal.resume(request_id=...)` publishes one existing intent.
It never prepares a missing request or allocates another key. The keyring stays
in its existing v1 format; the journal's immutable source/target snapshots and
hashes define the operation. The API is restricted to POSIX durability primitives.
Windows rejects publication before creating a lock or changing storage.

## Explicit lifecycle

Prepare with a retained request ID, expected generation and complete source hash
as described in the [journal contract](backup-rotation-journal.md). Then use that
same ID for every publication attempt:

```python
from noyra.core.backup_rotation import (
    BackupRotationJournal,
    RotationConflictError,
    RotationOutcomeUnknownError,
)

journal = BackupRotationJournal(keyring_path)
try:
    result = journal.resume(request_id=saved_request_id)
except RotationOutcomeUnknownError:
    # Retain saved_request_id. Inspect or resume it after storage recovers.
    raise
except RotationConflictError:
    # Inspect the existing evidence and storage; do not start a new rotation.
    raise
```

A successful `accepted` result confirms the exact target and acceptance receipt
under the declared file/directory synchronization profile. If an older accepted
request has been followed by a valid later recorded version, `accepted_superseded`
confirms the earlier request without reverting the current file. Its target
metadata describes that earlier operation; it does not freshly attest the later
version's durability. A conflicting or unavailable source is never overwritten.

## Publication order and fixed bytes

Under the existing stable sibling lock, the publisher validates the complete
bounded catalog and current keyring against the latest recorded chain. It then
reconfirms the header and requested intent before changing the target.

The exact prepared target is written into a reserved sibling of the main keyring:
`.noyra-rotation-<slot-and-request-digest>.publish.tmp`. The slot/request binding
prevents two keyrings in the same directory from sharing a staging name. The
file has the original path/owner/link-count/descriptor checks, bounded bytes and
private permissions; only an exact prefix of this intent may be recovered. The
original owner, group and mode are applied after complete bytes exist. This keeps
root-owned `0640` service-group access and valid private owner modes intact.

Final replacement uses a same-parent atomic rename. It avoids making publication
depend on independent persistence of a cross-directory source removal. Source
revision, original ancestors, held lock and staged inode are checked immediately
before replacement. The location boundary stays valid across a legitimate inode
change; no pinned source revision is reset to bypass a check.

After replacement, file and parent-directory synchronization plus protected
readback establish that this exact target is current. If the target is already
visible on retry, the publisher performs confirmation instead of another rename.
A deterministic acceptance-stage file in the private journal is file-synchronized
and linked create-only to its final receipt name. Only its exact two private
aliases may coexist after an interrupted link/unlink. Recovery validates and
removes the known alias, synchronizes the receipt and journal, and checks the
target again before returning success. An unexpected existing final receipt is
never overwritten.

## Unknown outcomes, conflicts and stage recovery

A failed call does not prove that replacement did not happen. I/O failures raise
`RotationOutcomeUnknownError`, preserving the request ID and a bounded visibility
observation when possible. A known evidence, identity, source or budget conflict
raises `RotationConflictError`; this also must not be interpreted as proof of no
side effects from an earlier attempt. Read-only `status` remains observational
and does not issue synchronization or confirm fresh durability.

Known temporary paths may contain incomplete prefixes after process termination.
Only the exact slot/request path with trusted ownership, a regular single-link
inode and a matching prefix is resumed. Mismatching content, reparse entries,
unrelated hard links, unknown journal orphans and a broken catalog are preserved
and rejected. No broad temporary-file deletion is performed. An acceptance
receipt's paired link is the only bounded journal hard-link exception. A third
link or a link outside that pair is rejected.

The same-directory publication stage contains key material. It is not a log or
public artifact. It is covered by ignore rules and the forced-add publication
boundary, and future key destruction must account for abandoned stages as well
as intent snapshots. Successful recovery removes the known stage. Inspection
reports catalog/keyring state; it does not claim every staging artifact is healthy.

## Compatibility and validation boundary

The legacy rotation writer remains fenced when a journal exists. Backup reading,
creation and restoration use the current keyring as before, including all retained
old decryption keys. The runtime, database schema, network access and provider
budgets are unchanged. This module exposes the Python publication API; CLI
request/expectation migration is a separate change. Do not delete evidence to
make a legacy command run again.

Tests inject errors before/after writes, synchronization, replacement, receipt
linking, alias removal and response, plus partial writes and unavailable readback.
Separate processes exit at each named checkpoint and during partial stage writes;
other processes contend for the same stable lock. Tests verify one generation
advance, a fixed target hash, no rename on visible-target retry, old-backup
restoration and service-group access. Process termination is not a hardware
power-cut test. Durability depends on the local filesystem honoring the platform
synchronization contract; the suite does not certify arbitrary network filesystems,
controllers that misreport flush completion or an attacker controlling the account.
