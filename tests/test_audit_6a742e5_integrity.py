from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from noyra.core import IdentityStore, SubjectKernel
from noyra.core.integrity import IntegrityRegistry, IntegrityReport, IntegrityRuntimeController
from noyra.core.types import canonical_json, content_hash, new_id, utc_now
from noyra.interaction import InteractionStore, TransportInput
from noyra.interaction.accounts import AccountStore
from noyra.service import NoyraHTTPServer
from test_audit_20261024_remediation import make_collaboration
from test_identity_challenges import verified_route
from test_participant_http import Client
from test_participant_http import http as base_http


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    owner = SubjectKernel(tmp_path / "noyra.sqlite3", "audit-event-contract", content_hash("audit"))
    owner.boot()
    owner.orient()
    owner.activate()
    try:
        yield owner
    finally:
        owner.close()


@pytest.fixture
def http(tmp_path: Path) -> Iterator[tuple[NoyraHTTPServer, str]]:
    yield from cast(Any, base_http).__wrapped__(tmp_path)


def report(owner: SubjectKernel) -> IntegrityReport:
    return IntegrityRegistry().run(
        owner.database,
        owner.subject_id,
        owner.database.path.parent,
        profile="manual",
        policy_mode="pause",
        deadline_seconds=15,
        check_ids=("core.actions", "model.ledger"),
    )


def assert_clean_and_active(owner: SubjectKernel) -> None:
    current = report(owner)
    assert current.status == "ok", current.to_dict()
    controller = IntegrityRuntimeController(owner, owner.database.path.parent, policy_mode="pause")
    assert controller._apply_policy(current).action["result"] == "clean"
    assert owner.lifecycle.current().state == "active"


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("already_decided", [False, True])
def test_real_application_withdrawal_and_historical_encoding_are_integrity_safe(
    kernel: SubjectKernel, legacy: bool, already_decided: bool
) -> None:
    collaboration, who, bounty = make_collaboration(kernel)
    result = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    if already_decided:
        from noyra.interaction import InteractionDecision

        with kernel.database.connection() as connection:
            interaction = connection.execute("SELECT interaction_id FROM interactions").fetchone()[
                0
            ]
        InteractionStore(kernel.database).decide(
            interaction, InteractionDecision(disposition="silent", rationale="Voluntary silence")
        )
    assert_clean_and_active(kernel)
    serializer = json.dumps if legacy else canonical_json
    with patch("noyra.interaction.collaboration.canonical_json", side_effect=serializer):
        collaboration.write(
            "/api/participants/withdraw",
            {"kind": "application", "id": result["application_id"]},
            who,
            "127.0.0.1",
        )
    assert_clean_and_active(kernel)
    with kernel.database.connection() as connection:
        row = connection.execute(
            "SELECT payload_json FROM audit_records "
            "WHERE action='participant_application_withdrawn'"
        ).fetchone()
    assert row[0] == serializer(json.loads(row[0]))
    assert (json.loads(row[0])["interaction_id"] is None) is already_decided


@pytest.mark.parametrize("legacy", [False, True])
def test_real_http_source_binding_history_survives_disabling(
    http: tuple[NoyraHTTPServer, str], legacy: bool
) -> None:
    owner, base = http
    account = AccountStore(owner.kernel.database, owner.kernel.subject_id).register(
        "alice", "correct horse battery", "Alice"
    )
    transport = owner.transports.configure(
        owner.kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="Synthetic",
            endpoint="https://example.test/incoming",
            credentials={"webhook_secret": SecretStr("synthetic")},
        ),
        actor="operator",
    )
    binding = owner.inbound.bind(
        owner.kernel.subject_id,
        transport.transport_id,
        external_account_id="account",
        external_sender_id="alice-native",
    )
    serializer = json.dumps if legacy else canonical_json
    with patch("noyra.interaction.participant_http.canonical_json", side_effect=serializer):
        status, body = Client(base).request(
            "/api/admin/participant-sources",
            {"entity_id": account["entity_id"], "binding_id": binding},
            Authorization="Bearer admin-test-key-with-enough-entropy",
        )
    assert status == 200, body
    assert_clean_and_active(owner.kernel)
    owner.inbound.change_binding(
        binding, owner.kernel.subject_id, status="revoked", actor="operator", reason="Withdraw"
    )
    with owner.kernel.database.transaction() as connection:
        connection.execute(
            "UPDATE participant_entities SET status='disabled' WHERE entity_id=?",
            (account["entity_id"],),
        )
    assert_clean_and_active(owner.kernel)


def test_real_qq_consent_history_survives_reroute_unlink_and_revocation(tmp_path: Path) -> None:
    owner, inbound, _, binding, window = verified_route(tmp_path)
    owner.boot()
    owner.orient()
    owner.activate()
    try:
        assert_clean_and_active(owner)
        inbound.route_private_chat(owner.subject_id, binding, window, share_web_history=True)
        assert_clean_and_active(owner)
        inbound.route_private_chat(
            owner.subject_id,
            binding,
            window,
            share_web_history=True,
            push_web_replies=True,
            expected_revision=1,
        )
        inbound.unlink_private_chat(owner.subject_id, binding)
        inbound.change_binding(
            binding, owner.subject_id, status="revoked", actor="operator", reason="Withdraw"
        )
        assert_clean_and_active(owner)
    finally:
        owner.close()


@pytest.mark.parametrize("damage", ["actor", "extra", "missing", "foreign", "duplicate", "nan"])
def test_participant_history_compatibility_still_rejects_invalid_evidence(
    kernel: SubjectKernel, damage: str
) -> None:
    collaboration, who, bounty = make_collaboration(kernel)
    result = collaboration.write(
        "/api/participants/apply",
        {"bounty_id": bounty.bounty_id, "content": "I can help", "idempotency_key": "interest"},
        who,
        "127.0.0.1",
    )
    collaboration.write(
        "/api/participants/withdraw",
        {"kind": "application", "id": result["application_id"]},
        who,
        "127.0.0.1",
    )
    assert_clean_and_active(kernel)
    with kernel.database.connection() as connection:
        audit = dict(
            connection.execute(
                "SELECT * FROM audit_records WHERE action='participant_application_withdrawn'"
            ).fetchone()
        )
    payload = json.loads(audit["payload_json"])
    actor, subject = audit["actor"], kernel.subject_id
    if damage == "actor":
        actor = "operator"
    elif damage == "extra":
        payload["extra"] = True
    elif damage == "missing":
        del payload["interaction_id"]
    elif damage == "foreign":
        subject = "foreign-audit-subject"
        IdentityStore(kernel.database).ensure(subject, content_hash(subject))
    raw = json.dumps(payload)
    if damage == "duplicate":
        raw = raw[:-1] + ', "application_id": "other"}'
    elif damage == "nan":
        raw = raw[:-1] + ', "extra": NaN}'
    with kernel.database.transaction() as connection:
        connection.execute(
            "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
            (new_id("audit"), subject, audit["action"], actor, raw, utc_now()),
        )
    if damage == "foreign":
        current = IntegrityRegistry().run(
            kernel.database,
            subject,
            kernel.database.path.parent,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=15,
            check_ids=("core.actions",),
        )
    else:
        current = report(kernel)
    assert current.status == "corrupt"


@pytest.mark.parametrize("damage", ["actor", "consent_type", "extra", "unknown_action"])
def test_private_route_audits_reject_invalid_evidence(tmp_path: Path, damage: str) -> None:
    owner, inbound, _, binding, window = verified_route(tmp_path)
    try:
        inbound.route_private_chat(owner.subject_id, binding, window, share_web_history=True)
        assert report(owner).status == "ok"
        payload: dict[str, Any] = {"binding_id": binding, "push_web_replies": False}
        actor, action = "operator", "private_route_changed"
        if damage == "actor":
            actor = "subject"
        elif damage == "consent_type":
            payload["push_web_replies"] = 1
        elif damage == "extra":
            payload["extra"] = True
        else:
            action = "private_route_unregistered"
        with owner.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records VALUES(?,?,?,?,?,?)",
                (
                    new_id("audit"),
                    owner.subject_id,
                    action,
                    actor,
                    canonical_json(payload),
                    utc_now(),
                ),
            )
        assert report(owner).status == "corrupt"
    finally:
        owner.close()
