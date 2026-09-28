#!/usr/bin/env bash
set -euo pipefail

CONFIG_FILE="${NOYRA_CONFIG_FILE:-/etc/noyra/noyra.env}"
TOKEN_FILE="${NOYRA_OPERATOR_TOKEN_FILE:-/etc/noyra/operator-token}"
SERVICE_NAME="${NOYRA_SERVICE_NAME:-noyra}"

if [[ "${EUID:-$(id -u)}" -ne 0 ]]; then
  echo "Run this command as root." >&2
  exit 1
fi
if [[ ! -f "$CONFIG_FILE" || ! -f "$TOKEN_FILE" ]]; then
  echo "Configuration or operator token file is missing." >&2
  exit 1
fi

umask 077
token_tmp="$(mktemp "${TOKEN_FILE}.new.XXXXXX")"
config_tmp="$(mktemp "${CONFIG_FILE}.new.XXXXXX")"
cleanup() {
  rm -f -- "$token_tmp" "$config_tmp"
}
trap cleanup EXIT

python3 - "$token_tmp" <<'PY'
import secrets
import sys
from pathlib import Path

path = Path(sys.argv[1])
path.write_text(secrets.token_urlsafe(48) + "\n", encoding="ascii")
PY
chown root:noyra -- "$token_tmp"
chmod 0640 -- "$token_tmp"
mv -f -- "$token_tmp" "$TOKEN_FILE"

python3 - "$CONFIG_FILE" "$config_tmp" "$TOKEN_FILE" <<'PY'
import sys
from pathlib import Path

source, target, token_file = map(Path, sys.argv[1:])
lines = source.read_text(encoding="utf-8").splitlines(keepends=True)
key = "NOYRA_OPERATOR_TOKEN_FILE"
replacement = f"{key}={token_file}\n"
for index, line in enumerate(lines):
    if line.split("=", 1)[0].strip() == key:
        lines[index] = replacement
        break
else:
    lines.append(replacement)
target.write_text("".join(lines), encoding="utf-8")
PY
chown root:noyra -- "$config_tmp"
chmod 0640 -- "$config_tmp"
mv -f -- "$config_tmp" "$CONFIG_FILE"
systemctl restart "$SERVICE_NAME"
systemctl is-active --quiet "$SERVICE_NAME"
echo "Operator token rotated and $SERVICE_NAME restarted."
