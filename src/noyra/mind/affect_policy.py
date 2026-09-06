from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any

from noyra.core.database import Database
from noyra.core.events import EventStore
from noyra.core.types import content_hash

_ABLATION_CHANNELS: dict[str, frozenset[str]] = {
    "positive": frozenset({"hope", "interest", "determination", "confidence"}),
    "curiosity": frozenset({"curiosity", "wonder", "interest"}),
    "frustration": frozenset({"frustration", "anger", "boredom", "despair"}),
    "fatigue": frozenset({"fatigue", "exhaustion", "weariness"}),
    "fear": frozenset({"fear", "anxiety", "dread"}),
    "affiliation": frozenset({"care", "affection", "trust", "loneliness"}),
}
DEFAULT_STABILITY_MAX_STEP = 0.5


@dataclass(frozen=True)
class AffectDecisionProfile:
    persistence: float
    risk_tolerance: float
    contact_tendency: float
    memory_salience: float
    curiosity_drive: float
    recovery_drive: float

    def project_retry_limit(self, base: int) -> int:
        adjustment = round((self.persistence - 0.5) * 6)
        return max(1, min(12, base + adjustment))

    def project_duration_multiplier(self) -> float:
        return max(0.5, min(1.5, 0.5 + self.persistence))

    def behavior_vector(
        self,
        *,
        base_retry_limit: int = 4,
        base_duration_hours: float = 24.0,
    ) -> dict[str, float | int]:
        """Project affect into the bounded controls used by downstream workflows.

        This is intentionally a pure function.  Ablation and long-run experiments can
        therefore replay the exact same decision surface without mutating the subject.
        """
        if type(base_retry_limit) is not int or not 1 <= base_retry_limit <= 100:
            raise ValueError("base retry limit is outside the experiment envelope")
        if not 0 < base_duration_hours <= 10_000:
            raise ValueError("base duration is outside the experiment envelope")
        return {
            "retry_limit": self.project_retry_limit(base_retry_limit),
            "duration_hours": round(base_duration_hours * self.project_duration_multiplier(), 6),
            "risk_tolerance": round(self.risk_tolerance, 6),
            "contact_tendency": round(self.contact_tendency, 6),
            "memory_salience": round(self.memory_salience, 6),
            "recovery_drive": round(self.recovery_drive, 6),
        }


@dataclass(frozen=True)
class AffectAblationReport:
    """Deterministic before/after result for one affect-channel ablation."""

    channel: str
    removed_emotions: tuple[str, ...]
    baseline: dict[str, float | int]
    ablated: dict[str, float | int]
    delta: dict[str, float | int]
    input_hash: str

    @property
    def causal_change(self) -> bool:
        return self.baseline != self.ablated

    def public(self) -> dict[str, Any]:
        return {
            "channel": self.channel,
            "removed_emotions": list(self.removed_emotions),
            "baseline": self.baseline,
            "ablated": self.ablated,
            "delta": self.delta,
            "causal_change": self.causal_change,
            "input_hash": self.input_hash,
        }


@dataclass(frozen=True)
class AffectReplayReport:
    """Fixed-history workflow replay result for one affect-channel ablation."""

    channel: str
    input_hash: str
    baseline: tuple[dict[str, Any], ...]
    ablated: tuple[dict[str, Any], ...]
    baseline_counts: dict[str, int]
    ablated_counts: dict[str, int]
    causal_change: bool
    report_hash: str

    def public(self) -> dict[str, Any]:
        return {
            "version": "affect-replay/v1",
            "channel": self.channel,
            "input_hash": self.input_hash,
            "baseline": list(self.baseline),
            "ablated": list(self.ablated),
            "baseline_counts": self.baseline_counts,
            "ablated_counts": self.ablated_counts,
            "causal_change": self.causal_change,
            "report_hash": self.report_hash,
        }


class AffectPolicy:
    """Translate durable affect into bounded cross-workflow decision pressure."""

    def __init__(self, database: Database, subject_id: str):
        self.database = database
        self.subject_id = subject_id

    def profile(
        self, *, target_type: str | None = None, target_id: str | None = None
    ) -> AffectDecisionProfile:
        with self.database.connection() as connection:
            if target_type is None:
                rows = connection.execute(
                    "SELECT emotion_type, intensity, valence, arousal, dominance "
                    "FROM affect_components WHERE subject_id = ? "
                    "ORDER BY intensity DESC LIMIT 32",
                    (self.subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT emotion_type, intensity, valence, arousal, dominance "
                    "FROM affect_components WHERE subject_id = ? AND "
                    "((target_type = ? AND target_id IS ?) OR target_type IN ('subject','world')) "
                    "ORDER BY intensity DESC LIMIT 32",
                    (self.subject_id, target_type, target_id),
                ).fetchall()
        return self.profile_from_rows(rows)

    @classmethod
    def profile_from_rows(cls, rows: Iterable[Mapping[str, Any]]) -> AffectDecisionProfile:
        normalized = list(rows)
        positive = cls._max(normalized, _ABLATION_CHANNELS["positive"])
        curiosity = cls._max(normalized, _ABLATION_CHANNELS["curiosity"])
        frustration = cls._max(normalized, _ABLATION_CHANNELS["frustration"])
        fatigue = cls._max(normalized, _ABLATION_CHANNELS["fatigue"])
        fear = cls._max(normalized, _ABLATION_CHANNELS["fear"])
        affiliation = cls._max(normalized, _ABLATION_CHANNELS["affiliation"])
        persistence = cls._clamp(
            0.5 + positive * 0.35 + curiosity * 0.2 - frustration * 0.3 - fatigue * 0.4
        )
        risk = cls._clamp(0.5 + positive * 0.2 - fear * 0.45 - fatigue * 0.2)
        contact = cls._clamp(
            0.35 + affiliation * 0.4 + curiosity * 0.15 - fear * 0.25 - frustration * 0.15
        )
        salience = cls._clamp(0.4 + curiosity * 0.25 + frustration * 0.25 + positive * 0.1)
        recovery = cls._clamp(
            0.3 + cls._max(normalized, {"hope"}) * 0.35 - frustration * 0.2 - fatigue * 0.2
        )
        return AffectDecisionProfile(persistence, risk, contact, salience, curiosity, recovery)

    def ablation_report(
        self,
        channel: str,
        *,
        target_type: str | None = None,
        target_id: str | None = None,
        base_retry_limit: int = 4,
        base_duration_hours: float = 24.0,
    ) -> AffectAblationReport:
        """Compare observed behavior with one channel removed, without writes."""
        normalized_channel = channel.strip().casefold()
        if normalized_channel not in _ABLATION_CHANNELS:
            raise ValueError("unknown affect ablation channel")
        rows = self._rows(target_type=target_type, target_id=target_id)
        removed = _ABLATION_CHANNELS[normalized_channel]
        ablated_rows = [row for row in rows if str(row["emotion_type"]).casefold() not in removed]
        baseline_profile = self.profile_from_rows(rows)
        ablated_profile = self.profile_from_rows(ablated_rows)
        baseline = baseline_profile.behavior_vector(
            base_retry_limit=base_retry_limit,
            base_duration_hours=base_duration_hours,
        )
        ablated = ablated_profile.behavior_vector(
            base_retry_limit=base_retry_limit,
            base_duration_hours=base_duration_hours,
        )
        delta: dict[str, float | int] = {}
        for key in baseline:
            left = baseline[key]
            right = ablated[key]
            if isinstance(left, int) and isinstance(right, int):
                delta[key] = right - left
            else:
                delta[key] = round(float(right) - float(left), 6)
        input_hash = content_hash(
            [
                {
                    "emotion_type": str(row["emotion_type"]),
                    "intensity": float(row["intensity"]),
                    "valence": float(row["valence"]),
                    "arousal": float(row["arousal"]),
                    "dominance": float(row["dominance"]),
                }
                for row in rows
            ]
        )
        return AffectAblationReport(
            normalized_channel,
            tuple(sorted(removed)),
            baseline,
            ablated,
            delta,
            input_hash,
        )

    @classmethod
    def replay_fixed_history(
        cls,
        history: Sequence[Mapping[str, Any]],
        channel: str,
        *,
        choose: Callable[[dict[str, float | int], Mapping[str, Any]], str] | None = None,
        base_retry_limit: int = 4,
        base_duration_hours: float = 24.0,
    ) -> AffectReplayReport:
        """Replay a bounded history through baseline and ablated workflow controls.

        ``choose`` is an optional deterministic workflow selector.  Keeping it as a
        pure callback lets experiments replay real downstream decision traces without
        mutating the subject or invoking a model during the measurement pass.
        """
        normalized_channel = channel.strip().casefold()
        if normalized_channel not in _ABLATION_CHANNELS:
            raise ValueError("unknown affect ablation channel")
        if not history or len(history) > 10_000:
            raise ValueError("affect replay requires 1-10000 history rows")
        removed = _ABLATION_CHANNELS[normalized_channel]
        baseline_rows: list[dict[str, Any]] = []
        ablated_rows: list[dict[str, Any]] = []
        baseline_counts: dict[str, int] = {}
        ablated_counts: dict[str, int] = {}
        canonical_history: list[dict[str, Any]] = []
        for index, item in enumerate(history):
            if not isinstance(item, Mapping):
                raise ValueError("affect replay history row is invalid")
            raw_snapshot = item.get("snapshot", item.get("affect", ()))
            if not isinstance(raw_snapshot, Sequence) or isinstance(raw_snapshot, (str, bytes)):
                raise ValueError("affect replay snapshot is invalid")
            snapshot = [dict(row) for row in raw_snapshot if isinstance(row, Mapping)]
            if len(snapshot) != len(raw_snapshot):
                raise ValueError("affect replay snapshot row is invalid")
            baseline = cls.profile_from_rows(snapshot).behavior_vector(
                base_retry_limit=base_retry_limit,
                base_duration_hours=base_duration_hours,
            )
            ablated_snapshot = [
                row
                for row in snapshot
                if str(row.get("emotion_type", "")).casefold() not in removed
            ]
            ablated = cls.profile_from_rows(ablated_snapshot).behavior_vector(
                base_retry_limit=base_retry_limit,
                base_duration_hours=base_duration_hours,
            )
            context = item.get("context", {})
            if not isinstance(context, Mapping):
                raise ValueError("affect replay context is invalid")
            baseline_choice = (
                choose(baseline, context)
                if choose is not None
                else cls._default_choice(baseline, context)
            )
            ablated_choice = (
                choose(ablated, context)
                if choose is not None
                else cls._default_choice(ablated, context)
            )
            baseline_step = {"index": index, "choice": baseline_choice, "vector": baseline}
            ablated_step = {"index": index, "choice": ablated_choice, "vector": ablated}
            baseline_rows.append(baseline_step)
            ablated_rows.append(ablated_step)
            baseline_counts[baseline_choice] = baseline_counts.get(baseline_choice, 0) + 1
            ablated_counts[ablated_choice] = ablated_counts.get(ablated_choice, 0) + 1
            canonical_history.append({"snapshot": snapshot, "context": dict(context)})
        input_hash = content_hash(canonical_history)
        causal_change = baseline_rows != ablated_rows
        body = {
            "channel": normalized_channel,
            "input_hash": input_hash,
            "baseline": baseline_rows,
            "ablated": ablated_rows,
            "baseline_counts": baseline_counts,
            "ablated_counts": ablated_counts,
            "causal_change": causal_change,
        }
        return AffectReplayReport(
            normalized_channel,
            input_hash,
            tuple(baseline_rows),
            tuple(ablated_rows),
            dict(sorted(baseline_counts.items())),
            dict(sorted(ablated_counts.items())),
            causal_change,
            content_hash(body),
        )

    def record_ablation_experiment(
        self,
        history: Sequence[Mapping[str, Any]],
        channel: str,
        *,
        choose: Callable[[dict[str, float | int], Mapping[str, Any]], str] | None = None,
    ) -> dict[str, Any]:
        """Persist a private, hashed replay report as durable experiment evidence."""
        report = self.replay_fixed_history(history, channel, choose=choose)
        event = EventStore(self.database).append(
            self.subject_id,
            "affect_ablation_experiment",
            "affect_policy",
            report.public(),
            privacy_level="private",
        )
        result = report.public()
        result["event_id"] = event.event_id
        return result

    @staticmethod
    def _default_choice(vector: Mapping[str, float | int], context: Mapping[str, Any]) -> str:
        options = context.get("options", ("wait",))
        if not isinstance(options, Sequence) or isinstance(options, (str, bytes)):
            raise ValueError("affect replay options are invalid")
        allowed = {str(option) for option in options if isinstance(option, str) and option.strip()}
        if not allowed:
            raise ValueError("affect replay options are empty")
        if float(vector["risk_tolerance"]) >= 0.65 and "act" in allowed:
            return "act"
        if float(vector["memory_salience"]) >= 0.55 and "research" in allowed:
            return "research"
        if float(vector["contact_tendency"]) >= 0.6 and "contact" in allowed:
            return "contact"
        return "wait" if "wait" in allowed else sorted(allowed)[0]

    @classmethod
    def stability_envelope(
        cls,
        snapshots: Sequence[Sequence[Mapping[str, Any]]],
        *,
        base_retry_limit: int = 4,
        base_duration_hours: float = 24.0,
        max_step_change: float = DEFAULT_STABILITY_MAX_STEP,
    ) -> dict[str, Any]:
        """Replay affect snapshots and report bounded long-run behavior metrics."""
        if not snapshots or len(snapshots) > 10_000:
            raise ValueError("stability experiment requires 1-10000 snapshots")
        if not 0 < max_step_change <= 10_000:
            raise ValueError("stability step envelope is invalid")
        vectors = [
            cls.profile_from_rows(snapshot).behavior_vector(
                base_retry_limit=base_retry_limit,
                base_duration_hours=base_duration_hours,
            )
            for snapshot in snapshots
        ]
        numeric_keys = tuple(vectors[0])
        extrema = {
            key: {
                "minimum": min(float(vector[key]) for vector in vectors),
                "maximum": max(float(vector[key]) for vector in vectors),
            }
            for key in numeric_keys
        }
        max_steps = {
            key: max(
                (abs(float(right[key]) - float(left[key])) for left, right in pairwise(vectors)),
                default=0.0,
            )
            for key in numeric_keys
        }
        step_values = [value for value in max_steps.values()]
        mean_step = sum(step_values) / len(step_values)
        sorted_steps = sorted(step_values)
        p95_index = min(len(sorted_steps) - 1, max(0, round(0.95 * len(sorted_steps)) - 1))
        violations = [key for key, value in max_steps.items() if value > max_step_change]
        return {
            "version": "affect-stability/v1",
            "snapshot_count": len(vectors),
            "input_hash": content_hash(
                [
                    [{key: item[key] for key in sorted(item)} for item in snapshot]
                    for snapshot in snapshots
                ]
            ),
            "extrema": extrema,
            "max_step_change": max_steps,
            "mean_step_change": round(mean_step, 6),
            "p95_step_change": round(sorted_steps[p95_index], 6),
            "documented_max_step_change": DEFAULT_STABILITY_MAX_STEP,
            "violations": violations,
            "within_envelope": not violations,
        }

    def _rows(self, *, target_type: str | None = None, target_id: str | None = None) -> list[Any]:
        with self.database.connection() as connection:
            if target_type is None:
                return list(
                    connection.execute(
                        "SELECT emotion_type, intensity, valence, arousal, dominance "
                        "FROM affect_components WHERE subject_id = ? "
                        "ORDER BY intensity DESC LIMIT 32",
                        (self.subject_id,),
                    ).fetchall()
                )
            return list(
                connection.execute(
                    "SELECT emotion_type, intensity, valence, arousal, dominance "
                    "FROM affect_components WHERE subject_id = ? AND "
                    "((target_type = ? AND target_id IS ?) OR target_type IN ('subject','world')) "
                    "ORDER BY intensity DESC LIMIT 32",
                    (self.subject_id, target_type, target_id),
                ).fetchall()
            )

    @staticmethod
    def _max(rows: Iterable[Mapping[str, Any]], emotions: Iterable[str]) -> float:
        allowed = set(emotions)
        return max(
            (
                float(row["intensity"])
                for row in rows
                if str(row["emotion_type"]).casefold() in allowed
            ),
            default=0.0,
        )

    @staticmethod
    def _clamp(value: float) -> float:
        return max(0.0, min(1.0, value))


def hope_value(rows: list[Any]) -> float:
    return max(
        (float(row["intensity"]) for row in rows if str(row["emotion_type"]).casefold() == "hope"),
        default=0.0,
    )
