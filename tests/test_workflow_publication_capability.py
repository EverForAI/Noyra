from __future__ import annotations

import base64
import hashlib
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.core.workflow_export_files import (
    WorkflowArtifactStore,
    WorkflowPublicationUnsupportedError,
)
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from noyra.service import NoyraService
from test_authentication_input_contract import call
from test_d2b_workflow_export_control import control as control
from test_d2b_workflow_export_control import run
from test_i2_workflow_increment_control import context as context
from test_i2_workflow_increment_control import publish
from test_m42_p3_06_operator_controls import _kernel
from test_m42_p3_06_operator_controls import service as service


def snapshot(control: OperatorControlService) -> tuple[Any, Any]:
    with control.database.read_transaction() as c:
        counts = {
            row[0]: c.execute('SELECT COUNT(*) FROM "' + row[0] + '"').fetchone()[0]
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        history = content_hash([dict(r) for r in c.execute("SELECT * FROM workflow_checkpoints")])
    cold = control.database.path.parent / "subject/cold"
    files = {
        p.relative_to(cold).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in cold.rglob("*")
        if p.is_file()
    }
    return (counts, history), files


def test_native_creation_capability_does_not_claim_windows_directory_durability() -> None:
    assert WorkflowArtifactStore.creation_supported() is (os.name == "posix")
    capability = WorkflowArtifactStore.creation_capability()
    assert capability["supported"] is (os.name == "posix")
    assert capability["history_read_available"] is True


@pytest.mark.skipif(os.name != "nt", reason="native Windows workflow creation admission")
def test_native_windows_rejects_creation_without_allocating_intent_or_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(32)).decode())
    kernel = _kernel(tmp_path, "native-publication-capability")
    try:
        kernel.pause("Synthetic native capability check")
        control = OperatorControlService(kernel)
        before = snapshot(control)
        with pytest.raises(WorkflowPublicationUnsupportedError):
            control.export_workflow(
                "no-new-workflow",
                1,
                "native-request-key",
                actor="operator",
                reason="Synthetic capability check",
            )
        assert snapshot(control) == before
        assert not (tmp_path / "subject/cold").exists()
    finally:
        kernel.close()


def test_unsupported_base_refuses_before_intent_key_history_or_storage_mutation(
    control: OperatorControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = snapshot(control)
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    with pytest.raises(WorkflowPublicationUnsupportedError):
        run(control)
    assert snapshot(control) == before
    assert control.workflow_exports()["creation_capability"]["supported"] is False
    assert control.kernel.admission.active_operations == 0


def test_unsupported_increment_refuses_before_new_intent_or_storage_mutation(
    context: tuple[OperatorControlService, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    control, _ = context
    before = snapshot(control)
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    with pytest.raises(WorkflowPublicationUnsupportedError):
        publish(context)
    assert snapshot(control) == before
    assert control.workflow_increments()["creation_capability"]["supported"] is False
    assert control.kernel.admission.active_operations == 0


def test_legacy_prepared_retry_is_retained_but_does_not_start_new_file_writes(
    control: OperatorControlService, monkeypatch: pytest.MonkeyPatch
) -> None:
    with (
        patch.object(WorkflowArtifactStore, "put", side_effect=OSError("synthetic interruption")),
        pytest.raises(OSError),
    ):
        run(control)
    before = snapshot(control)
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    with pytest.raises(WorkflowPublicationUnsupportedError):
        run(control)
    assert snapshot(control) == before
    assert control.workflow_exports()["items"][0]["status"] == "prepared"


@pytest.mark.parametrize("accepted", [False, True])
def test_legacy_published_retries_remain_readable_idempotent_and_do_not_promote(
    control: OperatorControlService, monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    with patch.object(WorkflowArtifactStore, "flush", return_value=accepted):
        first = run(control)
    before = snapshot(control)
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    with (
        patch.object(WorkflowArtifactStore, "flush", return_value=False),
        patch.object(WorkflowArtifactStore, "put", side_effect=AssertionError("file rewrite")),
    ):
        again = run(control)
    assert again["idempotent"] and again["publication_id"] == first["publication_id"]
    assert again["status"] == first["status"]
    assert snapshot(control) == before
    assert control.workflow_exports()["items"][0]["status"] == first["status"]


def test_http_advertises_unsupported_before_preparation_and_keeps_auth_boundary(
    service: tuple[NoyraService, str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    instance, token, readonly = service
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", "")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(32)).decode())
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    control = instance.http.operator_controls
    assert control is not None
    control.pause(actor="operator", reason="Synthetic capability check")
    before = snapshot(control)
    headers = {"Authorization": "Bearer " + token}
    for kind in ("workflow-exports", "workflow-increments"):
        path = "/api/v1/admin/" + kind
        payload = {
            "workflow_id": "not-created",
            "expected_version": 2,
            "request_key": "unsupported-key",
            "reason": "Synthetic check",
        }
        if kind == "workflow-increments":
            payload["parent_id"] = "wexport_" + "a" * 64
        for auth in ({}, {"Authorization": "Bearer " + readonly}):
            assert call(instance.http, path, headers=auth, payload=payload)[0] == 401
        status, result, _ = call(instance.http, path, headers=headers, payload=payload)
        assert status == 503 and result["error"] == "workflow_publication_unsupported"
        assert (
            result["creation_capability"]["reason_code"]
            == "platform_directory_durability_unavailable"
        )
        assert (
            call(instance.http, path, headers=headers)[1]["creation_capability"]["supported"]
            is False
        )
    assert snapshot(control) == before


@pytest.mark.parametrize("accepted", [False, True])
def test_increment_published_retry_remains_readable_without_creation_capability(
    context: tuple[OperatorControlService, str], monkeypatch: pytest.MonkeyPatch, accepted: bool
) -> None:
    control, _ = context
    with patch.object(WorkflowIncrementStore, "flush", return_value=accepted):
        first = publish(context)
    before = snapshot(control)
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    with (
        patch.object(WorkflowIncrementStore, "flush", return_value=False),
        patch.object(WorkflowIncrementStore, "put", side_effect=AssertionError("file rewrite")),
    ):
        again = publish(context)
    assert again["idempotent"] and again["status"] == first["status"]
    assert again["publication_id"] == first["publication_id"]
    assert snapshot(control) == before


def test_increment_prepared_retry_cannot_write_on_unsupported_platform(
    context: tuple[OperatorControlService, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    control, _ = context
    with (
        patch.object(WorkflowIncrementStore, "put", side_effect=OSError("synthetic interruption")),
        pytest.raises(OSError),
    ):
        publish(context)
    before = snapshot(control)
    monkeypatch.setattr(WorkflowArtifactStore, "creation_supported", staticmethod(lambda: False))
    with pytest.raises(WorkflowPublicationUnsupportedError):
        publish(context)
    assert snapshot(control) == before
    assert control.workflow_increments()["items"][0]["status"] == "prepared"
