from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from pydantic import SecretStr

from noyra.cognition import CognitionSettings, SleepReflectionCognition
from noyra.core import Database, EventStore, IdentityStore, SubjectKernel
from noyra.core.errors import NotFoundError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.types import content_hash
from noyra.interaction import InteractionStore
from noyra.interaction import erasure as erasure_module
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired
from noyra.mind import BeliefStore
from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelPricing, ModelUsage
from noyra.model.ledger import ModelLedger
from noyra.model.types import ProviderResponse
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.sleep import (
    SleepBeliefRevision,
    SleepEngine,
    SleepIntegrity,
    SleepMemory,
    SleepReflectionPlan,
)


def test_assessment_has_no_intent_marker_or_content_and_does_not_enable_deletion(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private title")
    message = InteractionStore(db).receive(
        "owner", "web", "web-user", "private raw text", conversation_id=window["conversation_id"]
    )
    erasure = ConversationErasure(db)
    with db.read_transaction() as connection:
        before = list(connection.iterdump())
    result = erasure.assess("owner", window["conversation_id"])
    assert result["status"] == "full_contract_pending"
    assert result["deletion_available"] is False
    assert result["counts"]["messages"] == 1
    assert result["coverage"] == "first_prototype_blocker"
    scope = result["sleep_reflection_scope"]
    assert scope["status"] == "observed" and not any(scope["counts"].values())
    assert "private raw text" not in json.dumps(result)
    assert "private title" not in json.dumps(result)
    assert not erasure.root.exists()
    with db.read_transaction() as connection:
        assert list(connection.iterdump()) == before
    assert InteractionStore(db).get(message.interaction_id).content == "private raw text"
    with pytest.raises(NotFoundError):
        erasure.assess("other", window["conversation_id"])


def test_assessment_reports_opaque_provenance_without_accepting_or_hiding_window(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private title")
    ModelLedger(db).prepare_call(
        "owner", "mock", "mock", "sleep_reflection:legacy", content_hash("input"), "legacy"
    )
    erasure = ConversationErasure(db)
    result = erasure.assess("owner", window["conversation_id"])
    assert result["status"] == "review_required"
    assert result["reason_code"] == "legacy_private_provenance_review_required"
    assert result["counts"] is None
    assert result["deletion_available"] is False
    assert not erasure.root.exists()
    assert erasure.get("owner", window["conversation_id"]) is None
    assert ConversationStore(db).list("owner")["items"][0]["status"] == "active"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["target_only", "mixed_windows", "unbound"])
async def test_assessment_distinguishes_exact_reflection_input_from_cited_events(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    subject = "assessment-owner"
    with SubjectKernel(tmp_path / "noyra.sqlite3", subject, content_hash(subject)) as kernel:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        db = kernel.database
        windows = ConversationStore(db)
        interactions = InteractionStore(db)
        target = windows.create(subject, "target window title")
        other = windows.create(subject, "other window title")
        message = interactions.receive(
            subject,
            "web",
            "web-user",
            "target message body",
            conversation_id=target["conversation_id"],
        )
        if mode == "mixed_windows":
            interactions.receive(
                subject,
                "web",
                "web-user",
                "other message body",
                conversation_id=other["conversation_id"],
            )
        with db.read_transaction() as connection:
            target_event = connection.execute(
                "SELECT event_id FROM events WHERE subject_id=? "
                "AND event_type='interaction_received' "
                "AND json_extract(payload_json,'$.interaction_id')=?",
                (subject, message.interaction_id),
            ).fetchone()[0]
        additional_event = EventStore(db).append(subject, "experience", "test", {}).event_id
        belief = BeliefStore(db).create(
            subject,
            "example proposition",
            confidence=0.5,
            scope="test",
            supporting_event_ids=(additional_event,),
        )
        engine = SleepEngine(db, subject)
        run = engine.start("subject_choice", "test provenance")
        run = engine.begin_reflection(run.sleep_id)
        output = SleepReflectionPlan(
            summary="derived model summary",
            memories=(
                SleepMemory(
                    content="derived memory content",
                    salience=0.5,
                    confidence=0.7,
                    source_event_ids=(target_event,),
                ),
                SleepMemory(
                    content="memory without a target citation",
                    salience=0.4,
                    confidence=0.6,
                    source_event_ids=(additional_event,),
                ),
            ),
            belief_revisions=(
                SleepBeliefRevision(
                    belief_id=belief.belief_id,
                    proposition="adjusted proposition",
                    confidence=0.4,
                    status="qualified",
                    reason="test evidence",
                    supporting_event_ids=(additional_event,),
                    counter_event_ids=(target_event,),
                ),
            ),
        )
        provider = FakeProvider(
            [
                ProviderResponse(
                    content=output.model_dump_json(exclude={"goal_revisions"}),
                    usage=ModelUsage(50, 50),
                )
            ]
        )
        ledger = ModelLedger(db)
        gateway = ModelGateway(
            provider,
            ledger,
            model="mock",
            limits=BudgetLimits(10, 500_000, 100_000, 5_000_000),
            pricing=ModelPricing(),
        )
        cognition = SleepReflectionCognition(
            db, subject, gateway, CognitionSettings(enabled=False, max_sleep_context_chars=60_000)
        )
        selected = await cognition.propose(run)
        if mode == "unbound":
            # A same-purpose response exists, but this locally supplied plan did not use it.
            selected = SleepReflectionPlan(summary="local reflection without this model call")
        committed = engine.commit_reflection(run.sleep_id, selected)
        assert engine.commit_reflection(run.sleep_id, selected) == committed
        assert committed.reflection_event_id is not None
        event = EventStore(db).get(committed.reflection_event_id)
        assert event.privacy_level == "private" and not event.causal_parent_ids
        assert event.payload.get("model_call_id") == selected._model_call_id
        assert "model_call_id" not in selected.model_dump()
        SleepIntegrity(db).verify(subject)
        erasure = ConversationErasure(db)
        with db.read_transaction() as connection:
            before = list(connection.iterdump())
        result = erasure.assess(subject, target["conversation_id"])
        expected_blocker = (
            "cross_window_derivative_review_required"
            if mode == "mixed_windows"
            else "reflection_derivative_review_required"
        )
        assert result["reason_code"] == expected_blocker
        assert result["deletion_available"] is False
        scope = result["sleep_reflection_scope"]
        assert scope == {
            "status": "observed",
            "coverage": "known_direct_links_only",
            "counts": {
                "committed_reflections": 1,
                "reflections_without_recorded_model_inputs": int(mode == "unbound"),
                "reflections_with_target_model_input": int(mode != "unbound"),
                "reflections_with_multiple_window_inputs": int(mode == "mixed_windows"),
                "memory_belief_receipts_with_target_model_input": 0 if mode == "unbound" else 3,
                "memory_belief_receipts_with_multiple_window_inputs": (
                    3 if mode == "mixed_windows" else 0
                ),
                "memory_belief_receipts_with_target_event": 0 if mode == "unbound" else 2,
                "target_receipts_with_unmatched_event_references": int(mode != "unbound"),
                "opaque_memory_belief_receipts": 0,
            },
        }
        assert erasure.assess(subject, other["conversation_id"])["sleep_reflection_scope"][
            "counts"
        ]["reflections_with_target_model_input"] == int(mode == "mixed_windows")
        encoded = json.dumps(result)
        for private_value in (
            subject,
            target["conversation_id"],
            target_event,
            "target message body",
            "target window title",
            output.summary,
            selected._model_call_id,
        ):
            if private_value is not None:
                assert private_value not in encoded
        with pytest.raises(ErasureReviewRequired, match="private_deletion_disabled"):
            erasure.request(subject, target["conversation_id"], expected_revision=1, confirmed=True)
        assert not erasure.root.exists()
        with db.read_transaction() as connection:
            assert list(connection.iterdump()) == before

        if mode == "target_only":
            with monkeypatch.context() as limited:
                limited.setattr(erasure_module, "MAX_ERASURE_ROWS", 2)
                bounded = erasure.assess(subject, target["conversation_id"])
                assert bounded["deletion_available"] is False
                assert bounded["sleep_reflection_scope"]["status"] == "bounded_out"
                assert bounded["sleep_reflection_scope"]["counts"] is None

            class Future(datetime):
                @classmethod
                def now(cls, tz: Any = None) -> Future:
                    return cls.fromtimestamp(
                        (datetime.now(tz) + timedelta(days=100)).timestamp(), tz
                    )

            monkeypatch.setenv(
                "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
            )
            archive = EventPayloadArchive(db, tmp_path / "cold", subject_id=subject)
            with monkeypatch.context() as archive_time:
                archive_time.setattr("noyra.core.event_archive.datetime", Future)
                assert archive.archive_cold(subject) > 0
            archived = erasure.assess(subject, target["conversation_id"])
            assert archived["reason_code"] == "reflection_derivative_review_required"
            assert archived["deletion_available"] is False
            assert archived["sleep_reflection_scope"]["counts"] == {
                **scope["counts"],
                "reflections_without_recorded_model_inputs": 1,
                "reflections_with_target_model_input": 0,
                "memory_belief_receipts_with_target_model_input": 0,
                "memory_belief_receipts_with_target_event": 0,
                "target_receipts_with_unmatched_event_references": 0,
            }
            SleepIntegrity(db).verify(subject)


def test_sleep_scope_fails_closed_on_the_shared_review_deadline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("owner", content_hash("owner"))
    window = ConversationStore(db).create("owner", "private title")
    monkeypatch.setattr(erasure_module, "MAX_REVIEW_SECONDS", 0)
    result = ConversationErasure(db).assess("owner", window["conversation_id"])
    assert result["deletion_available"] is False
    assert result["sleep_reflection_scope"] == {
        "status": "bounded_out",
        "coverage": "known_direct_links_only",
        "counts": None,
    }


def test_retired_erasure_http_routes_cannot_mutate_chat(tmp_path: Path) -> None:
    token = "synthetic-erasure-assessment-token"
    settings = ServiceSettings(
        data_dir=tmp_path,
        subject_id="assessment-http",
        genesis_hash=content_hash("assessment"),
        host="127.0.0.1",
        port=0,
        admin_token=SecretStr(token),
    )
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", settings.subject_id, settings.genesis_hash)
    kernel.boot()
    kernel.orient()
    kernel.activate()
    server = NoyraHTTPServer(kernel, settings)
    window = server.conversations.create(kernel.subject_id, "private HTTP title")
    server.start()
    try:
        base = f"http://127.0.0.1:{server.address[1]}"
        path = f"/api/v1/admin/conversations/{window['conversation_id']}/erasure-assessment"

        def call(
            route: str, *, auth: bool = True, method: str = "GET"
        ) -> tuple[int, dict[str, Any]]:
            headers = {"Authorization": f"Bearer {token}"} if auth else {}
            if method in {"POST", "DELETE"}:
                headers["Content-Type"] = "application/json"
            req = Request(
                base + route,
                headers=headers,
                method=method,
                data=b"{}" if method in {"POST", "DELETE"} else None,
            )
            try:
                reply = urlopen(req, timeout=10)
            except HTTPError as error:
                reply = error
            with reply:
                raw = reply.read()
                return reply.status, (
                    json.loads(raw)
                    if reply.headers.get_content_type() == "application/json"
                    else {}
                )

        with kernel.database.read_transaction() as connection:
            before = list(connection.iterdump())
        assert call(path, auth=False)[0] == 404
        status, body = call(path)
        assert status == 404 and body["error"] == "not_found"
        assert "private HTTP title" not in json.dumps(body)
        assert call(path.replace("/api/v1/", "/api/"))[0] == 404
        for suffix in ("?limit=1", "?bad=1", "?limit=1&limit=2"):
            assert call(path + suffix)[0] == 404
        assert call(path.replace(window["conversation_id"], "missing"))[0] == 404
        assert call(path, method="POST")[0] in {400, 404, 405}
        conversation_path = path.removesuffix("/erasure-assessment")
        assert call(conversation_path, method="DELETE")[0] == 501
        for operation in ("delete", "erase", "erasure"):
            assert call(conversation_path + "/" + operation, method="POST")[0] == 400
        assert not (tmp_path / "privacy-deletions").exists()
        with kernel.database.read_transaction() as connection:
            assert list(connection.iterdump()) == before
    finally:
        server.close()
        kernel.close()
