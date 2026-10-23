#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${NOYRA_CONFIG_FILE:-/etc/noyra/noyra.env}"
TOKEN_FILE="${NOYRA_OPERATOR_TOKEN_FILE:-/etc/noyra/operator-token}"
SERVICE_NAME="${NOYRA_SERVICE_NAME:-noyra}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this command as root." >&2
  exit 1
fi
command -v flock >/dev/null 2>&1 || { echo 'flock is required.' >&2; exit 1; }
umask 077
# Serialize publication and recovery; never source a configuration as shell code.
exec 9>"${CONFIG_FILE}.token-rotation.lock"
flock -n 9 || { echo 'Another token rotation is in progress.' >&2; exit 1; }

# Reject unsupported credential sources BEFORE replacing any token/configuration.
# This tool deliberately supports file-only deployments; systemd credential and
# inline deployments must use their own managed credential rotation procedure.
python3 -I - "$CONFIG_FILE" "$TOKEN_FILE" <<'PY'
import os
import re
import stat
import sys
from pathlib import Path

config, token = map(Path, sys.argv[1:])
for path in (config, token):
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise SystemExit("Rotation requires absolute regular configuration/token files.")
    metadata = path.stat()
    if metadata.st_uid != 0 or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) & 0o027:
        raise SystemExit("Configuration/token ownership or permissions are unsafe.")
values = {}
keys = {"NOYRA_OPERATOR_TOKEN", "NOYRA_OPERATOR_TOKEN_FILE", "NOYRA_OPERATOR_TOKEN_CREDENTIAL"}
for line in config.read_text(encoding="utf-8").splitlines():
    match = re.fullmatch(r"\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*", line)
    if match is None:
        if line.strip() and not line.lstrip().startswith(("#", ";")):
            raise SystemExit("Unsupported environment-file syntax; rotation stopped.")
        continue
    key, value = match.groups()
    if key not in keys:
        continue
    if key in values:
        raise SystemExit("Duplicate operator credential assignments; rotation stopped.")
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        value = value[1:-1]
    if any(char in value for char in "\\\n\r\x00"):
        raise SystemExit("Unsupported operator credential syntax; rotation stopped.")
    values[key] = value.strip()
if values.get("NOYRA_OPERATOR_TOKEN_CREDENTIAL") or values.get("NOYRA_OPERATOR_TOKEN"):
    raise SystemExit("Only file-only operator credentials are supported; no files were changed.")
configured = values.get("NOYRA_OPERATOR_TOKEN_FILE", "")
if not configured or os.path.normpath(configured) != str(token):
    raise SystemExit("Configured token source does not match TOKEN_FILE; no files were changed.")
old = token.read_text(encoding="utf-8").strip()
if not old or len(old) > 65536 or any(ord(c) < 32 for c in old):
    raise SystemExit("Existing operator token is invalid; rotation stopped.")
PY

token_tmp=""
backup=""
keep_backup=false
published=false
committed=false
was_active=false
cleanup() {
  local status=$?
  trap - EXIT HUP INT TERM
  # Restore the original source if publication/restart was interrupted. Mark
  # publication intent before mv so even a signal between shell commands is safe.
  if [[ "$published" == true && "$committed" != true && -n "$backup" && -f "$backup" ]]; then
    if mv -f -- "$backup" "$TOKEN_FILE"; then
      if [[ "$was_active" == true ]]; then
        if ! systemctl restart "$SERVICE_NAME" || ! systemctl is-active --quiet "$SERVICE_NAME"; then
          echo 'Previous token restored, but service recovery needs attention.' >&2
        fi
      fi
    else
      keep_backup=true
      echo "Token recovery file retained: $backup" >&2
    fi
  fi
  [[ -z "$token_tmp" ]] || rm -f -- "$token_tmp"
  if [[ "$keep_backup" != true && -n "$backup" ]]; then rm -f -- "$backup"; fi
  return "$status"
}
trap cleanup EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
token_tmp="$(mktemp "${TOKEN_FILE}.new.XXXXXX")"
backup="$(mktemp "${TOKEN_FILE}.previous.XXXXXX")"
# Preserve the old file's service-readable ownership/mode for atomic recovery.
cp --preserve=mode,ownership -- "$TOKEN_FILE" "$backup"
python3 -I - "$token_tmp" <<'PY'
import secrets
import sys
from pathlib import Path
Path(sys.argv[1]).write_text(secrets.token_urlsafe(48) + "\n", encoding="ascii")
PY
chown --reference="$TOKEN_FILE" -- "$token_tmp"
chmod --reference="$TOKEN_FILE" -- "$token_tmp"
if systemctl is-active --quiet "$SERVICE_NAME"; then was_active=true; fi
# The validated configuration is unchanged; publication has only one file.
published=true
mv -f -- "$token_tmp" "$TOKEN_FILE"
if [[ "$was_active" == true ]]; then
  if ! systemctl restart "$SERVICE_NAME" || ! systemctl is-active --quiet "$SERVICE_NAME"; then
    if ! mv -f -- "$backup" "$TOKEN_FILE"; then
      keep_backup=true
      echo "Token rollback failed; protected recovery file retained: $backup" >&2
      published=false
      exit 1
    fi
    published=false
    if ! systemctl restart "$SERVICE_NAME" || ! systemctl is-active --quiet "$SERVICE_NAME"; then
      echo 'Previous token restored, but service recovery failed; inspect systemd logs.' >&2
    else
      echo 'Rotation failed; previous token and running service restored.' >&2
    fi
    exit 1
  fi
  committed=true
  echo "Operator token rotated and $SERVICE_NAME restarted."
else
  committed=true
  echo "Operator token rotated; $SERVICE_NAME was not active and was left stopped."
fi
