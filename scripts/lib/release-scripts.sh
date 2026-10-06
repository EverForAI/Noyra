#!/usr/bin/env bash

# Keep systemd entrypoints with the Python package they import so pointer
# switches and rollback always select a matching set of code.
noyra_install_release_scripts() {
  local source_dir="$1" release_dir="$2" entry mode name
  local -a entries=(
    '0644:preflight-production.py'
    '0750:noyra-target-activation-runner.py'
    '0755:noyra-migration-agent.py'
  )

  install -d -o root -g root -m 0755 "$release_dir/scripts" || return 1
  for entry in "${entries[@]}"; do
    mode="${entry%%:*}"
    name="${entry#*:}"
    if [[ ! -f "$source_dir/scripts/$name" || -L "$source_dir/scripts/$name" ]]; then
      echo "Release entrypoint must be a regular file: $name" >&2
      return 1
    fi
    install -o root -g root -m "$mode" "$source_dir/scripts/$name" \
      "$release_dir/scripts/$name" || return 1
  done

  # Import each packaged entrypoint without invoking its command or accessing
  # live data. Isolated Python resolves dependencies from the staged venv.
  "$release_dir/.venv/bin/python" -I - "$release_dir" "${entries[@]}" <<'PY'
import runpy
import sys
from pathlib import Path

release = Path(sys.argv[1])
for entry in sys.argv[2:]:
    name = entry.split(":", 1)[1]
    runpy.run_path(str(release / "scripts" / name), run_name="noyra_release_check")
PY
}
