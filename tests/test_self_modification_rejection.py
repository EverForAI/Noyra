from __future__ import annotations

from typing import Any

import pytest

import test_self_modification as helpers
from test_self_modification_automatic import case as case
from test_self_modification_automatic import rows, stagnation


def reject_replay(
    case: helpers.ControlledSelfModificationTestCase, monkeypatch: pytest.MonkeyPatch
) -> Any:
    from unittest.mock import Mock

    original = case.manager._replay_simulation
    baseline = float(case.manager.effective("research_interval_seconds"))

    def conservative(
        history: list[dict[str, Any]], settings: dict[str, int | float]
    ) -> dict[str, Any]:
        result = original(history, settings)
        if settings["research_interval_seconds"] > baseline:
            result = {**result, "budget_units": 1000000}
        return result

    replay = Mock(side_effect=conservative)
    monkeypatch.setattr(case.manager, "_replay_simulation", replay)
    return replay


def test_same_automatic_rejection_does_not_repeat_simulation_or_append_history(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    control = stagnation(case)
    replay = reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    first = rows(case, "self_modification_proposals")
    assert len(first) == 1 and first[0]["status"] == "rejected"
    assert replay.call_count == 2
    for _ in range(4):
        assert case.manager.run_due() is None
    assert replay.call_count == 2
    assert rows(case, "self_modification_proposals") == first
    assert not rows(case, "self_modification_settings") and not rows(
        case, "self_modification_revisions"
    )
    assert not rows(case, "model_calls")
    control.verify_integrity()
    case.manager.verify_integrity()


@pytest.mark.parametrize("offset", [1, -1, 86400, -86400])
def test_restart_and_wall_clock_changes_do_not_reopen_identical_rejection(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
    offset: int,
) -> None:
    from datetime import datetime, timedelta

    from noyra.cognition import ControlledSelfModification
    from noyra.core.database import Database

    stagnation(case)
    replay = reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    before = rows(case, "self_modification_proposals")
    case.now = (datetime.fromisoformat(case.now) + timedelta(seconds=offset)).isoformat()
    restarted = ControlledSelfModification(
        Database(case.kernel.database.path, initialize=False),
        case.kernel.subject_id,
        case.manager.settings,
        clock=lambda: case.now,
    )

    def never(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("unchanged rejection must skip simulation after restart")

    monkeypatch.setattr(restarted, "_replay_simulation", never)
    assert restarted.run_due() is None
    assert rows(case, "self_modification_proposals") == before and replay.call_count == 2
    restarted.verify_integrity()


@pytest.mark.parametrize(
    "change", ["profile-and-evidence", "simulation-history", "configuration", "rules"]
)
def test_new_inputs_allow_immediate_reconsideration_without_rewriting_old_rejection(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
    change: str,
) -> None:
    from dataclasses import replace

    from noyra.cognition import CognitionSettings
    from noyra.cognition.metacognition import _Candidate

    control = stagnation(case)
    replay = reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    before = rows(case, "self_modification_proposals")
    if change == "profile-and-evidence":
        control._commit_decision(
            _Candidate("research", "none", None, "fixture_reconsideration", 0.5, 0.5, 0.0), 0.5, 0.0
        )
        control.record_result("fixture_waited")
    elif change == "simulation-history":
        before_candidate = case.manager._stagnant_strategy_candidate()
        control.clock = lambda: "2026-08-13T23:00:00+00:00"
        control._commit_decision(
            _Candidate("wait", "none", None, "fixture_other_history", 0.5, 0.5, 0.0), 0.5, 0.0
        )
        control.record_result("fixture_waited")
        assert case.manager._stagnant_strategy_candidate() == before_candidate
    elif change == "configuration":
        case.manager.settings = CognitionSettings(thought_interval_seconds=2400.0)
    else:
        rules = dict(case.manager.RULES)
        rules["research_interval_seconds"] = replace(
            rules["research_interval_seconds"], risk_weight=0.55
        )
        monkeypatch.setattr(case.manager, "RULES", rules)
    assert case.manager.run_due() == "self_modification_rejected"
    after = rows(case, "self_modification_proposals")
    assert len(after) == 2 and after[0] == before[0]
    assert replay.call_count == 4
    assert case.manager.run_due() is None
    assert replay.call_count == 4
    case.manager.verify_integrity()


def test_explicit_proposals_remain_explicit_and_strict(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stagnation(case)
    reject_replay(case, monkeypatch)
    evidence = (case.event.event_id,)
    first = case.manager.propose(
        "research_interval_seconds", 8640.0, reason="Explicit request", evidence_ids=evidence
    )
    second = case.manager.propose(
        "research_interval_seconds", 8640.0, reason="Explicit request", evidence_ids=evidence
    )
    assert first.status == second.status == "rejected" and first.proposal_id != second.proposal_id
    assert "automatic_evaluation" not in first.validation
    from noyra.cognition import SelfModificationError

    with pytest.raises(SelfModificationError):
        case.manager.apply(first.proposal_id)


def test_legacy_matching_rejection_reuses_verified_deterministic_result_without_history_update(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stagnation(case)
    replay = reject_replay(case, monkeypatch)
    candidate = case.manager._stagnant_strategy_candidate()
    assert candidate is not None
    key, value, reason, evidence = candidate
    legacy = case.manager.propose(key, value, reason=reason, evidence_ids=evidence)
    assert legacy.status == "rejected" and "automatic_evaluation" not in legacy.validation
    before = rows(case, "self_modification_proposals")
    assert case.manager.run_due() is None
    assert rows(case, "self_modification_proposals") == before
    # Old rows do not carry a complete evaluation key. They require a bounded
    # deterministic replay comparison rather than an invented legacy context.
    assert replay.call_count == 4


def test_corrupted_rejection_evidence_fails_closed_instead_of_suppressing_work(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.core.errors import IntegrityError
    from noyra.core.types import canonical_json

    stagnation(case)
    replay = reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    with case.kernel.database.transaction() as c:
        c.execute(
            "UPDATE self_modification_proposals SET validation_json=? WHERE subject_id=?",
            (
                canonical_json(
                    {
                        "automatic_evaluation": {
                            "version": "self-modification-auto/v1",
                            "fingerprint": "0" * 64,
                        }
                    }
                ),
                case.kernel.subject_id,
            ),
        )
    before = rows(case, "self_modification_proposals")
    with pytest.raises(IntegrityError):
        case.manager.run_due()
    assert rows(case, "self_modification_proposals") == before and replay.call_count == 2


def test_concurrent_automatic_rejection_evaluates_and_appends_once(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from concurrent.futures import ThreadPoolExecutor

    from noyra.cognition import ControlledSelfModification

    stagnation(case)
    replay = reject_replay(case, monkeypatch)
    second = ControlledSelfModification(
        case.kernel.database, case.kernel.subject_id, case.manager.settings, clock=lambda: case.now
    )
    monkeypatch.setattr(second, "_replay_simulation", replay)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda manager: manager.run_due(), [case.manager, second]))
    assert result.count("self_modification_rejected") == 1 and result.count(None) == 1
    assert len(rows(case, "self_modification_proposals")) == 1 and replay.call_count == 2
    case.manager.verify_integrity()


def test_safe_automatic_proposal_still_applies_and_rolls_back_under_existing_rules(
    case: helpers.ControlledSelfModificationTestCase,
) -> None:
    control = stagnation(case)
    assert case.manager.run_due() == "self_modification_simulated"
    record = case.manager.get(rows(case, "self_modification_proposals")[0]["proposal_id"])
    marker = record.validation["automatic_evaluation"]
    assert case.manager.run_due() == "self_modification_applied"
    applied = case.manager.get(record.proposal_id)
    assert applied.status == "applied" and applied.validation["automatic_evaluation"] == marker
    assert case.manager.effective(record.setting_key) == record.proposed_value
    result = case.manager.observe(record.proposal_id, productive=0, failed=2, stagnant=0)
    assert result.status == "rolled_back"
    assert case.manager.effective(record.setting_key) == record.old_value
    assert [row["action"] for row in rows(case, "self_modification_revisions")] == [
        "apply",
        "rollback",
    ]
    control.verify_integrity()
    case.manager.verify_integrity()


def test_automatic_append_failure_rolls_back_before_record_can_be_reused(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    stagnation(case)
    reject_replay(case, monkeypatch)
    original = case.manager._validated_record
    monkeypatch.setattr(
        case.manager,
        "_validated_record",
        Mock(side_effect=RuntimeError("synthetic post-insert validation failure")),
    )
    with pytest.raises(RuntimeError, match="post-insert"):
        case.manager.run_due()
    assert not rows(case, "self_modification_proposals")
    monkeypatch.setattr(case.manager, "_validated_record", original)
    assert case.manager.run_due() == "self_modification_rejected"
    assert len(rows(case, "self_modification_proposals")) == 1


def test_other_subject_does_not_reuse_rejection_from_first_subject(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.cognition import ControlledSelfModification, MetacognitiveControl
    from noyra.cognition.metacognition import _Candidate
    from noyra.core.identity import IdentityStore
    from noyra.core.types import content_hash

    control = stagnation(case)
    replay = reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    original = rows(case, "self_modification_proposals")[0]
    other = "other-selfmod-subject"
    IdentityStore(case.kernel.database).ensure(other, content_hash(other))
    peer = MetacognitiveControl(
        case.kernel.database, other, case.manager.settings, clock=lambda: case.now
    )
    for _ in range(4):
        peer._commit_decision(
            _Candidate("research", "none", None, "fixture_stagnation", 0.5, 0.5, 0.0), 0.5, 0.0
        )
        peer.record_result("fixture_waited")
    manager = ControlledSelfModification(
        case.kernel.database, other, case.manager.settings, clock=lambda: case.now
    )
    monkeypatch.setattr(manager, "_replay_simulation", replay)
    assert manager.run_due() == "self_modification_rejected"
    assert len(rows(case, "self_modification_proposals")) == 2
    assert rows(case, "self_modification_proposals")[0] == original
    assert manager.run_due() is None
    assert replay.call_count == 4
    control.verify_integrity()
    peer.verify_integrity()
    manager.verify_integrity()


def test_cold_archive_does_not_change_automatic_rejection_identity(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import base64
    from datetime import datetime, timedelta
    from unittest.mock import patch

    from noyra.core.event_archive import EventPayloadArchive
    from noyra.core.events import EventStore

    stagnation(case)
    replay = reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    before = rows(case, "self_modification_proposals")
    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY", base64.urlsafe_b64encode(bytes(range(32))).decode()
    )

    class Future(datetime):
        @classmethod
        def now(cls, tz: Any = None) -> Future:
            value = datetime.now(tz) + timedelta(days=100)
            return cls.fromtimestamp(value.timestamp(), tz)

    archive = EventPayloadArchive(
        case.kernel.database,
        case.kernel.database.path.parent / "subject/cold",
        subject_id=case.kernel.subject_id,
    )
    with patch("noyra.core.event_archive.datetime", Future):
        assert archive.archive_cold(case.kernel.subject_id, older_than_days=90) > 0
    assert case.manager.run_due() is None
    assert rows(case, "self_modification_proposals") == before and replay.call_count == 2
    case.manager.verify_integrity()
    EventStore(case.kernel.database).verify_chain(case.kernel.subject_id)


def test_legacy_rejection_is_not_reused_when_current_deterministic_simulation_differs(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stagnation(case)
    original = case.manager._replay_simulation
    reject_replay(case, monkeypatch)
    candidate = case.manager._stagnant_strategy_candidate()
    assert candidate is not None
    key, value, reason, evidence = candidate
    previous = case.manager.propose(key, value, reason=reason, evidence_ids=evidence)
    assert previous.status == "rejected"
    before = rows(case, "self_modification_proposals")[0]
    monkeypatch.setattr(case.manager, "_replay_simulation", original)
    assert case.manager.run_due() == "self_modification_simulated"
    assert len(rows(case, "self_modification_proposals")) == 2
    assert rows(case, "self_modification_proposals")[0] == before
    case.manager.verify_integrity()


def test_explicit_reason_normalization_is_hash_consistent_in_atomic_append(
    case: helpers.ControlledSelfModificationTestCase,
) -> None:
    proposal = case.manager.propose(
        "thought_interval_seconds",
        1500.0,
        reason="  bounded explicit change  ",
        evidence_ids=(case.event.event_id,),
    )
    assert proposal.reason == "bounded explicit change"
    assert case.manager.get(proposal.proposal_id) == proposal
    case.manager.verify_integrity()


def test_registry_rejects_malformed_automatic_marker_even_with_matching_proposal_hash(
    case: helpers.ControlledSelfModificationTestCase,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from noyra.core.integrity import IntegrityRegistry
    from noyra.core.types import canonical_json

    stagnation(case)
    reject_replay(case, monkeypatch)
    assert case.manager.run_due() == "self_modification_rejected"
    report = IntegrityRegistry().run(
        case.kernel.database,
        case.kernel.subject_id,
        case.kernel.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("cognition.self_modification",),
    )
    assert not report.p0 and not report.p1
    record = case.manager.get(rows(case, "self_modification_proposals")[0]["proposal_id"])
    malformed = {
        **record.validation,
        "automatic_evaluation": {"version": "self-modification-auto/v1", "fingerprint": "invalid"},
    }
    digest = case.manager._proposal_hash(
        record.setting_key,
        record.old_value,
        record.proposed_value,
        record.reason,
        record.evidence_ids,
        record.risk_score,
        record.status,
        malformed,
        record.simulation,
    )
    with case.kernel.database.transaction() as c:
        c.execute(
            "UPDATE self_modification_proposals SET validation_json=?,state_hash=? "
            "WHERE proposal_id=?",
            (canonical_json(malformed), digest, record.proposal_id),
        )
    report = IntegrityRegistry().run(
        case.kernel.database,
        case.kernel.subject_id,
        case.kernel.database.path.parent,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=10,
        check_ids=("cognition.self_modification",),
    )
    assert report.p0 or report.p1
