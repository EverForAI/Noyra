# Explicit backup-key commands and API migration

Backup rotation is an operation identified by a retained request ID and the exact
source version. An exception or missing command response does not mean that the
keyring was not replaced. All retries must retain the same operation identity.
Initialization, key reading, backup creation and restoration keep their existing
interfaces and encrypted v1 keyring/backup formats.

## Commands

`python -m noyra backup-key` provides five actions:

| Action | Required arguments besides `--path` | Behavior |
|---|---|---|
| `init` | None | Existing initializer and one metadata response |
| `status` | `--request-id` | Read-only keyring/journal inspection |
| `prepare` | `--request-id`, `--expected-generation`, `--expected-source-hash` | Persist exactly one proposed target; leave current keyring unchanged |
| `resume` | `--request-id` | Publish or confirm only that existing request |
| `rotate` | `--request-id`, `--expected-generation`, `--expected-source-hash` | Prepare if missing, otherwise resume the original bound request |

Generate a lowercase 128-bit hex request ID once and save it in the operator's
maintenance record. `status` returns `generation` and `observed_keyring_hash`.
Use those values as the expected source for `prepare`/`rotate`; do not re-read and
substitute the new source values after an uncertain outcome. The ID is not a
credential; journal snapshots are secret and must never be pasted into a report.

For a new operation, the preferred sequence is `status` → `prepare` → `resume`.
Preparation returns an immutable target hash. A missing request cannot be created
by `resume`. A combined `rotate` with the same ID and the original expectation
returns the same accepted operation even when the current keyring has advanced.
Changing the expectation while reusing an ID is a conflict. Starting a different
ID with the old source expectation also fails instead of rotating the new source.

## Output, exit codes and lost responses

Mutation commands emit a JSON request record and flush stdout before touching
storage. A new combined rotation also flushes its prepared target summary before
publication. Successful completion emits a JSON result record; output is JSONL,
not one large JSON object. `status` emits only its result, and `init` retains the
previous single metadata object. Results contain IDs, hashes, generations and
state, never key bytes or local paths. Read-only status never confirms a fresh
durability barrier.

| Exit code | Meaning | Next action |
|---:|---|---|
| 0 | Action completed; inspect the returned state | Retain the request record |
| 2 | Missing or invalid command arguments | Correct the arguments before retrying |
| 3 | Preparation/publication/response outcome is unknown | Inspect or resume the same saved request |
| 4 | Evidence, expectation or private storage conflicts | Inspect that request/storage; do not start another rotation |
| 5 | Required POSIX durability is unsupported | Use supported storage/platform; no publication attempted |
| 6 | Another process owns the rotation lock | Retry the same request after it releases ownership |

Errors are bounded JSON on stderr without raw exceptions, record contents,
commands or local filesystem paths. I/O or stdout failure after acceptance can
still mean the new generation is visible. If the initial request output fails,
mutation does not start; if the prepared summary fails, the target remains
unpublished and the prepared request can be resumed. When a response is lost,
retain the ID and source expectation, inspect, then resume the same request.

## Python API compatibility change

`BackupKeyring.rotate(path)` without explicit fields now raises
`RotationRequestRequiredError` before opening the path, acquiring a lock or
allocating entropy. The unsafe implicit writer and its `_rotate_locked` path have
been removed. Passing only some fields is also rejected.

The explicit call is:

```python
from uuid import uuid4
from noyra.core.at_rest import BackupKeyring
from noyra.core.backup_rotation import BackupRotationJournal

request_id = uuid4().hex  # Persist this ID before attempting mutation.
journal = BackupRotationJournal(keyring_path)
observed = journal.status(request_id=request_id)
assert observed.observed_keyring_hash is not None
result = BackupKeyring.rotate(
    keyring_path,
    request_id=request_id,
    expected_generation=observed.generation,
    expected_source_hash=observed.observed_keyring_hash,
)
```

The return type is now `RotationJournalStatus`, safe operation metadata, rather
than a `BackupKeyring` containing keys. Call `BackupKeyring.from_path` explicitly
when current decryption material is actually needed. A previous `.resolve` call
on the return value must be migrated. `BackupRotationJournal.rotate` exposes the
same combined protocol; `prepare`, `status` and `resume` remain available.
For retries, use the saved fields, not the current observed generation/hash.

## Platform and deployment scope

New rotation requires the POSIX file/directory synchronization profile verified
by the [publication protocol](backup-rotation-publication.md). Windows reports
exit 5 before keyring/journal access or lock creation for `prepare`/`rotate`/
`resume`; initialization, reading, backup/restoration and private-ACL status
inspection continue to work. This is an explicit capability boundary, not a
fallback that reports a no-op synchronization as durable.

The [journal contract](backup-rotation-journal.md) and
[path boundary](backup-keyring-rotation.md) still apply. Rotate through the new
commands; do not delete journal evidence to revive a legacy writer. There is no
runtime/database migration, provider call or network-policy change. Synthetic
process-exit tests verify request recovery, not hardware power failure, arbitrary
network filesystems or controllers that misreport synchronization completion.
