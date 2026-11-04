from __future__ import annotations

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

import test_self_modification as helpers
from noyra.cognition import CognitionSettings, ControlledSelfModification, MetacognitiveControl
from noyra.cognition.metacognition import CognitiveStrategy, _Candidate


@pytest.fixture
def case() -> Iterator[helpers.ControlledSelfModificationTestCase]:
    value = helpers.ControlledSelfModificationTestCase(methodName="runTest")
    value.setUp()
    try:
        yield value
    finally:
        value.tearDown()


def stagnation(
    case: helpers.ControlledSelfModificationTestCase,
    strategy: CognitiveStrategy = "research",
    count: int = 4,
) -> MetacognitiveControl:
    control = MetacognitiveControl(
        case.kernel.database, case.kernel.subject_id, case.manager.settings, clock=lambda: case.now
    )
    origin = datetime.fromisoformat(case.now)
    for index in range(count):
        case.now = (origin + timedelta(hours=index)).isoformat()
        control._commit_decision(
            _Candidate(strategy, "none", None, "fixture_stagnation", 0.5, 0.5, 0.0), 0.5, 0.0
        )
        control.record_result("fixture_waited")
    return control


def rows(case: helpers.ControlledSelfModificationTestCase, table: str) -> list[dict[str, Any]]:
    assert table in {
        "self_modification_proposals",
        "self_modification_settings",
        "self_modification_revisions",
        "model_calls",
    }
    with case.kernel.database.read_transaction() as connection:
        return [dict(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]


def test_real_automatic_research_candidate_at_hard_limit_yields_without_error_or_proposal(
    case: helpers.ControlledSelfModificationTestCase,
) -> None:
    case.manager.settings = CognitionSettings(research_interval_seconds=604800.0)
    control = stagnation(case)
    before = rows(case, "self_modification_proposals")
    assert case.manager.run_due() is None
    assert rows(case, "self_modification_proposals") == before
    assert not rows(case, "self_modification_settings") and not rows(case, "model_calls")
    control.verify_integrity()
    case.manager.verify_integrity()


def test_real_automatic_research_candidate_near_hard_limit_stays_inside_envelope(
    case: helpers.ControlledSelfModificationTestCase,
) -> None:
    case.manager.settings = CognitionSettings(research_interval_seconds=600000.0)
    stagnation(case)
    candidate = case.manager._stagnant_strategy_candidate()
    assert candidate is not None and candidate[0] == "research_interval_seconds"
    assert candidate[1] == 604800.0 and type(candidate[1]) is float
    assert case.manager.run_due() in {"self_modification_simulated", "self_modification_rejected"}
    record = case.manager._record(rows(case, "self_modification_proposals")[0])
    assert record.proposed_value == 604800.0


@pytest.mark.parametrize("setting_key", sorted(ControlledSelfModification.RULES))
def test_automatic_values_preserve_each_existing_numeric_type_and_hard_envelope(
    setting_key: str,
) -> None:
    rule = ControlledSelfModification.RULES[setting_key]
    integral = rule.adapter.json_schema()["type"] == "integer"
    lower = int(rule.minimum) if integral else float(rule.minimum)
    upper = int(rule.maximum) if integral else float(rule.maximum)
    middle = (lower + upper) // 2 if integral else (lower + upper) / 2
    for old in (lower, middle, upper):
        value = ControlledSelfModification._automatic_value(setting_key, old)
        if value is None:
            continue
        assert type(value) is type(old)
        assert rule.minimum <= value <= rule.maximum and value != old
        assert ControlledSelfModification._validate_value(setting_key, value) == value
        relative = abs(value - old) / max(abs(old), 1.0)
        assert relative <= rule.max_relative_change


def test_projection_does_not_relax_explicit_proposal_envelope_or_protected_settings(
    case: helpers.ControlledSelfModificationTestCase,
) -> None:
    from noyra.cognition import SelfModificationError

    for key, value in [
        ("research_interval_seconds", 725760.0),
        ("max_thought_no_change_streak", 0),
        ("retrieval_semantic_weight", 0.72),
        ("subject_id", 1),
    ]:
        with pytest.raises(SelfModificationError):
            case.manager.propose(
                key, value, reason="Explicit invalid proposal", evidence_ids=(case.event.event_id,)
            )
    assert not rows(case, "self_modification_proposals")
    for invalid in (True, float("nan"), float("inf")):
        with pytest.raises(SelfModificationError):
            case.manager._automatic_value("research_interval_seconds", invalid)


def test_integer_minimum_has_no_automatic_change_or_proposal(
    case: helpers.ControlledSelfModificationTestCase,
) -> None:
    case.manager.settings = CognitionSettings(max_thought_no_change_streak=1)
    stagnation(case, "think")
    assert case.manager.run_due() is None
    assert not rows(case, "self_modification_proposals")
