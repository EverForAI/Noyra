from __future__ import annotations

from typing import Any

from .database import Database, action_state_hash, behavior_log_state_hash
from .errors import DuplicateActionError, IntegrityError, InvalidTransitionError, NotFoundError
from .types import (
    ActionRecord,
    canonical_json,
    content_hash,
    new_id,
    strict_bool,
    strict_int,
    strict_json_loads,
    utc_now,
)

ACTION_STATES = frozenset({"prepared", "executing", "succeeded", "failed", "unknown", "cancelled"})
EXECUTOR_COMPLETION_STATES = frozenset({"succeeded", "failed", "unknown", "cancelled"})
RECONCILIATION_STATES = frozenset({"succeeded", "failed", "cancelled"})
PRIVATE_PUBLIC_TARGET = "[private]"


class ActionLedger:
    """Durable action intent and completion records with at-most-once recovery semantics."""

    def __init__(self, database: Database):
        self.database = database

    def prepare(
        self,
        subject_id: str,
        action_type: str,
        tool: str,
        target: str,
        input_payload: dict[str, Any],
        *,
        goal_id: str | None = None,
        project_id: str | None = None,
        phase_id: str | None = None,
        strategy_id: str | None = None,
        expected_outcome: str = "",
        side_effect: bool = False,
        resource_cost: dict[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> ActionRecord:
        if not action_type.strip() or not tool.strip() or not target.strip():
            raise ValueError("action_type, tool and target are required")
        if idempotency_key is not None and not idempotency_key.strip():
            raise ValueError("idempotency_key cannot be blank")
        resource_cost = resource_cost or {}
        input_hash = content_hash(input_payload)
        idempotency_key = idempotency_key or content_hash(
            {
                "subject_id": subject_id,
                "action_type": action_type,
                "tool": tool,
                "target": target,
                "input": input_payload,
            }
        )
        with self.database.transaction() as connection:
            if goal_id is not None:
                goal = connection.execute(
                    "SELECT subject_id FROM goals WHERE goal_id = ?", (goal_id,)
                ).fetchone()
                if goal is None or goal["subject_id"] != subject_id:
                    raise PermissionError("action goal is missing or belongs to another subject")
            if project_id is not None:
                project = connection.execute(
                    "SELECT subject_id FROM autonomous_projects WHERE project_id = ?",
                    (project_id,),
                ).fetchone()
                if project is None or project["subject_id"] != subject_id:
                    raise PermissionError("action project is missing or belongs to another subject")
            if phase_id is not None:
                phase = connection.execute(
                    "SELECT p.subject_id, ph.project_id FROM autonomous_project_phases ph "
                    "JOIN autonomous_projects p ON p.project_id = ph.project_id "
                    "WHERE ph.phase_id = ?",
                    (phase_id,),
                ).fetchone()
                if (
                    phase is None
                    or phase["subject_id"] != subject_id
                    or (project_id is not None and phase["project_id"] != project_id)
                ):
                    raise PermissionError("action phase is missing or outside the subject project")
            lifecycle = connection.execute(
                "SELECT state FROM runtime_state WHERE subject_id = ?", (subject_id,)
            ).fetchone()
            if lifecycle is not None and lifecycle["state"] in {
                "winding_down",
                "reflective_sleep",
                "deep_sleep",
                "waking",
            }:
                raise InvalidTransitionError(
                    f"new actions are disabled while lifecycle is {lifecycle['state']}"
                )
            blocked = connection.execute(
                """SELECT 1 FROM retry_blocks
                   WHERE subject_id = ? AND status = 'active' AND tool = ? AND target = ?
                   AND (goal_id IS NULL OR goal_id = ?)
                   AND (strategy_id IS NULL OR strategy_id = ?) LIMIT 1""",
                (subject_id, tool, target, goal_id, strategy_id),
            ).fetchone()
            if blocked is not None:
                raise InvalidTransitionError(
                    "this strategy is blocked until genuinely new evidence is recorded"
                )
            existing = connection.execute(
                "SELECT * FROM actions WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                if (
                    existing["action_type"] != action_type
                    or existing["tool"] != tool
                    or existing["target"] != target
                    or existing["input_hash"] != input_hash
                    or bool(existing["side_effect"]) != side_effect
                    or existing["project_id"] != project_id
                    or existing["phase_id"] != phase_id
                ):
                    raise DuplicateActionError(
                        "idempotency key already identifies a different action"
                    )
                return self._from_row(existing)

            action_id = new_id("act")
            prepared_at = utc_now()
            connection.execute(
                """INSERT INTO actions(
                    action_id, subject_id, goal_id, project_id, phase_id, strategy_id,
                    action_type, tool, target,
                    input_hash, idempotency_key, expected_outcome, side_effect, status,
                    retry_count, resource_cost_json, result_json, prepared_at,
                    started_at, completed_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prepared', 0, ?, NULL, ?, NULL, NULL
                )""",
                (
                    action_id,
                    subject_id,
                    goal_id,
                    project_id,
                    phase_id,
                    strategy_id,
                    action_type,
                    tool,
                    target,
                    input_hash,
                    idempotency_key,
                    expected_outcome,
                    int(side_effect),
                    canonical_json(resource_cost),
                    prepared_at,
                ),
            )
            self._record_revision(connection, action_id, "prepared")
            return self._load_connection(connection, action_id)

    def start(self, action_id: str) -> ActionRecord:
        with self.database.transaction() as connection:
            row = self._get_row(connection, action_id)
            if row["status"] != "prepared":
                raise InvalidTransitionError(f"cannot start action in state {row['status']}")
            connection.execute(
                "UPDATE actions SET status = 'executing', retry_count = retry_count + 1, "
                "started_at = ?, completed_at = NULL WHERE action_id = ?",
                (utc_now(), action_id),
            )
            self._record_revision(connection, action_id, "started")
            return self._load_connection(connection, action_id)

    def finish(
        self,
        action_id: str,
        status: str,
        result: dict[str, Any],
        *,
        public_goal_reference: str | None = None,
        public_target: str | None = None,
        side_effect_summary: str = "none",
        resource_summary: str = "none",
        public_explanation: str = "",
        redaction_reason: str | None = None,
    ) -> ActionRecord:
        if status not in EXECUTOR_COMPLETION_STATES:
            raise ValueError(f"invalid action completion status: {status}")
        with self.database.transaction() as connection:
            row = self._get_row(connection, action_id)
            if row["status"] != "executing":
                raise InvalidTransitionError(f"cannot finish action in state {row['status']}")
            completed_at = utc_now()
            connection.execute(
                "UPDATE actions SET status = ?, result_json = ?, completed_at = ? "
                "WHERE action_id = ?",
                (status, canonical_json(result), completed_at, action_id),
            )
            self._insert_behavior_log(
                connection,
                row,
                completed_at=completed_at,
                result_status=status,
                public_goal_reference=public_goal_reference,
                public_target=public_target,
                side_effect_summary=side_effect_summary,
                resource_summary=resource_summary,
                public_explanation=public_explanation,
                redaction_reason=redaction_reason,
            )
            self._record_revision(connection, action_id, "finished")
            return self._load_connection(connection, action_id)

    def cancel(self, action_id: str, reason: str) -> ActionRecord:
        if not reason.strip():
            raise ValueError("cancellation reason is required")
        with self.database.transaction() as connection:
            row = self._get_row(connection, action_id)
            if row["status"] != "prepared":
                raise InvalidTransitionError(f"cannot cancel action in state {row['status']}")
            completed_at = utc_now()
            connection.execute(
                "UPDATE actions SET status = 'cancelled', result_json = ?, completed_at = ? "
                "WHERE action_id = ?",
                (canonical_json({"reason": reason}), completed_at, action_id),
            )
            self._insert_behavior_log(
                connection,
                row,
                completed_at=completed_at,
                result_status="cancelled",
                public_goal_reference=None,
                public_target=None,
                side_effect_summary="none",
                resource_summary="none",
                public_explanation=reason,
                redaction_reason=None,
            )
            self._record_revision(connection, action_id, "cancelled")
            return self._load_connection(connection, action_id)

    def recover_interrupted(self, subject_id: str) -> list[ActionRecord]:
        """Quarantine interrupted actions as unknown so they cannot execute twice."""
        recovered: list[ActionRecord] = []
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM actions WHERE subject_id = ? AND status = 'executing' "
                "ORDER BY prepared_at, action_id",
                (subject_id,),
            ).fetchall()
            for row in rows:
                completed_at = utc_now()
                may_have_side_effect = bool(row["side_effect"])
                result = {
                    "recovery": "process ended before completion was durably recorded",
                    "side_effect_may_have_occurred": may_have_side_effect,
                }
                connection.execute(
                    "UPDATE actions SET status = 'unknown', result_json = ?, completed_at = ? "
                    "WHERE action_id = ? AND status = 'executing'",
                    (canonical_json(result), completed_at, row["action_id"]),
                )
                self._insert_behavior_log(
                    connection,
                    row,
                    completed_at=completed_at,
                    result_status="unknown",
                    public_goal_reference=None,
                    public_target=None,
                    side_effect_summary=(
                        "completion unknown; side effect may have occurred"
                        if may_have_side_effect
                        else "completion unknown"
                    ),
                    resource_summary="unavailable after interruption",
                    public_explanation="Action outcome requires reconciliation after restart.",
                    redaction_reason=None,
                )
                self._record_revision(connection, row["action_id"], "recovered")
                recovered.append(self._load_connection(connection, row["action_id"]))
        return recovered

    def reconcile_unknown(
        self,
        action_id: str,
        status: str,
        result: dict[str, Any],
        *,
        public_explanation: str = "Action outcome was reconciled.",
        side_effect_summary: str | None = None,
    ) -> ActionRecord:
        if status not in RECONCILIATION_STATES:
            raise ValueError(f"invalid reconciliation status: {status}")
        with self.database.transaction() as connection:
            row = self._get_row(connection, action_id)
            if row["status"] != "unknown":
                raise InvalidTransitionError(f"cannot reconcile action in state {row['status']}")
            log = connection.execute(
                "SELECT * FROM behavior_logs WHERE action_id = ?", (action_id,)
            ).fetchone()
            if log is None:
                raise NotFoundError(f"behavior log not found for action: {action_id}")
            latest = connection.execute(
                "SELECT * FROM behavior_log_revisions WHERE log_id = ? "
                "ORDER BY revision_number DESC LIMIT 1",
                (log["log_id"],),
            ).fetchone()
            if latest is None:
                raise NotFoundError(f"behavior log revision not found for action: {action_id}")
            completed_at = utc_now()
            connection.execute(
                "UPDATE actions SET status = ?, result_json = ?, completed_at = ? "
                "WHERE action_id = ?",
                (status, canonical_json(result), completed_at, action_id),
            )
            self._append_behavior_log_revision(
                connection,
                latest,
                occurred_at=completed_at,
                result_status=status,
                side_effect_summary=(
                    latest["side_effect_summary"]
                    if side_effect_summary is None
                    else side_effect_summary
                ),
                public_explanation=public_explanation,
                reason="unknown outcome reconciled",
            )
            self._record_revision(connection, action_id, "reconciled")
            return self._load_connection(connection, action_id)

    def recoverable(self, subject_id: str) -> list[ActionRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM actions WHERE subject_id = ? "
                "AND status IN ('prepared', 'unknown') ORDER BY prepared_at, action_id",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        """Verify action state, revision history, and behavior-log ownership."""
        with self.database.read_transaction() as connection:
            actions = connection.execute(
                "SELECT * FROM actions WHERE subject_id = ? ORDER BY prepared_at, action_id",
                (subject_id,),
            ).fetchall()
            revisions = connection.execute(
                "SELECT * FROM action_revisions WHERE subject_id = ? "
                "ORDER BY action_id, revision_number",
                (subject_id,),
            ).fetchall()
            logs = connection.execute(
                "SELECT * FROM behavior_logs WHERE subject_id = ? ORDER BY occurred_at, log_id",
                (subject_id,),
            ).fetchall()
            behavior_revisions = connection.execute(
                "SELECT * FROM behavior_log_revisions WHERE subject_id = ? "
                "ORDER BY log_id, revision_number",
                (subject_id,),
            ).fetchall()

        revisions_by_action: dict[str, list[Any]] = {}
        for revision in revisions:
            revisions_by_action.setdefault(str(revision["action_id"]), []).append(revision)
        logs_by_action: dict[str, list[Any]] = {}
        for log in logs:
            logs_by_action.setdefault(str(log["action_id"]), []).append(log)
        revisions_by_log: dict[str, list[Any]] = {}
        for revision in behavior_revisions:
            revisions_by_log.setdefault(str(revision["log_id"]), []).append(revision)
        actions_by_id = {str(action["action_id"]): action for action in actions}

        terminal = EXECUTOR_COMPLETION_STATES
        for action in actions:
            action_id = str(action["action_id"])
            self._from_row(action)
            history = revisions_by_action.get(action_id, [])
            if not history:
                raise IntegrityError(f"action revision history is missing: {action_id}")
            for expected_revision, revision in enumerate(history, start=1):
                try:
                    revision_number = strict_int(revision["revision_number"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"action revision sequence is invalid: {action_id}"
                    ) from error
                if revision_number != expected_revision:
                    raise IntegrityError(f"action revision sequence mismatch: {action_id}")
                if revision["subject_id"] != subject_id or revision["status"] not in ACTION_STATES:
                    raise IntegrityError(f"action revision ownership mismatch: {action_id}")
            latest = history[-1]
            if latest["status"] != action["status"] or latest["state_hash"] != action_state_hash(
                action
            ):
                raise IntegrityError(f"action revision hash mismatch: {action_id}")

            action_logs = logs_by_action.get(action_id, [])
            if action["status"] in terminal and len(action_logs) != 1:
                raise IntegrityError(f"terminal action behavior log mismatch: {action_id}")
            if action["status"] not in terminal and action_logs:
                raise IntegrityError(f"nonterminal action has behavior log: {action_id}")
            if action_logs:
                log = action_logs[0]
                history = revisions_by_log.get(str(log["log_id"]), [])
                if not history:
                    raise IntegrityError(f"behavior log revision history is missing: {action_id}")
                if len(history) > 2:
                    raise IntegrityError(f"behavior log revision count is invalid: {action_id}")
                for expected_revision, revision in enumerate(history, start=1):
                    try:
                        revision_number = strict_int(revision["revision_number"])
                    except (TypeError, ValueError) as error:
                        raise IntegrityError(
                            f"behavior log revision sequence is invalid: {action_id}"
                        ) from error
                    if revision_number != expected_revision:
                        raise IntegrityError(
                            f"behavior log revision sequence mismatch: {action_id}"
                        )
                    if (
                        revision["action_id"] != action_id
                        or revision["subject_id"] != subject_id
                        or revision["state_hash"] != behavior_log_state_hash(revision)
                    ):
                        raise IntegrityError(
                            f"behavior log revision integrity mismatch: {action_id}"
                        )
                if len(history) == 2:
                    baseline, reconciled = history
                    stable_fields = (
                        "action_type",
                        "public_goal_reference",
                        "tool",
                        "public_target",
                        "resource_summary",
                        "redaction_reason",
                    )
                    if (
                        baseline["result_status"] != "unknown"
                        or reconciled["result_status"] == "unknown"
                    ):
                        raise IntegrityError(
                            f"behavior log reconciliation baseline mismatch: {action_id}"
                        )
                    if any(baseline[field] != reconciled[field] for field in stable_fields):
                        raise IntegrityError(
                            f"behavior log reconciliation fields changed: {action_id}"
                        )
                baseline = history[0]
                for field in (
                    "log_id",
                    "action_id",
                    "subject_id",
                    "occurred_at",
                    "action_type",
                    "public_goal_reference",
                    "tool",
                    "public_target",
                    "result_status",
                    "side_effect_summary",
                    "resource_summary",
                    "public_explanation",
                    "redaction_reason",
                ):
                    if baseline[field] != log[field]:
                        raise IntegrityError(f"behavior log baseline mismatch: {action_id}")
                if history[-1]["result_status"] != action["status"]:
                    raise IntegrityError(f"behavior log status mismatch: {action_id}")

        for log in logs:
            action_id = str(log["action_id"])
            action = actions_by_id.get(action_id)
            if action is None or log["subject_id"] != subject_id:
                raise IntegrityError(f"behavior log ownership mismatch: {action_id}")

        return {
            "actions": len(actions),
            "action_revisions": len(revisions),
            "behavior_logs": len(logs),
            "behavior_log_revisions": len(behavior_revisions),
        }

    @staticmethod
    def _record_revision(connection: Any, action_id: str, reason: str) -> None:
        row = ActionLedger._get_row(connection, action_id)
        previous = connection.execute(
            "SELECT MAX(revision_number) AS revision_number FROM action_revisions "
            "WHERE action_id = ?",
            (action_id,),
        ).fetchone()
        revision_number = (
            1 if previous["revision_number"] is None else int(previous["revision_number"]) + 1
        )
        connection.execute(
            """INSERT INTO action_revisions(
                revision_id, action_id, subject_id, revision_number, status,
                state_hash, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("arev"),
                row["action_id"],
                row["subject_id"],
                revision_number,
                row["status"],
                action_state_hash(row),
                reason,
                utc_now(),
            ),
        )

    def behavior_logs(self, subject_id: str, *, limit: int = 100) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """SELECT l.*, r.revision_number AS behavior_revision_number,
                          r.occurred_at AS latest_occurred_at,
                          r.public_goal_reference AS latest_public_goal_reference,
                          r.public_target AS latest_public_target,
                          r.result_status AS latest_result_status,
                          r.side_effect_summary AS latest_side_effect_summary,
                          r.resource_summary AS latest_resource_summary,
                          r.public_explanation AS latest_public_explanation,
                          r.redaction_reason AS latest_redaction_reason
                   FROM behavior_logs l
                   JOIN behavior_log_revisions r ON r.log_id = l.log_id
                    AND r.revision_number = (
                        SELECT MAX(r2.revision_number)
                        FROM behavior_log_revisions r2 WHERE r2.log_id = l.log_id
                    )
                   WHERE l.subject_id = ?
                   ORDER BY r.occurred_at DESC, l.log_id DESC LIMIT ?""",
                (subject_id, max(1, min(limit, 1000))),
            ).fetchall()
        fields = (
            "occurred_at",
            "public_goal_reference",
            "public_target",
            "result_status",
            "side_effect_summary",
            "resource_summary",
            "public_explanation",
            "redaction_reason",
        )
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            for field in fields:
                item[f"original_{field}"] = item[field]
                item[field] = item.pop(f"latest_{field}")
            result.append(item)
        return result

    @staticmethod
    def _get_row(connection: Any, action_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM actions WHERE action_id = ?", (action_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"action not found: {action_id}")
        return row

    @classmethod
    def _load_connection(cls, connection: Any, action_id: str) -> ActionRecord:
        return cls._from_row(cls._get_row(connection, action_id))

    @staticmethod
    def _insert_behavior_log(
        connection: Any,
        row: Any,
        *,
        completed_at: str,
        result_status: str,
        public_goal_reference: str | None,
        public_target: str | None,
        side_effect_summary: str,
        resource_summary: str,
        public_explanation: str,
        redaction_reason: str | None,
    ) -> None:
        target_is_private = public_target is None or not public_target.strip()
        safe_public_target = PRIVATE_PUBLIC_TARGET if target_is_private else public_target
        safe_redaction_reason = redaction_reason
        if target_is_private and safe_redaction_reason is None:
            safe_redaction_reason = "target withheld by default"
        log_id = new_id("blog")
        connection.execute(
            """INSERT INTO behavior_logs(
                log_id, action_id, subject_id, occurred_at, action_type,
                public_goal_reference, tool, public_target, result_status,
                side_effect_summary, resource_summary, public_explanation, redaction_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                log_id,
                row["action_id"],
                row["subject_id"],
                completed_at,
                row["action_type"],
                public_goal_reference,
                row["tool"],
                safe_public_target,
                result_status,
                side_effect_summary,
                resource_summary,
                public_explanation,
                safe_redaction_reason,
            ),
        )
        log = connection.execute(
            "SELECT * FROM behavior_logs WHERE log_id = ?", (log_id,)
        ).fetchone()
        if log is None:
            raise IntegrityError("behavior log was not persisted")
        ActionLedger._append_behavior_log_revision(
            connection,
            log,
            occurred_at=completed_at,
            result_status=result_status,
            side_effect_summary=side_effect_summary,
            public_explanation=public_explanation,
            reason="initial outcome recorded",
        )

    @staticmethod
    def _append_behavior_log_revision(
        connection: Any,
        source: Any,
        *,
        occurred_at: str,
        result_status: str,
        side_effect_summary: str,
        public_explanation: str,
        reason: str,
    ) -> None:
        previous = connection.execute(
            "SELECT MAX(revision_number) AS revision_number FROM behavior_log_revisions "
            "WHERE log_id = ?",
            (source["log_id"],),
        ).fetchone()
        revision_number = (
            1 if previous["revision_number"] is None else int(previous["revision_number"]) + 1
        )
        revision = {
            "log_id": source["log_id"],
            "action_id": source["action_id"],
            "subject_id": source["subject_id"],
            "revision_number": revision_number,
            "occurred_at": occurred_at,
            "action_type": source["action_type"],
            "public_goal_reference": source["public_goal_reference"],
            "tool": source["tool"],
            "public_target": source["public_target"],
            "result_status": result_status,
            "side_effect_summary": side_effect_summary,
            "resource_summary": source["resource_summary"],
            "public_explanation": public_explanation,
            "redaction_reason": source["redaction_reason"],
            "reason": reason,
        }
        connection.execute(
            """INSERT INTO behavior_log_revisions(
                revision_id, log_id, action_id, subject_id, revision_number,
                occurred_at, action_type, public_goal_reference, tool, public_target,
                result_status, side_effect_summary, resource_summary, public_explanation,
                redaction_reason, state_hash, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("brev"),
                revision["log_id"],
                revision["action_id"],
                revision["subject_id"],
                revision["revision_number"],
                revision["occurred_at"],
                revision["action_type"],
                revision["public_goal_reference"],
                revision["tool"],
                revision["public_target"],
                revision["result_status"],
                revision["side_effect_summary"],
                revision["resource_summary"],
                revision["public_explanation"],
                revision["redaction_reason"],
                behavior_log_state_hash(revision),
                revision["reason"],
                utc_now(),
            ),
        )

    @staticmethod
    def _from_row(row: Any) -> ActionRecord:
        status = row["status"]
        if status not in ACTION_STATES:
            raise IntegrityError(f"invalid persisted action status: {status}")
        resource_cost_json = row["resource_cost_json"]
        result_json = row["result_json"]
        if not isinstance(resource_cost_json, str) or (
            result_json is not None and not isinstance(result_json, str)
        ):
            raise IntegrityError(f"invalid persisted action JSON: {row['action_id']}")
        try:
            resource_cost = strict_json_loads(resource_cost_json)
            result = strict_json_loads(result_json) if result_json is not None else None
            retry_count = strict_int(row["retry_count"])
            side_effect = strict_bool(row["side_effect"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError(f"invalid persisted action data: {row['action_id']}") from error
        if not isinstance(resource_cost, dict) or (
            result is not None and not isinstance(result, dict)
        ):
            raise IntegrityError(f"invalid persisted action JSON: {row['action_id']}")
        return ActionRecord(
            action_id=row["action_id"],
            subject_id=row["subject_id"],
            goal_id=row["goal_id"],
            project_id=row["project_id"],
            phase_id=row["phase_id"],
            strategy_id=row["strategy_id"],
            action_type=row["action_type"],
            tool=row["tool"],
            target=row["target"],
            input_hash=row["input_hash"],
            idempotency_key=row["idempotency_key"],
            expected_outcome=row["expected_outcome"],
            side_effect=side_effect,
            status=status,
            retry_count=retry_count,
            resource_cost=resource_cost,
            result=result,
            prepared_at=row["prepared_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
        )
