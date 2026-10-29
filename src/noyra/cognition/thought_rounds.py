"""Durable closure of failed thought rounds, independent of paid-call accounting.

Only known terminal ledger facts can advance a round. Successes remain real
thought episodes; unknown or active calls are never converted to failures.
No prompt/response text or model-accounting rows are written here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from noyra.core.cognitive_waits import WAIT_FIELDS, insert_wait_revision, validate_wait
from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.model.errors import ModelCallStateError
from noyra.model.ledger import ModelLedger
from noyra.model.request_identity import UNRUNNABLE_BUSINESS_CALL_SQL, logical_key_from_route
from noyra.model.types import CallRecord


class ThoughtRounds:
    def __init__(self, database: Database, subject_id: str):
        self.database = database
        self.subject_id = subject_id

    @staticmethod
    def identity(agenda_id: str, number: int) -> tuple[str, str]:
        return f"intrinsic_thought:{agenda_id}:{number}", f"intrinsic-thought:{agenda_id}:{number}"

    @staticmethod
    def timestamp(value: Any) -> datetime:
        try:
            if not isinstance(value, str):
                raise ValueError("timestamp must be text")
            result = datetime.fromisoformat(value)
            if result.tzinfo is None or result.utcoffset() is None:
                raise ValueError("timezone required")
            return result
        except (TypeError, ValueError) as error:
            raise IntegrityError("thought round timestamp is invalid") from error

    @classmethod
    def closure(cls, row: Any) -> dict[str, Any]:
        payload = dict(row)
        digest = payload.pop("state_hash")
        try:
            if content_hash(payload) != digest:
                raise IntegrityError("thought round closure hash mismatch")
            if type(payload["round_number"]) is not int or payload["round_number"] < 0:
                raise IntegrityError("thought round number is invalid")
            if payload["reason"] not in {"model_failed", "proposal_rejected"}:
                raise IntegrityError("thought round closure reason is invalid")
            if cls.timestamp(payload["retry_after"]) <= cls.timestamp(payload["closed_at"]):
                raise IntegrityError("thought round cooldown is invalid")
            evidence = strict_json_loads(payload["call_evidence_json"])
            if not isinstance(evidence, list) or not 1 <= len(evidence) <= 256:
                raise IntegrityError("thought round evidence is invalid")
        except (TypeError, ValueError) as error:
            raise IntegrityError("thought round closure is invalid") from error
        return payload

    def current(self, agenda_id: str) -> tuple[int, dict[str, Any] | None]:
        with self.database.read_transaction() as connection:
            return self._current(connection, agenda_id)

    def _current(self, connection: Any, agenda_id: str) -> tuple[int, dict[str, Any] | None]:
        owner = connection.execute(
            "SELECT subject_id FROM thought_agenda_items WHERE agenda_id=?", (agenda_id,)
        ).fetchone()
        if owner is None or owner[0] != self.subject_id:
            raise IntegrityError("thought round agenda ownership mismatch")
        episodes = connection.execute(
            "SELECT COUNT(*) FROM thought_episodes WHERE agenda_id=? AND subject_id=?",
            (agenda_id, self.subject_id),
        ).fetchone()[0]
        count = connection.execute(
            "SELECT COUNT(*) FROM thought_round_closures WHERE agenda_id=? AND subject_id=?",
            (agenda_id, self.subject_id),
        ).fetchone()[0]
        latest = connection.execute(
            "SELECT * FROM thought_round_closures WHERE agenda_id=? "
            "ORDER BY round_number DESC LIMIT 1",
            (agenda_id,),
        ).fetchone()
        closure = None if latest is None else self.closure(latest)
        number = int(episodes) + int(count)
        if closure is not None and (
            closure["subject_id"] != self.subject_id or closure["round_number"] >= number
        ):
            raise IntegrityError("thought round closure sequence mismatch")
        if closure is not None:
            self._verify_closure_evidence(connection, closure)
        return number, closure

    def calls(self, agenda_id: str, number: int) -> tuple[list[CallRecord], bool]:
        with self.database.read_transaction() as connection:
            return self._calls(connection, agenda_id, number)

    def _calls(self, connection: Any, agenda_id: str, number: int) -> tuple[list[CallRecord], bool]:
        purpose, key = self.identity(agenda_id, number)
        rows = connection.execute(
            f"SELECT c.*,{UNRUNNABLE_BUSINESS_CALL_SQL} AS unrunnable "
            "FROM model_calls c WHERE c.subject_id=:subject AND c.purpose=:purpose "
            "ORDER BY c.call_id LIMIT 257",
            {"subject": self.subject_id, "purpose": purpose, "require_prepared_authorization": 1},
        ).fetchall()
        if len(rows) > 256:
            raise IntegrityError("thought round has too many physical calls")
        calls = []
        blocked = False
        for row in rows:
            call = ModelLedger._call_from_row(row)
            try:
                logical_key = logical_key_from_route(call.idempotency_key, call.resource_pool)
            except ModelCallStateError as error:
                raise IntegrityError("thought round route identity is invalid") from error
            if logical_key != key:
                raise IntegrityError("thought round request identity mismatch")
            calls.append(call)
            # Prepared work requires the existing explicit operator grant.
            # UNRUNNABLE also preserves context-conflict and spent-grant fences.
            if row["unrunnable"] or call.status in {"executing", "unknown"}:
                blocked = True
            if (
                connection.execute(
                    "SELECT 1 FROM model_attempts WHERE call_id=? "
                    "AND status IN ('authorized','executing') LIMIT 1",
                    (call.call_id,),
                ).fetchone()
                is not None
            ):
                blocked = True
            unknown_attempt = connection.execute(
                "SELECT 1 FROM model_attempts WHERE call_id=? AND status='unknown' LIMIT 1",
                (call.call_id,),
            ).fetchone()
            if (
                unknown_attempt is not None
                and connection.execute(
                    "SELECT 1 FROM audit_records INDEXED BY idx_audit_subject_action_call "
                    "WHERE subject_id=? AND json_valid(payload_json) "
                    "AND json_extract(payload_json,'$.call_id')=? AND "
                    "(action='model_unknown_retry_authorized' OR "
                    "(action='model_unknown_reconciled' "
                    "AND json_extract(payload_json,'$.outcome')=?)) LIMIT 1",
                    (self.subject_id, call.call_id, call.status),
                ).fetchone()
                is None
            ):
                blocked = True
        # A grant for one physical route is not permission to replay an
        # already-successful business round or choose between forked routes.
        if sum(call.status in {"prepared", "succeeded"} for call in calls) > 1:
            blocked = True
        if sum(call.status == "succeeded" for call in calls) > 1:
            raise IntegrityError("thought round has multiple successful calls")
        return calls, blocked

    @staticmethod
    def evidence(calls: list[CallRecord]) -> str:
        return canonical_json(
            [
                {
                    "call_id": call.call_id,
                    "status": call.status,
                    "request_hash": call.request_hash,
                    "response_hash": call.response_hash,
                }
                for call in calls
            ]
        )

    def close(
        self, agenda_id: str, number: int, reason: str, closed_at: str, retry_after: str
    ) -> bool:
        with self.database.transaction() as connection:
            current, _ = self._current(connection, agenda_id)
            if current != number:
                return False
            calls, blocked = self._calls(connection, agenda_id, number)
            if not calls or blocked or any(c.status == "prepared" for c in calls):
                return False
            succeeded = any(call.status == "succeeded" for call in calls)
            if succeeded != (reason == "proposal_rejected"):
                raise IntegrityError("thought round closure contradicts model outcome")
            payload = {
                "agenda_id": agenda_id,
                "subject_id": self.subject_id,
                "round_number": number,
                "reason": reason,
                "call_evidence_json": self.evidence(calls),
                "closed_at": closed_at,
                "retry_after": retry_after,
            }
            self.closure(payload | {"state_hash": content_hash(payload)})
            connection.execute(
                "INSERT INTO thought_round_closures(agenda_id,subject_id,round_number,reason,"
                "call_evidence_json,closed_at,retry_after,state_hash) VALUES (?,?,?,?,?,?,?,?)",
                (*payload.values(), content_hash(payload)),
            )
            self.cancel_waits(connection, agenda_id, number, closed_at)
            return True

    def cancel_waits(self, connection: Any, agenda_id: str, number: int, now: str) -> None:
        # A completed business round cannot retain a runnable resource wait.
        # Preserve the existing wait/revision protocol and all retry counters;
        # cancellation is not a successful model call or released reservation.
        purpose, _ = self.identity(agenda_id, number)
        rows = connection.execute(
            "SELECT * FROM waiting_cognitive_tasks WHERE subject_id=? AND purpose=? "
            "AND status='waiting'",
            (self.subject_id, purpose),
        ).fetchall()
        for row in rows:
            validate_wait(connection, row)
            payload = {field: row[field] for field in WAIT_FIELDS}
            payload.update(status="cancelled", reason_code="thought_round_closed", updated_at=now)
            connection.execute(
                "UPDATE waiting_cognitive_tasks SET status='cancelled',reason_code=?,"
                "updated_at=?,state_hash=? WHERE task_id=? AND status='waiting'",
                (payload["reason_code"], now, content_hash(payload), row["task_id"]),
            )
            insert_wait_revision(
                connection,
                row["task_id"],
                "cancelled",
                payload["reason_code"],
                row["retry_count"],
                row["next_retry_at"],
                now,
            )

    def _verify_closure_evidence(self, connection: Any, payload: dict[str, Any]) -> None:
        owner = connection.execute(
            "SELECT subject_id FROM thought_agenda_items WHERE agenda_id=?", (payload["agenda_id"],)
        ).fetchone()
        if payload["subject_id"] != self.subject_id or owner is None or owner[0] != self.subject_id:
            raise IntegrityError("thought round closure crosses a subject boundary")
        calls, blocked = self._calls(connection, payload["agenda_id"], payload["round_number"])
        if (
            blocked
            or any(c.status == "prepared" for c in calls)
            or payload["call_evidence_json"] != self.evidence(calls)
        ):
            raise IntegrityError("thought round closure model evidence mismatch")
        if any(c.status == "succeeded" for c in calls) != (
            payload["reason"] == "proposal_rejected"
        ):
            raise IntegrityError("thought round closure outcome mismatch")

    def verify(self, connection: Any) -> int:
        rows = connection.execute(
            "SELECT r.* FROM thought_round_closures r LEFT JOIN thought_agenda_items a "
            "ON a.agenda_id=r.agenda_id WHERE r.subject_id=? OR a.subject_id=?",
            (self.subject_id, self.subject_id),
        ).fetchall()
        for row in rows:
            self._verify_closure_evidence(connection, self.closure(row))
        agendas = connection.execute(
            "SELECT agenda_id FROM thought_agenda_items WHERE subject_id=?", (self.subject_id,)
        ).fetchall()
        for agenda in agendas:
            agenda_id = agenda[0]
            numbers = {
                int(row[0])
                for row in connection.execute(
                    "SELECT round_number FROM thought_round_closures WHERE agenda_id=?",
                    (agenda_id,),
                ).fetchall()
            }
            for row in connection.execute(
                "SELECT idempotency_key,model_call_id FROM thought_episodes WHERE agenda_id=?",
                (agenda_id,),
            ).fetchall():
                prefix = f"intrinsic-thought:{agenda_id}:"
                key = row[0]
                if not isinstance(key, str) or not key.startswith(prefix):
                    raise IntegrityError("thought episode round key is invalid")
                suffix = key[len(prefix) :]
                if not suffix.isascii() or not suffix.isdecimal() or str(int(suffix)) != suffix:
                    raise IntegrityError("thought episode round number is invalid")
                number = int(suffix)
                if number in numbers:
                    raise IntegrityError("thought round has more than one closure")
                numbers.add(number)
                calls, blocked = self._calls(connection, agenda_id, number)
                if blocked or row[1] not in {c.call_id for c in calls if c.status == "succeeded"}:
                    raise IntegrityError("thought episode business round binding mismatch")
            if numbers and (min(numbers) != 0 or max(numbers) != len(numbers) - 1):
                raise IntegrityError("thought round sequence is not contiguous")
        return len(rows)
