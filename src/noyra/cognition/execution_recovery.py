"""Explicit review-only release of interrupted local project execution.

No execution, model, action, budget or artifact result is rewritten. A release
is not acceptance evidence and never authorizes a retry of an unknown effect.
"""

from __future__ import annotations

from typing import Any

from noyra.core.database import Database
from noyra.core.delivery_state import delivery_status_counts
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.types import content_hash, utc_now

RECOVERY_FORMAT = "project-execution-review-release/v1"
EVENT_TYPE = "project_execution_review_released"
LOCAL_OUTPUTS = frozenset({"research_note", "knowledge_collection", "software_prototype"})
AMBIGUOUS = frozenset({"executing", "unknown"})


def release_event_id(subject_id: str, execution_id: str) -> str:
    return "evt_project_review_release_" + content_hash([subject_id, execution_id])


def read_release(connection: Any, database: Database, execution: Any) -> dict[str, Any] | None:
    identifier = release_event_id(str(execution["subject_id"]), str(execution["execution_id"]))
    event = connection.execute("SELECT * FROM events WHERE event_id=?", (identifier,)).fetchone()
    if event is None:
        return None
    payload = EventStore(database).payload_from_row(event, connection=connection)
    fields = {
        "format",
        "execution_id",
        "subject_id",
        "project_id",
        "phase_id",
        "execution_key",
        "execution_hash",
        "project_revision",
        "phase_revision",
        "attempt_count",
        "request_fingerprint",
        "actor",
        "reason",
        "disposition",
    }
    if (
        event["subject_id"] != execution["subject_id"]
        or event["event_type"] != EVENT_TYPE
        or event["source"] != "operator_control"
        or event["privacy_level"] != "private"
        or content_hash(payload) != event["payload_hash"]
        or set(payload) != fields
        or payload.get("format") != RECOVERY_FORMAT
        or payload.get("disposition") != "review_only"
        or execution["status"] not in AMBIGUOUS
        or execution["execution_type"] not in LOCAL_OUTPUTS
        or payload.get("execution_hash") != content_hash(dict(execution))
        or any(
            payload.get(key) != execution[key]
            for key in ("execution_id", "subject_id", "project_id", "phase_id", "execution_key")
        )
        or any(
            type(payload.get(key)) is not int or payload[key] < 0
            for key in ("project_revision", "phase_revision", "attempt_count")
        )
        or not isinstance(payload.get("actor"), str)
        or len(payload["actor"]) > 256
        or not payload["actor"].strip()
        or payload["actor"].strip().casefold() == "subject"
        or not isinstance(payload.get("reason"), str)
        or len(payload["reason"]) > 2000
        or not payload["reason"].strip()
        or not isinstance(payload.get("request_fingerprint"), str)
        or len(payload["request_fingerprint"]) != 64
        or set(payload["request_fingerprint"]) - set("0123456789abcdef")
    ):
        raise IntegrityError("project execution release evidence is invalid")
    phase = connection.execute(
        "SELECT ph.subject_id,p.attempt_count,ph.project_id "
        "FROM autonomous_project_phase_revisions p "
        "JOIN autonomous_project_phases ph ON ph.phase_id=p.phase_id "
        "WHERE p.phase_id=? AND p.revision_number=?",
        (execution["phase_id"], payload["phase_revision"]),
    ).fetchone()
    # Phase revisions inherit ownership through the immutable phase parent.
    if (
        phase is None
        or phase["subject_id"] != execution["subject_id"]
        or phase["project_id"] != execution["project_id"]
        or phase["attempt_count"] != payload["attempt_count"]
    ):
        raise IntegrityError("project execution release phase evidence is invalid")
    revision = connection.execute(
        "SELECT 1 FROM autonomous_project_revisions r JOIN autonomous_projects p "
        "ON p.project_id=r.project_id WHERE r.project_id=? AND p.subject_id=? "
        "AND r.revision_number=?",
        (execution["project_id"], execution["subject_id"], payload["project_revision"]),
    ).fetchone()
    if revision is None:
        raise IntegrityError("project execution release revision is unavailable")
    if (
        execution["execution_key"]
        != f"{execution['project_id']}:{execution['phase_id']}:{payload['attempt_count']}"
    ):
        raise IntegrityError("project execution release attempt mismatch")
    return payload


def pending_effects(connection: Any, subject_id: str) -> dict[str, int]:
    result = {}
    for table, statuses in (
        ("actions", ("prepared", "executing", "unknown")),
        ("model_calls", ("prepared", "executing", "unknown")),
        ("model_attempts", ("authorized", "executing", "unknown")),
    ):
        marks = ",".join("?" for _ in statuses)
        result[table] = int(
            connection.execute(
                f"SELECT EXISTS(SELECT 1 FROM {table} WHERE subject_id=? AND status IN ({marks}))",
                (subject_id, *statuses),
            ).fetchone()[0]
        )
    counts = delivery_status_counts(connection, subject_id)
    result["deliveries"] = int(any(counts.get(s, 0) for s in ("queued", "sending", "unknown")))
    return result


class ProjectExecutionRecovery:
    def __init__(self, database: Database, subject_id: str):
        self.database, self.subject_id = database, subject_id

    def load(self, connection: Any, execution_id: str) -> tuple[Any, Any, Any]:
        from .execution import ProjectExecutionLedger

        row = connection.execute(
            "SELECT * FROM autonomous_project_executions WHERE subject_id=? AND execution_id=?",
            (self.subject_id, execution_id),
        ).fetchone()
        if row is None:
            raise NotFoundError("project execution not found")
        ProjectExecutionLedger(self.database, self.subject_id)._verify_execution_connection(
            connection, row
        )
        project = connection.execute(
            "SELECT * FROM autonomous_projects WHERE subject_id=? AND project_id=?",
            (self.subject_id, row["project_id"]),
        ).fetchone()
        phase = connection.execute(
            "SELECT * FROM autonomous_project_phases WHERE subject_id=? "
            "AND phase_id=? AND project_id=?",
            (self.subject_id, row["phase_id"], row["project_id"]),
        ).fetchone()
        if project is None or phase is None:
            raise IntegrityError("project execution recovery owner is unavailable")
        from .projects import AutonomousProjectManager

        AutonomousProjectManager._project_from_row(project)
        AutonomousProjectManager._phase_from_row(phase)
        return row, project, phase

    @staticmethod
    def fingerprint(row: Any, project: Any, phase: Any) -> str:
        return content_hash(
            {"execution": dict(row), "project": dict(project), "phase": dict(phase)}
        )

    @staticmethod
    def current(row: Any, project: Any, phase: Any) -> bool:
        return (
            project["status"] not in {"completed", "abandoned"}
            and project["current_phase_id"] == phase["phase_id"]
            and phase["status"] == "active"
            and row["execution_key"]
            == f"{project['project_id']}:{phase['phase_id']}:{phase['attempt_count']}"
            and row["status"] in AMBIGUOUS
            and row["execution_type"] in LOCAL_OUTPUTS
        )

    def released_for_review(self, execution_id: str) -> bool:
        with self.database.read_transaction() as c:
            row, project, phase = self.load(c, execution_id)
            release = read_release(c, self.database, row)
            return bool(
                release
                and self.current(row, project, phase)
                and not any(pending_effects(c, self.subject_id).values())
            )

    def catalog(self, connection: Any, *, limit: int = 50) -> dict[str, Any]:
        rows = connection.execute(
            "SELECT execution_id FROM autonomous_project_executions WHERE subject_id=? "
            "AND status IN ('executing','unknown') ORDER BY updated_at,execution_id LIMIT ?",
            (self.subject_id, limit + 1),
        ).fetchall()
        blocked = pending_effects(connection, self.subject_id)
        items = []
        for item in rows[:limit]:
            row, project, phase = self.load(connection, item["execution_id"])
            receipt = read_release(connection, self.database, row)
            supported = self.current(row, project, phase)
            items.append(
                {
                    "execution_id": row["execution_id"],
                    "project_id": row["project_id"],
                    "phase_id": row["phase_id"],
                    "status": row["status"],
                    "execution_type": row["execution_type"],
                    "attempt_count": phase["attempt_count"],
                    "expected_fingerprint": self.fingerprint(row, project, phase),
                    "has_recorded_result": any(
                        row[k] is not None
                        for k in ("research_id", "model_call_id", "action_id", "artifact_hash")
                    ),
                    "review_released": receipt is not None,
                    "eligible": supported and not receipt and not any(blocked.values()),
                    "reason_code": "review_released"
                    if receipt
                    else "unsupported_or_stale_attempt"
                    if not supported
                    else "unresolved_external_work"
                    if any(blocked.values())
                    else "review_release_available",
                }
            )
        return {"records": items, "truncated": len(rows) > limit, "external_blockers": blocked}

    def release(
        self,
        connection: Any,
        execution_id: str,
        expected_fingerprint: str,
        *,
        actor: str,
        reason: str,
    ) -> dict[str, Any]:
        # Called only inside the operator's paused/drained lifecycle control scope.
        if (
            not actor.strip()
            or actor.strip().casefold() == "subject"
            or len(actor) > 256
            or not reason.strip()
            or len(reason) > 2000
        ):
            raise ValueError("project_recovery_invalid_actor_or_reason")
        lifecycle = connection.execute(
            "SELECT state FROM runtime_state WHERE subject_id=?", (self.subject_id,)
        ).fetchone()
        if lifecycle is None or lifecycle[0] != "paused":
            raise ValueError("project_recovery_requires_paused")
        row, project, phase = self.load(connection, execution_id)
        existing = read_release(connection, self.database, row)
        if existing is not None:
            if existing["request_fingerprint"] != expected_fingerprint:
                raise ValueError("project_recovery_snapshot_conflict")
            return {"execution_id": execution_id, "review_released": True, "idempotent": True}
        if self.fingerprint(row, project, phase) != expected_fingerprint:
            raise ValueError("project_recovery_snapshot_conflict")
        if not self.current(row, project, phase):
            raise ValueError("project_recovery_unsupported_or_stale")
        if any(pending_effects(connection, self.subject_id).values()):
            raise ValueError("project_recovery_external_work_unresolved")
        payload = {
            "format": RECOVERY_FORMAT,
            "disposition": "review_only",
            **{
                key: row[key]
                for key in ("execution_id", "subject_id", "project_id", "phase_id", "execution_key")
            },
            "execution_hash": content_hash(dict(row)),
            "project_revision": project["current_revision"],
            "phase_revision": phase["current_revision"],
            "attempt_count": phase["attempt_count"],
            "request_fingerprint": expected_fingerprint,
            "actor": actor,
            "reason": reason,
        }
        EventStore(self.database)._append_connection(
            connection,
            self.subject_id,
            EVENT_TYPE,
            "operator_control",
            payload,
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=utc_now(),
            event_id=release_event_id(self.subject_id, execution_id),
        )
        return {"execution_id": execution_id, "review_released": True, "idempotent": False}
