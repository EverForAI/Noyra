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
from noyra.core.at_rest import BackupKeyring, EncryptedBackupManager
from noyra.core.errors import NotFoundError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.types import content_hash, new_id
from noyra.interaction import InteractionStore, PublicDiaryStore
from noyra.interaction import erasure as erasure_module
from noyra.interaction import reflection_lineage as lineage_module
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired
from noyra.mind import (
    BeliefStore,
    MemoryConsolidator,
    MemoryIntegrationSupervisor,
    MemoryStore,
)
from noyra.model import BudgetLimits, FakeProvider, ModelGateway, ModelPricing, ModelUsage
from noyra.model.ledger import ModelLedger
from noyra.model.types import ProviderResponse
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.sleep import (
    PersonalityCandidateInput,
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
@pytest.mark.parametrize(
    "mode", ["target_only", "mixed_windows", "consolidation", "duplicate_keeper", "unbound"]
)
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
        events = EventStore(db)
        additional_event = events.append(subject, "experience", "test", {}).event_id
        third_event = events.append(subject, "experience", "test", {"third": True}).event_id
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
            public_diary_candidate="Synthetic diary candidate",
            personality_candidates=(
                PersonalityCandidateInput(
                    trait="cautious curiosity",
                    direction=0.3,
                    confidence=0.4,
                    evidence_ids=(target_event, additional_event, third_event),
                ),
            ),
            memories=(
                SleepMemory(
                    content="derived memory content",
                    salience=0.2,
                    confidence=0.4,
                    source_event_ids=(target_event,),
                ),
                SleepMemory(
                    content="memory without a target citation",
                    salience=0.2,
                    confidence=0.4,
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
        if mode != "unbound":
            BeliefStore(db).revise(
                belief.belief_id,
                proposition="A later independent review of the belief.",
                confidence=0.45,
                status="qualified",
                supporting_event_ids=(additional_event,),
                counter_event_ids=(),
                reason="Synthetic follow-up after sleep.",
                expected_revision=2,
            )
        if mode in {"target_only", "mixed_windows"}:
            with db.read_transaction() as connection:
                target_memory = connection.execute(
                    "SELECT target_id FROM sleep_integrations WHERE sleep_id=? "
                    "AND integration_type='memory' ORDER BY rowid LIMIT 1",
                    (run.sleep_id,),
                ).fetchone()[0]
            independent_memory = MemoryStore(db).create(
                subject,
                "semantic",
                "independent integration parent",
                salience=0.4,
                confidence=0.5,
                source_event_ids=(additional_event,),
            )
            MemoryIntegrationSupervisor(db, subject).integrate(
                "merge",
                (target_memory, independent_memory.memory_id),
                synthesis="Merged private and independent evidence.",
                confidence=0.4,
                evidence_event_ids=(additional_event,),
                reason="bounded lineage review fixture",
            )
        elif mode == "consolidation":
            MemoryStore(db, clock=lambda: "2000-01-01T00:00:00.000+00:00").create(
                subject,
                "reflection",
                "independent stale consolidation parent",
                salience=0.2,
                confidence=0.4,
                source_event_ids=(additional_event,),
            )
            MemoryStore(db).create(
                subject,
                "semantic",
                "retained memory outside the cluster",
                salience=0.5,
                confidence=0.7,
                source_event_ids=(additional_event,),
            )
            future = (datetime.now().astimezone() + timedelta(days=200)).isoformat()
            assert (
                MemoryConsolidator(
                    db,
                    subject,
                    clock=lambda: future,
                    interval_seconds=0,
                    stale_after_days=1,
                    archive_after_days=1,
                    minimum_active_memories=3,
                ).run_due()
                == "memory_consolidation_committed"
            )
        elif mode == "duplicate_keeper":
            MemoryStore(db).create(
                subject,
                "reflection",
                selected.memories[0].content,
                salience=0.7,
                confidence=0.8,
                source_event_ids=(additional_event,),
            )
            assert MemoryConsolidator(db, subject).run_due() == "memory_consolidation_committed"
        assert engine.commit_reflection(run.sleep_id, selected) == committed
        assert committed.reflection_event_id is not None
        event = EventStore(db).get(committed.reflection_event_id)
        assert event.privacy_level == "private" and not event.causal_parent_ids
        assert event.payload.get("model_call_id") == selected._model_call_id
        assert "model_call_id" not in selected.model_dump()
        engine.enter_deep_sleep(run.sleep_id)
        engine.wake(run.sleep_id, "fixture", force=True)
        engine.complete_wake(run.sleep_id)
        if selected.public_diary_candidate is not None:
            PublicDiaryStore(db).publish(
                subject, run.sleep_id, "Synthetic title", selected.public_diary_candidate
            )
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
            "coverage": "known_direct_links_and_reviewed_sleep_derivatives",
            "archive": {
                "status": "not_present",
                "segments": 0,
                "events": 0,
                "target_events": 0,
                "read_mode": "local_only_no_restore",
            },
            "counts": {
                "committed_reflections": 1,
                "reflections_without_recorded_model_inputs": int(mode == "unbound"),
                "reflections_with_unbound_model_calls": int(mode == "unbound"),
                "reflections_with_target_model_input": int(mode != "unbound"),
                "reflections_with_multiple_window_inputs": int(mode == "mixed_windows"),
                "memory_belief_receipts_with_target_model_input": 0 if mode == "unbound" else 3,
                "memory_belief_receipts_with_multiple_window_inputs": (
                    3 if mode == "mixed_windows" else 0
                ),
                "memory_belief_receipts_with_target_event": 0 if mode == "unbound" else 2,
                "personality_candidate_receipts_with_target_model_input": int(mode != "unbound"),
                "personality_candidate_receipts_with_multiple_window_inputs": int(
                    mode == "mixed_windows"
                ),
                "personality_candidate_receipts_with_target_event": int(mode != "unbound"),
                "target_receipts_with_unmatched_event_references": int(mode != "unbound"),
                "opaque_memory_belief_receipts": 0,
                "opaque_derivative_receipts": 0,
                "sleep_integration_receipts": 0 if mode == "unbound" else 4,
                "sleep_integration_receipts_with_target_model_input": (
                    0 if mode == "unbound" else 4
                ),
                "sleep_integration_receipts_with_multiple_window_inputs": (
                    4 if mode == "mixed_windows" else 0
                ),
                "retry_blocks_linked_to_target_model_input": 0,
                "public_diary_entries_linked_to_target_model_input": int(mode != "unbound"),
                "project_sleep_reviews_on_target_model_run": 0,
            },
            "lineage": {
                "sleep_memory_records": 2 if mode != "unbound" else 0,
                "later_revisions_of_sleep_memories": int(mode not in {"consolidation", "unbound"}),
                "memory_integrations_using_lineage": int(mode in {"target_only", "mixed_windows"}),
                "memory_integrations_with_other_parents": int(
                    mode in {"target_only", "mixed_windows"}
                ),
                "memory_consolidations_using_lineage": int(
                    mode in {"consolidation", "duplicate_keeper"}
                ),
                "memory_consolidations_with_other_members": int(mode == "consolidation"),
                "downstream_memory_records": int(mode not in {"unbound", "duplicate_keeper"}),
                "downstream_memories_with_other_parents": int(
                    mode in {"target_only", "mixed_windows", "consolidation"}
                ),
                "sleep_beliefs_with_matched_revisions": 1 if mode != "unbound" else 0,
                "beliefs_with_later_revisions_to_review": int(mode != "unbound"),
                "later_belief_revisions_to_review": int(mode != "unbound"),
                "unmatched_sleep_belief_receipts": 0,
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
            with monkeypatch.context() as limited:
                limited.setattr(lineage_module, "_MAX_TOTAL_REFERENCE_BYTES", 1)
                bounded = erasure.assess(subject, target["conversation_id"])[
                    "sleep_reflection_scope"
                ]
                assert bounded["status"] == "bounded_out"
                assert bounded["lineage"] is None and bounded["counts"] is None

            class Future(datetime):
                @classmethod
                def now(cls, tz: Any = None) -> Future:
                    return cls.fromtimestamp(
                        (datetime.now(tz) + timedelta(days=100)).timestamp(), tz
                    )

            monkeypatch.setenv(
                "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
            )
            archive = EventPayloadArchive(
                db, db.path.parent / "subject" / "cold", subject_id=subject
            )
            with monkeypatch.context() as archive_time:
                archive_time.setattr("noyra.core.event_archive.datetime", Future)
                assert archive.archive_cold(subject) > 0
            with monkeypatch.context() as archive_review:
                archive_review.setattr(erasure_module, "MAX_REVIEW_SECONDS", 30)
                archived = erasure.assess(subject, target["conversation_id"])
            assert archived["reason_code"] == "reflection_derivative_review_required"
            assert archived["deletion_available"] is False
            assert archived["sleep_reflection_scope"]["archive"]["status"] == "verified_local"
            assert archived["sleep_reflection_scope"]["archive"]["target_events"] == 1
            assert archived["sleep_reflection_scope"]["counts"] == scope["counts"]
            assert archived["sleep_reflection_scope"]["lineage"] == scope["lineage"]
            with db.read_transaction() as connection:
                object_key = connection.execute(
                    "SELECT payload_archive_key FROM events WHERE event_id=?", (target_event,)
                ).fetchone()[0]
            object_path = archive.provider._path(object_key)
            unavailable_path = object_path.with_name(object_path.name + ".test-held")
            object_path.replace(unavailable_path)
            try:
                with monkeypatch.context() as unavailable_review:
                    unavailable_review.setattr(erasure_module, "MAX_REVIEW_SECONDS", 30)
                    unavailable = erasure.assess(subject, target["conversation_id"])
                assert unavailable["deletion_available"] is False
                assert unavailable["sleep_reflection_scope"]["status"] == "incomplete"
                assert unavailable["sleep_reflection_scope"]["archive"]["status"] == "unavailable"
                assert not object_path.exists()
            finally:
                unavailable_path.replace(object_path)
            assert target in ConversationStore(db).list(subject)["items"]
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
        "coverage": "known_direct_links_and_reviewed_sleep_derivatives",
        "counts": None,
        "lineage": None,
        "archive": {"status": "not_checked", "segments": 0, "events": 0},
    }


def test_sleep_lineage_assessment_on_encrypted_backup_is_read_only(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    subject = "encrypted-backup-lineage-review"
    with SubjectKernel(source / "noyra.sqlite3", subject, content_hash(subject)) as kernel:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        db = kernel.database
        windows = ConversationStore(db)
        window = windows.create(subject, "backup review window")
        other = windows.create(subject, "independent backup window")
        messages = InteractionStore(db)
        target_message = messages.receive(
            subject, "web", "web-user", "target text", conversation_id=window["conversation_id"]
        )
        independent_message = messages.receive(
            subject, "web", "web-user", "independent text", conversation_id=other["conversation_id"]
        )
        with db.read_transaction() as connection:
            source_events = {
                row["interaction_id"]: row["event_id"]
                for row in connection.execute(
                    "SELECT event_id,"
                    "json_extract(payload_json,'$.interaction_id') AS interaction_id "
                    "FROM events WHERE subject_id=? AND event_type='interaction_received'",
                    (subject,),
                )
            }
        event_id = source_events[target_message.interaction_id]
        independent_event = source_events[independent_message.interaction_id]
        belief = BeliefStore(db).create(
            subject,
            "backup private-derived belief",
            confidence=0.5,
            scope="test",
            supporting_event_ids=(event_id,),
        )
        engine = SleepEngine(db, subject)
        run = engine.start("subject_choice", "backup lineage preview")
        run = engine.begin_reflection(run.sleep_id)
        engine.commit_reflection(
            run.sleep_id,
            SleepReflectionPlan(
                summary="backup private-derived reflection",
                memories=(
                    SleepMemory(
                        content="backup private-derived memory",
                        salience=0.5,
                        confidence=0.6,
                        source_event_ids=(event_id,),
                    ),
                ),
                belief_revisions=(
                    SleepBeliefRevision(
                        belief_id=belief.belief_id,
                        proposition="backup revised belief",
                        confidence=0.6,
                        status="qualified",
                        reason="backup preview fixture",
                        supporting_event_ids=(event_id,),
                    ),
                ),
            ),
        )
        engine.enter_deep_sleep(run.sleep_id)
        engine.wake(run.sleep_id, "fixture", force=True)
        engine.complete_wake(run.sleep_id)
        with db.read_transaction() as connection:
            root_memory_id = connection.execute(
                "SELECT target_id FROM sleep_integrations WHERE sleep_id=? "
                "AND integration_type='memory'",
                (run.sleep_id,),
            ).fetchone()[0]
        independent = MemoryStore(db).create(
            subject,
            "semantic",
            "independent window memory",
            salience=0.5,
            confidence=0.6,
            source_event_ids=(independent_event,),
        )
        integration = MemoryIntegrationSupervisor(db, subject)
        parent_id = root_memory_id
        for index in range(2):
            merged = integration.integrate(
                "contradict",
                (parent_id, independent.memory_id),
                synthesis=f"mixed inference {index}",
                confidence=0.5,
                evidence_event_ids=(independent_event,),
                reason="backup derived evidence",
            )
            assert merged.output_memory_id is not None
            parent_id = merged.output_memory_id
        BeliefStore(db).revise(
            belief.belief_id,
            proposition="Follow-up with another window's evidence",
            confidence=0.5,
            status="qualified",
            supporting_event_ids=(independent_event,),
            counter_event_ids=(),
            reason="synthetic later review",
            expected_revision=2,
        )

    base_scope = ConversationErasure(db).assess(subject, window["conversation_id"])[
        "sleep_reflection_scope"
    ]
    keyring_path = tmp_path / "backup-keyring.json"
    BackupKeyring.initialize(keyring_path)
    backup_path = tmp_path / "encrypted-fixture.noyra-backup"
    manager = EncryptedBackupManager(source, keyring_path)
    manager.create(backup_path)
    restored_root = tmp_path / "isolated-restored-copy"
    manager.restore(backup_path, restored_root)
    restored = Database(restored_root / "noyra.sqlite3")
    with restored.read_transaction() as connection:
        before = list(connection.iterdump())
    result = ConversationErasure(restored).assess(subject, window["conversation_id"])
    assert result["deletion_available"] is False
    assert result["reason_code"] in {
        "reflection_derivative_review_required",
        "private_memory_derivative_review_required",
    }
    scope = result["sleep_reflection_scope"]
    assert scope["status"] == "observed" and scope == base_scope
    assert scope["lineage"]["sleep_memory_records"] == 1
    assert scope["lineage"]["downstream_memory_records"] == 2
    assert scope["lineage"]["downstream_memories_with_other_parents"] == 2
    assert scope["lineage"]["sleep_beliefs_with_matched_revisions"] == 1
    assert scope["lineage"]["later_belief_revisions_to_review"] == 1
    assert MemoryStore(restored).get(independent.memory_id) == independent
    with restored.read_transaction() as connection:
        assert list(connection.iterdump()) == before
    assert not (restored_root / "privacy-deletions").exists()

    # Model a recorded intent from the historical prototype. The production
    # request API stays disabled. Recovery must not publish this older copy.
    ConversationErasure(db)._write_marker(subject, window["conversation_id"], new_id("erase"))
    rejected_root = tmp_path / "rejected-restore"
    with pytest.raises(ErasureReviewRequired, match=str(result["reason_code"])):
        manager.restore(backup_path, rejected_root)
    assert not rejected_root.exists()
    assert not list(tmp_path.glob(".noyra-restore-*"))
    assert InteractionStore(db).get(target_message.interaction_id) == target_message
    assert MemoryStore(db).get(independent.memory_id) == independent


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
