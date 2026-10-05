#!/usr/bin/env bash
set -euo pipefail
root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
temporary="$(mktemp -d)"
trap 'rm -rf -- "$temporary"' EXIT
cd "$temporary"
NOYRA_PYTHON=/does-not-exist bash "$root/scripts/audit-deployment.sh" --static
if NOYRA_PYTHON=/does-not-exist bash "$root/scripts/audit-deployment.sh" --full > "$temporary/error" 2>&1; then
  echo 'Full audit accepted missing Python' >&2; exit 1
fi
grep -Fq "$root/scripts/prepare-audit-environment.sh" "$temporary/error"
if NOYRA_PYTHON=/does-not-exist bash "$root/scripts/audit-release.sh" > "$temporary/error" 2>&1; then
  echo 'Release audit accepted missing Python' >&2; exit 1
fi
grep -Fq 'Do not install developer dependencies' "$temporary/error"
# A provided runtime interpreter missing developer imports must fail before tests.
printf '#!/usr/bin/env bash\nexit 1\n' > "$temporary/runtime-python"
chmod 0700 "$temporary/runtime-python"
if NOYRA_PYTHON="$temporary/runtime-python" bash "$root/scripts/audit-release.sh" > "$temporary/error" 2>&1; then
  exit 1
fi
grep -Fq 'developer tools are missing' "$temporary/error"
echo 'audit environment contract passed'
