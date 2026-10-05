"""Exercise the installed root/service split with real POSIX credentials."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

from noyra.core.at_rest import AtRestError, BackupKeyring, _harden_private_paths

pytestmark = pytest.mark.skipif(
    os.name != "posix" or getattr(os, "geteuid", lambda: -1)() != 0,
    reason="requires POSIX root to exercise a separate unprivileged service account",
)


def _installed_layout(root: Path) -> Path:
    data = root / "data"
    service_uid = service_gid = 65534
    chown: Any = getattr(os, "chown", None)
    directories = {
        "": (service_uid, service_gid, 0o700),
        "migration": (0, service_gid, 0o750),
        "migration/source": (service_uid, service_gid, 0o700),
        "migration/requests": (service_uid, service_gid, 0o700),
        "migration/fences": (0, service_gid, 0o750),
        "migration/status": (0, service_gid, 0o750),
        "migration/target-activation": (0, service_gid, 0o750),
        "migration/target-activation/requests": (0, service_gid, 0o730),
        "migration/target-activation/status": (0, service_gid, 0o750),
        "migration/target-activation/state": (0, 0, 0o700),
    }
    for relative, (owner, group, mode) in directories.items():
        path = data / relative
        path.mkdir(parents=True, exist_ok=True)
        chown(path, owner, group)
        path.chmod(mode)
    fence = data / "migration/fences/active.json"
    fence.write_text('{"subject_id":"Noyra-other","task_id":"migration-task","status":"active"}')
    chown(fence, 0, service_gid)
    fence.chmod(0o640)
    (data / "migration/target-activation/state/root-journal").write_text("machine-local")
    return data


def test_installed_permissions_allow_real_service_boot_and_encrypted_backup() -> None:
    with tempfile.TemporaryDirectory(prefix="noyra-control-boundary-", dir="/var/tmp") as temp:
        root = Path(temp)
        root.chmod(0o755)
        data = _installed_layout(root)
        keyring = root / "keyring.json"
        BackupKeyring.initialize(keyring)
        chown: Any = getattr(os, "chown", None)
        chown(keyring, 0, 65534)
        keyring.chmod(0o640)
        output = root / "output"
        output.mkdir(mode=0o700)
        chown(output, 65534, 65534)
        # verify-committed uses a mode-0700 temporary checkout. Give only this
        # fixture's service process a readable copy of the exact tested sources.
        source = root / "src"
        shutil.copytree(Path(__file__).resolve().parents[1] / "src", source)
        for path in (source, *source.rglob("*")):
            path.chmod(0o755 if path.is_dir() else 0o644)
        program = """
import sys
from pathlib import Path
from noyra.core.at_rest import (
    VolumeEncryptionProbe, VolumeEncryptionStatus, EncryptedBackupManager,
)
from noyra.service import NoyraService, ServiceSettings
root = Path(sys.argv[1])
import noyra.service as service_module
read_channel = service_module.upgrade_channel
service_module.upgrade_channel = lambda: read_channel(root/'absent-upgrade-channel')
# The encrypted device probe is simulated and host configuration isolated;
# UID, modes, file access, application boot, encryption and restore are real.
VolumeEncryptionProbe.probe = lambda *a, **kw: VolumeEncryptionStatus(
    True, 'test', 'fixture', 'volume'
)
service = NoyraService(ServiceSettings(
    data_dir=root/'data', subject_id='Noyra-installed', genesis_hash='a'*64,
    profile='test', port=0, at_rest_mode='required', backup_keyring_path=root/'keyring.json',
))
try:
    service.boot()
    assert service.kernel.admission.accepting, service.integrity.summary()
    assert service.at_rest.health()['ready']
finally:
    service.close()
import base64, hashlib
from types import SimpleNamespace
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
public=X25519PrivateKey.generate().public_key().public_bytes_raw()
artifact=service.http.migration_http_executor.artifact_resolver(
    SimpleNamespace(task_id='task-fixture', target_id='target-fixture',
                    source_epoch='runtime-0', subject_id='Noyra-installed'),
    {'recipient_public_key':base64.urlsafe_b64encode(public).decode(),
     'recipient_key_fingerprint':hashlib.sha256(public).hexdigest()},
)
assert artifact.path.is_relative_to(root/'data/migration/source/outgoing')
assert service.at_rest.health()['ready']
manager=EncryptedBackupManager(root/'data',root/'keyring.json')
backup=root/'output/subject.backup'
manager.create(backup)
restored=manager.restore(backup,root/'output/restored')
assert (restored/'migration/source/epoch').is_file()
assert (restored/'migration/fences').is_dir()
assert (restored/'migration/fences/active.json').read_text() == (
    '{"subject_id":"Noyra-other","task_id":"migration-task","status":"active"}'
)
assert not (restored/'migration/target-activation').exists()
assert (root/'data/migration/target-activation/state').stat().st_uid == 0
print('boot_and_backup_ok')
"""
        credentials: dict[str, Any] = {"user": 65534, "group": 65534, "extra_groups": []}
        result = subprocess.run(
            [sys.executable, "-c", program, str(root)],
            capture_output=True,
            text=True,
            timeout=60,
            env=dict(os.environ, PYTHONPATH=str(source)),
            **credentials,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "boot_and_backup_ok" in result.stdout
        assert (data / "migration").stat().st_mode & 0o777 == 0o750


@pytest.mark.parametrize("tamper", ["owner", "writable", "state", "extra", "symlink"])
def test_migration_control_exception_rejects_untrusted_layout(tamper: str) -> None:
    with tempfile.TemporaryDirectory(prefix="noyra-control-reject-", dir="/var/tmp") as temp:
        root = Path(temp)
        data = _installed_layout(root)
        control = data / "migration"
        if tamper == "owner":
            chown: Any = getattr(os, "chown", None)
            chown(control / "fences", 65534, 65534)
        elif tamper == "writable":
            control.chmod(0o770)
        elif tamper == "state":
            (control / "target-activation/state").chmod(0o750)
        elif tamper == "extra":
            (control / "unexpected").mkdir()
        else:
            (control / "fences/link").symlink_to(root)
        with pytest.raises(AtRestError, match="migration"):
            _harden_private_paths(data)
