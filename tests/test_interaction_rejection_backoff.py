from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, InteractionCognition
from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, new_id
from noyra.interaction import InteractionStore
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure
from noyra.interaction.integrity import InteractionIntegrity
from noyra.interaction.rejections import EVENT_TYPE
from noyra.model import (
    BudgetLimits,
    FakeProvider,
    ModelGateway,
    ModelLedger,
    ModelPricing,
    ModelUsage,
    ProviderResponse,
    RetryPolicy,
)
from noyra.model.errors import ProviderCallError
from noyra.model.resources import RoutedModelGateway


def output(kind: str) -> str:
    if kind == "schema":
        return "{invalid synthetic JSON"
    value: dict[str, Any] = {
        "disposition": "silent",
        "rationale": "I choose to remain silent.",
        "response": None,
        "appraisal": {
            "novelty": 0.4,
            "goal_congruence": 0.0,
            "controllability": 0.5,
            "certainty": 0.8,
            "agency": "external correspondent",
            "narrative": "An invitation.",
        },
        "affect_impulses": [],
    }
    if kind == "target":
        value["affect_impulses"] = [
            {
                "emotion_type": "curiosity",
                "target_type": "goal",
                "target_id": "goal_forged",
                "impulse": 0.6,
                "valence": 0.0,
                "arousal": 0.2,
                "dominance": 0.2,
                "decay_rate": 0.1,
                "goal_effect": 1.0,
            }
        ]
    return json.dumps(value)


@dataclass
class Case:
    kernel: SubjectKernel
    now: datetime

    def clock(self) -> str:
        return self.now.isoformat(timespec="milliseconds")

    def engine(
        self, responses: list[str | ProviderCallError], *, routed: bool = False
    ) -> tuple[InteractionCognition, FakeProvider]:
        provider = FakeProvider(
            [
                value
                if isinstance(value, ProviderCallError)
                else ProviderResponse(content=value, usage=ModelUsage(100, 50))
                for value in responses
            ]
        )
        gateway: ModelGateway
        if routed:
            from test_audit_20261022_remediation import resources_for

            resources, _ = resources_for(
                self.kernel.database, self.kernel.subject_id, self.kernel.database.path.parent
            )
            gateway = RoutedModelGateway(
                self.kernel.database,
                self.kernel.subject_id,
                resources,
                provider_factory=lambda _: provider,
            )
        else:
            gateway = ModelGateway(
                provider,
                ModelLedger(self.kernel.database),
                model="test-model",
                limits=BudgetLimits(50, 500000, 100000, 5000000),
                pricing=ModelPricing(),
                retry_policy=RetryPolicy(max_attempts=1),
            )
        return InteractionCognition(
            self.kernel.database,
            self.kernel.subject_id,
            gateway,
            CognitionSettings(
                interaction_cooldown_seconds=0, max_interaction_model_calls_per_day=20
            ),
            clock=self.clock,
        ), provider

    def receive(
        self,
        key: str,
        *,
        conversation_id: str | None = None,
        counterparty: str = "synthetic-contact",
    ) -> str:
        with patch("noyra.interaction.store.utc_now", return_value=self.clock()):
            item = InteractionStore(self.kernel.database).receive(
                self.kernel.subject_id,
                "web",
                counterparty,
                "Synthetic invitation " + key,
                idempotency_key=key,
                conversation_id=conversation_id,
            )
        self.now += timedelta(milliseconds=1)
        return item.interaction_id


@pytest.fixture
def case(tmp_path: Path) -> Any:
    kernel = SubjectKernel(tmp_path / "noyra.sqlite3", "H08-Synthetic", content_hash("H08"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    try:
        yield Case(kernel, datetime.now(UTC))
    finally:
        kernel.close()


@pytest.mark.parametrize("kind", ["target", "schema"])
@pytest.mark.parametrize("routed", [False, True])
def test_invalid_proposal_yields_to_an_independent_invitation(
    case: Case, kind: str, routed: bool
) -> None:
    first = case.receive("first")
    second = case.receive("second")
    engine, provider = case.engine([output(kind), output("valid")], routed=routed)
    assert asyncio.run(engine.run_due()) in {
        "interaction_proposal_rejected",
        "interaction_model_failed",
    }
    if routed and kind == "schema":
        from test_audit_20261022_remediation import resources_for

        # Invalid schema retains the router's existing key-health cooldown.
        # A separately configured healthy route can serve the next invitation.
        resources_for(
            case.kernel.database,
            case.kernel.subject_id,
            case.kernel.database.path.parent,
            label="Independent healthy route",
        )
    assert asyncio.run(engine.run_due()) == "interaction_silent"
    store = InteractionStore(case.kernel.database)
    assert store.get(first).status == "offered"
    assert store.get(second).status == "silent"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("kind", ["target", "schema"])
@pytest.mark.parametrize("routed", [False, True])
def test_repeated_ticks_and_restart_do_not_spend_again_during_backoff(
    case: Case, kind: str, routed: bool
) -> None:
    invitation = case.receive("first")
    engine, provider = case.engine([output(kind)] * 5, routed=routed)
    assert asyncio.run(engine.run_due()) in {
        "interaction_proposal_rejected",
        "interaction_model_failed",
    }
    for _ in range(3):
        assert asyncio.run(engine.run_due()) is None
    restarted = InteractionCognition(
        case.kernel.database,
        case.kernel.subject_id,
        engine.gateway,
        engine.settings,
        clock=case.clock,
    )
    assert asyncio.run(restarted.run_due()) is None
    assert len(provider.requests) == 1
    assert InteractionStore(case.kernel.database).get(invitation).status == "offered"
    with case.kernel.database.read_transaction() as connection:
        for table in ("appraisals", "interaction_decisions"):
            assert connection.execute("SELECT COUNT(*) FROM " + table).fetchone()[0] == 0


def test_rejection_backoff_increases_and_retries_remain_bounded(case: Case) -> None:
    case.receive("first")
    engine, provider = case.engine([output("target")] * 4)
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    case.now += timedelta(seconds=60)
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    case.now += timedelta(seconds=60)
    assert asyncio.run(engine.run_due()) is None
    assert len(provider.requests) == 2
    case.now += timedelta(seconds=60)
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    assert len(provider.requests) == 3


def test_backoff_reaches_its_cap_and_eventually_allows_a_valid_decision(case: Case) -> None:
    invitation = case.receive("first")
    engine, provider = case.engine([output("target")] * 8 + [output("valid")])
    for count, delay in enumerate((60, 120, 240, 480, 960, 1920, 3600, 3600), start=1):
        assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
        case.now += timedelta(seconds=delay - 1)
        assert asyncio.run(engine.run_due()) is None
        assert len(provider.requests) == count
        case.now += timedelta(seconds=1)
    assert asyncio.run(engine.run_due()) == "interaction_silent"
    assert len(provider.requests) == 9
    assert InteractionStore(case.kernel.database).get(invitation).status == "silent"


def test_midnight_and_timezone_changes_do_not_reset_backoff(case: Case) -> None:
    case.now = datetime(2026, 11, 14, 23, 59, 30, tzinfo=UTC)
    case.receive("first")
    engine, provider = case.engine([output("target"), output("valid")])
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    case.now += timedelta(seconds=59)
    case.now = case.now.astimezone(timezone(timedelta(hours=8)))
    assert asyncio.run(engine.run_due()) is None
    assert len(provider.requests) == 1
    case.now += timedelta(seconds=1)
    assert asyncio.run(engine.run_due()) == "interaction_silent"
    assert len(provider.requests) == 2


@pytest.mark.parametrize("kind", ["target", "schema"])
def test_crash_before_receipt_recovers_paid_rejection_without_another_call(
    case: Case, kind: str
) -> None:
    case.receive("first")
    engine, provider = case.engine([output(kind)])
    with (
        patch.object(engine.rejections, "record", side_effect=RuntimeError("receipt crash")),
        pytest.raises(RuntimeError, match="receipt crash"),
    ):
        asyncio.run(engine.run_due())
    restarted, _ = case.engine([])
    assert asyncio.run(restarted.run_due()) is None
    assert asyncio.run(restarted.run_due()) is None
    assert len(provider.requests) == 1
    assert (
        InteractionIntegrity(case.kernel.database).verify(case.kernel.subject_id)[
            "interaction_proposal_rejections"
        ]
        == 1
    )


def test_successful_uncommitted_response_still_recovers_without_another_call(case: Case) -> None:
    invitation = case.receive("first")
    engine, provider = case.engine([output("valid")])
    with (
        patch.object(engine.mind, "process_event", side_effect=RuntimeError("commit crash")),
        pytest.raises(RuntimeError, match="commit crash"),
    ):
        asyncio.run(engine.run_due())
    restarted, queued = case.engine([])
    assert asyncio.run(restarted.run_due()) == "interaction_silent"
    assert len(provider.requests) == 1 and not queued.requests
    assert InteractionStore(case.kernel.database).get(invitation).status == "silent"


def test_backoff_expires_without_changing_the_decision_round_or_daily_cap(case: Case) -> None:
    invitation = case.receive("first")
    engine, provider = case.engine([output("target"), output("valid")])
    engine.settings = engine.settings.model_copy(update={"max_interaction_model_calls_per_day": 1})
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    case.now += timedelta(seconds=60)
    assert asyncio.run(engine.run_due()) is None
    assert len(provider.requests) == 1
    engine.settings = engine.settings.model_copy(update={"max_interaction_model_calls_per_day": 2})
    assert asyncio.run(engine.run_due()) == "interaction_silent"
    with case.kernel.database.read_transaction() as connection:
        purposes = [row[0] for row in connection.execute("SELECT purpose FROM model_calls")]
        assert connection.execute("SELECT COUNT(*) FROM interaction_decisions").fetchone()[0] == 1
    assert purposes == [f"interaction_cognition:{invitation}:0"] * 2
    assert len(provider.requests) == 2


def test_receipt_is_idempotent_private_and_preserves_accounting(case: Case) -> None:
    invitation = case.receive("first")
    engine, _ = case.engine([output("target")])
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    with case.kernel.database.read_transaction() as connection:
        call = ModelLedger._call_from_row(
            connection.execute("SELECT * FROM model_calls").fetchone()
        )
        before = [dict(row) for row in connection.execute("SELECT * FROM model_attempts")]
        receipt = engine.rejections.read(connection, call)
    assert receipt is not None
    case.now += timedelta(seconds=30)
    assert not engine.rejections.record(call.call_id, invitation, 0, "invalid_affect_target", 0)
    with case.kernel.database.read_transaction() as connection:
        assert engine.rejections.read(connection, call) == receipt
        assert before == [dict(row) for row in connection.execute("SELECT * FROM model_attempts")]
        assert (
            connection.execute(
                "SELECT privacy_level FROM events WHERE event_type=?", (EVENT_TYPE,)
            ).fetchone()[0]
            == "private"
        )
        assert connection.execute(
            "SELECT t.eligibility,t.redaction_status FROM training_records t "
            "JOIN events e ON e.event_id=t.event_id WHERE e.event_type=?",
            (EVENT_TYPE,),
        ).fetchone()[:] == ("excluded", "excluded")
    # The receipt has no message, prompt, model output or rationale copies.
    assert set(receipt) == {
        "format",
        "interaction_id",
        "decision_round",
        "model_call_id",
        "purpose",
        "request_hash",
        "status",
        "error_code",
        "reason",
        "retry_after",
    }
    with (
        case.kernel.database.transaction() as connection,
        pytest.raises(sqlite3.IntegrityError),
    ):
        connection.execute("UPDATE events SET payload_json='{}' WHERE event_type=?", (EVENT_TYPE,))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("reason", []),
        ("model_call_id", "call_unrelated"),
        ("interaction_id", "int_unrelated"),
        ("request_hash", "0" * 64),
        ("retry_after", "2099-01-01T00:00:00+00:00"),
    ],
)
def test_rehashed_corrupt_receipt_fails_closed_without_spending(
    case: Case, field: str, value: Any
) -> None:
    case.receive("first")
    engine, provider = case.engine([output("target")])
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    with case.kernel.database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM events WHERE event_type=?", (EVENT_TYPE,)
        ).fetchone()
        payload = json.loads(row["payload_json"])
        payload[field] = value
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_json=?,payload_hash=? WHERE event_id=?",
            (canonical_json(payload), content_hash(payload), row["event_id"]),
        )
    with pytest.raises(IntegrityError, match="interaction rejection"):
        asyncio.run(engine.run_due())
    with pytest.raises(IntegrityError, match="interaction rejection"):
        InteractionIntegrity(case.kernel.database).verify(case.kernel.subject_id)
    assert len(provider.requests) == 1


def test_unknown_outcome_has_no_rejection_receipt_and_keeps_its_reservation(case: Case) -> None:
    case.receive("first")
    case.receive("second")
    engine, provider = case.engine(
        [
            ProviderCallError("provider_outcome_unknown", retryable=False, outcome_unknown=True),
            output("valid"),
        ]
    )
    assert asyncio.run(engine.run_due()) == "interaction_model_failed"
    with case.kernel.database.read_transaction() as connection:
        call = dict(connection.execute("SELECT * FROM model_calls").fetchone())
        attempt = dict(connection.execute("SELECT * FROM model_attempts").fetchone())
    assert call["status"] == attempt["status"] == "unknown"
    case.now += timedelta(days=1)
    assert asyncio.run(engine.run_due()) == "interaction_silent"
    assert asyncio.run(engine.run_due()) is None
    with case.kernel.database.read_transaction() as connection:
        assert call == dict(
            connection.execute(
                "SELECT * FROM model_calls WHERE call_id=?", (call["call_id"],)
            ).fetchone()
        )
        assert attempt == dict(
            connection.execute(
                "SELECT * FROM model_attempts WHERE attempt_id=?", (attempt["attempt_id"],)
            ).fetchone()
        )
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM events WHERE event_type=?", (EVENT_TYPE,)
            ).fetchone()[0]
            == 0
        )
    assert len(provider.requests) == 2


def test_receipt_remains_valid_after_historical_private_erasure(case: Case) -> None:
    conversation = ConversationStore(case.kernel.database).create(
        case.kernel.subject_id, "Synthetic private conversation"
    )
    case.receive("first", conversation_id=conversation["conversation_id"], counterparty="web-user")
    engine, provider = case.engine([output("target")])
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    database, subject = case.kernel.database, case.kernel.subject_id
    with database.read_transaction() as connection:
        window = connection.execute(
            "SELECT c.conversation_id,c.revision FROM private_conversations c "
            "WHERE c.conversation_id=?",
            (conversation["conversation_id"],),
        ).fetchone()
        call_id = connection.execute("SELECT call_id FROM model_calls").fetchone()[0]
    erasure = ConversationErasure(database)
    # Exercise recovery of an already recorded historical intent, never enable deletion.
    erasure._write_marker(subject, window["conversation_id"], new_id("erase"))
    erasure._resume_recorded_intent(
        subject, window["conversation_id"], expected_revision=window["revision"]
    )
    assert erasure.run(subject, window["conversation_id"])["status"] == "completed"
    assert ModelLedger(database).get_call(call_id).response == {"privacy_erased": True}
    assert InteractionIntegrity(database).verify(subject)["interaction_proposal_rejections"] == 1
    assert asyncio.run(engine.run_due()) is None
    assert len(provider.requests) == 1


def test_archived_receipt_keeps_backoff_after_worker_restart(case: Case) -> None:
    case.now = datetime(2020, 1, 1, tzinfo=UTC)
    case.receive("first")
    engine, provider = case.engine([output("target")])
    assert asyncio.run(engine.run_due()) == "interaction_proposal_rejected"
    key = base64.urlsafe_b64encode(b"h" * 32).decode()
    database, subject = case.kernel.database, case.kernel.subject_id
    with patch.dict(os.environ, {"NOYRA_ARCHIVE_ENCRYPTION_KEY": key}):
        archive = EventPayloadArchive(
            database, database.path.parent / "subject" / "cold", subject_id=subject
        )
        assert archive.archive_cold(subject, older_than_days=30) >= 1
        with database.read_transaction() as connection:
            assert (
                connection.execute(
                    "SELECT payload_archive_key FROM events WHERE event_type=?", (EVENT_TYPE,)
                ).fetchone()[0]
                is not None
            )
        restarted, _ = case.engine([])
        assert asyncio.run(restarted.run_due()) is None
        assert (
            InteractionIntegrity(database).verify(subject)["interaction_proposal_rejections"] == 1
        )
    assert len(provider.requests) == 1


@pytest.mark.parametrize("reserved_id", [False, True])
def test_unbound_receipt_is_an_integrity_failure(case: Case, reserved_id: bool) -> None:
    engine, _ = case.engine([])
    EventStore(case.kernel.database).append(
        case.kernel.subject_id,
        EVENT_TYPE,
        "interaction_supervisor",
        {},
        event_id=engine.rejections.prefix + "call_missing" if reserved_id else None,
    )
    with pytest.raises(IntegrityError, match="interaction rejection"):
        InteractionIntegrity(case.kernel.database).verify(case.kernel.subject_id)
