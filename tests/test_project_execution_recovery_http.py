from __future__ import annotations

import json
from collections.abc import Iterator
from unittest.mock import Mock

import pytest
from pydantic import SecretStr

import test_autonomous_projects as helpers
from noyra.cognition import ProjectExecutionLedger
from noyra.core.at_rest import AtRestError
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings
from test_a0_project_recovery import case as case
from test_a0_project_recovery import count
from test_authentication_input_contract import TOKENS, call
from test_project_execution_recovery import interrupted

PATH = "/api/admin/project-execution-recovery"


@pytest.fixture
def http(
    case: helpers.AutonomousProjectsTestCase, monkeypatch: pytest.MonkeyPatch
) -> Iterator[NoyraHTTPServer]:
    settings = ServiceSettings(
        data_dir=case.kernel.database.path.parent,
        subject_id=case.subject_id,
        genesis_hash=content_hash({"seed": "autonomous-project-test"}),
        host="127.0.0.1",
        port=0,
        operator_token=SecretStr(TOKENS["operator"]),
        read_token=SecretStr(TOKENS["read"]),
    )
    server = NoyraHTTPServer(case.kernel, settings)
    server.operator_controls = OperatorControlService(case.kernel)
    errors = Mock(wraps=server.server.handle_error)
    monkeypatch.setattr(server.server, "handle_error", errors)
    server.start()
    try:
        yield server
        errors.assert_not_called()
    finally:
        server.close()


def authorized(role: str = "operator") -> dict[str, str]:
    return {"Authorization": "Bearer " + TOKENS[role]}


@pytest.mark.asyncio
async def test_http_release_is_operator_only_snapshot_bound_and_never_replays(
    case: helpers.AutonomousProjectsTestCase, http: NoyraHTTPServer
) -> None:
    _, identifier = await interrupted(case)
    before = ProjectExecutionLedger(case.kernel.database, case.subject_id).get(identifier)
    for headers in ({}, authorized("read")):
        assert call(http, PATH, headers=headers)[0] == 401
        assert call(http, PATH + "/release-review", payload={}, headers=headers)[0] == 401
    status, catalog, _ = call(http, PATH, headers=authorized())
    assert status == 200 and not catalog["records"][0]["can_release"]
    item = catalog["records"][0]
    payload = {
        "execution_id": identifier,
        "expected_fingerprint": item["expected_fingerprint"],
        "reason": "private-operator-reason-not-for-prompt-or-catalog",
    }
    assert call(http, PATH + "/release-review", payload=payload, headers=authorized())[0] == 409
    assert http.operator_controls is not None
    http.operator_controls.pause(actor="operator", reason="Inspect")
    assert call(http, PATH, headers=authorized())[1]["records"][0]["can_release"]
    assert (
        call(
            http,
            PATH + "/release-review",
            payload={**payload, "actor": "subject"},
            headers=authorized(),
        )[0]
        == 400
    )
    assert (
        call(
            http,
            PATH + "/release-review",
            payload={**payload, "expected_fingerprint": "0" * 64},
            headers=authorized(),
        )[0]
        == 409
    )
    model_count = count(case, "model_calls")
    for idempotent in (False, True):
        status, result, _ = call(
            http,
            PATH.replace("/api/", "/api/v1/") + "/release-review",
            payload=payload,
            headers=authorized(),
        )
        assert status == 200 and result["idempotent"] is idempotent
        assert result["review_released"]
    assert count(case, "model_calls") == model_count
    assert ProjectExecutionLedger(case.kernel.database, case.subject_id).get(identifier) == before
    status, catalog, _ = call(http, PATH, headers=authorized())
    assert status == 200 and catalog["records"][0]["review_released"]
    assert not catalog["records"][0]["can_release"]
    assert payload["reason"] not in json.dumps(catalog)
    with case.kernel.database.connection() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM audit_records "
                "WHERE action='project_execution_review_released'"
            ).fetchone()[0]
            == 1
        )
    assert case.kernel.lifecycle.current().state == "paused"


@pytest.mark.asyncio
async def test_http_recovery_cookie_requires_csrf_and_drained_runtime(
    case: helpers.AutonomousProjectsTestCase, http: NoyraHTTPServer
) -> None:
    _, identifier = await interrupted(case)
    _, catalog, _ = call(http, PATH, headers=authorized())
    payload = {
        "execution_id": identifier,
        "expected_fingerprint": catalog["records"][0]["expected_fingerprint"],
        "reason": "Verified local interruption",
    }
    _, login, cookie = call(http, "/admin/session", payload={"token": TOKENS["operator"]})
    headers = {"Cookie": cookie.split(";", 1)[0]}
    assert http.operator_controls is not None
    lease = case.kernel.admission.begin("synthetic-drain")
    http.operator_controls.pause(actor="operator", reason="Drain")
    assert call(http, PATH + "/release-review", payload=payload, headers=headers)[0] == 401
    headers["X-CSRF-Token"] = login["csrf_token"]
    assert call(http, PATH + "/release-review", payload=payload, headers=headers)[:2] == (
        409,
        {"error": "project_recovery_operations_not_drained"},
    )
    case.kernel.admission.finish(lease)
    assert call(http, PATH + "/release-review", payload=payload, headers=headers)[0] == 200


@pytest.mark.parametrize("boundary", ["quarantine", "at_rest", "ownership", "migration"])
def test_http_recovery_does_not_bypass_runtime_boundaries(
    case: helpers.AutonomousProjectsTestCase,
    http: NoyraHTTPServer,
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    assert http.operator_controls is not None
    http.operator_controls.pause(actor="operator", reason="Synthetic boundary check")
    if boundary == "quarantine":
        case.kernel.admission.quarantine()
    elif boundary == "at_rest":
        http.at_rest = Mock(required=True)
        http.at_rest.require_request_ready = Mock(side_effect=AtRestError("locked"))
    elif boundary == "ownership":
        monkeypatch.setattr(
            case.kernel.admission, "require_owned", Mock(side_effect=RuntimeOwnershipError("lost"))
        )
    else:
        with case.kernel.admission.migration_control_scope():
            case.kernel.admission.fence_for_migration(timeout=0)
    status, result, _ = call(
        http,
        PATH + "/release-review",
        headers=authorized(),
        payload={
            "execution_id": "not-a-real-execution",
            "expected_fingerprint": "a" * 64,
            "reason": "Must not pass boundary",
        },
    )
    assert status == (409 if boundary == "migration" else 503), result
    with case.kernel.database.connection() as c:
        assert (
            c.execute(
                "SELECT 1 FROM events WHERE event_type='project_execution_review_released'"
            ).fetchone()
            is None
        )
