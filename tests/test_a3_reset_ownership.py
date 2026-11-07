from __future__ import annotations

import base64
import json
import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import SecretStr

from noyra.core.admission import OperationInvalidated
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.locking import ProcessLock
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
from noyra.core.runtime import SubjectKernel, _migration_epoch_guard
from noyra.core.types import content_hash
from noyra.migration.fencing import EpochLease
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry
from noyra.service import NoyraHTTPServer, ServiceSettings


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    value = SubjectKernel(tmp_path / "noyra.sqlite3", "a3-reset", content_hash("a3-reset"))
    value.boot()
    value.orient()
    value.activate()
    OperatorControlService(value).pause(actor="operator", reason="Inspect before reset")
    try:
        yield value
    finally:
        value.close()


def acquire_epoch(kernel: SubjectKernel) -> EpochLease:
    private = Ed25519PrivateKey.generate()
    TargetRegistry(kernel.database, MigrationStore(kernel.database)).register(
        kernel.subject_id,
        target_id="a3-target",
        public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(),
        endpoint="https://target.example",
        capabilities={},
        region=None,
        provider=None,
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="test",
    )
    return EpochLease._acquire_unchecked(
        kernel.database, kernel.subject_id, "a3-target", expected_source_epoch=None, actor="test"
    )


def snapshot(kernel: SubjectKernel) -> dict[str, list[dict[str, Any]]]:
    with kernel.database.read_transaction() as connection:
        return {
            table: [
                dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
            ]
            for table in ("runtime_state", "events", "audit_records", "autonomy_loop_state")
        }


@pytest.mark.parametrize("epoch_state", ["active", "completed"])
def test_reset_never_changes_subject_while_another_migration_epoch_owns_it(
    kernel: SubjectKernel, epoch_state: str
) -> None:
    epoch = acquire_epoch(kernel)
    if epoch_state == "completed":
        epoch.complete("test")
    before = snapshot(kernel)
    with pytest.raises((OperatorControlConflict, RuntimeOwnershipError)):
        OperatorControlService(kernel).reset(actor="operator", reason="Must remain fenced")
    assert snapshot(kernel) == before
    assert not kernel.admission.accepting


def test_reset_never_writes_after_runtime_process_lock_is_released(kernel: SubjectKernel) -> None:
    kernel.process_lock.release()
    before = snapshot(kernel)
    with pytest.raises((OperatorControlConflict, RuntimeOwnershipError)):
        OperatorControlService(kernel).reset(actor="operator", reason="Stale operator request")
    assert snapshot(kernel) == before
    assert not kernel.admission.accepting


@pytest.mark.parametrize("restriction", ["migration", "quarantine", "draining", "in_flight"])
def test_reset_respects_existing_gate_boundaries_without_any_durable_changes(
    kernel: SubjectKernel, restriction: str
) -> None:
    lease = None
    if restriction == "migration":
        with kernel.admission.migration_control_scope():
            kernel.admission.fence_for_migration(timeout=0)
    elif restriction == "quarantine":
        kernel.admission.quarantine()
    elif restriction == "draining":
        kernel.admission.begin_drain()
    else:
        lease = kernel.admission.begin("preexisting-operation", allow_quarantine=True)
    before = snapshot(kernel)
    try:
        with pytest.raises(OperatorControlConflict):
            OperatorControlService(kernel).reset(actor="operator", reason="Still unavailable")
        assert snapshot(kernel) == before
        assert not kernel.admission.accepting
    finally:
        if lease is not None:
            kernel.admission.finish(lease)


@pytest.mark.parametrize("malformed", [False, True])
def test_reset_checks_filesystem_migration_fence_in_its_write_transaction(
    kernel: SubjectKernel, tmp_path: Path, malformed: bool
) -> None:
    fence_root = tmp_path / "migration"
    folder = fence_root / "fences"
    folder.mkdir(parents=True)
    marker = folder / "a3-task.json"
    marker.write_text(
        "invalid"
        if malformed
        else json.dumps({"subject_id": kernel.subject_id, "status": "active"}),
        encoding="utf8",
    )
    kernel.lifecycle.mutation_guard = lambda c: _migration_epoch_guard(
        c, kernel.subject_id, fence_root
    )
    before = snapshot(kernel)
    with pytest.raises(OperatorControlConflict, match="reset_runtime_unavailable"):
        OperatorControlService(kernel).reset(actor="operator", reason="File fence remains")
    assert snapshot(kernel) == before
    assert not kernel.admission.accepting


def test_reset_rechecks_durable_epoch_after_health_preflight(kernel: SubjectKernel) -> None:
    controls = OperatorControlService(kernel)
    with (
        patch.object(controls, "_require_resume_health", side_effect=lambda: acquire_epoch(kernel)),
        pytest.raises(OperatorControlConflict, match="reset_runtime_unavailable"),
    ):
        controls.reset(actor="operator", reason="Epoch acquired after health check")
    assert kernel.lifecycle.current().state == "paused"
    with kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='operator_reset'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM migration_epochs WHERE status='active'"
            ).fetchone()[0]
            == 1
        )


def test_revoked_epoch_allows_normal_reset_and_new_operations(kernel: SubjectKernel) -> None:
    epoch = acquire_epoch(kernel)
    epoch.revoke("Verified rollback", "operator")
    result = OperatorControlService(kernel).reset(actor="operator", reason="Recovery complete")
    assert result.lifecycle["state"] == "active" and kernel.admission.accepting
    with kernel.admission.operation("new-healthy-work"):
        pass


def test_matching_target_ownership_keeps_its_existing_migration_exception(
    kernel: SubjectKernel,
) -> None:
    acquire_epoch(kernel)
    kernel._migration_target_id = "a3-target"
    assert (
        OperatorControlService(kernel)
        .reset(actor="operator", reason="Target owns this epoch")
        .lifecycle["state"]
        == "active"
    )
    with kernel.admission.operation("target-owned-work"):
        pass


def test_mutation_guard_runs_inside_same_transaction_before_any_transition(
    kernel: SubjectKernel,
) -> None:
    calls = []
    guard = kernel.lifecycle.mutation_guard
    assert guard is not None

    def observe(connection: Any) -> None:
        assert connection.in_transaction
        assert (
            connection.execute(
                "SELECT state FROM runtime_state WHERE subject_id=?", (kernel.subject_id,)
            ).fetchone()[0]
            == "paused"
        )
        calls.append(True)
        guard(connection)

    kernel.lifecycle.mutation_guard = observe
    result = OperatorControlService(kernel).reset(actor="operator", reason="Verify writer guard")
    assert calls == [True] and result.lifecycle["state"] == "active"


@pytest.mark.parametrize("edge", ["stopped", "booting", "active", "audit"])
def test_reset_failure_rolls_back_all_lifecycle_edges_and_audits(
    kernel: SubjectKernel, edge: str
) -> None:
    controls = OperatorControlService(kernel)
    before = snapshot(kernel)
    transition = kernel.lifecycle._transition_connection

    def fault(connection: Any, state: str, reason: str, *, actor: str) -> Any:
        result = transition(connection, state, reason, actor=actor)
        if state == edge:
            raise RuntimeError("synthetic reset transition fault")
        return result

    target = (
        patch.object(
            controls, "_audit_connection", side_effect=RuntimeError("synthetic reset audit fault")
        )
        if edge == "audit"
        else patch.object(kernel.lifecycle, "_transition_connection", side_effect=fault)
    )
    with target, pytest.raises(OperatorControlConflict, match="reset_failed"):
        controls.reset(actor="operator", reason="Must be atomic")
    assert snapshot(kernel) == before
    assert not kernel.admission.accepting
    assert (
        controls.reset(actor="operator", reason="Retry after local fault").lifecycle["state"]
        == "active"
    )


def test_reset_pins_process_ownership_until_commit_and_gate_reopen(kernel: SubjectKernel) -> None:
    requested, released = threading.Event(), threading.Event()
    guard = kernel.lifecycle.mutation_guard
    assert guard is not None
    worker = None

    def release() -> None:
        requested.set()
        kernel.process_lock.release()
        released.set()

    def checked(connection: Any) -> None:
        nonlocal worker
        guard(connection)
        worker = threading.Thread(target=release)
        worker.start()
        assert requested.wait(5)
        assert not released.wait(0.05)

    kernel.lifecycle.mutation_guard = checked
    result = OperatorControlService(kernel).reset(actor="operator", reason="Pin writer ownership")
    assert result.lifecycle["state"] == "active"
    assert worker is not None
    worker.join(timeout=5)
    assert released.is_set() and not kernel.process_lock.held
    with pytest.raises(RuntimeOwnershipError):
        kernel.admission.begin("stale-owner-after-release")


def test_owned_scope_releases_mutex_after_error_and_never_acquires_an_unowned_lock(
    tmp_path: Path,
) -> None:
    lock = ProcessLock(tmp_path / "ownership.lock")
    with pytest.raises(RuntimeOwnershipError), lock.ownership_scope():
        pytest.fail("unowned scope entered")
    assert not lock.held
    lock.acquire()
    with pytest.raises(ValueError), lock.ownership_scope():
        raise ValueError("synthetic scope failure")
    lock.release()
    assert not lock.held
    assert lock.acquire()
    lock.release()


def test_concurrent_resets_produce_one_atomic_reset(kernel: SubjectKernel) -> None:
    controls = OperatorControlService(kernel)

    def reset(_: int) -> str:
        try:
            return str(controls.reset(actor="operator", reason="One reset").lifecycle["state"])
        except OperatorControlConflict as error:
            return error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reset, range(2)))
    assert sorted(results) == ["active", "reset_requires_paused"]
    assert kernel.lifecycle.current().state == "active"
    with kernel.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='operator_reset'"
            ).fetchone()[0]
            == 1
        )


def test_reset_invalidates_pre_pause_lease_even_after_success(kernel: SubjectKernel) -> None:
    kernel.admission.open()
    lease = kernel.admission.begin("pre-pause-work")
    kernel.admission.finish(lease)
    kernel.admission.invalidate()
    OperatorControlService(kernel).reset(actor="operator", reason="Old operations stay stale")
    with pytest.raises(OperationInvalidated):
        lease.assert_current()


@pytest.mark.parametrize("restriction", ["migration", "released"])
def test_http_reset_preserves_authentication_and_refuses_stale_ownership(
    kernel: SubjectKernel, restriction: str
) -> None:
    owner = NoyraHTTPServer(
        kernel,
        ServiceSettings(
            data_dir=kernel.database.path.parent,
            subject_id=kernel.subject_id,
            genesis_hash=kernel.identity.genesis_hash
            if kernel.identity is not None
            else content_hash("a3-reset"),
            host="127.0.0.1",
            port=0,
            operator_token=SecretStr("synthetic-a3-operator-" + "o" * 32),
            read_token=SecretStr("synthetic-a3-reader-" + "r" * 32),
        ),
    )
    owner.operator_controls = OperatorControlService(kernel)
    owner.start()
    try:
        token = owner.settings.operator_token
        reader = owner.settings.read_token
        assert token is not None and reader is not None

        def request(secret: str) -> tuple[int, dict[str, Any]]:
            req = Request(
                f"http://127.0.0.1:{owner.address[1]}/api/v1/admin/lifecycle/reset",
                data=json.dumps({"reason": "Only the current owner may reset"}).encode(),
                headers={"Content-Type": "application/json", "Authorization": "Bearer " + secret},
            )
            try:
                with urlopen(req, timeout=10) as response:
                    return response.status, json.loads(response.read())
            except HTTPError as error:
                return error.code, json.loads(error.read())

        before = snapshot(kernel)
        assert request(reader.get_secret_value())[0] == 401
        assert snapshot(kernel) == before
        if restriction == "migration":
            acquire_epoch(kernel)
        else:
            kernel.process_lock.release()
        before = snapshot(kernel)
        status, body = request(token.get_secret_value())
        assert status == 409 and body == {"error": "reset_runtime_unavailable"}
        assert snapshot(kernel) == before
        assert not kernel.admission.accepting
    finally:
        owner.close()
