from __future__ import annotations

from pathlib import Path

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import content_hash
from noyra.mind import AffectImpulse, AffectPolicy, AppraisalInput, MindEngine


def _seed(tmp_path: Path) -> tuple[Database, str]:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-p303-affect"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    event = EventStore(database).append(
        subject_id,
        "p303_fixture",
        "test",
        {"kind": "causal-ablation"},
    )
    MindEngine(database).process_event(
        subject_id,
        event.event_id,
        AppraisalInput(
            novelty=0.8,
            goal_congruence=0.4,
            controllability=0.6,
            certainty=0.7,
            agency="p303-test",
            narrative="A bounded affect ablation fixture.",
        ),
        (
            AffectImpulse(
                emotion_type="curiosity",
                target_type="world",
                impulse=0.85,
                valence=0.7,
                arousal=0.7,
                dominance=0.2,
            ),
            AffectImpulse(
                emotion_type="fear",
                target_type="world",
                impulse=0.65,
                valence=-0.7,
                arousal=0.8,
                dominance=-0.3,
            ),
            AffectImpulse(
                emotion_type="hope",
                target_type="world",
                impulse=0.55,
                valence=0.8,
                arousal=0.4,
                dominance=0.1,
            ),
        ),
    )
    return database, subject_id


def test_affect_channel_ablation_has_reproducible_causal_behavior_delta(
    tmp_path: Path,
) -> None:
    database, subject_id = _seed(tmp_path)
    try:
        policy = AffectPolicy(database, subject_id)
        before = policy.profile().behavior_vector()
        report = policy.ablation_report("curiosity")
        repeated = policy.ablation_report("curiosity")

        assert report.public() == repeated.public()
        assert report.causal_change
        assert report.baseline == before
        assert report.ablated["retry_limit"] <= report.baseline["retry_limit"]
        assert report.ablated["duration_hours"] <= report.baseline["duration_hours"]
        assert report.delta["memory_salience"] < 0
        assert len(report.input_hash) == 64
        with database.connection() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM affect_components WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
                == 3
            )
    finally:
        del database


def test_affect_long_run_replay_reports_bounded_envelope_and_detects_jumps() -> None:
    snapshots = [
        (
            {
                "emotion_type": "curiosity",
                "intensity": intensity,
                "valence": 0.5,
                "arousal": 0.5,
                "dominance": 0.0,
            },
            {
                "emotion_type": "fear",
                "intensity": 0.2,
                "valence": -0.5,
                "arousal": 0.6,
                "dominance": -0.2,
            },
        )
        for intensity in (0.2, 0.25, 0.3, 0.35, 0.4)
    ]
    report = AffectPolicy.stability_envelope(snapshots, max_step_change=1.0)
    assert report["version"] == "affect-stability/v1"
    assert report["snapshot_count"] == len(snapshots)
    assert report["within_envelope"] is True
    assert report["violations"] == []
    assert all(
        0.0 <= bound <= 36.0 for extrema in report["extrema"].values() for bound in extrema.values()
    )

    jump = AffectPolicy.stability_envelope(
        [snapshots[0], ({"emotion_type": "curiosity", "intensity": 1.0},)],
        max_step_change=0.01,
    )
    assert jump["within_envelope"] is False
    assert jump["violations"]


def test_fixed_history_replay_persists_private_hashed_experiment(tmp_path: Path) -> None:
    database, subject_id = _seed(tmp_path)
    try:
        history = (
            {
                "snapshot": (
                    {
                        "emotion_type": "curiosity",
                        "intensity": 0.9,
                        "valence": 0.6,
                        "arousal": 0.7,
                        "dominance": 0.2,
                    },
                ),
                "context": {"options": ("research", "wait")},
            },
            {
                "snapshot": (
                    {
                        "emotion_type": "fear",
                        "intensity": 0.8,
                        "valence": -0.7,
                        "arousal": 0.8,
                        "dominance": -0.2,
                    },
                ),
                "context": {"options": ("act", "wait")},
            },
        )
        policy = AffectPolicy(database, subject_id)
        result = policy.record_ablation_experiment(history, "curiosity")
        assert result["causal_change"] is True
        assert len(result["report_hash"]) == 64
        assert isinstance(result["event_id"], str)
        with database.connection() as connection:
            row = connection.execute(
                "SELECT privacy_level, event_type, payload_hash FROM events WHERE event_id = ?",
                (result["event_id"],),
            ).fetchone()
        assert row is not None
        assert row["privacy_level"] == "private"
        assert row["event_type"] == "affect_ablation_experiment"
        assert len(row["payload_hash"]) == 64
    finally:
        del database
