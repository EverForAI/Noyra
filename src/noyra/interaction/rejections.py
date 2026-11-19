"""Private, ledger-bound cooldown receipts for invalid communication proposals."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from math import isfinite
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.core.types import content_hash, utc_now
from noyra.model import ModelLedger
from noyra.model.request_identity import UNRUNNABLE_BUSINESS_CALL_SQL
from noyra.model.types import CallRecord

FORMAT = "interaction-rejection/v1"
EVENT_TYPE = "model_interaction_rejected"


class InteractionRejections:
    def __init__(self, database: Database, subject_id: str, clock: Callable[[], str] = utc_now):
        self.database, self.subject_id, self.clock = database, subject_id, clock
        self.events = EventStore(database)
        self.prefix = "evt_interaction_rejected_" + content_hash(subject_id) + ":"

    @staticmethod
    def _time(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone required")
            return parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("interaction rejection time is invalid") from error

    def read(self, connection: Any, call: CallRecord) -> dict[str, Any] | None:
        row = connection.execute(
            "SELECT * FROM events WHERE event_id=?", (self.prefix + call.call_id,)
        ).fetchone()
        if row is None:
            return None
        event = self.events._from_row(row)
        value = event.payload
        expected = {
            "format": FORMAT,
            "model_call_id": call.call_id,
            "purpose": call.purpose,
            "request_hash": call.request_hash,
            "status": call.status,
            "error_code": call.error_code,
        }
        interaction_id = value.get("interaction_id")
        number = value.get("decision_round")
        if (
            call.subject_id != self.subject_id
            or event.subject_id != self.subject_id
            or event.event_type != EVENT_TYPE
            or event.source != "interaction_supervisor"
            or event.privacy_level != "private"
            or event.causal_parent_ids
            or set(value)
            != set(expected) | {"interaction_id", "decision_round", "reason", "retry_after"}
            or any(value.get(key) != item for key, item in expected.items())
            or not isinstance(interaction_id, str)
            or type(number) is not int
            or number < 0
            or call.purpose != f"interaction_cognition:{interaction_id}:{number}"
            or not isinstance(value.get("reason"), str)
            or value["reason"] not in {"invalid_schema", "invalid_affect_target"}
            or not self._eligible(call, value["reason"])
        ):
            raise IntegrityError("interaction rejection receipt is invalid")
        owner = connection.execute(
            "SELECT subject_id,direction FROM interactions WHERE interaction_id=?",
            (interaction_id,),
        ).fetchone()
        if (
            owner is None
            or owner["subject_id"] != self.subject_id
            or owner["direction"] != "incoming"
        ):
            raise IntegrityError("interaction rejection ownership mismatch")
        delay = self._time(value["retry_after"]) - self._time(event.occurred_at)
        if not timedelta(seconds=60) <= delay <= timedelta(days=1):
            raise IntegrityError("interaction rejection cooldown is invalid")
        return value

    @staticmethod
    def _eligible(call: CallRecord, reason: str) -> bool:
        return call.status == "succeeded" or (
            reason == "invalid_schema"
            and call.status == "failed"
            and call.error_code == "structured_output_invalid"
        )

    def ready(
        self, connection: Any, purpose: str, *, provider_failure_cooldown_seconds: float = 60
    ) -> bool:
        # The latest physical call determines recovery. An explicitly prepared
        # retry or an uncommitted success must retain its original identity.
        row = connection.execute(
            "SELECT * FROM model_calls WHERE subject_id=? AND purpose=? "
            "ORDER BY rowid DESC LIMIT 1",
            (self.subject_id, purpose),
        ).fetchone()
        if row is None:
            return True
        call = ModelLedger._call_from_row(row)
        receipt = self.read(connection, call)
        if receipt is not None:
            return self._time(self.clock()) >= self._time(receipt["retry_after"])
        if (
            call.status == "failed"
            and call.error_code is not None
            and call.error_code.startswith("provider_")
        ):
            # A known provider rejection is not an invalid proposal. Use its
            # durable settlement time without inventing a rejection receipt or
            # changing the business round. Explicit operator closure stays runnable.
            if connection.execute(
                "SELECT 1 FROM audit_records INDEXED BY idx_audit_subject_action_call "
                "WHERE subject_id=? AND action='model_unknown_retry_closed' "
                "AND json_valid(payload_json) "
                "AND json_extract(payload_json,'$.call_id')=? LIMIT 1",
                (self.subject_id, call.call_id),
            ).fetchone():
                return True
            delay = max(60.0, provider_failure_cooldown_seconds)
            return self._time(self.clock()) >= self._time(call.completed_at) + timedelta(
                seconds=delay
            )
        return True

    def record(
        self,
        call_id: str,
        interaction_id: str,
        number: int,
        reason: str,
        interval_seconds: float,
    ) -> bool:
        if reason not in {"invalid_schema", "invalid_affect_target"}:
            raise ValueError("invalid interaction rejection reason")
        if not isfinite(interval_seconds) or not 0 <= interval_seconds <= 86_400:
            raise ValueError("invalid interaction rejection interval")
        with self.database.transaction() as connection:
            call = ModelLedger._load_call_connection(connection, call_id)
            if (
                call.subject_id != self.subject_id
                or call.purpose != f"interaction_cognition:{interaction_id}:{number}"
                or not self._eligible(call, reason)
            ):
                raise IntegrityError("interaction rejection requires a settled matching call")
            if self.read(connection, call) is not None:
                return False
            ModelLedger._check_lifecycle(connection, self.subject_id, call.purpose)
            # Recording a failed proposal cannot authorize a retry past a
            # pending/ambiguous call or an unresolved operator authorization.
            if (
                connection.execute(
                    "SELECT 1 FROM model_calls c "
                    "WHERE c.subject_id=:subject AND c.purpose=:purpose "
                    "AND (" + UNRUNNABLE_BUSINESS_CALL_SQL + " OR c.status='prepared') LIMIT 1",
                    {
                        "subject": self.subject_id,
                        "purpose": call.purpose,
                        "require_prepared_authorization": 1,
                    },
                ).fetchone()
                or connection.execute(
                    "SELECT 1 FROM model_attempts a JOIN model_calls c ON c.call_id=a.call_id "
                    "WHERE c.subject_id=? AND c.purpose=? "
                    "AND a.status IN ('authorized','executing') "
                    "LIMIT 1",
                    (self.subject_id, call.purpose),
                ).fetchone()
            ):
                return False
            owner = connection.execute(
                "SELECT subject_id,direction FROM interactions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()
            decisions = connection.execute(
                "SELECT COUNT(*) FROM interaction_decisions WHERE interaction_id=?",
                (interaction_id,),
            ).fetchone()[0]
            if (
                owner is None
                or owner["subject_id"] != self.subject_id
                or owner["direction"] != "incoming"
                or decisions != number
            ):
                raise IntegrityError("interaction rejection source changed")
            count = connection.execute(
                "SELECT COUNT(*) FROM (SELECT 1 FROM model_calls c JOIN events e "
                "ON e.event_id=?||c.call_id WHERE c.subject_id=? AND c.purpose=? LIMIT 6)",
                (self.prefix, self.subject_id, call.purpose),
            ).fetchone()[0]
            base = max(60.0, interval_seconds)
            delay = min(max(3600.0, base), base * 2 ** int(count))
            now = self._time(self.clock())
            self.events._append_connection(
                connection,
                self.subject_id,
                EVENT_TYPE,
                "interaction_supervisor",
                {
                    "format": FORMAT,
                    "interaction_id": interaction_id,
                    "decision_round": number,
                    "model_call_id": call.call_id,
                    "purpose": call.purpose,
                    "request_hash": call.request_hash,
                    "status": call.status,
                    "error_code": call.error_code,
                    "reason": reason,
                    "retry_after": (now + timedelta(seconds=delay)).isoformat(
                        timespec="milliseconds"
                    ),
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now.isoformat(timespec="milliseconds"),
                event_id=self.prefix + call.call_id,
            )
            return True

    def verify_connection(self, connection: Any) -> int:
        count = 0
        for row in connection.execute(
            "SELECT * FROM events WHERE event_id>=? AND event_id<? ORDER BY event_id",
            (self.prefix, self.prefix[:-1] + ";"),
        ):
            call_id = str(row["event_id"])[len(self.prefix) :]
            try:
                call = ModelLedger._load_call_connection(connection, call_id)
            except NotFoundError as error:
                raise IntegrityError("interaction rejection call is missing") from error
            self.read(connection, call)
            count += 1
        total = connection.execute(
            "SELECT COUNT(*) FROM events WHERE subject_id=? AND event_type=?",
            (self.subject_id, EVENT_TYPE),
        ).fetchone()[0]
        if total != count:
            raise IntegrityError("interaction rejection is outside its reserved namespace")
        return count
