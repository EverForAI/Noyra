from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

from .errors import FatigueStateError
from .types import FatigueInputs, FatigueState


class FatigueTracker:
    """Durable fatigue derived from hard resource pressure and cognitive strain."""

    def __init__(self, database: Database):
        self.database = database

    def ensure(self, subject_id: str) -> FatigueState:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if row is not None:
                return self._from_row(row)
            now = utc_now()
            state_hash = self._state_hash(
                0,
                "active",
                FatigueInputs.model_validate(
                    {
                        "resource_pressure": 0.0,
                        "cognitive_load": 0.0,
                        "frustration": 0.0,
                        "goal_conflict": 0.0,
                        "staleness": 0.0,
                    }
                ),
            )
            connection.execute(
                """INSERT INTO fatigue_states(
                    subject_id, fatigue, mode, resource_pressure, cognitive_load,
                    frustration, goal_conflict, staleness, state_hash, version, updated_at
                ) VALUES (?, 0, 'active', 0, 0, 0, 0, 0, ?, 1, ?)""",
                (subject_id, state_hash, now),
            )
            return self._load_connection(connection, subject_id)

    def assess(self, subject_id: str, inputs: FatigueInputs, *, reason: str) -> FatigueState:
        if not reason.strip() or len(reason) > 10_000:
            raise ValueError("fatigue assessment reason is invalid")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if row is None:
                self._insert_initial(connection, subject_id)
                row = connection.execute(
                    "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
                ).fetchone()
            old = float(row["fatigue"])
            instantaneous = self._instantaneous(inputs)
            fatigue = max(old * 0.96, instantaneous)
            if inputs.resource_pressure >= 1 and self._all_configured_pools_exhausted(
                connection, subject_id
            ):
                fatigue = 100.0
            mode = self.mode_for(fatigue)
            version = int(row["version"]) + 1
            now = utc_now()
            state_hash = self._state_hash(fatigue, mode, inputs)
            connection.execute(
                """UPDATE fatigue_states SET fatigue = ?, mode = ?, resource_pressure = ?,
                    cognitive_load = ?, frustration = ?, goal_conflict = ?, staleness = ?,
                    state_hash = ?, version = ?, updated_at = ? WHERE subject_id = ?""",
                (
                    fatigue,
                    mode,
                    inputs.resource_pressure,
                    inputs.cognitive_load,
                    inputs.frustration,
                    inputs.goal_conflict,
                    inputs.staleness,
                    state_hash,
                    version,
                    now,
                    subject_id,
                ),
            )
            connection.execute(
                """INSERT INTO fatigue_transitions(
                    transition_id, subject_id, old_fatigue, new_fatigue, mode,
                    resource_pressure, cognitive_load, frustration, goal_conflict,
                    staleness, reason, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("fat"),
                    subject_id,
                    old,
                    fatigue,
                    mode,
                    inputs.resource_pressure,
                    inputs.cognitive_load,
                    inputs.frustration,
                    inputs.goal_conflict,
                    inputs.staleness,
                    reason,
                    state_hash,
                    now,
                ),
            )
            return self._load_connection(connection, subject_id)

    def restore_after_sleep(self, subject_id: str, *, hours: float, reason: str) -> FatigueState:
        if hours < 0 or hours > 168 or not reason.strip():
            raise ValueError("sleep duration or restoration reason is invalid")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if row is None:
                self._insert_initial(connection, subject_id)
                row = connection.execute(
                    "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
                ).fetchone()
            old = float(row["fatigue"])
            fatigue = max(0.0, old - min(100.0, hours * 18.0))
            inputs = FatigueInputs(
                resource_pressure=float(row["resource_pressure"]),
                cognitive_load=0.0,
                frustration=0.0,
                goal_conflict=0.0,
                staleness=0.0,
            )
            mode = self.mode_for(fatigue)
            version = int(row["version"]) + 1
            now = utc_now()
            state_hash = self._state_hash(fatigue, mode, inputs)
            connection.execute(
                """UPDATE fatigue_states SET fatigue = ?, mode = ?, cognitive_load = 0,
                    frustration = 0, goal_conflict = 0, staleness = 0, state_hash = ?,
                    version = ?, updated_at = ? WHERE subject_id = ?""",
                (fatigue, mode, state_hash, version, now, subject_id),
            )
            connection.execute(
                """INSERT INTO fatigue_transitions(
                    transition_id, subject_id, old_fatigue, new_fatigue, mode,
                    resource_pressure, cognitive_load, frustration, goal_conflict,
                    staleness, reason, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("fat"),
                    subject_id,
                    old,
                    fatigue,
                    mode,
                    inputs.resource_pressure,
                    0,
                    0,
                    0,
                    0,
                    reason,
                    state_hash,
                    now,
                ),
            )
            return self._load_connection(connection, subject_id)

    def get(self, subject_id: str) -> FatigueState:
        with self.database.connection() as connection:
            return self._load_connection(connection, subject_id)

    def should_sleep(self, subject_id: str) -> tuple[bool, tuple[str, ...]]:
        state = self.get(subject_id)
        reasons: list[str] = []
        if state.fatigue >= 90:
            reasons.append("fatigue_threshold")
        if state.resource_pressure >= 1 and self._all_configured_pools_exhausted_for_subject(
            subject_id
        ):
            reasons.append("hard_resource_budget_exhausted")
        if state.frustration >= 0.85:
            reasons.append("repeated_failure_pressure")
        if state.goal_conflict >= 0.85:
            reasons.append("goal_conflict_pressure")
        if state.staleness >= 0.9:
            reasons.append("information_staleness")
        return bool(reasons), tuple(reasons)

    def set_pool_pressures(self, subject_id: str, pressures: dict[str, float]) -> None:
        """Persist independent resource pressure signals.

        The legacy ``fatigue_states.resource_pressure`` remains for backwards
        compatibility, but sleep is triggered by it only when every configured
        cognitive pool is exhausted.  This prevents an economy outage from
        disabling deep cognition (and vice versa).
        """
        now = utc_now()
        allowed = {"economy", "deep", "search", "browser", "embedding"}
        with self.database.transaction() as connection:
            for pool, value in pressures.items():
                if pool not in allowed:
                    raise ValueError(f"unknown resource pool: {pool}")
                bounded = max(0.0, min(1.0, float(value)))
                connection.execute(
                    "INSERT INTO resource_pool_pressures(subject_id, pool, pressure, updated_at) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(subject_id, pool) DO UPDATE SET "
                    "pressure = excluded.pressure, updated_at = excluded.updated_at",
                    (subject_id, pool, bounded, now),
                )

    def pool_pressures(self, subject_id: str) -> dict[str, float]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT pool, pressure FROM resource_pool_pressures WHERE subject_id = ?",
                (subject_id,),
            ).fetchall()
        return {str(row["pool"]): float(row["pressure"]) for row in rows}

    def _all_configured_pools_exhausted_for_subject(self, subject_id: str) -> bool:
        with self.database.connection() as connection:
            return self._all_configured_pools_exhausted(connection, subject_id)

    @staticmethod
    def _all_configured_pools_exhausted(connection: Any, subject_id: str) -> bool:
        rows = connection.execute(
            "SELECT pressure FROM resource_pool_pressures WHERE subject_id = ?",
            (subject_id,),
        ).fetchall()
        return not rows or all(float(row["pressure"]) >= 1.0 for row in rows)

    @staticmethod
    def mode_for(fatigue: float) -> str:
        if fatigue >= 100:
            return "sleeping"
        if fatigue >= 90:
            return "winding_down"
        if fatigue >= 70:
            return "conservative"
        if fatigue >= 40:
            return "saving"
        return "active"

    @staticmethod
    def _instantaneous(inputs: FatigueInputs) -> float:
        return 100.0 * (
            0.45 * inputs.resource_pressure
            + 0.20 * inputs.cognitive_load
            + 0.20 * inputs.frustration
            + 0.10 * inputs.goal_conflict
            + 0.05 * inputs.staleness
        )

    @staticmethod
    def _state_hash(fatigue: float, mode: str, inputs: FatigueInputs) -> str:
        return content_hash({"fatigue": float(fatigue), "mode": mode, **inputs.model_dump()})

    @classmethod
    def _from_row(cls, row: Any) -> FatigueState:
        inputs = FatigueInputs(
            resource_pressure=float(row["resource_pressure"]),
            cognitive_load=float(row["cognitive_load"]),
            frustration=float(row["frustration"]),
            goal_conflict=float(row["goal_conflict"]),
            staleness=float(row["staleness"]),
        )
        if cls._state_hash(float(row["fatigue"]), row["mode"], inputs) != row["state_hash"]:
            raise IntegrityError(f"fatigue state hash mismatch: {row['subject_id']}")
        if row["mode"] != cls.mode_for(float(row["fatigue"])):
            raise FatigueStateError(f"fatigue mode mismatch: {row['subject_id']}")
        return FatigueState(
            row["subject_id"],
            float(row["fatigue"]),
            row["mode"],
            inputs.resource_pressure,
            inputs.cognitive_load,
            inputs.frustration,
            inputs.goal_conflict,
            inputs.staleness,
            int(row["version"]),
            row["updated_at"],
        )

    @classmethod
    def _load_connection(cls, connection: Any, subject_id: str) -> FatigueState:
        row = connection.execute(
            "SELECT * FROM fatigue_states WHERE subject_id = ?", (subject_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"fatigue state not found: {subject_id}")
        return cls._from_row(row)

    @staticmethod
    def _insert_initial(connection: Any, subject_id: str) -> None:
        inputs = FatigueInputs(
            resource_pressure=0.0,
            cognitive_load=0.0,
            frustration=0.0,
            goal_conflict=0.0,
            staleness=0.0,
        )
        now = utc_now()
        connection.execute(
            """INSERT OR IGNORE INTO fatigue_states(
                subject_id, fatigue, mode, resource_pressure, cognitive_load,
                frustration, goal_conflict, staleness, state_hash, version, updated_at
            ) VALUES (?, 0, 'active', 0, 0, 0, 0, 0, ?, 1, ?)""",
            (subject_id, FatigueTracker._state_hash(0, "active", inputs), now),
        )
