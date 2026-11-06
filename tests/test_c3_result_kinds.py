from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from noyra.cognition import CognitionSettings, MetacognitiveControl
from noyra.cognition.metacognition import MetacognitiveDecisionRecord
from noyra.core import SubjectKernel
from noyra.core.types import content_hash


@pytest.mark.parametrize(
    "result",
    [
        "research_waiting_reconciliation",
        "epistemic_review_waiting_reconciliation",
        "research_planner_wait",
        "research_planner_context_wait",
        "intrinsic_thought_in_progress",
        "intrinsic_thought_target_not_due",
        "relationship_social_context_changed",
        "goal_governance_budget_exhausted",
        "intrinsic_thought_budget_exhausted",
    ],
)
def test_operational_wait_is_not_failed_or_unknown_paid_work(tmp_path: Path, result: str) -> None:
    with SubjectKernel(tmp_path / "subject.sqlite3", "c3-results", content_hash("c3")) as kernel:
        kernel.boot()
        control = MetacognitiveControl(kernel.database, kernel.subject_id, CognitionSettings())
        decision = MetacognitiveDecisionRecord(
            "synthetic-decision",
            kernel.subject_id,
            "research",
            "goal",
            None,
            "synthetic",
            0.5,
            0.5,
            0.0,
            0.0,
            "2026-11-06T00:00:00+00:00",
        )
        classified = control._classify(decision, result)
        assert classified[2] == "stagnant"
        assert classified[3] == result and classified[4] == 0
        # Neither a result string nor a borrowed success token attests progress.
        for strategy in (
            "think",
            "research",
            "action",
            "epistemic_review",
            "goal_review",
            "social_review",
        ):
            assert (
                control._classify(replace(decision, strategy=strategy), "research_accepted")[2]
                == "unknown"
            )


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        ("research_waiting_reconciliation", "waiting"),
        ("action_deliberation_budget_exhausted", "waiting"),
        ("epistemic_review_no_change", "no_change"),
        ("research_replayed", "no_change"),
        ("research_model_failed", "failed"),
        ("relationship_social_rejected", "failed"),
        ("research_accepted", "unverified"),
        ("unknown_custom_success", "unverified"),
        ("unfailed_operation", "unverified"),
        ("research_waiting_reconciliation_extra", "unverified"),
    ],
)
def test_result_categories_use_complete_statuses(result: str, expected: str) -> None:
    from noyra.cognition.result_kinds import cycle_result_kind

    assert cycle_result_kind(result) == expected
