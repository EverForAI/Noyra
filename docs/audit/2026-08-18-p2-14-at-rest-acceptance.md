# P2-14 At-Rest Acceptance

Date: 2026-08-18

## Result

P2-14 is implemented, but remains short of `verified` until a production-host restore drill
and key-custody record are completed. The runtime and backup contracts were exercised on real
encrypted volumes on both supported platforms.

## Windows BitLocker

- Created a temporary 128 GiB dynamic VHDX and attached it as `R:`.
- Enabled BitLocker XTS-AES-256 with protection on and verified
  `VolumeStatus=FullyEncrypted`, `ProtectionStatus=On`, and `EncryptionPercentage=100`.
- Ran `AtRestGuard` in `required` mode against the temporary encrypted test mount (local path redacted for publication); the native BitLocker probe returned
  `encrypted=true` and the Windows private ACL audit passed.
- Created an encrypted backup, rotated the external keyring, created a second backup, restored
  both backups on the encrypted volume, and verified the private credential round trip.
- Confirmed the backup bytes did not contain the test credential.
- Removed the retired key and confirmed restoring the first backup failed closed with
  `BackupKeyUnavailableError` and no published target.
- Detached and deleted the temporary VHDX after the run.

## WSL2 Ubuntu LUKS

- Created a temporary LUKS2 loopback volume, mounted it through a dm-crypt mapping, and verified
  the native sysfs ancestry probe returned `encrypted=true` and `backend=luks`.
- Ran the same required-guard, permission, encrypted-backup, rotation, restore, and historical-key
  loss matrix on the mounted data root.
- Unmounted and closed the mapping, detached the loop device, and removed the temporary image.

## Regression Evidence

- Windows focused suite: `22 passed, 1 platform-specific symlink skip`.
- WSL2 Ubuntu focused suite: `23 passed`.
- Previously recorded Windows and WSL2 full suite: `746 passed, 3 platform-specific skips` on each
  host.

## Remaining Closure Evidence

The temporary volumes prove the native platform integrations without changing the user's real
disk encryption state. They do not replace a clean production-host exercise covering offline key
custody, service stop/start, backup transfer, restore into the real deployment mount, and the
operator's documented recovery record. That operational evidence is required before changing the
acceptance status to `verified`.
