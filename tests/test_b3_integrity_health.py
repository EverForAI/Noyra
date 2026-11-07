from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.integrity import IntegrityRuntimeController
from noyra.service import NoyraHTTPServer, ServiceSettings
from test_b3_integrity_evidence import kernel as kernel
from test_b3_integrity_evidence import registry


def test_health_projection_preserves_checker_and_watchdog_states_without_disk_reads() -> None:
    from noyra.http_routes.health import _integrity_health

    owner = SimpleNamespace(
        integrity=SimpleNamespace(
            summary=lambda: {
                "status": "evidence_unavailable",
                "policy_mode": "alert",
                "evidence": {"reason_code": "digest_missing"},
                "coverage": {"status": "partial"},
            }
        ),
        integrity_watchdog_health=lambda: {
            "status": "escalated",
            "consecutive_failures": 4,
            "escalated": True,
            "persisted": True,
        },
    )
    integrity, watchdog = _integrity_health(cast(NoyraHTTPServer, owner))
    assert integrity["status"] == "evidence_unavailable"
    assert integrity["evidence"]["reason_code"] == "digest_missing"
    assert integrity["coverage"]["status"] == "partial"
    assert watchdog["status"] == "escalated"
    assert watchdog["consecutive_failures"] == 4


def test_health_projection_reports_unavailable_watchdog_without_treating_it_as_clean() -> None:
    from noyra.http_routes.health import _integrity_health

    owner = SimpleNamespace(
        integrity=SimpleNamespace(summary=lambda: {"status": "ok", "policy_mode": "alert"}),
        integrity_watchdog_health=lambda: (_ for _ in ()).throw(OSError("disk")),
    )
    _, watchdog = _integrity_health(cast(NoyraHTTPServer, owner))
    assert watchdog == {"status": "evidence_unavailable", "persisted": False}


@pytest.fixture
def server(kernel: SubjectKernel, tmp_path: Path) -> Iterator[NoyraHTTPServer]:
    assert kernel.identity is not None
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id=kernel.subject_id,
        genesis_hash=kernel.identity.genesis_hash,
        host="127.0.0.1",
        port=0,
        health_cache_ttl_seconds=60,
        admin_token=SecretStr("b3-health-admin-token-with-enough-entropy"),
    )
    owner = NoyraHTTPServer(kernel, settings)
    owner.integrity = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    owner.integrity.run_startup()
    owner.integrity_watchdog_health = lambda: {
        "status": "ok",
        "consecutive_failures": 0,
        "persisted": True,
    }
    owner.start()
    try:
        yield owner
    finally:
        owner.close()


def get(owner: NoyraHTTPServer, path: str, *, auth: bool = True) -> tuple[int, dict[str, Any]]:
    request = Request(
        f"http://127.0.0.1:{owner.address[1]}" + path,
        headers={
            "Authorization": "Bearer b3-health-admin-token-with-enough-entropy" if auth else ""
        },
    )
    try:
        response = urlopen(request, timeout=10)
    except HTTPError as error:
        response = error
    with response:
        payload = json.loads(response.read())
        assert isinstance(payload, dict)
        return response.status, payload


@pytest.mark.parametrize("status", ["backoff", "escalated", "evidence_unavailable"])
def test_watchdog_fault_invalidates_cached_green_health_and_readiness(
    server: NoyraHTTPServer, status: str
) -> None:
    assert get(server, "/health")[0] == 200
    server.integrity_watchdog_health = lambda: {
        "status": status,
        "consecutive_failures": 3,
        "escalated": status == "escalated",
        "persisted": False,
    }
    code, payload = get(server, "/health")
    assert code == 503 and payload["components"]["integrity_watchdog"]["status"] == status
    assert get(server, "/health/ready", auth=False)[0] == 503
    detailed = get(server, "/api/v1/admin/readiness")[1]
    assert detailed["execution_admission"]["runtime_gate_accepting"]
    assert get(server, "/health/live", auth=False)[0] == 200
    assert server.kernel.admission.accepting  # Health reporting does not change alert admission.
    server.integrity_watchdog_health = lambda: {
        "status": "ok",
        "consecutive_failures": 0,
        "persisted": True,
    }
    assert get(server, "/health")[0] == 200


def test_no_integrity_run_is_visible_as_not_checked_while_off_remains_available(
    server: NoyraHTTPServer,
) -> None:
    assert server.integrity is not None
    fresh = IntegrityRuntimeController(
        server.kernel, server.settings.data_dir / "fresh", registry=registry()
    )
    server.integrity = fresh
    code, payload = get(server, "/api/v1/admin/readiness")
    assert code == 503 and payload["integrity"]["status"] == "not_checked"
    fresh.policy_mode = "off"
    assert get(server, "/health/ready", auth=False)[0] == 200
    assert get(server, "/api/v1/admin/readiness", auth=False)[0] == 401


def test_public_health_does_not_expose_integrity_report_or_failure_details(
    server: NoyraHTTPServer,
) -> None:
    server.integrity_watchdog_health = lambda: {
        "status": "escalated",
        "consecutive_failures": 4,
        "last_error_type": "SyntheticPrivateError",
        "escalated": True,
    }
    code, payload = get(server, "/health", auth=False)
    assert code == 503 and payload == {"status": "degraded", "service": "noyra"}


@pytest.mark.parametrize("damage", [False, True])
def test_sealed_core_diagnostic_never_rewrites_evidence_or_subject_state(
    kernel: SubjectKernel,
    tmp_path: Path,
    damage: bool,
) -> None:
    from contextlib import closing

    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    controller.run_startup()
    snapshot = tmp_path / "diagnostic.sqlite3"
    with kernel.database.connection() as source, closing(sqlite3.connect(snapshot)) as destination:
        source.backup(destination)
        destination.execute("PRAGMA journal_mode=DELETE")
        if damage:
            destination.execute("DROP TRIGGER prevent_training_record_delete")
        destination.commit()
    before = {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    }
    program = Path(__file__).resolve().parents[1] / "scripts" / "diagnose-integrity-evidence.py"
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            str(program),
            str(controller._directory),
            "--snapshot",
            str(snapshot),
            "--subject",
            kernel.subject_id,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=20,
    )
    assert result.returncode == (1 if damage else 0), result.stderr
    payload = json.loads(result.stdout)
    assert payload["snapshot_recheck"]["status"] == ("corrupt" if damage else "ok")
    assert payload["snapshot_recheck"]["runtime_state_changed"] is False
    assert {
        str(path.relative_to(tmp_path)): path.read_bytes()
        for path in tmp_path.rglob("*")
        if path.is_file() and not path.name.endswith(".lock")
    } == before
    assert kernel.admission.accepting


def test_pause_policy_cannot_resume_before_evidence_reconstruction_finishes(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    from noyra.core.operator_controls import OperatorControlConflict, OperatorControlService

    controller = IntegrityRuntimeController(
        kernel, tmp_path, policy_mode="pause", registry=registry()
    )
    controller.run_startup()
    controller._state_path.write_text("broken", encoding="utf-8")
    reopened = IntegrityRuntimeController(
        kernel, tmp_path, policy_mode="pause", registry=registry()
    )
    kernel.pause("Diagnostic pending")
    controls = OperatorControlService(kernel, integrity=reopened)
    with pytest.raises(OperatorControlConflict, match="integrity_health_unavailable"):
        controls.resume(actor="operator", reason="Try resume before check")
    assert kernel.lifecycle.current().state == "paused" and not kernel.admission.accepting
    reopened.run_periodic_if_due(force=True)
    assert not reopened.summary()["evidence"]["recheck_pending"]
    assert (
        controls.resume(actor="operator", reason="Completed check").lifecycle["state"] == "active"
    )


def test_disabled_integrity_does_not_degrade_operator_health_when_no_checks_were_run(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    from noyra.core.operator_controls import OperatorControlService

    controller = IntegrityRuntimeController(
        kernel, tmp_path, policy_mode="off", registry=registry()
    )
    controls = OperatorControlService(kernel, integrity=controller)
    component = controls.health()["integrity"]
    assert component["state"] == "disabled" and component["status"] == "not_checked"
