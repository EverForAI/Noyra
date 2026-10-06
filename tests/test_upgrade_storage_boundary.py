from __future__ import annotations

import importlib.util
import os
import shutil
import stat
import subprocess
import sys
import tempfile
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from noyra.core import at_rest
from noyra.core.at_rest import AtRestError, BackupKeyring, EncryptedBackupManager

ROOT = Path(__file__).resolve().parents[1]


def _chown(path: Path, owner: int, group: int) -> None:
    chown = getattr(os, "chown", None)
    assert callable(chown)
    chown(path, owner, group)


@pytest.mark.parametrize("mode", [0o600, 0o640, 0o644, 0o660, 0o400, 0o740, 0o604])
def test_root_keyring_permissions_do_not_depend_on_service_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: int
) -> None:
    keyring = tmp_path / "keyring"
    keyring.write_bytes(b"key")
    metadata = keyring.stat()
    simulated = os.stat_result(
        (
            stat.S_IFREG | mode,
            metadata.st_ino,
            metadata.st_dev,
            1,
            0,
            996,
            metadata.st_size,
            metadata.st_atime,
            metadata.st_mtime,
            metadata.st_ctime,
        )
    )
    monkeypatch.setattr(at_rest, "os", SimpleNamespace(name="posix"))
    monkeypatch.setattr(at_rest, "_effective_uid", lambda: 0)
    monkeypatch.setattr(at_rest, "_effective_gid", lambda: 0)
    monkeypatch.setattr(at_rest, "_effective_groups", lambda: ())
    monkeypatch.setattr(Path, "stat", lambda *args, **kwargs: simulated)
    assert (at_rest._keyring_permission_error(keyring) is None) == (mode in {0o600, 0o640})


@pytest.fixture
def installed_upgrade_tree() -> Any:
    if os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0:
        pytest.skip("requires real Linux root and service-account permissions")
    accounts: Any = import_module("pwd")
    account = accounts.getpwnam("nobody")
    root = Path(tempfile.mkdtemp(prefix="noyra-upgrade-boundary-"))
    root.chmod(0o755)
    data = root / "data"
    data.mkdir(mode=0o700)
    _chown(data, account.pw_uid, account.pw_gid)
    control = data / "upgrade"
    control.mkdir(mode=0o750)
    _chown(control, 0, account.pw_gid)
    requests = control / "requests"
    requests.mkdir(mode=0o700)
    _chown(requests, account.pw_uid, account.pw_gid)
    (control / "processing").mkdir(mode=0o700)
    lock = control / "manager.lock"
    lock.touch(mode=0o660)
    lock.chmod(0o660)
    _chown(lock, 0, account.pw_gid)
    status = control / "status.json"
    status.write_text('{"status":"idle"}')
    status.chmod(0o640)
    _chown(status, 0, account.pw_gid)
    try:
        yield root, data, account
    finally:
        shutil.rmtree(root)


def test_real_service_can_harden_backup_and_root_can_verify_restore(
    installed_upgrade_tree: Any,
) -> None:
    root, data, account = installed_upgrade_tree
    keyring = root / "keyring.json"
    BackupKeyring.initialize(keyring)
    keyring.chmod(0o640)
    _chown(keyring, 0, account.pw_gid)
    output = root / "out"
    output.mkdir(mode=0o700)
    _chown(output, account.pw_uid, account.pw_gid)
    backup = output / "cold.noyra-backup"
    # Hosted CI checkouts can live below a private runner home. Give the
    # service an independent, read-only code tree without changing that home.
    service_code = root / "code"
    shutil.copytree(
        ROOT / "src" / "noyra",
        service_code / "noyra",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    for path in (service_code, *service_code.rglob("*")):
        path.chmod(0o755 if path.is_dir() else 0o644)
    code = """
import sys
from pathlib import Path
from noyra.core import at_rest
from noyra.core.at_rest import (
    _harden_private_paths, _private_permission_error, EncryptedBackupManager,
)
from noyra.core.database import Database
from noyra.core.identity import IdentityStore
from noyra.core.types import content_hash
data, key, backup, source = map(Path, sys.argv[1:])
assert Path(at_rest.__file__).resolve().is_relative_to(source)
database = Database(data / 'noyra.sqlite3')
IdentityStore(database).ensure('upgrade-boundary', content_hash({'seed': 'upgrade'}))
(data / 'secret').write_text('private-data')
(data / 'upgrade/requests/pending.json').write_text('machine-local-request')
_harden_private_paths(data)
assert _private_permission_error(data) is None
EncryptedBackupManager(data, key).create(backup)
"""
    result = subprocess.run(
        [
            "runuser",
            "-u",
            account.pw_name,
            "--",
            sys.executable,
            "-c",
            code,
            str(data),
            str(keyring),
            str(backup),
            str(service_code),
        ],
        env={**os.environ, "PYTHONPATH": str(service_code)},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    assert (data / "upgrade").stat().st_uid == 0
    assert stat.S_IMODE((data / "upgrade/manager.lock").stat().st_mode) == 0o660
    restored = EncryptedBackupManager(data, keyring).restore(backup, root / "restored")
    assert (restored / "secret").read_text() == "private-data"
    assert not (restored / "upgrade").exists()
    spec = importlib.util.spec_from_file_location(
        "maintenance", ROOT / "scripts/deployment-maintenance.py"
    )
    assert spec is not None and spec.loader is not None
    helper: Any = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    helper.verify_backup(backup, data, keyring)
    assert helper.verified_backup(backup)
    assert not list(data.glob(".upgrade-verify-*"))


@pytest.mark.parametrize("mutation", ["owner", "mode", "unknown", "link", "hardlink"])
def test_upgrade_boundary_rejects_unsafe_control_state(
    installed_upgrade_tree: Any, mutation: str
) -> None:
    _, data, account = installed_upgrade_tree
    control = data / "upgrade"
    if mutation == "owner":
        _chown(control / "processing", account.pw_uid, account.pw_gid)
    elif mutation == "mode":
        control.chmod(0o770)
    elif mutation == "unknown":
        (control / "unexpected").touch()
    elif mutation == "link":
        (control / "status.json").unlink()
        (control / "status.json").symlink_to(control / "manager.lock")
    else:
        (control / "alias").hardlink_to(control / "manager.lock")
    with pytest.raises(AtRestError, match="upgrade control"):
        list(at_rest._private_paths(data))


@pytest.mark.skipif(os.name != "posix", reason="Bash installer recovery")
@pytest.mark.parametrize("failure", ["false", "exit 17"])
def test_installer_error_and_explicit_exit_restore_layout_before_restart(
    tmp_path: Path, failure: str
) -> None:
    installer = (ROOT / "scripts/install-ubuntu.sh").read_text()
    on_error = installer[installer.index("on_error() {") : installer.index("trap on_error ERR")]
    on_exit = installer[installer.index("on_exit() {") : installer.index("trap on_exit EXIT")]
    script = (
        """
set -euo pipefail
source "$1/scripts/lib/control-layout.sh"
root="$2"
UPGRADE_COMPONENTS_CHANGED=false
MIGRATION_COMPONENTS_CHANGED=false
upgrade_components_restored=true
migration_components_restored=true
cleanup_failed=false
failure_handled=false
switched=false
release_published=false
legacy_moved=false
service_was_stopped=true
cleanup_staging() { :; }
systemctl() {
  if [[ "$1" == start ]]; then
    [[ ! -e "$root/upgrade" ]] || return 1
    printf 'old-release-restarted' > "$root/restarted"
  fi
}
"""
        + on_error
        + on_exit
        + """
trap on_error ERR
trap on_exit EXIT
noyra_control_layout_snapshot "$root" "$root/upgrade" "$root/upgrade/requests" \
  "$root/upgrade/manager.lock"
mkdir -p "$root/upgrade/requests"
touch "$root/upgrade/manager.lock"
noyra_control_layout_record_file "$root/upgrade/manager.lock"
"""
        + failure
    )
    result = subprocess.run(
        ["bash", "-c", script, "installer-test", str(ROOT), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == (17 if failure.startswith("exit") else 1), result.stderr
    assert (tmp_path / "restarted").read_text() == "old-release-restarted"
    assert not (tmp_path / "upgrade").exists()
