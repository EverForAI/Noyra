from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from noyra.core import SubjectKernel
from noyra.core.admission import OperationInvalidated
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService
from noyra.service import NoyraService
from noyra.sleep import SleepEngine, SleepIntegrity
from test_authentication_input_contract import call
from test_c2_sleep_recovery import at_phase, snapshot
from test_c2_sleep_recovery import kernel as kernel
from test_m42_p3_06_operator_controls import service as service


def test_operator_abort_is_targeted_audited_idempotent_and_resumable(kernel: SubjectKernel) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    controls = OperatorControlService(kernel)
    directory = controls.sleep_recovery()
    assert directory["run"]["sleep_id"] == identifier
    version = directory["run"]["expected_version"]
    with pytest.raises(OperatorControlConflict, match="snapshot_conflict"):
        controls.abort_sleep(identifier, version + 1, actor="operator", reason="Bad snapshot")
    result = controls.abort_sleep(
        identifier, version, actor="operator", reason="Private explanation"
    )
    assert result["status"] == "failed" and not result["idempotent"]
    assert result["lifecycle"] == "paused" and not kernel.admission.accepting
    again = controls.abort_sleep(identifier, version, actor="operator", reason="Duplicate request")
    assert again["idempotent"]
    with kernel.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records WHERE action='operator_sleep_abort'"
            ).fetchone()[0]
            == 1
        )
    controls.resume(actor="operator", reason="Fault resolved")
    assert kernel.lifecycle.current().state == "active"
    assert engine.current() is None
    SleepIntegrity(kernel.database).verify(kernel.subject_id)


def test_operator_abort_and_private_audit_share_one_transaction(kernel: SubjectKernel) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    controls = OperatorControlService(kernel)
    before = snapshot(kernel)
    with (
        patch.object(controls, "_audit_connection", side_effect=OSError("audit fault")),
        pytest.raises(OSError, match="audit fault"),
    ):
        controls.abort_sleep(
            identifier, engine.get(identifier).version, actor="operator", reason="Abort"
        )
    assert snapshot(kernel) == before and kernel.admission.accepting


@pytest.mark.parametrize("fence", ["inflight", "quarantine", "ownership", "migration"])
def test_operator_abort_rejects_fenced_or_undrained_work(kernel: SubjectKernel, fence: str) -> None:
    engine, identifier = at_phase(kernel, "reflective_sleep")
    controls = OperatorControlService(kernel)
    version = engine.get(identifier).version
    lease = None
    if fence == "inflight":
        lease = kernel.admission.begin("sleep_tick", sleep_work=True)
    elif fence == "quarantine":
        kernel.admission.quarantine()
    elif fence == "ownership":
        kernel.process_lock.release()
    else:
        with kernel.admission.migration_control_scope():
            kernel.admission.fence_for_migration(timeout=0)
    before = snapshot(kernel)
    try:
        with pytest.raises((OperatorControlConflict, OperationInvalidated, RuntimeOwnershipError)):
            controls.abort_sleep(identifier, version, actor="operator", reason="Abort")
        assert snapshot(kernel) == before
    finally:
        if lease is not None:
            kernel.admission.finish(lease)


def test_sleep_recovery_http_requires_operator_csrf_and_exact_snapshot(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, operator_token, read_token = service
    engine = SleepEngine(instance.kernel.database, instance.kernel.subject_id)
    run = engine.start("subject_choice", "Synthetic HTTP sleep")
    engine.begin_reflection(run.sleep_id)
    path = "/api/v1/admin/sleep-recovery"
    headers = {"Authorization": "Bearer " + operator_token}
    payload: dict[str, Any] = {
        "sleep_id": run.sleep_id,
        "expected_version": engine.get(run.sleep_id).version,
        "reason": "Private maintenance explanation",
    }
    for auth in ({}, {"Authorization": "Bearer " + read_token}):
        assert call(instance.http, path, headers=auth)[0] == 401
        assert call(instance.http, path + "/abort", headers=auth, payload=payload)[0] == 401
    status, directory, _ = call(instance.http, path, headers=headers)
    assert status == 200 and directory["run"]["sleep_id"] == run.sleep_id
    assert "Synthetic HTTP sleep" not in str(directory)
    assert (
        call(
            instance.http,
            path + "/abort",
            headers=headers,
            payload={**payload, "expected_version": True},
        )[0]
        == 400
    )
    assert (
        call(
            instance.http,
            path + "/abort",
            headers=headers,
            payload={**payload, "expected_version": 999},
        )[0]
        == 409
    )
    _, login, cookie = call(instance.http, "/admin/session", payload={"token": operator_token})
    session = {"Cookie": cookie.split(";", 1)[0]}
    assert call(instance.http, path + "/abort", headers=session, payload=payload)[0] == 401
    session["X-CSRF-Token"] = login["csrf_token"]
    status, result, _ = call(instance.http, path + "/abort", headers=session, payload=payload)
    assert status == 200 and result["lifecycle"] == "paused"
    assert call(instance.http, path + "/abort", headers=session, payload=payload)[1]["idempotent"]
    assert instance.kernel.lifecycle.current().state == "paused"
