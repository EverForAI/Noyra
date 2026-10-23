from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "posix", reason="requires native POSIX Bash/filesystem")


def git(root: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True, text=True, timeout=15
    )
    return result.stdout.strip()


def repository(root: Path) -> str:
    root.mkdir()
    git(root, "init", "--quiet")
    (root / "src").mkdir()
    (root / "src/code.py").write_text("original = True\n")
    (root / ".gitignore").write_text("/src/ignored.py\n")
    git(root, "add", ".")
    git(root, "-c", "user.name=Test", "-c", "user.email=test@invalid", "commit", "-qm", "fixture")
    return git(root, "rev-parse", "HEAD")


def snapshot(source: Path, sha: str, destination: Path) -> subprocess.CompletedProcess[str]:
    destination.mkdir()
    return subprocess.run(
        [
            "bash",
            "-c",
            'set -euo pipefail; source "$1"; noyra_snapshot_release_source "$2" "$3" "$4"',
            "_",
            str(ROOT / "scripts/lib/release-source.sh"),
            str(source),
            sha,
            str(destination),
        ],
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )


def test_release_build_snapshot_excludes_ignored_inputs_and_survives_source_changes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    sha = repository(source)
    (source / "src/ignored.py").write_text("must_not_enter_release = True\n")
    destination = tmp_path / "snapshot"
    result = snapshot(source, sha, destination)
    assert result.returncode == 0, result.stderr
    assert not (destination / "src/ignored.py").exists()
    (source / "src/code.py").write_text("mutated_after_snapshot = True\n")
    assert (destination / "src/code.py").read_text() == "original = True\n"


@pytest.mark.parametrize("kind", ["tracked", "untracked", "wrong-sha", "submodule"])
def test_release_source_mismatch_rejected_before_build(tmp_path: Path, kind: str) -> None:
    source = tmp_path / "source"
    sha = repository(source)
    if kind == "tracked":
        (source / "src/code.py").write_text("dirty = True\n")
    elif kind == "untracked":
        (source / "src/extra.py").write_text("dirty = True\n")
    elif kind == "wrong-sha":
        sha = "0" * 40
    else:
        git(source, "update-index", "--add", "--cacheinfo", "160000", sha, "nested")
        git(
            source,
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@invalid",
            "commit",
            "-qm",
            "submodule",
        )
        sha = git(source, "rev-parse", "HEAD")
    destination = tmp_path / "snapshot"
    result = snapshot(source, sha, destination)
    assert result.returncode == 1, result.stderr
    assert list(destination.iterdir()) == []


@pytest.mark.skipif(
    getattr(os, "geteuid", lambda: -1)() != 0, reason="requires Linux root ownership"
)
@pytest.mark.parametrize(
    "scenario",
    [
        "credential",
        "duplicate",
        "mismatch",
        "success",
        "restart-failure",
        "interrupted-restart",
        "inactive",
    ],
)
def test_token_rotation_preflights_source_and_rolls_back_failed_restart(
    tmp_path: Path, scenario: str
) -> None:
    config = tmp_path / "noyra.env"
    token = tmp_path / "operator-token"
    token.write_text("synthetic-previous-token\n")
    config.write_text(f"NOYRA_PROFILE=production\nNOYRA_OPERATOR_TOKEN_FILE={token}\n")
    if scenario == "credential":
        config.write_text("NOYRA_PROFILE=production\nNOYRA_OPERATOR_TOKEN_CREDENTIAL=operator\n")
    elif scenario == "duplicate":
        config.write_text(config.read_text() + f"NOYRA_OPERATOR_TOKEN_FILE={token}\n")
    elif scenario == "mismatch":
        config.write_text(f"NOYRA_OPERATOR_TOKEN_FILE={tmp_path / 'another-token'}\n")
    for path in (config, token):
        path.chmod(0o640)
    before_config, before_token = config.read_bytes(), token.read_bytes()
    commands = tmp_path / "bin"
    commands.mkdir()
    calls = tmp_path / "systemctl-calls"
    restarts = tmp_path / "restart-count"
    mock = commands / "systemctl"
    mock.write_text("""#!/usr/bin/env bash
set -eu
printf '%s\\n' "$*" >> "$AUDIT_SYSTEMCTL_CALLS"
if [[ "$1" == is-active ]]; then
  [[ "$AUDIT_SCENARIO" != inactive ]]
  exit $?
fi
if [[ "$1" == restart ]]; then
  count=0
  if [[ -f "$AUDIT_RESTART_COUNT" ]]; then count="$(cat "$AUDIT_RESTART_COUNT")"; fi
  count=$((count + 1))
  printf '%s' "$count" > "$AUDIT_RESTART_COUNT"
  if [[ "$AUDIT_SCENARIO" == restart-failure && "$count" == 1 ]]; then exit 1; fi
  # This parent is the synthetic rotation subprocess created by this test.
  if [[ "$AUDIT_SCENARIO" == interrupted-restart && "$count" == 1 ]]; then
    kill -TERM "$PPID"
  fi
  exit 0
fi
exit 3
""")
    mock.chmod(0o755)
    env = os.environ.copy()
    env.update(
        PATH=str(commands) + os.pathsep + env["PATH"],
        NOYRA_CONFIG_FILE=str(config),
        NOYRA_OPERATOR_TOKEN_FILE=str(token),
        NOYRA_SERVICE_NAME="noyra-synthetic-test",
        AUDIT_SYSTEMCTL_CALLS=str(calls),
        AUDIT_RESTART_COUNT=str(restarts),
        AUDIT_SCENARIO=scenario,
    )
    result = subprocess.run(
        ["bash", str(ROOT / "scripts/rotate-operator-token.sh")],
        env=env,
        text=True,
        capture_output=True,
        timeout=20,
        check=False,
    )
    assert config.read_bytes() == before_config
    assert "synthetic-previous-token" not in result.stdout + result.stderr
    assert not list(tmp_path.glob("operator-token.new.*"))
    assert not list(tmp_path.glob("operator-token.previous.*"))
    if scenario in {"credential", "duplicate", "mismatch"}:
        assert result.returncode != 0
        assert token.read_bytes() == before_token
        assert not calls.exists()
    elif scenario in {"restart-failure", "interrupted-restart"}:
        assert result.returncode != 0
        assert token.read_bytes() == before_token
        assert restarts.read_text() == "2"
    else:
        assert result.returncode == 0, result.stderr
        assert token.read_bytes() != before_token
        assert os.stat(token).st_mode & 0o777 == 0o640
        assert restarts.exists() == (scenario == "success")


@pytest.mark.skipif(shutil.which("python3") is None, reason="python3 is unavailable")
def test_final_path_virtualenv_console_entrypoint_remains_executable(tmp_path: Path) -> None:
    # The installer builds at its final path. A pointer switch must not relocate
    # the interpreter embedded by pip in generated console-script shebangs.
    release = tmp_path / "releases/final-v1"
    subprocess.run(
        ["python3", "-m", "venv", str(release / ".venv")],
        check=True,
        capture_output=True,
        timeout=40,
    )
    pointer = tmp_path / "current"
    pointer.symlink_to(release, target_is_directory=True)
    result = subprocess.run(
        [str(pointer / ".venv/bin/pip"), "--version"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    launcher = (release / ".venv/bin/pip").read_text()
    # distlib uses a shell trampoline for long or spaced interpreter paths.
    assert str(release / ".venv/bin/python3") in "\n".join(launcher.splitlines()[:4])
