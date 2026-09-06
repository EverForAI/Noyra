from __future__ import annotations

import json
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .errors import GenesisProtocolError
from .types import GenesisCycleRecord, GenesisRunRecord

GENESIS_TRANSITIONS: dict[str, frozenset[str]] = {
    "created": frozenset({"observing", "failed"}),
    "observing": frozenset({"interpreting", "failed"}),
    "interpreting": frozenset({"observing", "forecasting", "failed"}),
    "forecasting": frozenset({"observing", "goal_seeding", "failed"}),
    "goal_seeding": frozenset({"observing", "ready_for_sleep", "failed"}),
    "ready_for_sleep": frozenset({"complete", "failed"}),
    "complete": frozenset(),
    "failed": frozenset(),
}


class GenesisProtocol:
    def __init__(self, database: Database):
        self.database = database

    def start(self, subject_id: str, *, minimum_cycles: int = 3) -> GenesisRunRecord:
        if not 1 <= minimum_cycles <= 100:
            raise ValueError("genesis minimum cycles must be 1-100")
        with self.database.transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM genesis_runs WHERE subject_id = ? AND status != 'failed'",
                (subject_id,),
            ).fetchone()
            if existing is not None:
                return self._from_row(existing)
            run_id = new_id("gen")
            now = utc_now()
            state_hash = self._state_hash("created", minimum_cycles, 0, None, 1)
            connection.execute(
                """INSERT INTO genesis_runs(
                    run_id, subject_id, status, minimum_cycles, completed_cycles,
                    sleep_reference, state_hash, version, created_at, updated_at
                ) VALUES (?, ?, 'created', ?, 0, NULL, ?, 1, ?, ?)""",
                (run_id, subject_id, minimum_cycles, state_hash, now, now),
            )
            return self._load_connection(connection, run_id)

    def transition(
        self,
        run_id: str,
        to_status: str,
        reason: str,
        *,
        sleep_reference: str | None = None,
    ) -> GenesisRunRecord:
        if not reason.strip() or len(reason) > 10_000:
            raise ValueError("genesis transition reason is invalid")
        with self.database.transaction() as connection:
            row = self._get_row(connection, run_id)
            current = row["status"]
            if to_status not in GENESIS_TRANSITIONS[current]:
                raise GenesisProtocolError(f"cannot transition genesis {current} -> {to_status}")
            cycles = int(row["completed_cycles"])
            if to_status == "ready_for_sleep" and cycles < int(row["minimum_cycles"]):
                raise GenesisProtocolError("genesis has not completed its minimum cycles")
            if to_status == "complete" and not (sleep_reference and sleep_reference.strip()):
                raise GenesisProtocolError("genesis completion requires a sleep reference")
            version = int(row["version"]) + 1
            now = utc_now()
            resolved_sleep = sleep_reference or row["sleep_reference"]
            state_hash = self._state_hash(
                to_status,
                int(row["minimum_cycles"]),
                cycles,
                resolved_sleep,
                version,
            )
            connection.execute(
                """UPDATE genesis_runs SET status = ?, sleep_reference = ?,
                    state_hash = ?, version = ?, updated_at = ? WHERE run_id = ?""",
                (to_status, resolved_sleep, state_hash, version, now, run_id),
            )
            transition_hash = self._transition_hash(current, to_status, reason)
            connection.execute(
                """INSERT INTO genesis_transitions(
                    transition_id, run_id, from_status, to_status, reason,
                    state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("gtr"),
                    run_id,
                    current,
                    to_status,
                    reason,
                    transition_hash,
                    now,
                ),
            )
            return self._load_connection(connection, run_id)

    def record_cycle(
        self,
        run_id: str,
        cycle_number: int,
        *,
        observation_ids: tuple[str, ...],
        appraisal_ids: tuple[str, ...],
        prediction_ids: tuple[str, ...],
        goal_ids: tuple[str, ...] = (),
        summary: str,
    ) -> GenesisCycleRecord:
        if cycle_number < 1 or not summary.strip() or len(summary) > 20_000:
            raise ValueError("genesis cycle number or summary is invalid")
        normalized_observations = tuple(dict.fromkeys(observation_ids))
        normalized_appraisals = tuple(dict.fromkeys(appraisal_ids))
        normalized_predictions = tuple(dict.fromkeys(prediction_ids))
        normalized_goals = tuple(dict.fromkeys(goal_ids))
        payload = {
            "cycle_number": cycle_number,
            "observation_ids": list(normalized_observations),
            "appraisal_ids": list(normalized_appraisals),
            "prediction_ids": list(normalized_predictions),
            "goal_ids": list(normalized_goals),
            "summary": summary,
        }
        state_hash = content_hash(payload)
        with self.database.transaction() as connection:
            run = self._get_row(connection, run_id)
            existing = connection.execute(
                "SELECT * FROM genesis_cycles WHERE run_id = ? AND cycle_number = ?",
                (run_id, cycle_number),
            ).fetchone()
            if existing is not None:
                if existing["state_hash"] != state_hash:
                    raise GenesisProtocolError(
                        "genesis cycle number already has different evidence"
                    )
                return self._cycle_from_row(existing)
            if run["status"] != "goal_seeding":
                raise GenesisProtocolError("genesis cycle can commit only after goal seeding")
            expected_cycle = int(run["completed_cycles"]) + 1
            if cycle_number != expected_cycle:
                raise GenesisProtocolError(
                    f"expected genesis cycle {expected_cycle}, received {cycle_number}"
                )
            subject_id = run["subject_id"]
            observations = self._validate_ids(
                connection,
                "observations",
                "observation_id",
                subject_id,
                normalized_observations,
                required=True,
            )
            prior_cycles = connection.execute(
                "SELECT observation_ids_json FROM genesis_cycles WHERE run_id = ?",
                (run_id,),
            ).fetchall()
            prior_observations = {
                observation_id
                for prior in prior_cycles
                for observation_id in json.loads(prior["observation_ids_json"])
            }
            if prior_observations.intersection(observations):
                raise GenesisProtocolError(
                    "each genesis cycle requires at least one new observation set"
                )
            appraisals = self._validate_ids(
                connection,
                "appraisals",
                "appraisal_id",
                subject_id,
                normalized_appraisals,
                required=True,
            )
            predictions = self._validate_ids(
                connection,
                "predictions",
                "prediction_id",
                subject_id,
                normalized_predictions,
                required=True,
            )
            goals = self._validate_ids(
                connection,
                "goals",
                "goal_id",
                subject_id,
                normalized_goals,
                required=False,
            )
            cycle_id = new_id("gcy")
            now = utc_now()
            connection.execute(
                """INSERT INTO genesis_cycles(
                    cycle_id, run_id, cycle_number, observation_ids_json,
                    appraisal_ids_json, prediction_ids_json, goal_ids_json,
                    summary, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    cycle_id,
                    run_id,
                    cycle_number,
                    canonical_json(list(observations)),
                    canonical_json(list(appraisals)),
                    canonical_json(list(predictions)),
                    canonical_json(list(goals)),
                    summary,
                    state_hash,
                    now,
                ),
            )
            version = int(run["version"]) + 1
            run_hash = self._state_hash(
                run["status"],
                int(run["minimum_cycles"]),
                cycle_number,
                run["sleep_reference"],
                version,
            )
            connection.execute(
                """UPDATE genesis_runs SET completed_cycles = ?, state_hash = ?,
                    version = ?, updated_at = ? WHERE run_id = ?""",
                (cycle_number, run_hash, version, now, run_id),
            )
            row = connection.execute(
                "SELECT * FROM genesis_cycles WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
            return self._cycle_from_row(row)

    def get(self, run_id: str) -> GenesisRunRecord:
        with self.database.connection() as connection:
            return self._load_connection(connection, run_id)

    def cycles(self, run_id: str) -> list[GenesisCycleRecord]:
        with self.database.connection() as connection:
            self._get_row(connection, run_id)
            rows = connection.execute(
                "SELECT * FROM genesis_cycles WHERE run_id = ? ORDER BY cycle_number",
                (run_id,),
            ).fetchall()
        return [self._cycle_from_row(row) for row in rows]

    @staticmethod
    def _validate_ids(
        connection: Any,
        table: str,
        key: str,
        subject_id: str,
        values: tuple[str, ...],
        *,
        required: bool,
    ) -> tuple[str, ...]:
        ids = tuple(dict.fromkeys(values))
        if required and not ids:
            raise GenesisProtocolError(f"genesis cycle requires {table}")
        if len(ids) > 64:
            raise GenesisProtocolError(f"genesis cycle has too many {table}")
        if not ids:
            return ()
        placeholders = ",".join("?" for _ in ids)
        rows = connection.execute(
            f"SELECT {key} FROM {table} WHERE subject_id = ? AND {key} IN ({placeholders})",
            (subject_id, *ids),
        ).fetchall()
        if {row[key] for row in rows} != set(ids):
            raise GenesisProtocolError(f"genesis {table} are missing or belong to another subject")
        return ids

    @staticmethod
    def _state_hash(
        status: str,
        minimum_cycles: int,
        completed_cycles: int,
        sleep_reference: str | None,
        version: int,
    ) -> str:
        return content_hash(
            {
                "status": status,
                "minimum_cycles": minimum_cycles,
                "completed_cycles": completed_cycles,
                "sleep_reference": sleep_reference,
                "version": version,
            }
        )

    @staticmethod
    def _transition_hash(from_status: str, to_status: str, reason: str) -> str:
        return content_hash({"from_status": from_status, "to_status": to_status, "reason": reason})

    @staticmethod
    def _get_row(connection: Any, run_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM genesis_runs WHERE run_id = ?", (run_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"genesis run not found: {run_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, run_id: str) -> GenesisRunRecord:
        return cls._from_row(cls._get_row(connection, run_id))

    @classmethod
    def _from_row(cls, row: Any) -> GenesisRunRecord:
        expected = cls._state_hash(
            row["status"],
            int(row["minimum_cycles"]),
            int(row["completed_cycles"]),
            row["sleep_reference"],
            int(row["version"]),
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"genesis run hash mismatch: {row['run_id']}")
        return GenesisRunRecord(
            row["run_id"],
            row["subject_id"],
            row["status"],
            int(row["minimum_cycles"]),
            int(row["completed_cycles"]),
            row["sleep_reference"],
            row["state_hash"],
            int(row["version"]),
            row["created_at"],
            row["updated_at"],
        )

    @staticmethod
    def _cycle_from_row(row: Any) -> GenesisCycleRecord:
        observations = tuple(json.loads(row["observation_ids_json"]))
        appraisals = tuple(json.loads(row["appraisal_ids_json"]))
        predictions = tuple(json.loads(row["prediction_ids_json"]))
        goals = tuple(json.loads(row["goal_ids_json"]))
        expected = content_hash(
            {
                "cycle_number": int(row["cycle_number"]),
                "observation_ids": list(observations),
                "appraisal_ids": list(appraisals),
                "prediction_ids": list(predictions),
                "goal_ids": list(goals),
                "summary": row["summary"],
            }
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"genesis cycle hash mismatch: {row['cycle_id']}")
        return GenesisCycleRecord(
            row["cycle_id"],
            row["run_id"],
            int(row["cycle_number"]),
            observations,
            appraisals,
            predictions,
            goals,
            row["summary"],
            row["state_hash"],
            row["created_at"],
        )
