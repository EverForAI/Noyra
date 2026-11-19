from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from noyra.core.at_rest import _windows_permission_audit
from noyra.migration import http_executor
from noyra.migration.bundle import decrypt_bundle
from noyra.migration.executor import MigrationExecutionError
from noyra.migration.http_executor import SQLiteArtifactProvider
from noyra.migration.local_wallet_approval import grant_local_wallet
from noyra.wallet.keystore import create_keystore, load_account
from test_b2_local_wallet_approval import setup_local

PASSWORD = "synthetic-H02-wallet-password-should-not-be-spooled"


def wallet_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    case = setup_local(tmp_path)
    keystore = tmp_path / "wallet" / "synthetic.json"
    password = tmp_path / "wallet" / "synthetic-password"
    create_keystore(keystore, PASSWORD, private_key="11" * 32)
    password.write_text(PASSWORD, encoding="utf-8")
    password.chmod(0o600)
    address = load_account(keystore, PASSWORD).address
    grant = grant_local_wallet(case.database, case.task, address, actor="operator")
    for name, value in {
        "NOYRA_WALLET_MODE": "local",
        "NOYRA_WALLET_KEYSTORE_PATH": str(keystore),
        "NOYRA_WALLET_PASSWORD_FILE": str(password),
        "NOYRA_WALLET_RPC_URLS_JSON": '{"1":"https://rpc.example.com/synthetic-rpc-secret"}',
    }.items():
        monkeypatch.setenv(name, value)
    recipient = case.recipient.public_key().public_bytes_raw()
    case.proof = {
        "artifact_id": "synthetic-artifact",
        "recipient_public_key": base64.urlsafe_b64encode(recipient).decode().rstrip("="),
        "recipient_key_fingerprint": hashlib.sha256(recipient).hexdigest(),
        "target_id": case.task.target_id,
        "wallet_binding": {"mode": "local_wallet_transfer", "address": address, "approval": grant},
    }
    case.output = tmp_path / "outgoing"
    case.snapshots = tmp_path / "migration/source/snapshots"
    case.password = password
    return case


def encrypt_hook() -> str:
    return (
        "encrypt_bundle_stream"
        if hasattr(http_executor, "encrypt_bundle_stream")
        else "encrypt_bundle"
    )


def test_export_never_stages_a_plaintext_wallet_tar_or_rpc_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = wallet_case(tmp_path, monkeypatch)
    original = getattr(http_executor, encrypt_hook())
    reached = []

    def inspect(*args: Any, **kwargs: Any) -> Any:
        reached.append(True)
        assert not list(case.output.glob("*.tar"))
        assert not list(case.output.glob("*.rpc.json"))
        assert not list(case.output.glob("*.sqlite"))
        snapshot = case.snapshots / ".synthetic-artifact.sqlite"
        assert snapshot.read_bytes().startswith(b"SQLite format 3")
        if os.name == "posix":
            assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600
            assert stat.S_IMODE(case.snapshots.stat().st_mode) == 0o700
        elif os.name == "nt":
            assert _windows_permission_audit(case.snapshots)[0]
        return original(*args, **kwargs)

    monkeypatch.setattr(http_executor, encrypt_hook(), inspect)
    bundle = SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert reached == [True]
    with case.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM migration_audit_events "
                "WHERE action='migration_local_wallet_material_authorized'"
            ).fetchone()[0]
            == 1
        )
    assert list(case.output.iterdir()) == [bundle.path]
    assert list(case.snapshots.iterdir()) == []
    assert case.password.read_text(encoding="utf-8") == PASSWORD
    plaintext = tmp_path / "synthetic-target-payload.tar"
    decrypt_bundle(
        bundle.path,
        plaintext,
        recipient_private_key=case.recipient,
        manifest=bundle.manifest,
        expected_context={
            key: bundle.manifest[key]
            for key in (
                "task_id",
                "target_id",
                "source_epoch",
                "artifact_id",
                "subject_id",
                "schema_version",
                "artifact_format",
                "payload_format",
                "database_sha256",
                "inventory_sha256",
            )
        },
    )
    from noyra.migration.subject_payload import restore_payload

    restored = restore_payload(plaintext, tmp_path / "restored", case.task.subject_id)
    with sqlite3.connect(restored) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert (restored.parent / "migration-wallet/password").read_text() == PASSWORD
    assert json.loads((restored.parent / "migration-wallet/rpc.json").read_text()) == {
        "1": "https://rpc.example.com/synthetic-rpc-secret"
    }


@pytest.mark.parametrize("phase", ["snapshot", "before-encryption", "measuring", "encrypting"])
def test_native_exit_keeps_only_private_snapshot_and_ciphertext_and_refuses_blind_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: str
) -> None:
    case = wallet_case(tmp_path, monkeypatch)
    request = tmp_path / "synthetic-export-request.json"
    request.write_text(
        json.dumps(
            {
                "task": asdict(case.task),
                "proof": case.proof,
                "database": str(case.database.path),
                "output": str(case.output),
                "phase": phase,
            }
        ),
        encoding="utf-8",
    )
    code = r"""
import json, os, sqlite3, sys
from pathlib import Path
from noyra.migration import bundle, http_executor
from noyra.migration.manager import MigrationTask
v=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
def stop(*a, **k): os._exit(73)
if v['phase']=='snapshot' and hasattr(bundle, '_DigestWriter'):
    connect=sqlite3.connect
    class Interrupted(sqlite3.Connection):
        def backup(self,target,**kwargs):
            return super().backup(target,pages=1,progress=lambda *values: stop())
    def open_source(path,*args,**kwargs):
        if str(path).endswith('?mode=ro'):
            kwargs['factory']=Interrupted
        return connect(path,*args,**kwargs)
    sqlite3.connect=open_source
elif v['phase']=='measuring' and hasattr(bundle, '_DigestWriter'):
    original=bundle._DigestWriter.write
    def halt(self, data): original(self,data); stop()
    bundle._DigestWriter.write=halt
elif v['phase']=='encrypting' and hasattr(bundle, '_EncryptedWriter'):
    original=bundle._EncryptedWriter.write
    def halt(self,data): original(self,data); self.output.flush(); stop()
    bundle._EncryptedWriter.write=halt
else:
    name = ('encrypt_bundle_stream' if hasattr(http_executor, 'encrypt_bundle_stream')
            else 'encrypt_bundle')
    setattr(http_executor,name,stop)
http_executor.SQLiteArtifactProvider(v['database'],v['output'])(MigrationTask(**v['task']),v['proof'])
raise AssertionError('exit hook not reached')
"""
    child = subprocess.run(
        [sys.executable, "-B", "-c", code, str(request)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert child.returncode == 73, child.stderr
    assert not list(case.output.glob("*.tar"))
    assert not list(case.output.glob("*.rpc.json"))
    assert not list(case.output.glob("*.sqlite"))
    assert not list(case.output.glob("*.bundle"))
    assert (case.snapshots / ".synthetic-artifact.sqlite").exists()
    if phase == "encrypting":
        partials = list(case.output.glob("*.tmp"))
        assert len(partials) == 1 and partials[0].stat().st_size > 0
    before = {
        path: path.read_bytes() for root in (case.output, case.snapshots) for path in root.iterdir()
    }
    assert all(PASSWORD.encode() not in raw for raw in before.values())
    with pytest.raises(MigrationExecutionError, match="snapshot_recovery_required"):
        SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert before == {path: path.read_bytes() for path in before}


@pytest.mark.parametrize("suffix", [".sqlite", ".tar", ".rpc.json"])
def test_old_export_residue_is_preserved_for_explicit_recovery(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, suffix: str
) -> None:
    case = wallet_case(tmp_path, monkeypatch)
    case.output.mkdir(mode=0o700)
    residue = case.output / (".synthetic-artifact" + suffix)
    residue.write_bytes(b"synthetic-prior-evidence")
    with pytest.raises(MigrationExecutionError, match="snapshot_recovery_required"):
        SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert residue.read_bytes() == b"synthetic-prior-evidence"
    assert not list(case.output.glob("*.bundle"))


def test_export_failure_cleans_only_its_own_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = wallet_case(tmp_path, monkeypatch)

    def fail(*args: Any, **kwargs: Any) -> None:
        raise ValueError("synthetic encryption fault")

    monkeypatch.setattr(http_executor, encrypt_hook(), fail)
    with pytest.raises(MigrationExecutionError, match="artifact_snapshot_failed"):
        SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert list(case.output.iterdir()) == []
    assert list(case.snapshots.iterdir()) == []
    assert case.password.read_text() == PASSWORD


def test_exclusive_snapshot_creation_does_not_delete_a_competitors_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = wallet_case(tmp_path, monkeypatch)
    original = os.open
    expected = case.snapshots / ".synthetic-artifact.sqlite"

    def race(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
        if Path(path) == expected and flags & os.O_EXCL:
            expected.write_bytes(b"synthetic-competing-export")
        return original(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", race)
    with pytest.raises(MigrationExecutionError, match="artifact_snapshot_failed"):
        SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert expected.read_bytes() == b"synthetic-competing-export"


@pytest.mark.parametrize("phase", [1, 2])
def test_revocation_during_either_pass_prevents_publication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, phase: int
) -> None:
    from noyra.migration.local_wallet_approval import revoke_local_wallet
    from noyra.migration.subject_payload import PayloadSource

    case = wallet_case(tmp_path, monkeypatch)
    original = PayloadSource.write_to
    calls = 0

    def emit(self: PayloadSource, output: Any) -> None:
        nonlocal calls
        calls += 1
        original(self, output)
        if calls == phase:
            revoke_local_wallet(
                case.database,
                case.task,
                case.proof["wallet_binding"]["approval"]["approval_id"],
                actor="operator",
            )

    monkeypatch.setattr(PayloadSource, "write_to", emit)
    with pytest.raises(MigrationExecutionError, match="artifact_snapshot_failed"):
        SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert calls == phase
    assert list(case.output.iterdir()) == []
    assert list(case.snapshots.iterdir()) == []


def test_cleanup_preserves_a_snapshot_path_replaced_after_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = wallet_case(tmp_path, monkeypatch)
    snapshot = case.snapshots / ".synthetic-artifact.sqlite"
    moved = case.snapshots / ".synthetic-artifact-moved.sqlite"

    def replace_then_fail(*args: Any, **kwargs: Any) -> None:
        snapshot.rename(moved)
        snapshot.write_bytes(b"synthetic-unrelated-replacement")
        raise ValueError("synthetic failure after replacement")

    monkeypatch.setattr(http_executor, encrypt_hook(), replace_then_fail)
    with pytest.raises(MigrationExecutionError, match="artifact_snapshot_cleanup_failed"):
        SQLiteArtifactProvider(case.database.path, case.output)(case.task, case.proof)
    assert snapshot.read_bytes() == b"synthetic-unrelated-replacement"
    assert moved.read_bytes().startswith(b"SQLite format 3")
