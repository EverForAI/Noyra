# Read-only SQLite roles

`Database(path, initialize=False, read_only=True)` opens an existing database
with SQLite `mode=ro` and `PRAGMA query_only=ON`. This is the default for live
runtime preflight and local diagnostics. Committed changes in an active WAL
are visible. A `read_transaction()` holds one SQLite snapshot across its
queries; a later transaction may observe newer commits.

Read-only means no writes to the application's tables or schema. SQLite may
create or update `-wal` / `-shm` coordination files, depending on journal mode,
existing sidecars, permissions and SQLite version. A live reader therefore
needs the directory/sidecar permissions required by SQLite. A failed live open
must not fall back to `immutable=1`: that can hide committed WAL changes.
Long-lived read transactions also retain WAL history. Use the existing
`read_snapshot()` facility when an isolated copy is appropriate for long work.

`Database(path, initialize=False, read_only=True, immutable_snapshot=True)`
is reserved for a sealed snapshot. The caller must ensure that the file has
no concurrent writer and contains all committed state in its main database
file, for the whole period of reading. Examples are a verified migration
payload in a privately controlled staging tree and a fixed offline fixture.
Copying a live main file without its journal is not a sealing procedure.
Use a fenced, coordinated SQLite backup to produce a complete snapshot.

Snapshot mode explicitly uses `immutable=1`. It rejects visible `-wal`, `-shm`
or `-journal` paths on every connection, including empty files and dangling
links, instead of silently ignoring journal state. That check is a safeguard,
not a replacement for caller-owned sealing: a writer can start after a check,
or use a journal mode without those files. There is no automatic detection of
immutability based on file permissions or an absent WAL. A sealed snapshot
can be read without generating coordination files, including in a directory
without write permission, and cannot be promoted to the writable facade.

Both read-only roles reject initialization and missing files without creating
a database or parent directory. SQLite `mode=ro` still blocks writes if a caller
disables the extra `query_only` guard. Paths are encoded as file URIs so spaces,
Unicode and URI metacharacters do not alter connection options.

Runtime ownership remains a separate boundary. A live preflight facade may
be promoted only by its owning runtime after acquiring `ProcessLock`; the
`Database` facade itself is not a lock manager. Migration archive/credential
validation opts into snapshot mode for its staged copies. Other live readers
keep the WAL-aware default. No schema migration or persistent data rewrite
is required to select these roles.

`tests/test_a4_readonly_database.py` exercises committed/uncommitted WAL reads,
consistent transactions, write rejection, missing inputs, sealed files,
journal refusal, filename escaping and runtime ownership transfer. Its POSIX
permission case requires a non-root account; Windows or root-only runs must
report that case as skipped rather than claim equivalent permission coverage.

## Runtime construction versus explicit diagnostics

A service that loses the process-lock race constructs only a deferred preflight
facade. Its constructor and rejected `boot()` must not open SQLite or change the
main file or its coordination files. Calling `kernel.health()` explicitly is a
separate live diagnostic read, with the WAL-aware read-only contract above. It
must see committed WAL identity/checkpoint/lifecycle changes without acquiring
ownership or enabling writes. Main-file and committed WAL bytes remain unchanged;
SHM coordination state is not treated as application data or immutable evidence.

The startup gate checks the zero-access boundary before requesting diagnostics.
A separate live-WAL regression verifies the diagnostic state, unchanged business
rows and write rejection with `query_only` disabled. Restoring `immutable=1` for
these live reads would violate the diagnostic contract even if it happened to
avoid creating sidecars.
