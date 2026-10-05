from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.actions import ActionLedger
from noyra.core.errors import InvalidTransitionError
from noyra.core.retention import RetentionManager
from noyra.core.types import content_hash
from noyra.model.ledger import ModelLedger
from noyra.model.types import BudgetLimits, ModelUsage


def test_evidence_limit_preserves_idempotency_and_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    subject = "Noyra-evidence-capacity"
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ledger = ModelLedger(db)
    args = (subject, "provider", "model", "test", content_hash({"prompt": "test"}), "once")
    call, _ = ledger.prepare_call(*args)
    attempt = ledger.authorize_attempt(
        call.call_id,
        BudgetLimits(100, 10000, 10000, 10000),
        reserved_input_tokens=1,
        reserved_output_tokens=1,
        reserved_cost_microusd=1,
    )
    ledger.start_attempt(attempt.attempt_id)
    ledger.finish_attempt(attempt.attempt_id, "failed", usage=ModelUsage(0, 0), cost_microusd=0)
    ledger.finish_call(call.call_id, "failed")
    replay, created = ledger.prepare_call(*args)
    assert not created
    assert replay.call_id == call.call_id
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        ledger.prepare_call(*args[:-1], "different")
    status = RetentionManager(db).evidence_capacity(subject)
    assert status["blocked"] is True
    assert status["counts"]["model_calls"] == 1
    assert status["counts"]["model_attempts"] == 1
    assert ledger.get_call(call.call_id).status == "failed"


def test_action_limit_still_allows_completion_and_duplicate_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOYRA_EVIDENCE_MAX_ROWS_PER_TABLE", "1")
    subject = "Noyra-action-capacity"
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    ledger = ActionLedger(db)
    action = ledger.prepare(subject, "test", "test", "target", {}, idempotency_key="once")
    with pytest.raises(InvalidTransitionError, match="evidence_capacity_reached"):
        ledger.prepare(subject, "test", "test", "another", {}, idempotency_key="twice")
    ledger.start(action.action_id)
    ledger.finish(action.action_id, "succeeded", {})
    assert (
        ledger.prepare(subject, "test", "test", "target", {}, idempotency_key="once").action_id
        == action.action_id
    )
    assert ledger.verify_integrity(subject)["behavior_logs"] == 1
