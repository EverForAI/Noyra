# Windows desktop deployment

The installed `noyra-desktop` command starts the same subject kernel used by the Ubuntu service and
opens the local status interface in the default browser. Source checkouts can use
`deploy/windows/start-noyra.ps1`. The launcher contains no local model; it uses the same remote
economy/deep resource pools and continuity database as the service build.

The desktop build uses the same `python -m noyra serve` service and dashboard as Ubuntu. Run it
from your local clone. The data and backup paths below are examples on an encrypted volume;
replace the volume placeholders and adapt the drive letter and directories to your machine.
This is a deployment reference, not
the restricted public research-preview configuration.

```powershell
# Start PowerShell in the root of your cloned Noyra repository.
. .\scripts\env.ps1
.\.venv\Scripts\Activate.ps1
$env:NOYRA_HOST = '127.0.0.1'
$env:NOYRA_PORT = '8765'
# Generate a fresh local token; known placeholder values are rejected.
$env:NOYRA_ADMIN_TOKEN = python -c "import secrets; print(secrets.token_urlsafe(32))"
$env:NOYRA_DATA_DIR = '<protected-data-volume>:\NoyraData'
$env:NOYRA_AT_REST_MODE = 'required'
$env:NOYRA_VOLUME_ENCRYPTION_BACKEND = 'auto'
$env:NOYRA_BACKUP_KEYRING_PATH = "$env:USERPROFILE\.noyra\backup-keyring.json"
if (-not (Test-Path -LiteralPath $env:NOYRA_BACKUP_KEYRING_PATH)) {
    python -m noyra backup-key init --path $env:NOYRA_BACKUP_KEYRING_PATH
}
python -m noyra serve
```

Run `Get-BitLockerVolume -MountPoint E:` first and require `VolumeStatus=FullyEncrypted`,
`ProtectionStatus=On`, and `EncryptionPercentage=100`. Required mode validates those fields itself,
removes inherited ACLs from the data root/database/secret tree, and limits access to the current
identity, SYSTEM, and Administrators. It fails before opening SQLite if BitLocker is suspended,
unavailable, or incomplete. A live Administrator, the signed-in service identity, process memory,
and an already unlocked volume remain outside the offline-media threat model.

Initialize the backup keyring only once and keep an offline copy separate from `NOYRA_DATA_DIR`.
Later rotations use `python -m noyra backup-key rotate --path
$env:NOYRA_BACKUP_KEYRING_PATH`; retained backups still require their retired historical keys.

Open `http://127.0.0.1:8765` for the public site or `/admin` for the creator management surface.
The management surface accepts the configured operator/admin/break-glass token once and keeps an
in-memory HttpOnly session until restart or expiry. Windows and Ubuntu use the same identity database, resource pools,
training policy, export endpoint, and storage boundaries. Stop the process before copying the
runtime directory; never run two processes against the same subject identity.

The runtime creates these separate directories beneath `NOYRA_DATA_DIR`:

```text
subject/       subject state and secrets
training_raw/  training provenance/archive staging
workspace/     autonomous project artifacts and downloads
cache/         rebuildable indexes and temporary data
exports/       generated runtime and training packages
```

## Encrypted backup and restore

Stop the desktop service before backup. The command refuses to proceed while another Noyra process
owns the database lock and never writes a plaintext backup outside the BitLocker volume:

```powershell
python -m noyra backup --output '<protected-backup-volume>:\NoyraBackups\subject.noyra-backup'
```

Restore to an absent or empty directory on a fully encrypted volume. Every AES-GCM chunk, file hash,
path, and the SQLite `quick_check` must pass before the restored directory is published:

```powershell
python -m noyra restore-backup `
  --input '<protected-backup-volume>:\NoyraBackups\subject.noyra-backup' `
  --target '<protected-data-volume>:\NoyraData-restored'
```

Missing historical keys, changed ciphertext, permission drift, and partial archives fail closed and
leave no published restored identity.
