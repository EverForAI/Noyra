# Encrypted backup restore publication

Restore is an offline operation. Keep the service stopped and use an absent or
empty child directory whose parent is on the same encrypted filesystem. An empty
target contains no prior subject data to preserve. The input encrypted backup and
its external keyring remain the retry source; restore does not consume either.

## Validation and publication order

The manager authenticates the encrypted archive, extracts bounded regular files,
verifies its manifest and SQLite database, replays current privacy deletions, and
hardens permissions before publishing. Extraction already syncs each file, but
privacy replay and permission hardening happen later. Publication therefore syncs
all final files again, then syncs nested directories from leaves to root on POSIX.
Empty directories are included. It also syncs the staging parent before moving
the tree and both rename parents afterward. If the target's parent directories
were created by this restore, their parent entries are synced toward the first
existing ancestor before success is returned.

On POSIX, one directory replacement publishes an absent target or atomically
replaces an existing empty target. Restore does not first remove the empty target.
On Windows, directory replacement requires an absent destination, so an existing
empty target is removed immediately before movement. The manager uses
`MoveFileExW` with `MOVEFILE_WRITE_THROUGH`, without `MOVEFILE_REPLACE_EXISTING` or
cross-volume copy fallback. A native movement error is propagated, and an ordinary
failure before publication recreates a preexisting empty target if needed.

## Failure and retry

- Authentication, replay, permission, or pre-publication sync failure publishes no
  restored identity. Ordinary exception cleanup removes this attempt's staging.
- Failure after the directory has moved can leave the complete target published
  even though the call raises. In particular, a final POSIX directory-sync error
  must not be reported as success. The manager keeps that target and refuses to
  overwrite it on retry. Investigate it while the service remains stopped, or
  retry from the retained encrypted backup into a different absent child directory.
- A process terminated with `os._exit`, a kill, or a host crash does not execute
  Python's `finally` cleanup. An orphan `.noyra-restore-*` directory can remain
  beside the target. Before movement it can contain the complete validated tree;
  after movement the tree is at the target. Neither case deletes the encrypted
  input. On Windows, interruption after removal of an empty target can leave that
  target absent; restore to a fresh target from the encrypted backup.

Do not promote an orphan plaintext staging directory manually. Keep it on the
encrypted volume until it has been reviewed, and retain the encrypted archive and
historical key generation until an independent restore has been verified.

## Durability evidence and platform limits

Tests cover absent and empty targets, final-file and directory-sync failures,
native movement failure, post-move sync failure, new parent directories, privacy
replay ordering, and native process exits immediately before and after movement.
They compare all restored file hashes and verify that the encrypted input remains
unchanged and usable for another restore.

POSIX `fsync` calls and the Windows write-through request are explicit persistence
requests. Windows has no general directory `fsync` implemented by this module;
the write-through move is not a substitute for flushing every nested directory's
metadata. Neither platform's process-exit tests prove power-loss behavior of a
particular filesystem, controller, device cache, or encrypted volume. Keep the
original backup and keyring, and complete the deployment's host-specific restore
and power-loss acceptance work before relying on that behavior. This protocol
does not replace a live, nonempty data directory or a mounted volume root.
