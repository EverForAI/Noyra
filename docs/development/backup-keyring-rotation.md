# Backup keyring rotation boundary

Administrative rotation keeps the backup-keyring v1 format, monotonically
advancing generations and retained decryption keys. It uses the existing stable
sibling `.rotation.lock` and remains non-blocking when another process owns that
lock. Rotation does not acquire runtime ownership, change database state or
perform provider calls.

## Accepted storage layout

Use an ordinary keyring file and ordinary ancestor directories. The original
configured path is validated before resolving it, including relative paths and
parent traversal. Symlinks, junctions, other reparse components, hard-linked
keyrings and unsafe lock files are rejected. A rejected layout is not silently
repaired, chmodded, redirected or copied into another location.

On POSIX, rotation requires the keyring owner to be the effective user. Ancestors
must belong to that user or root and must not be writable by other accounts.
A sticky temporary ancestor is allowed above a protected private child, but the
immediate key directory must not be shared writable storage. Existing private
service-owned keyrings and root-owned `0600`/`0640` layouts remain supported.
The temporary replacement retains the original POSIX owner, group and mode,
including a read-only service group on a root-owned `0640` keyring. A service
user may read that layout through existing backup verification; reading does
not grant authority to rotate a root-owned file.

On Windows, the keyring and its immediate directory must have private ACLs for
the current identity, SYSTEM and Administrators. Existing trusted OS ancestors
can have normal public read/create permissions; rights that permit another
account to replace existing protected entries are rejected. TrustedInstaller
is accepted for OS ancestors. The probe is bounded and non-recursive. ACL probe
failure rejects rotation, without including the command or key content in the
diagnostic. The generated private replacement uses the existing Windows ACL
hardening behavior.

## Read and publication checks

The boundary retains the original path and ancestor identities throughout the
operation. Lock opening is an opt-in descriptor provider for `ProcessLock`;
ordinary runtime-lock callers retain their existing behavior. Before the lock
can be initialized, its regular-file identity and access policy are checked.
The source is read through a bounded descriptor whose identity is checked before
and after the read. The original path, permissions, held lock and temporary
identity are checked again before replacement. A changed source or ancestor
causes rejection rather than publication over an unexpected target. Cleanup does
not unlink through an ancestor that failed its pinned check. If a newly created
temporary descriptor cannot provide an identity, it is closed before propagating
the error; its empty private placeholder is left in place instead of unlinking
an unverified pathname. No key bytes are written in that case.

Windows descriptor/path creation-time semantics can differ, so source revision
comparison uses device/inode, size and modification time there; ACLs are probed
separately. POSIX revision comparison also includes change time. The boundary
trusts the current account and platform administrators. It does not claim to
protect a process from an attacker already controlling that account or root.

## Durability and validation scope

File fsync, atomic replacement and directory fsync retain their existing error
semantics. A directory-sync error after replacement can still leave the new
generation visible. Path protection alone does not classify that case as
unpublished or swallow its exception. Explicit prepared requests now use the
separate [publication/resume protocol](backup-rotation-publication.md) to verify
the fixed target and confirm its acceptance without another rotation. The
[command and API migration](backup-key-commands.md) requires an explicit request
and source expectation for every new rotation.

Regression coverage uses synthetic keys and isolated storage. It covers native
POSIX permissions, root/service-group reads, Windows ACLs and junctions, injected
path/descriptor changes, concurrent rotation, old-backup restoration and failure
cleanup. Native sharing rules may themselves deny a Windows rename of a held
lock; that is asserted as a successful barrier rather than counted as a skipped
identity test. Local verification does not rotate or attest production keys.

The separate [preparation journal](backup-rotation-journal.md) records explicit
requests before publication. Implicit legacy rotation is now rejected before
path access. Explicit rotation uses the same prepared request and cannot bypass
its journal evidence. See the command/API migration before updating callers.
