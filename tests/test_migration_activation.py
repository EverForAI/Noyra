from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core.events import EventStore
from noyra.core.lifecycle import LifecycleManager
from noyra.core.types import content_hash
from noyra.migration import activation as activation_module
from noyra.migration.activation import (
    TargetActivationError,
    TargetRuntimeActivator,
    run_target_activation_requests,
)
from noyra.migration.fencing import EpochLease
from noyra.migration.manager import MigrationManager
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry

SUBJECT_ID = "Noyra-0001"
TARGET_ID = "target-1"
ARTIFACT_ID = "artifact-1"
MANIFEST = {
    "artifact_id": ARTIFACT_ID,
    "artifact_format": "sqlite",
    "subject_id": SUBJECT_ID,
}
MANIFEST_DIGEST = content_hash(MANIFEST)
_ARTIFACT_HASHES: dict[str, str] = {}
_REAL_SECURE_ROOT_DIRECTORY = activation_module._secure_root_directory


@pytest.fixture(autouse=True)
def _allow_current_user_owned_test_roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Model root ownership for isolated temp trees without weakening production checks."""
    if os.name == "nt":
        return

    test_root = tmp_path.resolve()
    test_owner = test_root.stat()
    monkeypatch.setattr(activation_module, "_service_uid", lambda: test_owner.st_uid)
    monkeypatch.setattr(activation_module, "_service_gid", lambda: test_owner.st_gid)
    if test_owner.st_uid == 0:
        return

    def validate_test_root(
        path: Path,
        error_code: str,
        *,
        create: bool = False,
        trusted_root: Path | None = None,
        allow_service_owned_root: bool = False,
    ) -> None:
        candidate = Path(path)
        try:
            candidate.absolute().relative_to(test_root)
            if trusted_root is not None:
                Path(trusted_root).absolute().relative_to(test_root)
        except ValueError:
            _REAL_SECURE_ROOT_DIRECTORY(
                candidate,
                error_code,
                create=create,
                trusted_root=trusted_root,
                allow_service_owned_root=allow_service_owned_root,
            )
            return

        if create:
            candidate.mkdir(mode=0o700, parents=True, exist_ok=True)
        if candidate.is_symlink() or not candidate.is_dir():
            raise TargetActivationError(error_code)

        directories = [candidate]
        parent = candidate.parent
        while trusted_root is not None and parent != Path(trusted_root).parent:
            directories.append(parent)
            if parent == trusted_root:
                break
            parent = parent.parent
        if trusted_root is not None and Path(trusted_root) not in directories:
            raise TargetActivationError(error_code)

        for directory in directories:
            if directory.is_symlink() or not directory.is_dir():
                raise TargetActivationError(error_code)
            metadata = directory.stat(follow_symlinks=False)
            if (
                metadata.st_uid not in {0, test_owner.st_uid}
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                raise TargetActivationError(error_code)

    monkeypatch.setattr(activation_module, "_secure_root_directory", validate_test_root)


class _Systemd:
    def __init__(self, ready: Any | None = None, *, start_error: bool = False) -> None:
        self.active = True
        self.calls: list[str] = []
        self.ready = ready or (lambda subject, target: True)
        self.start_error = start_error

    def stop(self) -> None:
        self.calls.append("stop")
        self.active = False

    def start(self) -> None:
        self.calls.append("start")
        if self.start_error:
            self.start_error = False
            self.active = False
            raise TargetActivationError("service_start_failed")
        self.active = True

    def daemon_reload(self) -> None:
        self.calls.append("reload")

    def is_active(self) -> bool:
        return self.active

    def wait_ready(self, subject_id: str, target_id: str | None, timeout: float) -> bool:
        self.calls.append(f"ready:{subject_id}:{target_id}")
        return bool(self.ready(subject_id, target_id))


def _boot_runtime_from_dropin(
    activator: TargetRuntimeActivator, initial_environment: dict[str, str]
) -> dict[str, Any]:
    """Real application boot; only unit orchestration is replaced in this fixture."""
    environment = {key: value for key, value in os.environ.items() if not key.startswith("NOYRA_")}
    environment.update(
        NOYRA_PROFILE="test",
        NOYRA_PORT="0",
        NOYRA_DATA_DIR=str(activator.data_root),
        NOYRA_WALLET_MODE="disabled",
        NOYRA_INTEGRITY_MODE="pause",
        PYTHONUTF8="1",
    )
    environment.update(initial_environment)
    dropin = activator.dropin_dir / "migration-target.conf"
    if dropin.exists():
        for line in dropin.read_text(encoding="utf-8").splitlines():
            if line.startswith("EnvironmentFile="):
                path = Path(shlex.split(line.partition("=")[2])[0].replace("%%", "%"))
                for assignment in path.read_text(encoding="utf-8").splitlines():
                    key, _, value = shlex.split(assignment)[0].partition("=")
                    environment[key] = value
    completed = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-c",
            """
import json
from noyra.service import NoyraService
service = NoyraService.from_env()
try:
    service.boot()
    assert service.kernel.admission.accepting, service.integrity.summary()
    print(json.dumps({
        'subject_id': service.settings.subject_id,
        'genesis_hash': service.settings.genesis_hash,
        'target_id': service.kernel.migration_target_id,
    }))
finally:
    service.close()
""",
        ],
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    return dict(json.loads(completed.stdout.strip().splitlines()[-1]))


def _prepared_target_database(path: Path) -> tuple[str, str]:
    database = Database(path)
    IdentityStore(database).ensure(SUBJECT_ID, content_hash({"subject": SUBJECT_ID}))
    LifecycleManager(database, EventStore(database), SUBJECT_ID).ensure_initial()
    store = MigrationStore(database)
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    targets = TargetRegistry(database, store)
    targets.register(
        SUBJECT_ID,
        target_id=TARGET_ID,
        public_key=base64.urlsafe_b64encode(public).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = targets.issue_challenge(TARGET_ID, source_epoch="runtime-0")
    targets.attest(
        TARGET_ID,
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    policy = store.read_policy(SUBJECT_ID)
    policy = store.update_policy(SUBJECT_ID, policy.revision, {"enabled": True}, "operator")
    manager = MigrationManager(database, store)
    proposal = manager.create_proposal(
        subject_id=SUBJECT_ID,
        target_id=TARGET_ID,
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="approved migration fixture",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(
        proposal.proposal_id, actor="operator", idempotency_key="activation-test"
    )
    epoch = EpochLease._acquire_unchecked(
        database,
        SUBJECT_ID,
        TARGET_ID,
        expected_source_epoch=task.source_epoch,
        actor="test",
    )
    manager.transition_task(
        task.task_id,
        "preparing",
        actor="test",
        target_epoch_id=epoch.epoch_id,
    )
    manager.transition_task(
        task.task_id,
        "transferring",
        actor="test",
        manifest_digest=MANIFEST_DIGEST,
        artifact_id=ARTIFACT_ID,
    )
    manager.transition_task(task.task_id, "restoring", actor="test")
    manager.transition_task(task.task_id, "validating", actor="test")
    _ARTIFACT_HASHES[task.task_id] = hashlib.sha256(path.read_bytes()).hexdigest()
    return task.task_id, content_hash(
        {
            "task_id": task.task_id,
            "source_epoch": task.source_epoch,
            "epoch_id": epoch.epoch_id,
            "epoch_number": epoch.epoch_number,
            "status": "active",
        }
    )


def _request(task_id: str, source_fence_digest: str) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "subject_id": SUBJECT_ID,
        "target_id": TARGET_ID,
        "source_epoch": "runtime-0",
        "manifest_digest": MANIFEST_DIGEST,
        "artifact_id": ARTIFACT_ID,
        "format": "noyra-target-activation/v2",
        "artifact_sha256": "f" * 64,
        "restored_database_sha256": _ARTIFACT_HASHES.get(task_id, "e" * 64),
        "inventory_sha256": None,
        "recipient_key_fingerprint": "c" * 64,
        "target_volume_proof_digest": "e" * 64,
        "credential_binding_digest": "f" * 64,
        "signer_binding_digest": None,
        "wallet_mode": "disabled",
        "wallet_proof_digest": None,
        "health_report_digest": "b" * 64,
        "source_fence_digest": source_fence_digest,
    }


def _runtime_fixture(tmp_path: Path, *, ready: Any | None = None) -> tuple[Any, ...]:
    data_root = tmp_path / "var-lib-noyra"
    task_id, fence_digest = _prepared_target_database(tmp_path / "target.sqlite3")
    restored = data_root / "migration-agent" / "restored" / task_id / "noyra.sqlite3"
    restored.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(tmp_path / "target.sqlite3", restored)

    active_database = data_root / "noyra.sqlite3"
    active_database.parent.mkdir(parents=True, exist_ok=True)
    old_database = Database(active_database)
    IdentityStore(old_database).ensure(SUBJECT_ID, content_hash({"subject": SUBJECT_ID}))
    LifecycleManager(old_database, EventStore(old_database), SUBJECT_ID).ensure_initial()
    previous_digest = hashlib.sha256(active_database.read_bytes()).hexdigest()

    systemd = _Systemd(ready)
    activator = TargetRuntimeActivator(
        data_root,
        tmp_path / "etc-noyra",
        systemd=systemd,
        ready_timeout_seconds=0.01,
    )
    return task_id, fence_digest, active_database, previous_digest, systemd, activator


def test_target_activation_switches_to_the_restored_subject_database(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, _, systemd, activator = _runtime_fixture(tmp_path)

    receipt = activator.activate(_request(task_id, fence_digest))

    assert receipt["status"] == "active"
    assert receipt["service_unit"] == "noyra.service"
    assert receipt["source_fence_digest"] == fence_digest
    assert (
        receipt["active_database_sha256"]
        == hashlib.sha256(active_database.read_bytes()).hexdigest()
    )
    assert systemd.calls[-2:] == ["start", f"ready:{SUBJECT_ID}:{TARGET_ID}"]
    with sqlite3.connect(active_database) as connection:
        task = connection.execute(
            "SELECT status FROM migration_tasks WHERE task_id=?", (task_id,)
        ).fetchone()
    assert task == ("committed",)


def _prepare_complete_subject(
    tmp_path: Path, activator: Any, task_id: str, fence: str
) -> dict[str, Any]:
    from noyra.migration.subject_payload import create_payload, restore_payload

    restored = activator.agent_root / "restored" / task_id
    source_tree = tmp_path / "source-tree"
    (source_tree / "workspace" / "empty").mkdir(parents=True)
    (source_tree / "workspace" / "new.txt").write_text("source work")
    (activator.data_root / "workspace").mkdir()
    (activator.data_root / "workspace" / "old.txt").write_text("target work")
    payload = tmp_path / "subject.tar"
    inventory = create_payload(restored / "noyra.sqlite3", source_tree, payload, SUBJECT_ID)
    prepared = tmp_path / "prepared"
    restore_payload(payload, prepared, SUBJECT_ID)
    shutil.rmtree(restored)
    prepared.rename(restored)
    return {**_request(task_id, fence), "inventory_sha256": content_hash(inventory)}


@pytest.mark.parametrize("fail_start", [False, True])
def test_complete_subject_directories_follow_database_activation_and_rollback(
    tmp_path: Path, fail_start: bool
) -> None:
    task_id, fence, _, _, systemd, activator = _runtime_fixture(tmp_path)
    request = _prepare_complete_subject(tmp_path, activator, task_id, fence)
    systemd.start_error = fail_start
    if fail_start:
        with pytest.raises(TargetActivationError, match="service_start_failed"):
            activator.activate(request)
    else:
        receipt = activator.activate(request)
        assert receipt["inventory_sha256"] == request["inventory_sha256"]
        assert (activator.data_root / "workspace" / "new.txt").read_text() == "source work"
        assert (activator.data_root / "workspace" / "empty").is_dir()
        assert not (activator.data_root / "workspace" / "old.txt").exists()
        activator.deactivate(
            {
                key: request[key]
                for key in ("task_id", "target_id", "source_epoch", "manifest_digest")
            }
        )
    assert (activator.data_root / "workspace" / "old.txt").read_text() == "target work"
    assert not (activator.data_root / "workspace" / "new.txt").exists()
    assert systemd.active


def test_crash_after_subject_directory_switch_restores_old_tree_before_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fence, _, _, _, activator = _runtime_fixture(tmp_path)
    request = _prepare_complete_subject(tmp_path, activator, task_id, fence)

    class Crash(BaseException):
        pass

    def crash(*args: Any) -> None:
        raise Crash()

    monkeypatch.setattr(activator, "_move_database_set", crash)
    with pytest.raises(Crash):
        activator.activate(request)
    assert (activator.data_root / "workspace" / "new.txt").is_file()
    assert activator.recover_incomplete() == 1
    assert (activator.data_root / "workspace" / "old.txt").read_text() == "target work"
    assert not (activator.data_root / "workspace" / "new.txt").exists()
    assert activator.recover_incomplete() == 0


@pytest.mark.parametrize("local_wallet", [False, True])
def test_approved_source_task_runs_real_bundle_cli_bridge_and_root_activation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, local_wallet: bool
) -> None:
    import json
    import time
    from concurrent.futures import ThreadPoolExecutor

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from noyra.core.admission import RuntimeAdmissionGate
    from noyra.core.at_rest import VolumeEncryptionStatus
    from noyra.migration.activation import TargetActivationBridge
    from noyra.migration.agent import MigrationAgent
    from noyra.migration.cutover import CutoverCoordinator
    from noyra.migration.http_executor import HTTPMigrationExecutor, SQLiteArtifactProvider
    from test_migration_agent_cli import _module

    source = Database(tmp_path / "source" / "noyra.sqlite3")
    IdentityStore(source).ensure(SUBJECT_ID, "a" * 64)
    LifecycleManager(source, EventStore(source), SUBJECT_ID).ensure_initial()
    (source.path.parent / "workspace").mkdir()
    (source.path.parent / "workspace" / "work.txt").write_text("retained")
    signing, recipient = Ed25519PrivateKey.generate(), X25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(signing.public_key().public_bytes_raw()).decode()
    recipient_public = (
        base64.urlsafe_b64encode(recipient.public_key().public_bytes_raw()).decode().rstrip("=")
    )
    store = MigrationStore(source)
    policy = store.read_policy(SUBJECT_ID)
    policy = store.update_policy(
        SUBJECT_ID, policy.revision, {"enabled": True, "wallet_mode": "disabled"}, "operator"
    )
    wallet_binding: dict[str, Any] = {"mode": "disabled"}
    if local_wallet:
        from noyra.wallet.keystore import create_keystore

        wallet_root = source.path.parent / "secrets" / "wallet"
        address = create_keystore(wallet_root / "wallet.json", "transfer-test-pass")
        (wallet_root / "password").write_text("transfer-test-pass")
        (wallet_root / "password").chmod(0o600)
        monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
        monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(wallet_root / "wallet.json"))
        monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", str(wallet_root / "password"))
        monkeypatch.setenv("NOYRA_WALLET_RPC_URLS_JSON", '{"11155111":"https://rpc.example"}')
        policy = store.update_policy(
            SUBJECT_ID,
            policy.revision,
            {"wallet_mode": "local_wallet_transfer", "local_wallet_transfer_enabled": True},
            "operator",
        )
        wallet_binding = {"mode": "local_wallet_transfer", "address": address}
    registry = TargetRegistry(source, store)
    target = registry.register(
        SUBJECT_ID,
        target_id=TARGET_ID,
        public_key=public,
        recipient_public_key=recipient_public,
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge(TARGET_ID, source_epoch="runtime-0")
    registry.attest(
        TARGET_ID,
        challenge,
        base64.urlsafe_b64encode(signing.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    manager = MigrationManager(source, store)
    proposal = manager.create_proposal(
        subject_id=SUBJECT_ID,
        target_id=TARGET_ID,
        policy_revision=policy.revision,
        reason_code="maintenance",
        reason="move to trusted resource",
        expires_at="2099-01-01T00:00:00+00:00",
    )
    task = manager.approve(proposal.proposal_id, actor="operator", idempotency_key="full-flow")
    if local_wallet:
        wallet_binding["approval"] = {
            "task_id": task.task_id,
            "address": address,
            "approval_id": "approval-local",
            "channel_id": "recipient-channel",
            "expires_at": "2099-01-01T00:00:00+00:00",
        }
    target_root = tmp_path / "target"
    target_db = Database(target_root / "noyra.sqlite3")
    IdentityStore(target_db).ensure("Noyra-standby", "b" * 64)
    LifecycleManager(target_db, EventStore(target_db), "Noyra-standby").ensure_initial()
    systemd = _Systemd()
    activator = TargetRuntimeActivator(target_root, tmp_path / "etc", systemd=systemd)
    initial_environment = {
        "NOYRA_SUBJECT_ID": "Noyra-standby",
        "NOYRA_GENESIS_HASH": "b" * 64,
        "NOYRA_MIGRATION_TARGET_ID": "obsolete-target",
    }
    boots: list[dict[str, Any]] = []

    def boot_ready(subject: str, target: str | None) -> bool:
        boot = _boot_runtime_from_dropin(activator, initial_environment)
        boots.append(boot)
        return bool(boot["subject_id"] == subject and boot["target_id"] == target)

    systemd.ready = boot_ready
    root = activator.state_root.parent
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"target_id": TARGET_ID, "public_key": public}))
    identity.chmod(0o600)
    bridge = TargetActivationBridge(
        root / "requests", root / "status", signing, timeout_seconds=60, poll_seconds=0.01
    )
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda *a, **kw: VolumeEncryptionStatus(True, "test", "fixture", "volume"),
    )
    agent = MigrationAgent(
        target_id=TARGET_ID,
        key_fingerprint=target.key_fingerprint,
        signing_key=signing,
        recipient_private_key=recipient,
        data_root=activator.agent_root,
        restore_root=activator.agent_root / "restored",
        activation_controller=bridge,
    )
    cli = _module()

    class Transport:
        def request(self, url: str, body: dict[str, Any], token: str) -> dict[str, Any]:
            operation = url.rsplit("/", 1)[-1]
            if operation == "receive-chunk":
                operation = "receive"
            if operation != "activate":
                return dict(cli.dispatch(agent, operation, dict(body)))
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(cli.dispatch, agent, operation, dict(body))
                deadline = time.monotonic() + 10
                while not list((root / "requests").glob("*.json")) and not future.done():
                    assert time.monotonic() < deadline
                    time.sleep(0.01)
                run_target_activation_requests(
                    root=root,
                    identity_file=identity,
                    data_root=target_root,
                    config_root=activator.config_root,
                    systemd=systemd,
                )
                return dict(future.result(timeout=10))

    admission = RuntimeAdmissionGate(SUBJECT_ID)

    def source_fence(current: Any, epoch: str) -> str:
        admission.assert_migration_fenced()
        with source.connection() as connection:
            row = connection.execute(
                "SELECT e.* FROM migration_epochs e JOIN migration_tasks t "
                "ON t.target_epoch_id=e.epoch_id WHERE t.task_id=?",
                (current.task_id,),
            ).fetchone()
        return content_hash(
            {
                "task_id": current.task_id,
                "source_epoch": epoch,
                "epoch_id": row["epoch_id"],
                "epoch_number": row["epoch_number"],
                "status": row["status"],
            }
        )

    executor = HTTPMigrationExecutor(
        target_resolver=lambda t: target.__dict__,
        token_resolver=lambda t: "t" * 32,
        artifact_resolver=SQLiteArtifactProvider(source.path, source.path.parent / "outgoing"),
        source_fence=source_fence,
        source_unfence=lambda *a: None,
        transport=Transport(),
    )
    coordinator = CutoverCoordinator(source, admission=admission, executor=executor)
    result = coordinator.run(
        task.task_id,
        binding={
            "credential_binding": {"references": {}, "fingerprints": {}},
            "wallet_binding": wallet_binding,
        },
    )
    if local_wallet:
        from noyra.migration.wallet_material import verify_local_wallet

        verify_local_wallet(target_root / "secrets" / f"migration-{task.task_id}", address)
        assert "EnvironmentFile=" in (activator.dropin_dir / "migration-target.conf").read_text()
        assert (wallet_root / "wallet.json").is_file()  # source retained and fenced
    assert result["status"] == "committed"
    assert boots == [{"subject_id": SUBJECT_ID, "genesis_hash": "a" * 64, "target_id": TARGET_ID}]
    assert not admission.accepting
    assert (target_root / "workspace" / "work.txt").read_text() == "retained"
    assert (
        MigrationManager(Database(target_root / "noyra.sqlite3", initialize=False), store)
        .get_task(task.task_id)
        .status
        == "committed"
    )


def test_controller_recovery_state_is_outside_the_agent_writable_root(tmp_path: Path) -> None:
    data_root = tmp_path / "var-lib-noyra"
    activator = TargetRuntimeActivator(data_root, tmp_path / "etc-noyra", systemd=_Systemd())

    assert activator.rollback_root.is_relative_to(data_root / "migration-agent") is False
    assert activator.activation_root.is_relative_to(data_root / "migration-agent") is False


def test_secure_root_directory_rejects_untrusted_owner_or_writable_mode(
    tmp_path: Path,
) -> None:
    if os.name == "nt":
        pytest.skip("POSIX ownership and mode checks are not available on Windows")
    path = tmp_path / "root-only-state"
    path.mkdir(mode=0o700)
    if path.stat().st_uid == 0:
        path.chmod(0o777)

    with pytest.raises(TargetActivationError, match="activation_state_directory_invalid"):
        _REAL_SECURE_ROOT_DIRECTORY(path, "activation_state_directory_invalid")


def test_target_activation_reverts_the_previous_runtime_after_readiness_failure(
    tmp_path: Path,
) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path,
        ready=lambda subject, target: target is None,
    )

    with pytest.raises(TargetActivationError, match="target_runtime_readiness_failed"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.active is True
    assert systemd.calls[-2:] == ["start", f"ready:{SUBJECT_ID}:None"]


def test_activation_rejects_wrong_subject_before_touching_current_runtime(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    request = _request(task_id, fence_digest) | {"subject_id": "Noyra-9999"}

    with pytest.raises(TargetActivationError, match="restored_database_identity_invalid"):
        activator.activate(request)

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


@pytest.mark.parametrize("reject_new_runtime", [False, True])
def test_runtime_identity_survives_real_boot_and_restores_previous_dropin(
    tmp_path: Path, reject_new_runtime: bool
) -> None:
    task_id, fence, active, _, systemd, activator = _runtime_fixture(tmp_path)
    active.unlink()
    standby = Database(active)
    IdentityStore(standby).ensure("Noyra-standby", "b" * 64)
    LifecycleManager(standby, EventStore(standby), "Noyra-standby").ensure_initial()
    activator.dropin_dir.mkdir(parents=True)
    previous_env = tmp_path / "previous.env"
    previous_env.write_text(
        "NOYRA_SUBJECT_ID=Noyra-standby\nNOYRA_GENESIS_HASH="
        + "b" * 64
        + "\nNOYRA_MIGRATION_TARGET_ID=prior-target\n"
    )
    activator._write_target_dropin("prior-target", previous_env)
    dropin = activator.dropin_dir / "migration-target.conf"
    original_dropin = dropin.read_bytes()
    initial_environment = {"NOYRA_SUBJECT_ID": "Noyra-unused", "NOYRA_GENESIS_HASH": "c" * 64}
    boots: list[dict[str, Any]] = []

    def boot_ready(subject: str, target: str | None) -> bool:
        boot = _boot_runtime_from_dropin(activator, initial_environment)
        boots.append(boot)
        assert boot["subject_id"] == subject and boot["target_id"] == target
        return not (reject_new_runtime and target == TARGET_ID)

    systemd.ready = boot_ready
    request = _request(task_id, fence)
    if reject_new_runtime:
        with pytest.raises(TargetActivationError, match="target_runtime_readiness_failed"):
            activator.activate(request)
    else:
        activator.activate(request)
        cancel = {
            key: request[key] for key in ("task_id", "target_id", "source_epoch", "manifest_digest")
        }
        assert activator.deactivate(cancel)["status"] == "deactivated"
    assert boots == [
        {
            "subject_id": SUBJECT_ID,
            "genesis_hash": content_hash({"subject": SUBJECT_ID}),
            "target_id": TARGET_ID,
        },
        {"subject_id": "Noyra-standby", "genesis_hash": "b" * 64, "target_id": "prior-target"},
    ]
    assert dropin.read_bytes() == original_dropin
    runtime_env = activator.state_root / "runtime-environment" / f"{task_id}.env"
    assert runtime_env.is_file()
    if os.name == "posix":
        assert stat.S_IMODE(runtime_env.stat().st_mode) == 0o600


def test_invalid_genesis_cannot_be_injected_into_runtime_environment(tmp_path: Path) -> None:
    task_id, fence, active, digest, systemd, activator = _runtime_fixture(tmp_path)
    restored = activator.agent_root / "restored" / task_id / "noyra.sqlite3"
    with sqlite3.connect(restored) as connection:
        connection.execute(
            "UPDATE subject_identity SET genesis_hash=?", ("a" * 64 + "\nNOYRA_PROFILE=test",)
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    request = _request(task_id, fence) | {
        "restored_database_sha256": hashlib.sha256(restored.read_bytes()).hexdigest()
    }
    with pytest.raises(TargetActivationError, match="restored_database_identity_invalid"):
        activator.activate(request)
    assert systemd.calls == []
    assert hashlib.sha256(active.read_bytes()).hexdigest() == digest


def test_activation_rejects_mismatched_source_fence_before_switch(tmp_path: Path) -> None:
    task_id, _, active_database, old_digest, systemd, activator = _runtime_fixture(tmp_path)

    with pytest.raises(TargetActivationError, match="restored_migration_epoch_invalid"):
        activator.activate(_request(task_id, "f" * 64))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_controller_revalidates_the_root_owned_staged_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    wrong_path = tmp_path / "wrong.sqlite3"
    wrong_database = Database(wrong_path)
    wrong_subject = "Noyra-9999"
    IdentityStore(wrong_database).ensure(wrong_subject, content_hash({"subject": wrong_subject}))
    LifecycleManager(wrong_database, EventStore(wrong_database), wrong_subject).ensure_initial()

    def substitute_copy(source: Path, destination: Path) -> None:
        shutil.copyfile(wrong_path, destination)

    monkeypatch.setattr("noyra.migration.activation._copy_private", substitute_copy)

    with pytest.raises(TargetActivationError, match="restored_artifact_digest_mismatch"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_activation_reverts_previous_runtime_after_service_start_failure(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, _, activator = _runtime_fixture(tmp_path)
    systemd = _Systemd(start_error=True)
    activator.systemd = systemd

    with pytest.raises(TargetActivationError, match="service_start_failed"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.active is True
    assert systemd.calls[-3:] == ["reload", "start", f"ready:{SUBJECT_ID}:None"]


def test_duplicate_activation_returns_same_receipt_without_restarting(tmp_path: Path) -> None:
    task_id, fence_digest, _, _, systemd, activator = _runtime_fixture(tmp_path)

    first = activator.activate(_request(task_id, fence_digest))
    calls = list(systemd.calls)
    second = activator.activate(_request(task_id, fence_digest))

    assert second == first
    assert systemd.calls == calls


def test_cancellation_before_activation_persists_across_restart_and_rejects_late_request(
    tmp_path: Path,
) -> None:
    task_id, fence_digest, _, _, systemd, activator = _runtime_fixture(tmp_path)
    request = _request(task_id, fence_digest)
    cancel = {
        key: request[key] for key in ("task_id", "target_id", "source_epoch", "manifest_digest")
    }
    result = activator.deactivate(cancel)
    assert result["activation_revoked"] is True
    restarted = TargetRuntimeActivator(activator.data_root, activator.config_root, systemd=systemd)
    assert restarted.deactivate(cancel) == result
    with pytest.raises(TargetActivationError, match="conflicts"):
        restarted.activate(request)
    assert systemd.calls == []


def test_live_activation_lock_prevents_recovery_from_undoing_intentional_restart(
    tmp_path: Path,
) -> None:
    _, _, _, _, _, activator = _runtime_fixture(tmp_path)
    lock = activator._control_lock()
    try:
        assert activator.recover_incomplete() == 0
    finally:
        lock.release()


def test_deactivation_is_idempotent_and_restores_previous_database(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    activator.activate(_request(task_id, fence_digest))
    deactivate_request = {
        "task_id": task_id,
        "target_id": TARGET_ID,
        "source_epoch": "runtime-0",
        "manifest_digest": MANIFEST_DIGEST,
    }

    first = activator.deactivate(deactivate_request)
    calls = list(systemd.calls)
    second = activator.deactivate(deactivate_request)

    assert first == {**deactivate_request, "status": "deactivated", "activation_revoked": True}
    assert second == first
    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == calls


def test_deactivation_allows_database_changes_since_activation(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(task_id, fence_digest))
    connection = sqlite3.connect(active_database)
    connection.execute("PRAGMA user_version=1")
    connection.close()

    result = activator.deactivate(
        {
            "task_id": task_id,
            "target_id": TARGET_ID,
            "source_epoch": "runtime-0",
            "manifest_digest": MANIFEST_DIGEST,
        }
    )

    assert result["status"] == "deactivated" and result["activation_revoked"] is True
    assert result["task_id"] == task_id


def test_deactivation_readiness_failure_leaves_durable_recovery_state(tmp_path: Path) -> None:
    task_id, fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(task_id, fence_digest))
    activator.systemd = _Systemd(ready=lambda subject, target: target == TARGET_ID)

    with pytest.raises(TargetActivationError, match="rollback_runtime_readiness_failed"):
        activator.deactivate(
            {
                "task_id": task_id,
                "target_id": TARGET_ID,
                "source_epoch": "runtime-0",
                "manifest_digest": MANIFEST_DIGEST,
            }
        )

    marker = activator.activation_root / f"{task_id}.json"
    import json

    assert json.loads(marker.read_text())["status"] == "deactivating"


def test_recovery_handles_crash_after_activating_journal_before_database_backup(
    tmp_path: Path,
) -> None:
    task_id, _, active_database, previous_digest, systemd, activator = _runtime_fixture(tmp_path)
    activator._ensure_dirs()
    marker = activator.activation_root / f"{task_id}.json"
    rollback = activator.rollback_root / task_id
    rollback.mkdir()
    import json

    marker.write_text(
        json.dumps(
            {
                "task_id": task_id,
                "status": "activating",
                "previous_database_sha256": previous_digest,
                "manifest": MANIFEST,
                "manifest_digest": MANIFEST_DIGEST,
                "previous_subject_id": SUBJECT_ID,
                "previous_dropin_present": False,
            }
        )
    )

    assert activator.recover_incomplete() == 1

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == previous_digest
    assert json.loads(marker.read_text())["status"] == "recovered"
    assert systemd.active is True


def test_stale_deactivation_cannot_restore_previous_database_after_new_activation(
    tmp_path: Path,
) -> None:
    task_id, fence_digest, active_database, _, systemd, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(task_id, fence_digest))
    old_target_digest = hashlib.sha256(active_database.read_bytes()).hexdigest()

    next_task_id, next_fence_digest = _prepared_target_database(tmp_path / "next-target.sqlite3")
    next_restored = activator.agent_root / "restored" / next_task_id / "noyra.sqlite3"
    next_restored.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / "next-target.sqlite3", next_restored)
    _ARTIFACT_HASHES[next_task_id] = hashlib.sha256(next_restored.read_bytes()).hexdigest()
    activator.activate(_request(next_task_id, next_fence_digest))
    current_digest = hashlib.sha256(active_database.read_bytes()).hexdigest()
    calls = list(systemd.calls)

    with pytest.raises(TargetActivationError, match="active_runtime_ownership_mismatch"):
        activator.deactivate(
            {
                "task_id": task_id,
                "target_id": TARGET_ID,
                "source_epoch": "runtime-0",
                "manifest_digest": MANIFEST_DIGEST,
            }
        )

    assert current_digest != old_target_digest
    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == current_digest
    assert systemd.calls == calls


def test_deactivation_of_later_activation_restores_previous_runtime_owner(tmp_path: Path) -> None:
    first_task_id, first_fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(first_task_id, first_fence_digest))
    current_path = activator.state_root / "current.json"
    import json

    first_owner = json.loads(current_path.read_text())

    second_task_id, second_fence_digest = _prepared_target_database(
        tmp_path / "second-target.sqlite3"
    )
    second_restored = activator.agent_root / "restored" / second_task_id / "noyra.sqlite3"
    second_restored.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / "second-target.sqlite3", second_restored)
    _ARTIFACT_HASHES[second_task_id] = hashlib.sha256(second_restored.read_bytes()).hexdigest()
    activator.activate(_request(second_task_id, second_fence_digest))

    result = activator.deactivate(
        {
            "task_id": second_task_id,
            "target_id": TARGET_ID,
            "source_epoch": "runtime-0",
            "manifest_digest": MANIFEST_DIGEST,
        }
    )

    assert result["status"] == "deactivated" and result["activation_revoked"] is True
    assert result["task_id"] == second_task_id
    assert json.loads(current_path.read_text()) == first_owner


def test_deactivation_recovery_restores_previous_runtime_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_task_id, first_fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(first_task_id, first_fence_digest))
    current_path = activator.state_root / "current.json"
    import json

    first_owner = json.loads(current_path.read_text())

    second_task_id, second_fence_digest = _prepared_target_database(
        tmp_path / "second-target.sqlite3"
    )
    second_restored = activator.agent_root / "restored" / second_task_id / "noyra.sqlite3"
    second_restored.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / "second-target.sqlite3", second_restored)
    _ARTIFACT_HASHES[second_task_id] = hashlib.sha256(second_restored.read_bytes()).hexdigest()
    activator.activate(_request(second_task_id, second_fence_digest))

    from noyra.migration import activation as activation_module

    atomic_json = activation_module._atomic_json
    second_marker = activator.activation_root / f"{second_task_id}.json"

    def crash_after_runtime_restore(path: Path, value: Any, mode: int, **kwargs: Any) -> None:
        if path == second_marker and value.get("status") == "deactivated":
            raise SystemExit("simulated crash before deactivation receipt")
        atomic_json(path, value, mode, **kwargs)

    monkeypatch.setattr(activation_module, "_atomic_json", crash_after_runtime_restore)

    with pytest.raises(SystemExit, match="simulated crash"):
        activator.deactivate(
            {
                "task_id": second_task_id,
                "target_id": TARGET_ID,
                "source_epoch": "runtime-0",
                "manifest_digest": MANIFEST_DIGEST,
            }
        )

    assert activator.recover_incomplete() == 1
    assert json.loads(current_path.read_text()) == first_owner


def test_failed_later_activation_restores_previous_runtime_ownership_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_task_id, first_fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(first_task_id, first_fence_digest))
    current_path = activator.state_root / "current.json"
    import json

    first_owner = json.loads(current_path.read_text())

    second_task_id, second_fence_digest = _prepared_target_database(
        tmp_path / "second-target.sqlite3"
    )
    second_restored = activator.agent_root / "restored" / second_task_id / "noyra.sqlite3"
    second_restored.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / "second-target.sqlite3", second_restored)
    _ARTIFACT_HASHES[second_task_id] = hashlib.sha256(second_restored.read_bytes()).hexdigest()

    from noyra.migration import activation as activation_module

    atomic_json = activation_module._atomic_json
    second_marker = activator.activation_root / f"{second_task_id}.json"

    def fail_final_activation_marker(path: Path, value: Any, mode: int, **kwargs: Any) -> None:
        if path == second_marker and value.get("status") == "active":
            raise OSError("simulated final activation journal failure")
        atomic_json(path, value, mode, **kwargs)

    monkeypatch.setattr(activation_module, "_atomic_json", fail_final_activation_marker)

    with pytest.raises(TargetActivationError, match="target_activation_failed"):
        activator.activate(_request(second_task_id, second_fence_digest))

    assert json.loads(current_path.read_text()) == first_owner


def test_activation_recovery_restores_previous_runtime_ownership_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first_task_id, first_fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    activator.activate(_request(first_task_id, first_fence_digest))
    current_path = activator.state_root / "current.json"
    import json

    first_owner = json.loads(current_path.read_text())

    second_task_id, second_fence_digest = _prepared_target_database(
        tmp_path / "second-target.sqlite3"
    )
    second_restored = activator.agent_root / "restored" / second_task_id / "noyra.sqlite3"
    second_restored.parent.mkdir(parents=True)
    shutil.copyfile(tmp_path / "second-target.sqlite3", second_restored)
    _ARTIFACT_HASHES[second_task_id] = hashlib.sha256(second_restored.read_bytes()).hexdigest()

    from noyra.migration import activation as activation_module

    atomic_json = activation_module._atomic_json
    second_marker = activator.activation_root / f"{second_task_id}.json"

    def crash_after_current_owner_switch(path: Path, value: Any, mode: int, **kwargs: Any) -> None:
        if path == second_marker and value.get("status") == "active":
            raise SystemExit("simulated process crash after owner switch")
        atomic_json(path, value, mode, **kwargs)

    monkeypatch.setattr(activation_module, "_atomic_json", crash_after_current_owner_switch)

    with pytest.raises(SystemExit, match="simulated process crash"):
        activator.activate(_request(second_task_id, second_fence_digest))

    assert activator.recover_incomplete() == 1
    assert json.loads(current_path.read_text()) == first_owner


def test_activation_rejects_artifact_id_mismatch_in_restored_migration_task(
    tmp_path: Path,
) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    restored = activator.agent_root / "restored" / task_id / "noyra.sqlite3"
    with sqlite3.connect(restored) as connection:
        connection.execute(
            "UPDATE migration_tasks SET artifact_id=? WHERE task_id=?",
            ("artifact-other", task_id),
        )
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    with pytest.raises(TargetActivationError, match="restored_migration_epoch_invalid"):
        activator.activate(_request(task_id, fence_digest))

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_activation_keeps_a_root_staged_snapshot_when_agent_copy_changes_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task_id, fence_digest, _, _, _, activator = _runtime_fixture(tmp_path)
    restored = activator.agent_root / "restored" / task_id / "noyra.sqlite3"
    original_copy = __import__("noyra.migration.activation", fromlist=["_copy_private"])
    copy_private = original_copy._copy_private

    def mutate_source_after_copy(source: Path, destination: Path) -> None:
        copy_private(source, destination)
        with source.open("ab") as stream:
            stream.write(b"agent-side-race")

    monkeypatch.setattr("noyra.migration.activation._copy_private", mutate_source_after_copy)

    receipt = activator.activate(_request(task_id, fence_digest))

    marker = activator.activation_root / f"{task_id}.json"
    import json

    state = json.loads(marker.read_text())
    assert state["staged_database_sha256"] == state["root_staged_database_sha256"]
    assert state["root_staged_database_sha256"] != hashlib.sha256(restored.read_bytes()).hexdigest()
    assert (
        receipt["active_database_sha256"]
        == hashlib.sha256((activator.data_root / "noyra.sqlite3").read_bytes()).hexdigest()
    )
    assert restored.read_bytes().endswith(b"agent-side-race")


def test_root_activation_rejects_manifest_artifact_digest_mismatch(tmp_path: Path) -> None:
    task_id, fence_digest, active_database, old_digest, systemd, activator = _runtime_fixture(
        tmp_path
    )
    request = _request(task_id, fence_digest)
    request["restored_database_sha256"] = "c" * 64

    with pytest.raises(TargetActivationError, match="restored_artifact_digest_mismatch"):
        activator.activate(request)

    assert hashlib.sha256(active_database.read_bytes()).hexdigest() == old_digest
    assert systemd.calls == []


def test_root_activation_runner_rejects_unsigned_request_and_persists_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra.migration.activation import _atomic_json

    root = tmp_path / "target-activation"
    requests = root / "requests"
    requests.mkdir(parents=True)
    statuses = root / "status"
    statuses.mkdir()
    identity_file = tmp_path / "identity.json"
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    identity_file.write_text(
        '{"target_id":"target-1","public_key":"' + base64.urlsafe_b64encode(public).decode() + '"}'
    )
    task_id = "migrationtask_unsigned"
    _atomic_json(
        requests / f"{task_id}.activate.json",
        {
            "payload": _request(task_id, "c" * 64),
            "signature": "invalid",
            "request_digest": content_hash(_request(task_id, "c" * 64)),
        },
        0o600,
    )

    if os.name != "nt":
        group_calls: list[tuple[str, int, int]] = []
        monkeypatch.setitem(
            sys.modules,
            "grp",
            SimpleNamespace(getgrnam=lambda name: SimpleNamespace(gr_gid=4242)),
        )
        monkeypatch.setattr(
            os,
            "chown",
            lambda path, uid, gid: group_calls.append((str(path), uid, gid)),
        )

    assert (
        run_target_activation_requests(
            root=root,
            identity_file=identity_file,
            data_root=tmp_path / "var-lib-noyra",
            config_root=tmp_path / "etc-noyra",
            systemd=_Systemd(),
        )
        == 1
    )
    status = statuses / f"{task_id}.activate.json"
    assert status.is_file()
    import json

    assert json.loads(status.read_text())["error_code"] == "activation_request_signature_invalid"
    assert status.is_file(), "root runner result must remain available for audit"
    if os.name != "nt":
        assert group_calls == [(str(status), 0, 4242)]
        assert stat.S_IMODE(status.stat().st_mode) == 0o640


def test_activation_systemd_units_use_fixed_entrypoint_and_recovery_precedes_service() -> None:
    from pathlib import Path

    deploy = Path(__file__).resolve().parents[1] / "deploy" / "systemd"
    runner = (deploy / "noyra-target-activation.service").read_text(encoding="utf-8")
    path = (deploy / "noyra-target-activation.path").read_text(encoding="utf-8")
    recovery = (deploy / "noyra-target-activation-recover.service").read_text(encoding="utf-8")
    service = (deploy / "noyra.service").read_text(encoding="utf-8")
    installer = (deploy.parents[1] / "scripts" / "install-ubuntu.sh").read_text(encoding="utf-8")

    assert "User=root" in runner
    assert "noyra-target-activation-runner.py" in runner
    assert "ExecStart=" in runner and "systemctl" not in runner
    assert "PathChanged=/var/lib/noyra/migration/target-activation/requests" in path
    assert "noyra-target-activation-recover.service" in service
    assert "noyra-target-activation-runner.py" in recovery
    assert "--recover" in recovery
    assert "noyra-target-activation.path" in installer
    assert "enable --now noyra-target-activation.path" in installer
    assert 'noyra_control_layout_prepare_activation_state "$DATA_DIR"' in installer
    control_layout = (deploy.parents[1] / "scripts/lib/control-layout.sh").read_text(
        encoding="utf-8"
    )
    assert 'install -d -o root -g root -m 0700 "$path"' in control_layout


def test_real_agent_bridge_root_and_source_receipt_share_activation_contract(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import json
    import time
    from concurrent.futures import ThreadPoolExecutor

    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

    from noyra.core.at_rest import VolumeEncryptionStatus
    from noyra.migration.activation import TargetActivationBridge
    from noyra.migration.agent import MigrationAgent
    from noyra.migration.http_executor import HTTPMigrationExecutor

    task_id, fence, _, _, systemd, activator = _runtime_fixture(tmp_path)
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    identity = tmp_path / "identity.json"
    identity.write_text(json.dumps({"target_id": TARGET_ID, "public_key": public}))
    root = activator.state_root.parent
    bridge = TargetActivationBridge(
        root / "requests", root / "status", private, timeout_seconds=15, poll_seconds=0.01
    )
    monkeypatch.setattr(
        "noyra.core.at_rest.VolumeEncryptionProbe.probe",
        lambda *args, **kwargs: VolumeEncryptionStatus(True, "test", "fixture", "volume"),
    )
    agent = MigrationAgent(
        target_id=TARGET_ID,
        key_fingerprint=hashlib.sha256(private.public_key().public_bytes_raw()).hexdigest(),
        signing_key=private,
        recipient_private_key=X25519PrivateKey.generate(),
        data_root=activator.agent_root,
        activation_controller=bridge,
    )
    request = _request(task_id, fence)
    request["recipient_key_fingerprint"] = agent.recipient_key_fingerprint
    binding = agent.binding_proof(
        {
            **{
                key: request[key]
                for key in (
                    "task_id",
                    "subject_id",
                    "target_id",
                    "source_epoch",
                    "manifest_digest",
                    "artifact_id",
                    "recipient_key_fingerprint",
                )
            },
            "credential_binding": {"references": {}, "fingerprints": {}},
            "wallet_binding": {"mode": "disabled"},
        }
    )
    request["target_volume_proof_digest"] = content_hash(
        {
            "task_id": task_id,
            "manifest_digest": MANIFEST_DIGEST,
            "proof": binding["target_volume_proof"],
        }
    )
    request["credential_binding_digest"] = content_hash(
        {
            "task_id": task_id,
            "manifest_digest": MANIFEST_DIGEST,
            "binding": binding["credential_binding"],
        }
    )
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(agent.activate, request)
        deadline = time.monotonic() + 10
        while not list((root / "requests").glob("*.json")) and not pending.done():
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert (
            run_target_activation_requests(
                root=root,
                identity_file=identity,
                data_root=activator.data_root,
                config_root=activator.config_root,
                systemd=systemd,
            )
            == 1
        )
        receipt = pending.result(timeout=10)
    assert (
        HTTPMigrationExecutor._verify_activation_receipt({"public_key": public}, request, receipt)
        == receipt
    )
    assert receipt["restored_database_sha256"] != receipt["artifact_sha256"]
    assert receipt["active_database_sha256"] != receipt["restored_database_sha256"]
    # A valid target signature cannot excuse mismatched binding fields.
    wrong = {**receipt, "credential_binding_digest": "0" * 64}
    with pytest.raises(Exception, match="receipt_invalid"):
        HTTPMigrationExecutor._verify_activation_receipt({"public_key": public}, request, wrong)
