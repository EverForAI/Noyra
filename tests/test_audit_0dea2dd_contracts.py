from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core import SubjectKernel
from noyra.core import integrity as contract
from noyra.core.integrity import IntegrityRegistry
from noyra.core.types import canonical_json, new_id
from noyra.core.upgrade import UpgradeError
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure
from noyra.research.types import SearchProviderInput
from noyra.service import NoyraHTTPServer
from test_audit_6a742e5_integrity import assert_clean_and_active
from test_audit_6a742e5_integrity import kernel as base_kernel
from test_participant_http import Client
from test_participant_http import http as base_http


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    yield from cast(Any, base_kernel).__wrapped__(tmp_path)


@pytest.fixture
def http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    yield from cast(Any, base_http).__wrapped__(tmp_path)


def test_real_management_writers_remain_valid_after_later_changes(
    http: tuple[NoyraHTTPServer, str],
) -> None:
    owner, base = http
    client = Client(base)
    headers = {"Authorization": "Bearer admin-test-key-with-enough-entropy"}
    assert_clean_and_active(owner.kernel)
    # Six anonymous failed attempts exercise the default real limiter.
    for _ in range(5):
        assert client.request("/admin/session", {"token": "wrong"})[0] == 401
    assert client.request("/admin/session", {"token": "wrong"})[0] == 429
    assert_clean_and_active(owner.kernel)
    assert (
        client.request(
            "/api/admin/retention/run", {"reason": "local audit", "batch_size": 1}, **headers
        )[0]
        == 200
    )
    settings = owner.retention.policy(owner.kernel.subject_id)["settings"]
    for revision in range(2):
        assert (
            client.request(
                "/api/admin/retention/settings",
                {
                    "settings": settings,
                    "expected_revision": revision,
                    "backup": owner.retention.backup_policy.public(),
                },
                **headers,
            )[0]
            == 200
        )
    provider = owner.search_providers.configure(
        owner.kernel.subject_id,
        SearchProviderInput(provider_type="brave", label="Fixture", api_key="synthetic"),
        actor="operator",
    )
    for weight in (2, 3):
        assert (
            client.request(
                f"/api/config/search-providers/{provider.config_id}/routing",
                {"priority": 90, "weight": weight, "reason": "local update"},
                **headers,
            )[0]
            == 200
        )
    # No runner starts: exercise the route with bounded local manager outcomes.
    payload = {"reason": "local fixture", "idempotency_key": "audit-upgrade-123"}
    with patch.object(
        owner.upgrade_manager, "start", side_effect=UpgradeError("upgrade_unavailable")
    ):
        assert client.request("/api/admin/upgrade", payload, **headers)[0] == 503
    with patch.object(
        owner.upgrade_manager,
        "start",
        return_value={
            "task_id": "a" * 32,
            "target_sha": "b" * 40,
        },
    ):
        assert client.request("/api/admin/upgrade", payload, **headers)[0] == 202
    owner.search_providers.revoke(
        provider.config_id,
        reason="later revocation",
        actor="operator",
        subject_id=owner.kernel.subject_id,
    )
    assert_clean_and_active(owner.kernel)
    with owner.kernel.database.connection() as c:
        before = [tuple(row) for row in c.execute("SELECT * FROM audit_records")]
    legacy_policy = {
        "settings": {key: value for key, value in settings.items() if key != "runtime_days"},
        "revision": 1,
        "source": "saved",
        "updated_at": before[-1][5],
    }
    owner.audit_admin_event("retention_policy_updated", "web-operator", legacy_policy)
    owner.audit_admin_event(
        "retention_batch_run",
        "web-operator",
        {
            "reason": "historical run",
            "deleted_by_table": {"model_calls": 0},
            "protected_rows": 0,
        },
    )
    # New canonical encodings are accepted too; exact legacy bytes are not rewritten.
    for row in before:
        if row[2] in contract._OPERATION_MANAGEMENT_AUDIT_FIELDS:
            with owner.kernel.database.transaction() as c:
                c.execute(
                    "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                    (
                        new_id("audit"),
                        row[1],
                        row[2],
                        row[3],
                        canonical_json(json.loads(row[4])),
                        row[5],
                    ),
                )
    assert_clean_and_active(owner.kernel)
    with owner.kernel.database.connection() as c:
        assert [tuple(row) for row in c.execute("SELECT * FROM audit_records")][
            : len(before)
        ] == before


def test_legacy_privacy_recovery_audit_remains_valid(kernel: SubjectKernel) -> None:
    window = ConversationStore(kernel.database).create(kernel.subject_id, "legacy window")
    InteractionStore(kernel.database).receive(
        kernel.subject_id,
        "web",
        "web-user",
        "synthetic private text",
        conversation_id=window["conversation_id"],
    )
    erasure = ConversationErasure(kernel.database)
    erasure._write_marker(kernel.subject_id, window["conversation_id"], new_id("erase"))
    assert [job["status"] for job in erasure.resume(kernel.subject_id)] == ["completed"]
    assert_clean_and_active(kernel)
    assert erasure.resume(kernel.subject_id) == []
    assert_clean_and_active(kernel)


@pytest.mark.parametrize(
    ("action", "payload", "actor"),
    [
        ("admin_login_rate_limited", {"path": "/wrong"}, "web-anonymous"),
        ("admin_login_rate_limited", {"path": "/admin/session"}, "subject"),
        ("admin_login_rate_limited", {"path": "/admin/session", "extra": 1}, "web-anonymous"),
        (
            "retention_batch_run",
            {"reason": "audit", "deleted_by_table": {"goals": 1}, "protected_rows": 0},
            "web-operator",
        ),
        (
            "retention_batch_run",
            {"reason": "audit", "deleted_by_table": {"retention_runs": True}, "protected_rows": 0},
            "web-operator",
        ),
        (
            "retention_batch_run",
            {"reason": "audit", "deleted_by_table": {"retention_runs": 0}, "protected_rows": -1},
            "web-operator",
        ),
        (
            "admin_upgrade_started",
            {"task_id": "a" * 32, "target_short_sha": "b" * 12, "reason": "audit"},
            "subject",
        ),
        (
            "admin_upgrade_started",
            {"task_id": "../target", "target_short_sha": "b" * 12, "reason": "audit"},
            "web-operator",
        ),
        ("admin_upgrade_rejected", {"error_code": "arbitrary_secret"}, "web-operator"),
        ("private_erasure_requested", {"job_id": "missing"}, "operator"),
        ("unknown_management_event", {}, "web-operator"),
    ],
)
def test_new_audit_contracts_reject_invalid_evidence(
    kernel: SubjectKernel,
    action: str,
    payload: dict[str, Any],
    actor: str,
) -> None:
    from noyra.core.types import utc_now

    with kernel.database.transaction() as c:
        c.execute(
            "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
            (new_id("audit"), kernel.subject_id, action, actor, canonical_json(payload), utc_now()),
        )
    report = IntegrityRegistry().run(
        kernel.database,
        kernel.subject_id,
        kernel.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("core.actions",),
    )
    assert report.status == "corrupt"


def test_all_literal_service_audit_writers_have_registered_contracts() -> None:
    # An independent call-site inventory prevents a new writer silently
    # escaping the validator. Dynamic internal wrappers remain explicitly reviewed.
    root = Path(__file__).parents[1] / "src/noyra"
    allowed = (
        contract._CORE_AUDIT_ACTIONS
        | contract._ADMIN_AUDIT_ACTIONS
        | contract._OPERATOR_AUDIT_ACTIONS
        | contract._WALLET_AUDIT_REFERENCES.keys()
        | {"wallet_payment_policy_updated"}
    )
    missing = []
    dynamic_wrappers = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_bytes())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            if node.func.attr == "audit_admin_event" and node.args:
                value = node.args[0]
                if (
                    isinstance(value, ast.Constant)
                    and isinstance(value.value, str)
                    and value.value not in allowed
                ):
                    missing.append((str(path), node.lineno, value.value))
            if node.func.attr != "execute" or len(node.args) < 2:
                continue
            try:
                sql = ast.literal_eval(node.args[0])
            except (ValueError, TypeError):
                continue
            if not isinstance(sql, str) or "INSERT INTO audit_records" not in sql:
                continue
            values = re.findall(r"'([^']*)'|(\?)", sql.partition("VALUES")[2])
            if len(values) != 6:
                raise AssertionError((str(path), node.lineno, "unrecognized audit SQL"))
            action = values[2][0]
            if not action:
                params = node.args[1]
                if isinstance(params, (ast.Tuple, ast.List)) and len(params.elts) == 6:
                    expression = params.elts[2]
                    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
                        action = expression.value
                if not action:
                    dynamic_wrappers.add(path.relative_to(root).as_posix())
                    continue
            if action not in allowed:
                missing.append((str(path), node.lineno, action))
    assert not missing, missing
    # Every remaining dynamic writer is an explicitly reviewed domain wrapper.
    assert dynamic_wrappers <= {
        "service.py",
        "core/operator_controls.py",
        "wallet/economy.py",
        "wallet/store.py",
    }, dynamic_wrappers
