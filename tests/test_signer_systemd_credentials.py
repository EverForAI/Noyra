"""Opt-in, isolated real-systemd credential and crash-restart contract."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.environ.get("NOYRA_TEST_SYSTEMD_CREDENTIALS") != "1",
    reason="requires explicit opt-in to isolated systemd transient services",
)


def test_real_systemd_credentials_survive_restart_without_state_copies(tmp_path: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("requires a POSIX systemd test host")
    if os.geteuid() != 0 or Path("/proc/1/comm").read_text().strip() != "systemd":
        pytest.skip("requires a root-run systemd test host")
    import pwd

    from noyra.wallet.keystore import create_keystore

    account = pwd.getpwnam("nobody")
    root = Path(tempfile.mkdtemp(prefix="noyra-credential-test-", dir="/run"))
    root.chmod(0o755)
    unit = root.name + ".service"
    state = root / "state"
    state.mkdir(mode=0o700)
    inputs = root / "inputs"
    inputs.mkdir(mode=0o700)
    expected = {
        "wallet-password": "  synthetic mounted wallet password  ",
        "sepolia-rpc-url": "https://rpc.example/synthetic",
        "signer-token": "synthetic-systemd-bearer",
    }
    probe = root / "probe.py"
    probe.write_text(
        r"""
import os
import time
from pathlib import Path
from unittest.mock import Mock, patch
from noyra import __main__
from noyra.core.credentials import read_secret_file
from noyra.wallet.keystore import load_account, read_password_credential

credential_root = Path(os.environ['CREDENTIALS_DIRECTORY'])
state = Path(os.environ['PROBE_STATE'])
assert credential_root.stat().st_dev != credential_root.parent.stat().st_dev
password = read_password_credential(credential_root / 'wallet-password')
assert password == '  synthetic mounted wallet password  '
assert read_secret_file(credential_root / 'signer-token') == 'synthetic-systemd-bearer'
assert read_secret_file(credential_root / 'sepolia-rpc-url') == 'https://rpc.example/synthetic'
for name in ('wallet-password', 'sepolia-rpc-url', 'signer-token'):
    path = credential_root / name
    assert path.stat().st_uid in {0, os.geteuid()}
    assert not path.stat().st_mode & 0o027
    try:
        with path.open('ab'):
            pass
    except OSError:
        pass
    else:
        raise AssertionError('credential mount must be read-only')

class Server:
    def __init__(self, engine, *, bearer_token, host, port):
        assert bearer_token == 'synthetic-systemd-bearer'
        assert host == '127.0.0.1' and port == 8780
        with (state / 'ready').open('a') as stream:
            stream.write(str(os.getpid()) + '\n')
    def serve_forever(self):
        while True:
            time.sleep(0.1)
    def server_close(self):
        pass

with (
    patch('noyra.signer_service.HTTPRPC', return_value=Mock()),
    patch('noyra.signer_service.SignerHTTPServer', Server),
    patch('noyra.wallet.keystore.load_account', wraps=load_account),
):
    # Execute the real CLI, keystore decryption, engine and SQLite setup; only
    # external RPC and the listening server are replaced with local test doubles.
    __main__._signer_command([
        '--keystore', str(state / 'wallet.json'), '--state', str(state / 'signer.sqlite3'),
        '--wallet-password-credential', str(credential_root / 'wallet-password'),
        '--rpc-url-credential', str(credential_root / 'sepolia-rpc-url'),
        '--bearer-token-credential', str(credential_root / 'signer-token'),
    ])
""",
        encoding="utf-8",
    )
    compile(probe.read_text(), str(probe), "exec")

    def command(*arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        return subprocess.run(arguments, capture_output=True, text=True, check=check, timeout=20)

    def ready(count: int) -> list[str]:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            path = state / "ready"
            rows = path.read_text().splitlines() if path.exists() else []
            if len(rows) >= count:
                return rows
            time.sleep(0.1)
        status = command(
            "systemctl", "show", unit, "-p", "ActiveState", "-p", "Result", check=False
        )
        pytest.fail("isolated signer did not become ready: " + status.stdout)

    try:
        create_keystore(
            state / "wallet.json", expected["wallet-password"], private_key="0x" + "11" * 32
        )
        for path in (state, state / "wallet.json"):
            os.chown(path, account.pw_uid, account.pw_gid)
        for name, value in expected.items():
            path = inputs / name
            path.write_text(value + "\n")
            path.chmod(0o600)
        source_root = Path(__file__).resolve().parents[1] / "src"
        properties = {
            "User": account.pw_name,
            "Group": str(account.pw_gid),
            "Restart": "on-failure",
            "RestartSec": "1",
            "RuntimeMaxSec": "60",
            "UMask": "0077",
            "NoNewPrivileges": "true",
            "PrivateTmp": "true",
            "PrivateDevices": "true",
            "ProtectSystem": "strict",
            "ProtectHome": "true",
            "ProtectKernelTunables": "true",
            "ProtectKernelModules": "true",
            "ProtectControlGroups": "true",
            "RestrictSUIDSGID": "true",
            "LockPersonality": "true",
            "MemoryDenyWriteExecute": "true",
            "ReadWritePaths": str(state),
        }
        arguments = ["systemd-run", "--quiet", "--unit=" + unit]
        for key, value in properties.items():
            arguments.extend(("-p", key + "=" + value))
        for name in expected:
            arguments.extend(("-p", "LoadCredential=" + name + ":" + str(inputs / name)))
        arguments.extend(
            (
                "-p",
                "Environment=PROBE_STATE=" + str(state),
                "-p",
                "Environment=PYTHONDONTWRITEBYTECODE=1",
                "-p",
                "Environment=PYTHONPATH=" + str(source_root),
            )
        )
        arguments.extend((sys.executable, str(probe)))
        command(*arguments)
        first = ready(1)
        command("systemctl", "kill", "--signal=KILL", "--kill-whom=main", unit)
        restarted = ready(2)
        assert restarted[0] != restarted[1]
        allowed = {
            "wallet.json",
            "signer.sqlite3",
            "signer.sqlite3-wal",
            "signer.sqlite3-shm",
            "signer.sqlite3-journal",
            "ready",
        }
        assert {path.name for path in state.iterdir()} <= allowed
        for path in state.iterdir():
            data = path.read_bytes()
            assert all(value.encode() not in data for value in expected.values())
        command("systemctl", "stop", unit)
        assert not (Path("/run/credentials") / unit).exists()
        (tmp_path / "systemd-result.json").write_text(
            json.dumps(
                {
                    "systemd_version": command("systemd", "--version").stdout.splitlines()[0],
                    "non_root_uid": account.pw_uid,
                    "started": len(first),
                    "restarted": len(restarted),
                    "credential_mount_removed_on_stop": True,
                    "plaintext_state_copies": 0,
                },
                indent=2,
            )
        )
    finally:
        command("systemctl", "stop", unit, check=False)
        command("systemctl", "reset-failed", unit, check=False)
        assert root.parent == Path("/run") and root.name.startswith("noyra-credential-test-")
        shutil.rmtree(root)
