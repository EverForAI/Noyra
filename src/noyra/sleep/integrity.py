from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import (
    content_hash,
    strict_bool,
    strict_finite_float,
    strict_int,
    strict_json_loads,
)

from .engine import SleepEngine
from .errors import FatigueStateError
from .fatigue import FatigueTracker
from .types import FatigueState


class SleepIntegrity:
    """Verify fatigue, sleep histories, integrations, and retry barriers."""

    def __init__(self, database: Database):
        self.database = database

    def verify(self, subject_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.read_transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise IntegrityError("sleep state contains broken foreign keys")

            fatigue = connection.execute(
                "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            transitions = connection.execute(
                "SELECT * FROM fatigue_transitions WHERE subject_id = ? ORDER BY rowid",
                (subject_id,),
            ).fetchall()
            latest_transition: FatigueState | None = None
            previous_fatigue = 0.0
            for transition in transitions:
                transition_id = transition["transition_id"]
                if not isinstance(transition_id, str) or not transition_id.strip():
                    raise IntegrityError("fatigue transition id is invalid")
                if transition["subject_id"] != subject_id:
                    raise IntegrityError(f"fatigue transition ownership mismatch: {transition_id}")
                old_fatigue = self._persisted_float(
                    transition["old_fatigue"],
                    f"fatigue transition {transition_id} old_fatigue",
                    minimum=0.0,
                    maximum=100.0,
                )
                if old_fatigue != previous_fatigue:
                    raise IntegrityError(f"fatigue transition sequence mismatch: {transition_id}")
                latest_transition = self._fatigue_from_row(
                    {
                        "subject_id": subject_id,
                        "fatigue": transition["new_fatigue"],
                        "mode": transition["mode"],
                        "resource_pressure": transition["resource_pressure"],
                        "cognitive_load": transition["cognitive_load"],
                        "frustration": transition["frustration"],
                        "goal_conflict": transition["goal_conflict"],
                        "staleness": transition["staleness"],
                        "state_hash": transition["state_hash"],
                        "version": 1,
                        "updated_at": transition["created_at"],
                    }
                )
                if not isinstance(transition["reason"], str) or not transition["reason"].strip():
                    raise IntegrityError(f"fatigue transition reason is invalid: {transition_id}")
                previous_fatigue = latest_transition.fatigue
            if transitions and not fatigue:
                raise IntegrityError(f"fatigue history has no current state: {subject_id}")
            for row in fatigue:
                state = self._fatigue_from_row(row)
                if transitions and (
                    latest_transition is None
                    or latest_transition.fatigue != state.fatigue
                    or transitions[-1]["state_hash"] != row["state_hash"]
                ):
                    raise IntegrityError(f"fatigue history mismatch: {subject_id}")
                if state.version != len(transitions) + 1:
                    raise IntegrityError(f"fatigue version mismatch: {subject_id}")
            counts["fatigue_states"] = len(fatigue)
            counts["fatigue_transitions"] = len(transitions)

            runs = connection.execute(
                "SELECT * FROM sleep_runs WHERE subject_id = ? ORDER BY started_at",
                (subject_id,),
            ).fetchall()
            for row in runs:
                run = self._sleep_run_from_row(row)
                transitions = connection.execute(
                    "SELECT * FROM sleep_transitions WHERE sleep_id = ? ORDER BY rowid",
                    (row["sleep_id"],),
                ).fetchall()
                expected_from: str | None = None
                for transition in transitions:
                    expected_hash = content_hash(
                        {
                            "from_status": transition["from_status"],
                            "to_status": transition["to_status"],
                            "reason": transition["reason"],
                        }
                    )
                    if (
                        transition["from_status"] != expected_from
                        or transition["state_hash"] != expected_hash
                    ):
                        raise IntegrityError(f"sleep transition mismatch: {row['sleep_id']}")
                    expected_from = transition["to_status"]
                if not transitions or expected_from != row["status"]:
                    raise IntegrityError(f"sleep current state mismatch: {row['sleep_id']}")

                reflection = connection.execute(
                    "SELECT * FROM sleep_reflections WHERE sleep_id = ?",
                    (row["sleep_id"],),
                ).fetchone()
                if row["reflection_event_id"] is None:
                    if reflection is not None:
                        raise IntegrityError(f"orphan sleep reflection: {row['sleep_id']}")
                else:
                    if reflection is None or reflection["event_id"] != row["reflection_event_id"]:
                        raise IntegrityError(f"sleep reflection mismatch: {row['sleep_id']}")
                    self._verify_reflection(reflection)
                    event = connection.execute(
                        "SELECT subject_id, event_type FROM events WHERE event_id = ?",
                        (reflection["event_id"],),
                    ).fetchone()
                    if (
                        event is None
                        or event["subject_id"] != subject_id
                        or event["event_type"] != "sleep_reflection"
                    ):
                        raise IntegrityError(f"sleep reflection event mismatch: {row['sleep_id']}")
                if row["status"] in {"deep_sleep", "waking", "complete"}:
                    checkpoint = connection.execute(
                        "SELECT subject_id FROM state_snapshots WHERE snapshot_id = ?",
                        (row["checkpoint_id"],),
                    ).fetchone()
                    if checkpoint is None or checkpoint["subject_id"] != subject_id:
                        raise IntegrityError(f"sleep checkpoint mismatch: {row['sleep_id']}")
                reflection_commits = 1 if row["reflection_event_id"] else 0
                if run.version != len(transitions) + reflection_commits:
                    raise IntegrityError(f"sleep version mismatch: {row['sleep_id']}")

                integrations = connection.execute(
                    "SELECT * FROM sleep_integrations WHERE sleep_id = ?",
                    (row["sleep_id"],),
                ).fetchall()
                for integration in integrations:
                    self._json_ids(
                        integration["source_ids_json"],
                        f"sleep integration {integration['integration_id']}",
                        allow_empty=False,
                    )
                    if len(integration["result_hash"]) != 64:
                        raise IntegrityError(
                            f"sleep integration hash invalid: {integration['integration_id']}"
                        )
            counts["sleep_runs"] = len(runs)
            counts["sleep_integrations"] = int(
                connection.execute(
                    """SELECT COUNT(*) FROM sleep_integrations
                       JOIN sleep_runs ON sleep_runs.sleep_id = sleep_integrations.sleep_id
                       WHERE sleep_runs.subject_id = ?""",
                    (subject_id,),
                ).fetchone()[0]
            )

            retry_blocks = connection.execute(
                "SELECT * FROM retry_blocks WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in retry_blocks:
                status = row["status"]
                if status not in {"active", "released"}:
                    raise IntegrityError(f"retry block status invalid: {row['block_id']}")
                event_boundary = self._persisted_integer(
                    row["evidence_event_boundary"],
                    f"retry block {row['block_id']} evidence_event_boundary",
                    minimum=0,
                )
                expected = SleepEngine._retry_hash(
                    row["goal_id"],
                    row["strategy_id"],
                    row["tool"],
                    row["target"],
                    row["reason"],
                    status,
                    row["release_evidence_event_id"],
                    event_boundary,
                )
                if expected != row["state_hash"]:
                    raise IntegrityError(f"retry block mismatch: {row['block_id']}")
                release_event_id = row["release_evidence_event_id"]
                if status == "released":
                    release_event = connection.execute(
                        "SELECT rowid, subject_id FROM events WHERE event_id = ?",
                        (release_event_id,),
                    ).fetchone()
                    if (
                        row["released_at"] is None
                        or not isinstance(release_event_id, str)
                        or release_event is None
                        or release_event["subject_id"] != subject_id
                        or self._persisted_integer(
                            release_event["rowid"],
                            f"retry block {row['block_id']} release event rowid",
                            minimum=1,
                        )
                        <= event_boundary
                    ):
                        raise IntegrityError(f"retry block release mismatch: {row['block_id']}")
                elif release_event_id is not None or row["released_at"] is not None:
                    raise IntegrityError(f"retry block release mismatch: {row['block_id']}")
            counts["retry_blocks"] = len(retry_blocks)

            candidates = connection.execute(
                "SELECT * FROM personality_candidates WHERE subject_id = ?",
                (subject_id,),
            ).fetchall()
            for row in candidates:
                evidence = self._json_ids(
                    row["evidence_ids_json"],
                    f"personality candidate {row['candidate_id']}",
                    allow_empty=False,
                )
                for evidence_id in evidence:
                    event = connection.execute(
                        "SELECT subject_id FROM events WHERE event_id = ?", (evidence_id,)
                    ).fetchone()
                    if event is None or event["subject_id"] != subject_id:
                        raise IntegrityError(
                            f"personality candidate evidence mismatch: {row['candidate_id']}"
                        )
                direction = self._persisted_float(
                    row["direction"],
                    f"personality candidate {row['candidate_id']} direction",
                    minimum=-1.0,
                    maximum=1.0,
                )
                confidence = self._persisted_float(
                    row["confidence"],
                    f"personality candidate {row['candidate_id']} confidence",
                    minimum=0.0,
                    maximum=1.0,
                )
                expected = content_hash(
                    {
                        "trait": row["trait"],
                        "direction": direction,
                        "confidence": confidence,
                        "evidence_ids": list(evidence),
                        "status": row["status"],
                    }
                )
                if expected != row["state_hash"]:
                    raise IntegrityError(f"personality candidate mismatch: {row['candidate_id']}")
            counts["personality_candidates"] = len(candidates)
        return counts

    @classmethod
    def _sleep_run_from_row(cls, row: Any) -> Any:
        sleep_id = str(row["sleep_id"])
        context = f"sleep run {sleep_id}"
        normalized = dict(row)
        normalized["emergency"] = cls._persisted_bool(row["emergency"], f"{context} emergency")
        normalized["pre_sleep_fatigue"] = cls._persisted_float(
            row["pre_sleep_fatigue"],
            f"{context} pre_sleep_fatigue",
            minimum=0.0,
            maximum=100.0,
        )
        normalized["version"] = cls._persisted_integer(
            row["version"], f"{context} version", minimum=1
        )
        try:
            return SleepEngine._from_row(normalized)
        except IntegrityError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} durable state is invalid") from error

    @classmethod
    def _fatigue_from_row(cls, row: Any) -> FatigueState:
        subject_id = str(row["subject_id"])
        context = f"fatigue state {subject_id}"
        mode = row["mode"]
        if not isinstance(mode, str) or mode not in {
            "active",
            "saving",
            "conservative",
            "winding_down",
            "sleeping",
        }:
            raise IntegrityError(f"{context} mode is invalid")
        normalized = {
            "subject_id": subject_id,
            "fatigue": cls._persisted_float(
                row["fatigue"], f"{context} fatigue", minimum=0.0, maximum=100.0
            ),
            "mode": mode,
            "resource_pressure": cls._persisted_float(
                row["resource_pressure"],
                f"{context} resource_pressure",
                minimum=0.0,
                maximum=1.0,
            ),
            "cognitive_load": cls._persisted_float(
                row["cognitive_load"],
                f"{context} cognitive_load",
                minimum=0.0,
                maximum=1.0,
            ),
            "frustration": cls._persisted_float(
                row["frustration"],
                f"{context} frustration",
                minimum=0.0,
                maximum=1.0,
            ),
            "goal_conflict": cls._persisted_float(
                row["goal_conflict"],
                f"{context} goal_conflict",
                minimum=0.0,
                maximum=1.0,
            ),
            "staleness": cls._persisted_float(
                row["staleness"],
                f"{context} staleness",
                minimum=0.0,
                maximum=1.0,
            ),
            "state_hash": row["state_hash"],
            "version": cls._persisted_integer(row["version"], f"{context} version", minimum=1),
            "updated_at": row["updated_at"],
        }
        try:
            return FatigueTracker._from_row(normalized)
        except FatigueStateError as error:
            raise IntegrityError(f"{context} mode is inconsistent") from error

    @staticmethod
    def _persisted_float(
        value: Any,
        context: str,
        *,
        minimum: float,
        maximum: float,
    ) -> float:
        try:
            parsed = strict_finite_float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if not minimum <= parsed <= maximum:
            raise IntegrityError(f"{context} is invalid")
        return parsed

    @staticmethod
    def _persisted_integer(value: Any, context: str, *, minimum: int) -> int:
        try:
            parsed = strict_int(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if parsed < minimum:
            raise IntegrityError(f"{context} is invalid")
        return parsed

    @staticmethod
    def _persisted_bool(value: Any, context: str) -> bool:
        try:
            return strict_bool(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error

    @staticmethod
    def _verify_reflection(row: Any) -> None:
        raw_plan = row["plan_json"]
        plan = SleepIntegrity._json_value(
            raw_plan, f"sleep reflection plan invalid: {row['reflection_id']}"
        )
        if not isinstance(plan, dict) or content_hash(plan) != row["plan_hash"]:
            raise IntegrityError(f"sleep reflection plan mismatch: {row['reflection_id']}")
        for name in (
            "facts_json",
            "contradictions_json",
            "prediction_errors_json",
            "unresolved_questions_json",
        ):
            SleepIntegrity._json_ids(
                row[name], f"sleep reflection {row['reflection_id']} {name}", allow_empty=True
            )
        if not row["summary"].strip() or len(row["plan_hash"]) != 64:
            raise IntegrityError(f"sleep reflection content invalid: {row['reflection_id']}")
        if (
            plan.get("summary") != row["summary"]
            or plan.get("facts")
            != SleepIntegrity._json_value(
                row["facts_json"], f"sleep reflection facts invalid: {row['reflection_id']}"
            )
            or plan.get("contradictions")
            != SleepIntegrity._json_value(
                row["contradictions_json"],
                f"sleep reflection contradictions invalid: {row['reflection_id']}",
            )
            or plan.get("prediction_errors")
            != SleepIntegrity._json_value(
                row["prediction_errors_json"],
                f"sleep reflection prediction errors invalid: {row['reflection_id']}",
            )
            or plan.get("unresolved_questions")
            != SleepIntegrity._json_value(
                row["unresolved_questions_json"],
                f"sleep reflection unresolved questions invalid: {row['reflection_id']}",
            )
            or plan.get("public_diary_candidate") != row["public_diary_candidate"]
        ):
            raise IntegrityError(f"sleep reflection projection mismatch: {row['reflection_id']}")

    @staticmethod
    def _json_ids(value: object, context: str, *, allow_empty: bool) -> tuple[str, ...]:
        decoded = SleepIntegrity._json_value(value, f"{context} JSON is invalid")
        if (
            not isinstance(decoded, list)
            or (not allow_empty and not decoded)
            or not all(isinstance(item, str) for item in decoded)
            or len(set(decoded)) != len(decoded)
        ):
            raise IntegrityError(f"{context} list is invalid")
        return tuple(decoded)

    @staticmethod
    def _json_value(value: object, message: str) -> Any:
        if not isinstance(value, str):
            raise IntegrityError(message)
        try:
            return strict_json_loads(value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise IntegrityError(message) from error
