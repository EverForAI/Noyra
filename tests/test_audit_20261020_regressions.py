from __future__ import annotations

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.autonomy.loop import AutonomyLoop
from noyra.cognition import CognitionSettings, SleepReflectionCognition
from noyra.core import EventStore, SubjectKernel
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.backup_retention import backup_inventory
from noyra.core.storage_mode import StorageLayout, StorageMode
from noyra.core.types import content_hash
from noyra.deployment_setup import SetupError, SetupOptions, SetupRunner, SubprocessRunner
from noyra.interaction.store import InteractionStore
from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelLedger
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.sleep import SleepEngine, SleepReflectionPlan


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    instance = SubjectKernel(
        tmp_path / "data" / "noyra.sqlite3", "audit-regression", content_hash("audit")
    )
    instance.boot()
    instance.orient()
    instance.activate()
    try:
        yield instance
    finally:
        instance.close()


def reflection(kernel: SubjectKernel) -> SleepReflectionCognition:
    gateway = ModelGateway(
        FakeProvider([]),
        ModelLedger(kernel.database),
        model="fixture",
        limits=BudgetLimits(20, 100_000, 100_000, 1_000_000),
    )
    return SleepReflectionCognition(
        kernel.database, kernel.subject_id, gateway, CognitionSettings()
    )


def finish_sleep(engine: SleepEngine, sleep_id: str) -> None:
    engine.commit_reflection(sleep_id, SleepReflectionPlan(summary="local integration"))
    engine.enter_deep_sleep(sleep_id)
    engine.wake(sleep_id, "test wake", force=True)
    engine.complete_wake(sleep_id)


@pytest.mark.parametrize("offset", [0, -60, 60])
def test_sleep_arrivals_are_deferred_once_even_with_equal_or_shifted_timestamps(
    kernel: SubjectKernel,
    offset: int,
) -> None:
    base = datetime.now(UTC) + timedelta(seconds=1)
    clock = [base]

    def stamp() -> str:
        return clock[0].isoformat(timespec="milliseconds")

    engine = SleepEngine(kernel.database, kernel.subject_id, clock=stamp)
    cognition = reflection(kernel)
    first = engine.start("subject_choice", "first")
    first = engine.begin_reflection(first.sleep_id)
    events = EventStore(kernel.database)
    late = events.append(
        kernel.subject_id,
        "experience",
        "test",
        {"body": "during sleep"},
        occurred_at=(base + timedelta(seconds=offset)).isoformat(timespec="milliseconds"),
    )
    message = InteractionStore(kernel.database).receive(
        kernel.subject_id, "test", "person", "during sleep"
    )
    assert late.event_id not in cognition._context(first).event_ids
    assert message.interaction_id not in cognition._context(first).interaction_ids
    clock[0] = base + timedelta(seconds=2)
    finish_sleep(engine, first.sleep_id)
    # Reconstruct readers to exercise durable recovery rather than an in-memory cursor.
    cognition = reflection(kernel)
    clock[0] = base + timedelta(seconds=3)
    second = engine.start("subject_choice", "second")
    second = engine.begin_reflection(second.sleep_id)
    context = cognition._context(second)
    assert late.event_id in context.event_ids
    assert message.interaction_id in context.interaction_ids
    assert context.event_ids == cognition._context(second).event_ids
    finish_sleep(engine, second.sleep_id)
    clock[0] = base + timedelta(seconds=4)
    third = engine.begin_reflection(engine.start("subject_choice", "third").sleep_id)
    assert late.event_id not in cognition._context(third).event_ids
    assert message.interaction_id not in cognition._context(third).interaction_ids


@pytest.mark.asyncio
@pytest.mark.parametrize("previous_failures", [0, 4, 1023, 1024, 10**12])
async def test_backoff_saturates_with_real_persisted_failure_state(
    kernel: SubjectKernel,
    monkeypatch: pytest.MonkeyPatch,
    previous_failures: int,
) -> None:
    loop = AutonomyLoop(kernel)
    loop.health()
    with kernel.database.transaction() as db:
        db.execute(
            "UPDATE autonomy_loop_state SET consecutive_failures=? WHERE subject_id=?",
            (previous_failures, kernel.subject_id),
        )
    stop = asyncio.Event()
    observed: list[float] = []
    wait_for = asyncio.wait_for

    async def failure() -> Any:
        stop.set()
        raise RuntimeError("fixture")

    async def capture(awaitable: Any, timeout: float) -> Any:
        observed.append(timeout)
        return await wait_for(awaitable, timeout)

    monkeypatch.setattr(loop, "_tick_once", failure)
    monkeypatch.setattr(asyncio, "wait_for", capture)
    await loop.run_forever(stop)
    assert loop.health()["consecutive_failures"] == previous_failures + 1
    assert observed == [min(1800, 30 * 2 ** min(previous_failures, 6))]


def test_unreadable_inventory_error_is_not_hidden(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def denied(self: Path) -> Any:
        raise PermissionError("fixture")

    monkeypatch.setattr(Path, "iterdir", denied)
    with pytest.raises(PermissionError):
        backup_inventory(tmp_path)


@pytest.mark.skipif(
    os.name != "posix" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="requires Linux root and a real unprivileged account",
)
def test_unreadable_backup_plan_with_real_unprivileged_user() -> None:
    if shutil.which("runuser") is None:
        pytest.skip("runuser unavailable")
    source = str(Path(__file__).resolve().parents[1] / "src")
    with tempfile.TemporaryDirectory(
        dir="/var/tmp", prefix="noyra-retention-regression-"
    ) as scratch:
        base = Path(scratch)
        base.chmod(0o755)
        backups = base / "backups"
        backups.mkdir(mode=0o700)
        (backups / "noyra-fixture.noyra-backup").write_bytes(b"fixture")
        script = (
            "import sys,tempfile,json;from pathlib import Path;sys.path.insert(0,sys.argv[1]);"
            "from noyra.core import SubjectKernel;from noyra.core.types import content_hash;"
            "from noyra.core.retention import RetentionManager,RetentionSettings;"
            "tmp=tempfile.TemporaryDirectory();k=SubjectKernel(Path(tmp.name)/'noyra.sqlite3','audit',content_hash('audit'));"
            "k.boot();k.orient();k.activate();"
            "p=RetentionManager(k.database,RetentionSettings(),backup_root=Path(sys.argv[2])).retention_plan('audit');"
            "print(json.dumps(p['backup']));k.close();tmp.cleanup()"
        )
        result = subprocess.run(
            [
                "runuser",
                "-u",
                "nobody",
                "--",
                sys.executable,
                "-B",
                "-c",
                script,
                source,
                str(backups),
            ],
            capture_output=True,
            text=True,
            timeout=90,
            check=True,
        )
        plan = json.loads(result.stdout)
        assert plan["status"] == "unavailable"
        assert plan["reason_code"] == "backup_inventory_unavailable"
        assert plan["apply_allowed"] is False


def test_readiness_refreshes_mount_loss_and_recovers(
    kernel: SubjectKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = tmp_path / "data"
    backups = tmp_path / "backups"
    backups.mkdir()
    layout = StorageLayout(
        data, backups, data / "upgrade", StorageMode.SYSTEM_DISK, "/dev/system", "/dev/backup", True
    )
    state = [layout]
    monkeypatch.setattr("noyra.service.detect_storage_layout", lambda *args: state[0])
    token = "audit-readiness-token-0123456789abcdef"
    settings = ServiceSettings(
        data_dir=data,
        backup_root=backups,
        subject_id=kernel.subject_id,
        genesis_hash=content_hash("audit"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(token),
        storage_profile="recovery_capable",
        health_cache_ttl_seconds=1,
    )
    http = NoyraHTTPServer(kernel, settings)
    http.start()
    try:

        def read() -> tuple[int, Any]:
            http._storage_observation_at = 0
            request = Request(
                f"http://127.0.0.1:{http.address[1]}/api/admin/readiness",
                headers={"Authorization": f"Bearer {token}"},
            )
            try:
                response = urlopen(request, timeout=10)
            except HTTPError as error:
                response = error
            with response:
                return response.code, json.loads(response.read())

        assert read()[0] == 200
        state[0] = replace(layout, backup_reason="backup_mount_missing")
        status, body = read()
        assert status == 503
        assert body["components"]["backup"]["reason_code"] == "backup_mount_missing"
        state[0] = layout
        assert read()[0] == 200
        state[0] = replace(
            layout,
            mode=StorageMode.UNAVAILABLE,
            writable=False,
            reason_code="storage_mount_identity_mismatch",
        )
        status, body = read()
        assert status == 503
        assert body["components"]["storage"]["reason_code"] == "storage_mount_identity_mismatch"
    finally:
        http.close()


def test_subprocess_timeout_is_bounded_and_redacted() -> None:
    started = time.monotonic()
    with pytest.raises(SetupError, match="COMMAND_TIMEOUT") as caught:
        SubprocessRunner().run(
            [sys.executable, "-c", "import time; time.sleep(30)", "secret-fixture"],
            timeout_seconds=0.1,
        )
    assert "secret-fixture" not in str(caught.value)
    assert time.monotonic() - started < 10


def test_setup_timeout_restores_files_and_has_fresh_rollback_budget(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    env = tmp_path / "noyra.env"
    original = "NOYRA_HOST=127.0.0.1\nNOYRA_PORT=8765\n"
    env.write_text(original, encoding="utf-8")
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("original caddy", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args: [])

    class Runner:
        def run(self, argv: Any, **kwargs: Any) -> Any:
            calls.append(tuple(argv))
            if argv[0] == "curl":
                setup._deadline = time.monotonic() - 1
                raise subprocess.TimeoutExpired(argv, 1, stderr="secret-fixture")
            assert setup._remaining() > 0
            return subprocess.CompletedProcess(argv, 0, "", "")

    setup = SetupRunner(
        SetupOptions(
            mode="public",
            public_domain="public.example.com",
            admin_domain="admin.example.com",
            env_path=env,
            caddy_path=caddy,
            backup_root=tmp_path / "backups",
            replace=True,
        ),
        Runner(),
    )
    result = setup.run()
    assert not result.ok and "COMMAND_TIMEOUT" in result.stderr
    assert "secret-fixture" not in result.stderr
    assert env.read_text(encoding="utf-8") == original
    assert caddy.read_text(encoding="utf-8") == "original caddy"
    assert calls[-2:] == [("systemctl", "restart", "noyra"), ("systemctl", "reload", "caddy")]
    curl = next(command for command in calls if command[0] == "curl")
    assert "--connect-timeout" in curl and "--max-time" in curl


def test_cloudflare_setup_from_installed_wheel(tmp_path: Path) -> None:
    repo = Path(__file__).resolve().parents[1]
    source = tmp_path / "source"
    source.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(repo / name, source / name)
    shutil.copytree(
        repo / "src",
        source / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info"),
    )
    wheels = tmp_path / "wheels"
    subprocess.run(
        [
            sys.executable,
            "-B",
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-cache-dir",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheels),
            str(source),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    installed = tmp_path / "installed"
    with zipfile.ZipFile(next(wheels.glob("*.whl"))) as archive:
        assert "noyra/resources/cloudflared-noyra.service" in archive.namelist()
        assert (
            archive.read("noyra/resources/cloudflared-noyra.service")
            == (repo / "deploy/systemd/cloudflared-noyra.service.example").read_bytes()
        )
        archive.extractall(installed)
    script = (
        "import sys,json;from pathlib import Path;from subprocess import CompletedProcess;"
        "from unittest.mock import patch;sys.path.insert(0,sys.argv[1]);"
        "import noyra.deployment_setup as s;"
        "assert Path(s.__file__).is_relative_to(Path(sys.argv[1]));"
        "env=Path(sys.argv[2]);env.write_text('NOYRA_HOST=127.0.0.1'+chr(10)+'NOYRA_PORT=8765'+chr(10));"
        "runner=type('Runner',(),{'run':lambda self,argv,**kw:CompletedProcess(argv,0,'','')})();"
        "ctx=patch('socket.getaddrinfo',return_value=[]);ctx.start();"
        "r=s.SetupRunner(s.SetupOptions(mode='cloudflare',public_domain='public.example.com',"
        "admin_domain='admin.example.com',env_path=env,dry_run=True,tunnel_token='fixture'),runner).run();"
        "ctx.stop();print(json.dumps({'ok':r.ok,'stderr':r.stderr}));assert r.ok,r.stderr"
    )
    subprocess.run(
        [sys.executable, "-I", "-B", "-c", script, str(installed), str(tmp_path / "noyra.env")],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=90,
        check=True,
    )


def test_concurrent_rotation_is_rejected_then_retry_preserves_backup(tmp_path: Path) -> None:
    keyring = tmp_path / "keyring.json"
    BackupKeyring.initialize(keyring)
    entered, release = tmp_path / "entered", tmp_path / "release"
    source = str(Path(__file__).resolve().parents[1] / "src")
    script = """
import sys,time
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from noyra.core.at_rest import BackupKeyring
original=BackupKeyring.write
entered,release=Path(sys.argv[3]),Path(sys.argv[4])
def controlled(self,*args,**kwargs):
    entered.write_text('ready')
    deadline=time.monotonic()+60
    while not release.exists():
        if time.monotonic()>deadline:raise TimeoutError('test schedule')
        time.sleep(.02)
    return original(self,*args,**kwargs)
BackupKeyring.write=controlled
BackupKeyring.rotate(sys.argv[2])
"""
    first = subprocess.Popen(
        [sys.executable, "-B", "-c", script, source, str(keyring), str(entered), str(release)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 60
        while not entered.exists() and first.poll() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert entered.exists()
        attempt = subprocess.run(
            [
                sys.executable,
                "-B",
                "-c",
                "import sys;sys.path.insert(0,sys.argv[1]);"
                "from noyra.core.at_rest import BackupKeyring;"
                "BackupKeyring.rotate(sys.argv[2])",
                source,
                str(keyring),
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert attempt.returncode != 0
        assert "RuntimeOwnershipError" in attempt.stderr
    finally:
        release.write_text("release", encoding="utf-8")
        _, stderr = first.communicate(timeout=90)
    assert first.returncode == 0, stderr
    intermediate = BackupKeyring.from_path(keyring)
    data = tmp_path / "data"
    subject = SubjectKernel(data / "noyra.sqlite3", "backup-test", content_hash("backup"))
    subject.boot()
    subject.orient()
    subject.activate()
    subject.close()
    manager = EncryptedBackupManager(data, keyring)
    artifact = manager.create(tmp_path / "test.noyra-backup")
    manager.restore(artifact.path, tmp_path / "before")
    final = BackupKeyring.rotate(keyring)
    assert final.generation == 3
    assert final.resolve(intermediate.active.key_id, intermediate.active.fingerprint)
    manager.restore(artifact.path, tmp_path / "after")


def test_failed_rotation_releases_lock_and_keeps_previous_keyring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    keyring = tmp_path / "keyring.json"
    before = BackupKeyring.initialize(keyring)
    with monkeypatch.context() as patch:

        def failed_write(*args: Any, **kwargs: Any) -> None:
            raise OSError("fixture")

        patch.setattr(BackupKeyring, "write", failed_write)
        with pytest.raises(OSError):
            BackupKeyring.rotate(keyring)
    assert BackupKeyring.from_path(keyring).active_key_id == before.active_key_id
    assert BackupKeyring.rotate(keyring).generation == 2


def test_legacy_sleep_cutoff_catches_sleep_arrivals_without_schema_migration(
    kernel: SubjectKernel,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    base = datetime.now(UTC) + timedelta(seconds=1)
    clock = [base]

    def stamp() -> str:
        return clock[0].isoformat(timespec="milliseconds")

    engine = SleepEngine(kernel.database, kernel.subject_id, clock=stamp)
    original = EventStore._append_connection

    def legacy_append(
        self: EventStore,
        connection: Any,
        subject_id: str,
        event_type: str,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if event_type == "sleep_reflection_boundary":
            return None
        return original(self, connection, subject_id, event_type, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(EventStore, "_append_connection", legacy_append)
        first = engine.start("subject_choice", "legacy sleep")
    first = engine.begin_reflection(first.sleep_id)
    late = EventStore(kernel.database).append(
        kernel.subject_id,
        "experience",
        "test",
        {"body": "legacy arrival"},
        occurred_at=(base + timedelta(seconds=1)).isoformat(timespec="milliseconds"),
    )
    cognition = reflection(kernel)
    assert late.event_id not in cognition._context(first).event_ids
    clock[0] = base + timedelta(seconds=2)
    finish_sleep(engine, first.sleep_id)
    clock[0] = base + timedelta(seconds=3)
    second = engine.begin_reflection(engine.start("subject_choice", "new sleep").sleep_id)
    assert late.event_id in cognition._context(second).event_ids
    # No in-memory reader or database connection is required to retain the cutoff.
    data_path = kernel.database.path
    kernel.close()
    reopened = SubjectKernel(data_path, kernel.subject_id, content_hash("audit"))
    try:
        restored_run = SleepEngine(reopened.database, reopened.subject_id).current()
        assert restored_run is not None
        assert late.event_id in reflection(reopened)._context(restored_run).event_ids
    finally:
        reopened.close()


@pytest.mark.parametrize(
    "taskkill_delay",
    [
        0.0,
        pytest.param(
            1.1,
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows taskkill timing"),
        ),
    ],
)
def test_subprocess_timeout_terminates_descendants(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, taskkill_delay: float
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    completed = tmp_path / "descendant-completed"
    real_run = subprocess.run

    def delayed_taskkill(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if taskkill_delay and argv[0] == "taskkill":
            time.sleep(taskkill_delay)
        return real_run(argv, *args, **kwargs)

    monkeypatch.setattr("noyra.deployment_setup.subprocess.run", delayed_taskkill)
    with socket.socket() as listener, ThreadPoolExecutor(max_workers=1) as executor:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(15)
        child = (
            "import socket;from pathlib import Path;"
            f"s=socket.create_connection({listener.getsockname()!r},timeout=20);"
            "s.settimeout(None);s.sendall(b'R');"
            f"Path({str(completed)!r}).touch() if s.recv(1)==b'G' else None;"
            "s.close()"
        )
        parent = (
            "import subprocess,sys,time;subprocess.Popen([sys.executable,'-c',"
            + repr(child)
            + "]);time.sleep(30)"
        )
        result = executor.submit(
            SubprocessRunner().run, [sys.executable, "-c", parent], timeout_seconds=5
        )
        connection, _ = listener.accept()
        with connection:
            connection.settimeout(20)
            assert connection.recv(1) == b"R"  # The real descendant is running.
            with pytest.raises(SetupError, match="COMMAND_TIMEOUT"):
                result.result(timeout=20)
            # Timeout initiates cleanup; it does not promise an instantaneous
            # kill. Only release the side effect AFTER cleanup has returned.
            try:
                connection.sendall(b"G")
                assert connection.recv(1) == b""
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass
        assert not completed.exists()


@pytest.mark.parametrize("operation", ["logout", "capacity"])
def test_session_revocation_survives_wall_clock_rollback(
    kernel: SubjectKernel, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    settings = ServiceSettings(
        data_dir=kernel.database.path.parent,
        backup_root=tmp_path / "backups",
        subject_id=kernel.subject_id,
        genesis_hash=content_hash("audit"),
        host="127.0.0.1",
        port=0,
        admin_session_max_count=8,
        operator_token=SecretStr("clock-regression-operator-" + "x" * 40),
    )
    http = NoyraHTTPServer(kernel, settings)
    now = [2_000.0]
    monkeypatch.setattr("noyra.service.time.time", lambda: now[0])
    try:
        count = settings.admin_session_max_count if operation == "capacity" else 1
        old_ids = [
            http.create_admin_session(role="operator", actor="clock-test")[0] for _ in range(count)
        ]
        now[0] = 1_990.0
        if operation == "logout":
            http.revoke_admin_session(old_ids[0])
        else:
            new_id, _ = http.create_admin_session(role="operator", actor="clock-test")
            assert http.admin_session(new_id) is not None
        revoked = [value for value in old_ids if http.admin_session(value) is None]
        assert len(revoked) == 1
        with kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT created_at, revoked_at FROM admin_sessions WHERE session_hash=?",
                (http._admin_session_hash(revoked[0]),),
            ).fetchone()
        assert row is not None and row["revoked_at"] == row["created_at"] == 2_000.0
    finally:
        http.close()
