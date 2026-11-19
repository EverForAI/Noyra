# Signer credentials on systemd

The self-hosted signer unit reads `wallet-password`, `sepolia-rpc-url`, and `signer-token`
directly from systemd's read-only credential directory. Its command line contains `%d/<name>`
paths, never credential values. The unit does not copy credentials into `/var/lib/noyra-signer`.
The wallet keystore and signer request database remain in that private state directory.

## Provisioning

The checked-in unit uses these input files:

| Credential | Root-managed input file |
| --- | --- |
| Wallet password | `/etc/noyra-signer/credentials/wallet-password` |
| Sepolia RPC URL | `/etc/noyra-signer/credentials/sepolia-rpc-url` |
| Signer bearer token | `/etc/noyra/credentials/staging-signer-token` |

Keep input directories root-owned and private (`0700`) and files root-owned with mode `0600`.
Systemd reads these inputs as the service manager and presents separate read-only credentials
accessible to the service account. Systemd may use service ownership or a root-owned file
with a named-user read ACL. The input files remain intentionally persistent: removing the
extra state-directory copies does not encrypt, delete or rotate the original inputs. Protect
those inputs and any backups independently. Do not print their contents in validation output.

The mounted credential directory can be a separate filesystem. The signer password reader
accepts that layout while checking the credential is a stable regular file with one hard link,
no symlink/reparse path components and private POSIX ownership/mode. Password spaces are
preserved. A root-owned systemd ACL file is accepted only inside `CREDENTIALS_DIRECTORY`,
on its own read-only mount, with an exact ACL granting read access to that service UID and
no other non-root user or group. This does not relax ordinary credential-file permissions.
Only one terminal LF or CRLF is removed from a password. RPC URL and bearer-token credentials use
the existing bounded single-line reader. Invalid credentials fail before keystore decryption
or signer state is opened. The ordinary wallet-file reader retains its private-directory and
filesystem checks; Windows password reads retain the existing wallet ACL checks.

## Updating a deployment with the older unit

Install the updated Python code and unit together. The old password reader rejects a separate
systemd credential mount, so deploying only the unit is insufficient. During a planned signer
maintenance window:

1. Stop `noyra-signer.service` and confirm it is inactive.
2. Install the matching code and unit. Preserve the encrypted `wallet.json`, `signer.sqlite3`
   and any SQLite companions; these contain wallet identity and broadcast reconciliation state.
3. Check for the three legacy plaintext copies below. If present, remove only those copies
   while the signer is stopped:

   ```sh
   sudo rm -f -- /var/lib/noyra-signer/.wallet-password \
     /var/lib/noyra-signer/.sepolia-rpc-url \
     /var/lib/noyra-signer/.signer-token
   ```

4. Run `systemctl daemon-reload`, start the signer, and verify loopback health/authentication
   using the deployment's normal private checks. Confirm no legacy credential copies reappear.

The code update does not automatically delete existing files, restart a running signer, or
rotate any credentials. File removal is not a claim of secure physical erasure. Review old
backups separately if they may contain legacy plaintext copies.

## Failure and validation boundaries

Systemd invokes `ExecStopPost` after an unexpected process exit, including a killed main
process; a process SIGKILL by itself is not evidence that this hook is skipped. Host power loss
can prevent userspace cleanup. Direct credential reads remove the need for an application
cleanup command for extra plaintext state files. Systemd manages the credential mount lifecycle.
This does not remove decrypted key material from a live signer's memory or replace host security.

The opt-in integration test starts an isolated transient test unit with a non-root account and
synthetic credentials/keystore on `/run` tmpfs. It executes the actual signer CLI and keystore
decryption with RPC and the listening server replaced by local test doubles. It checks private,
read-only credential mounts, SIGKILL followed by automatic restart, absence of plaintext in the
state directory, and credential mount removal after stop. It never contacts a chain, broadcasts
a transaction, or operates `noyra-signer.service`.

On a dedicated root-run Ubuntu systemd test host, with the project and interpreter readable by
the test account:

```sh
NOYRA_TEST_SYSTEMD_CREDENTIALS=1 PYTHONDONTWRITEBYTECODE=1 \
  python -m pytest tests/test_signer_systemd_credentials.py -q
```

Systemd references: `systemd.exec(5)` (Credentials, `%d`) and `systemd.service(5)`
(`ExecStopPost`). Ubuntu 24.04/systemd 255 is the integration target. The test covers process
failure and restart, not physical power-loss or secure-storage erasure guarantees.
