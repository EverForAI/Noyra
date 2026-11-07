from __future__ import annotations

import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from noyra.backup_key_cli import run
from noyra.core.at_rest import BackupKeyring, BackupKeyUnavailableError
from noyra.core.backup_rotation import BackupRotationJournal
from noyra.core.backup_rotation_records import (
    RotationConflictError,
    RotationOutcomeUnknownError,
    RotationRequestRequiredError,
    RotationUnsupportedError,
    sha256,
)
from noyra.core.locking import ProcessLock

ROOT = Path(__file__).resolve().parents[1]
REQ = "a" * 32
POSIX = pytest.mark.skipif(os.name != "posix", reason="POSIX durable rotation")


def command(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-m", "noyra", "backup-key", *arguments],
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src"),
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        },
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=45,
    )


def setup(root: Path) -> tuple[Path, bytes, list[str]]:
    target = root / "keyring.json"
    BackupKeyring.initialize(target)
    source = target.read_bytes()
    return target, source, ["--path", str(target), "--request-id", REQ]


def expected(source: bytes, generation: int = 1) -> list[str]:
    return ["--expected-generation", str(generation), "--expected-source-hash", sha256(source)]


def test_legacy_missing_identity_refuses_before_touching_path(tmp_path: Path) -> None:
    missing = tmp_path / "absent" / "keyring.json"
    with pytest.raises(BackupKeyUnavailableError, match="request_id"):
        BackupKeyring.rotate(missing)
    assert not missing.parent.exists()


@pytest.mark.parametrize(
    "kwargs", [{}, {"request_id": REQ}, {"request_id": REQ, "expected_generation": 1}]
)
def test_missing_legacy_expectation_refuses_even_valid_storage(
    rotation_directory: Path, kwargs: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, _ = setup(rotation_directory)

    def forbidden(*args: Any, **kw: Any) -> None:
        raise AssertionError("missing request fields must be rejected before storage/entropy")

    with monkeypatch.context() as patch:
        patch.setattr(os, "urandom", forbidden)
        patch.setattr(ProcessLock, "acquire", forbidden)
        with pytest.raises(RotationRequestRequiredError):
            BackupKeyring.rotate(target, **kwargs)
    assert target.read_bytes() == source
    assert {p.name for p in rotation_directory.iterdir()} == {"keyring.json"}


@pytest.mark.parametrize("action", ["prepare", "resume", "rotate", "status"])
def test_cli_missing_identity_is_usage_error_before_storage(tmp_path: Path, action: str) -> None:
    root = tmp_path / "absent"
    result = command(action, "--path", str(root / "keyring.json"))
    assert result.returncode == 2 and not root.exists()
    assert "--request-id" in result.stderr


@pytest.mark.parametrize(
    "extra",
    [
        [
            "--request-id",
            "../escape",
            "--expected-generation",
            "1",
            "--expected-source-hash",
            "a" * 64,
        ],
        ["--request-id", REQ, "--expected-generation", "0", "--expected-source-hash", "a" * 64],
        ["--request-id", REQ, "--expected-generation", "1", "--expected-source-hash", "bad"],
        ["--request-id", REQ, "--expected-generation", "1"],
    ],
)
def test_cli_invalid_expectation_does_not_touch_missing_root(
    tmp_path: Path, extra: list[str]
) -> None:
    root = tmp_path / "absent"
    result = command("rotate", "--path", str(root / "keyring.json"), *extra)
    assert result.returncode == 2 and not root.exists()


def test_cli_init_and_status_are_compatible_and_secret_free(rotation_directory: Path) -> None:
    target = rotation_directory / "keyring.json"
    initialized = command("init", "--path", str(target))
    assert initialized.returncode == 0, initialized.stderr
    metadata = json.loads(initialized.stdout)
    assert metadata["generation"] == 1 and "event" not in metadata
    before = target.read_bytes()
    inspected = command("status", "--path", str(target), "--request-id", REQ)
    assert inspected.returncode == 0, inspected.stderr
    state = json.loads(inspected.stdout)
    assert state["state"] == "absent" and not state["durability_confirmed"]
    assert state["observed_keyring_hash"] == sha256(before)
    assert "key_b64" not in initialized.stdout + inspected.stdout
    assert {p.name for p in rotation_directory.iterdir()} == {"keyring.json"}


@pytest.mark.skipif(os.name != "nt", reason="native Windows capability")
@pytest.mark.parametrize("action", ["prepare", "resume", "rotate"])
def test_cli_windows_mutation_is_unsupported_before_touching_missing_path(
    tmp_path: Path, action: str
) -> None:
    root = tmp_path / "absent"
    args = [action, "--path", str(root / "keyring.json"), "--request-id", REQ]
    if action != "resume":
        args += expected(b"synthetic")
    result = command(*args)
    assert result.returncode == 5 and not root.exists(), result.stderr
    assert json.loads(result.stderr)["code"] == "unsupported"
    assert json.loads(result.stdout)["event"] == "request"


@POSIX
def test_cli_prepare_resume_status_and_combined_rotate_retries(rotation_directory: Path) -> None:
    target, source, args = setup(rotation_directory)
    prepared = command("prepare", *args, *expected(source))
    assert prepared.returncode == 0, prepared.stderr
    lines = [json.loads(x) for x in prepared.stdout.splitlines()]
    assert [x["event"] for x in lines] == ["request", "result"]
    assert lines[-1]["state"] == "prepared" and target.read_bytes() == source
    resumed = command("resume", *args)
    assert resumed.returncode == 0, resumed.stderr
    accepted = json.loads(resumed.stdout.splitlines()[-1])
    assert accepted["state"] == "accepted" and accepted["generation"] == 2
    inode = target.stat().st_ino
    repeated = command("rotate", *args, *expected(source))
    assert repeated.returncode == 0, repeated.stderr
    assert json.loads(repeated.stdout.splitlines()[-1])["state"] == "accepted"
    assert target.stat().st_ino == inode
    mismatch = command("rotate", *args, *expected(target.read_bytes(), 2))
    assert mismatch.returncode == 4 and target.stat().st_ino == inode
    another = command("rotate", "--path", str(target), "--request-id", "b" * 32, *expected(source))
    assert another.returncode == 4 and BackupKeyring.from_path(target).generation == 2
    fresh = command(
        "rotate", "--path", str(target), "--request-id", "c" * 32, *expected(target.read_bytes(), 2)
    )
    assert fresh.returncode == 0, fresh.stderr
    assert [json.loads(line)["event"] for line in fresh.stdout.splitlines()] == [
        "request",
        "prepared",
        "result",
    ]
    assert BackupKeyring.from_path(target).generation == 3
    old = command("rotate", *args, *expected(source))
    assert (
        old.returncode == 0
        and json.loads(old.stdout.splitlines()[-1])["state"] == "accepted_superseded"
    )
    assert BackupKeyring.from_path(target).generation == 3


@POSIX
def test_explicit_backupkeyring_api_returns_operation_metadata_and_fixed_replay(
    rotation_directory: Path,
) -> None:
    target, source, _ = setup(rotation_directory)
    before = BackupKeyring.from_path(target)
    result = BackupKeyring.rotate(
        target, request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
    )
    assert result.state == "accepted" and result.generation == 2
    assert not isinstance(result, BackupKeyring) and not hasattr(result, "resolve")
    inode = target.stat().st_ino
    assert (
        BackupKeyring.rotate(
            target, request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
        )
        == result
    )
    assert target.stat().st_ino == inode
    assert BackupKeyring.from_path(target).resolve(before.active.key_id, before.active.fingerprint)


@POSIX
def test_combined_api_unknown_retry_keeps_original_expected_source(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target, source, _ = setup(rotation_directory)

    def fail(name: str) -> None:
        if name == "target_replaced":
            raise OSError("synthetic response lost after replacement")

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", fail)
        with pytest.raises(RotationOutcomeUnknownError):
            BackupKeyring.rotate(
                target, request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
            )
    inode = target.stat().st_ino
    result = BackupKeyring.rotate(
        target, request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
    )
    assert result.generation == 2 and target.stat().st_ino == inode


@POSIX
def test_stdout_failure_before_prepared_summary_prevents_publication(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target, source, args = setup(rotation_directory)

    class BrokenOutput(io.StringIO):
        def write(self, text: str) -> int:
            if '"event": "prepared"' in text:
                raise BrokenPipeError("synthetic closed output")
            return super().write(text)

    out = BrokenOutput()
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", out)
        with pytest.raises(SystemExit) as caught:
            run(["rotate", *args, *expected(source)])
    assert caught.value.code == 3 and target.read_bytes() == source
    assert BackupRotationJournal(target).status(request_id=REQ).state == "prepared_recorded"
    assert json.loads(capsys.readouterr().err)["code"] == "outcome_unknown"
    assert (
        BackupKeyring.rotate(
            target, request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
        ).generation
        == 2
    )


@POSIX
def test_cli_prints_and_flushes_request_before_any_mutation_and_target_before_publication(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, source, args = setup(rotation_directory)

    class Output(io.StringIO):
        flushes = 0

        def flush(self) -> None:
            self.flushes += 1
            super().flush()

    stream = Output()
    original = BackupRotationJournal.prepare

    def prepare(self: BackupRotationJournal, **kwargs: Any) -> Any:
        assert '"event": "request"' in stream.getvalue() and stream.flushes >= 1
        return original(self, **kwargs)

    def checkpoint(name: str) -> None:
        if name == "before_target_replace":
            assert '"event": "prepared"' in stream.getvalue() and stream.flushes >= 2

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", stream)
        patch.setattr(BackupRotationJournal, "prepare", prepare)
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", checkpoint)
        run(["rotate", *args, *expected(source)])
    assert json.loads(stream.getvalue().splitlines()[-1])["state"] == "accepted"


@POSIX
def test_cli_unknown_and_busy_exit_codes_keep_request_id(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target, source, args = setup(rotation_directory)
    api = BackupRotationJournal(target)
    api.prepare(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    owner = ProcessLock(target.with_name(target.name + ".rotation.lock"))
    owner.acquire()
    try:
        result = command("resume", *args)
        assert result.returncode == 6 and json.loads(result.stderr)["request_id"] == REQ
    finally:
        owner.release()

    def fail(name: str) -> None:
        if name == "target_replaced":
            raise OSError("synthetic disk response failure")

    with monkeypatch.context() as patch:
        patch.setattr("noyra.core.backup_rotation_publication.checkpoint", fail)
        with pytest.raises(SystemExit) as caught:
            run(["resume", *args])
    assert caught.value.code == 3
    stderr = capsys.readouterr().err
    assert json.loads(stderr)["code"] == "outcome_unknown" and "synthetic disk" not in stderr
    inode = target.stat().st_ino
    assert command("resume", *args).returncode == 0 and target.stat().st_ino == inode


@POSIX
def test_cli_real_process_exit_after_publication_uses_same_request_on_restart(
    rotation_directory: Path,
) -> None:
    target, source, args = setup(rotation_directory)
    script = """
import os,sys
sys.path.insert(0,sys.argv[1])
from noyra.backup_key_cli import run
import noyra.core.backup_rotation_publication as publication
def checkpoint(name):
    if name=='target_replaced':os._exit(91)
publication.checkpoint=checkpoint
run(['rotate','--path',sys.argv[2],'--request-id','a'*32,'--expected-generation','1',
     '--expected-source-hash',sys.argv[3]])
"""
    crashed = subprocess.run(
        [sys.executable, "-B", "-c", script, str(ROOT / "src"), str(target), sha256(source)],
        capture_output=True,
        text=True,
        timeout=45,
    )
    assert crashed.returncode == 91, crashed.stderr
    assert [json.loads(line)["event"] for line in crashed.stdout.splitlines()] == [
        "request",
        "prepared",
    ]
    inode = target.stat().st_ino
    result = command("rotate", *args, *expected(source))
    assert result.returncode == 0, result.stderr
    assert target.stat().st_ino == inode and BackupKeyring.from_path(target).generation == 2


@POSIX
def test_api_changed_expectation_and_corrupt_lookup_are_typed_without_mutation(
    rotation_directory: Path,
) -> None:
    target, source, _ = setup(rotation_directory)
    api = BackupRotationJournal(target)
    api.rotate(request_id=REQ, expected_generation=1, expected_source_hash=sha256(source))
    current = target.read_bytes()
    with pytest.raises(RotationConflictError, match="bound"):
        api.rotate(request_id=REQ, expected_generation=2, expected_source_hash=sha256(current))
    assert target.read_bytes() == current
    absent = rotation_directory / "missing" / "keyring.json"
    with pytest.raises(RotationConflictError):
        BackupKeyring.rotate(
            absent, request_id=REQ, expected_generation=1, expected_source_hash="a" * 64
        )
    assert not absent.parent.exists()


@pytest.mark.parametrize("action", ["prepare", "rotate", "resume"])
def test_failed_initial_output_does_not_touch_storage_even_for_valid_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], action: str
) -> None:
    class Broken(io.StringIO):
        def write(self, text: str) -> int:
            raise BrokenPipeError("synthetic closed stdout")

    missing = tmp_path / "absent" / "keyring.json"
    args = [action, "--path", str(missing), "--request-id", REQ]
    if action != "resume":
        args += expected(b"synthetic")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", Broken())
        with pytest.raises(SystemExit) as caught:
            run(args)
    assert caught.value.code == 3 and not missing.parent.exists()
    message = json.loads(capsys.readouterr().err)
    assert message["request_id"] == REQ and message["code"] == "outcome_unknown"


@POSIX
def test_cli_response_failure_after_acceptance_is_recoverable_same_id(
    rotation_directory: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    target, source, args = setup(rotation_directory)

    class BrokenFinal(io.StringIO):
        def write(self, text: str) -> int:
            if '"event": "result"' in text:
                raise BrokenPipeError("synthetic lost response")
            return super().write(text)

    with monkeypatch.context() as patch:
        patch.setattr(sys, "stdout", BrokenFinal())
        with pytest.raises(SystemExit) as caught:
            run(["rotate", *args, *expected(source)])
    assert caught.value.code == 3 and BackupKeyring.from_path(target).generation == 2
    assert json.loads(capsys.readouterr().err)["code"] == "outcome_unknown"
    inode = target.stat().st_ino
    result = command("rotate", *args, *expected(source))
    assert result.returncode == 0 and target.stat().st_ino == inode


@POSIX
def test_corrupt_evidence_cli_error_never_echoes_key_or_path(rotation_directory: Path) -> None:
    target, source, args = setup(rotation_directory)
    BackupRotationJournal(target).prepare(
        request_id=REQ, expected_generation=1, expected_source_hash=sha256(source)
    )
    header = target.with_name("." + target.name + ".rotation-journal") / "header.json"
    marker = b"SYNTHETIC_PRIVATE_RECORD_NEVER_ECHO"
    header.write_bytes(marker)
    result = command("resume", *args)
    assert result.returncode == 4 and json.loads(result.stderr)["code"] == "conflict"
    assert marker.decode() not in result.stderr + result.stdout
    assert str(rotation_directory) not in result.stderr + result.stdout
    assert "Traceback" not in result.stderr and target.read_bytes() == source


def test_rotation_dispatcher_never_reaches_the_removed_implicit_writer() -> None:
    import ast

    source = (ROOT / "src/noyra/core/at_rest.py").read_text(encoding="utf-8")
    parsed = ast.parse(source)
    ring = next(
        node
        for node in parsed.body
        if isinstance(node, ast.ClassDef) and node.name == "BackupKeyring"
    )
    methods = {node.name: node for node in ring.body if isinstance(node, ast.FunctionDef)}
    assert "_rotate_locked" not in methods
    calls = [node for node in ast.walk(methods["rotate"]) if isinstance(node, ast.Call)]
    assert not any(
        isinstance(node.func, ast.Attribute) and node.func.attr in {"write", "replace", "urandom"}
        for node in calls
    )


def test_cli_deployment_docs_share_current_requests_and_exit_codes() -> None:
    contract = (ROOT / "docs/development/backup-key-commands.md").read_text(encoding="utf-8")
    assert all(
        token in contract
        for token in [
            "--request-id",
            "--expected-generation",
            "--expected-source-hash",
            "RotationRequestRequiredError",
            "RotationJournalStatus",
        ]
    )
    ubuntu = (ROOT / "docs/deployment/ubuntu.md").read_text(encoding="utf-8")
    desktop = (ROOT / "docs/deployment/desktop.md").read_text(encoding="utf-8")
    assert (
        "backup-key prepare" in ubuntu
        and "backup-key resume" in ubuntu
        and "--request-id" in ubuntu
    )
    assert "exit code 5" in desktop and "backup-key-commands.md" in desktop


@POSIX
def test_root_explicit_api_retains_group_read_and_rejects_retried_altered_expectation(
    rotation_directory: Path,
) -> None:
    uid, chown = getattr(os, "geteuid", None), getattr(os, "chown", None)
    assert callable(uid) and callable(chown)
    if uid() != 0:
        pytest.skip("actual root service group")
    target, source, args = setup(rotation_directory)
    chown(target, 0, 65534)
    target.chmod(0o640)
    result = command("rotate", *args, *expected(source))
    assert result.returncode == 0, result.stderr
    metadata = target.stat()
    import stat

    assert (metadata.st_uid, metadata.st_gid, stat.S_IMODE(metadata.st_mode)) == (0, 65534, 0o640)
    inode = metadata.st_ino
    assert command("rotate", *args, *expected(source)).returncode == 0
    assert command("rotate", *args, *expected(target.read_bytes(), 2)).returncode == 4
    assert target.stat().st_ino == inode


@pytest.mark.skipif(os.name != "nt", reason="native Windows explicit API capability")
def test_explicit_api_reports_unsupported_before_missing_path_access(tmp_path: Path) -> None:
    root = tmp_path / "absent"
    with pytest.raises(RotationUnsupportedError):
        BackupKeyring.rotate(
            root / "keyring.json",
            request_id=REQ,
            expected_generation=1,
            expected_source_hash="a" * 64,
        )
    assert not root.exists()
