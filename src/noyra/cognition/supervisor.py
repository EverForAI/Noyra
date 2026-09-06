from __future__ import annotations

from datetime import UTC, datetime, timedelta

from noyra.mind import AffectImpulse, AppraisalInput
from noyra.world import ClaimProposal, SourceRecord
from noyra.world.types import ObservationRecord

from .types import CognitionProposal, CognitionValidationError, ValidatedCognition


class CognitionSupervisor:
    """Validates and bounds model proposals before any durable subject state changes."""

    def validate(
        self,
        proposal: CognitionProposal,
        *,
        source: SourceRecord,
        observation: ObservationRecord,
        source_trust: float,
        allowed_goal_ids: frozenset[str],
        now: str,
    ) -> ValidatedCognition:
        if not 0 <= source_trust <= 1:
            raise CognitionValidationError("historical source trust is invalid")
        affect_keys: set[tuple[str, str, str | None]] = set()
        affect_impulses = []
        for impulse in proposal.affect_impulses:
            key = (impulse.emotion_type, impulse.target_type, impulse.target_id)
            if key in affect_keys:
                raise CognitionValidationError("duplicate affect targets are not allowed")
            affect_keys.add(key)
            if observation.injection_signals and impulse.target_type == "goal":
                raise CognitionValidationError(
                    "injection-signaled data cannot target an existing goal"
                )
            self._validate_target(
                impulse.target_type,
                impulse.target_id,
                source=source,
                observation=observation,
                allowed_goal_ids=allowed_goal_ids,
            )
            affect_impulses.append(
                AffectImpulse(
                    emotion_type=impulse.emotion_type,
                    target_type=impulse.target_type,
                    target_id=impulse.target_id,
                    impulse=impulse.impulse,
                    valence=impulse.valence,
                    arousal=impulse.arousal,
                    dominance=impulse.dominance,
                    decay_rate=impulse.decay_rate,
                    goal_effect=0.0 if observation.injection_signals else impulse.goal_effect,
                )
            )

        goal_candidates = () if observation.injection_signals else proposal.goals
        goals = []
        for goal in goal_candidates:
            self._validate_target(
                goal.motive_target_type,
                goal.motive_target_id,
                source=source,
                observation=observation,
                allowed_goal_ids=allowed_goal_ids,
            )
            normalized_emotion = " ".join(goal.motive_emotion.strip().lower().split())
            motive = next(
                (
                    impulse
                    for impulse in affect_impulses
                    if impulse.emotion_type == normalized_emotion
                    and impulse.target_type == goal.motive_target_type
                    and impulse.target_id == goal.motive_target_id
                ),
                None,
            )
            if motive is None or motive.impulse < goal.minimum_motive_intensity:
                raise CognitionValidationError(
                    "autonomous goal lacks a matching positive affect impulse"
                )
            goals.append(goal.to_goal_candidate())

        self._validate_uniqueness([claim.proposition for claim in proposal.claims], "world claims")
        self._validate_uniqueness(
            [prediction.statement for prediction in proposal.predictions], "predictions"
        )
        self._validate_uniqueness([goal.title for goal in goal_candidates], "goal titles")

        current = self._parse_time(now)
        latest = current + timedelta(days=366)
        predictions = []
        for prediction in proposal.predictions:
            target = self._parse_time(prediction.target_at)
            if target <= current:
                raise CognitionValidationError("prediction target must remain in the future")
            if target > latest:
                raise CognitionValidationError("prediction horizon exceeds one year")
            predictions.append(prediction)

        trust_cap = source_trust * (0.5 if observation.injection_signals else 1.0)
        appraisal = AppraisalInput(
            novelty=proposal.appraisal.novelty,
            goal_congruence=proposal.appraisal.goal_congruence,
            controllability=proposal.appraisal.controllability,
            certainty=min(proposal.appraisal.certainty, trust_cap),
            agency=proposal.appraisal.agency,
            narrative=proposal.appraisal.narrative,
        )
        claims = tuple(
            ClaimProposal(
                proposition=claim.proposition,
                confidence=min(claim.confidence, trust_cap),
            )
            for claim in proposal.claims
        )
        return ValidatedCognition(
            summary=proposal.summary,
            appraisal=appraisal,
            affect_impulses=tuple(affect_impulses),
            claims=claims,
            predictions=tuple(predictions),
            goals=tuple(goals),
        )

    @staticmethod
    def _validate_target(
        target_type: str,
        target_id: str | None,
        *,
        source: SourceRecord,
        observation: ObservationRecord,
        allowed_goal_ids: frozenset[str],
    ) -> None:
        valid = (
            (target_type in {"world", "subject"} and target_id is None)
            or (target_type == "source" and target_id == source.source_id)
            or (target_type == "observation" and target_id == observation.observation_id)
            or (target_type == "goal" and target_id in allowed_goal_ids)
        )
        if not valid:
            raise CognitionValidationError("proposal references an unauthorized affect target")

    @staticmethod
    def _validate_uniqueness(values: list[str], label: str) -> None:
        normalized = [" ".join(value.lower().split()) for value in values]
        if len(normalized) != len(set(normalized)):
            raise CognitionValidationError(f"duplicate {label} are not allowed")

    @staticmethod
    def _parse_time(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as error:
            raise CognitionValidationError("cognition timestamp is invalid") from error
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise CognitionValidationError("cognition timestamps require a timezone")
        return parsed.astimezone(UTC)
