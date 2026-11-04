"""Durable, private rejection receipts for hourly recipient-bound social rounds."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.events import EventStore
from noyra.model import ModelLedger
from noyra.model.request_identity import UNRUNNABLE_BUSINESS_CALL_SQL
from noyra.model.types import CallRecord


class SocialRoundWaiting(RuntimeError):
    """A local wait, never permission to settle or repeat an uncertain request."""


class SocialRoundControl:
    def __init__(self, database: Database, subject_id: str, clock: Callable[[], str]):
        self.database = database
        self.subject_id = subject_id
        self.clock = clock
        self.events = EventStore(database)

    def recovery_purpose(self, current: str, route_hash: str) -> str:
        # Continue an explicitly prepared retry or an uncommitted success on
        # its ORIGINAL hour/key. A rejected old round is terminal; a new hour
        # may make a fresh decision. Never recover a proposal into another room.
        suffix = ":route:" + route_hash
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT c.* FROM model_calls c WHERE c.subject_id=? "
                "AND c.purpose LIKE 'relationship_social:%' "
                "AND substr(c.purpose,-?)=? AND (c.status='prepared' OR "
                "(c.status='succeeded' AND NOT EXISTS (SELECT 1 FROM relationship_social_runs r "
                "WHERE r.subject_id=c.subject_id AND r.model_call_id=c.call_id))) "
                "ORDER BY (c.status='prepared') DESC,c.rowid DESC LIMIT 1",
                (self.subject_id, len(suffix), suffix),
            ).fetchone()
        if row is None:
            return current
        call = ModelLedger._call_from_row(row)
        if call.status == "prepared" or self._receipt(call) is None:
            return call.purpose
        return current

    def require_ready(
        self,
        connection: Any,
        purpose: str,
        *,
        require_prepared_authorization: bool,
        own_prepared_call: str | None = None,
    ) -> None:
        # Check the whole lane: changing the hour, room or physical resource is
        # not evidence that an earlier dispatch did not run. Independent lanes
        # and other subjects remain unaffected. Prepared retries keep their key.
        row = connection.execute(
            "SELECT c.purpose,c.status FROM model_calls c WHERE c.subject_id=:subject "
            "AND c.purpose LIKE 'relationship_social:%' AND ("
            + UNRUNNABLE_BUSINESS_CALL_SQL.replace(
                ":require_prepared_authorization",
                "(:require_prepared_authorization AND (:own_call IS NULL OR c.call_id!=:own_call))",
            )
            + " OR (c.status='prepared' AND (c.purpose!=:purpose OR "
            "(:own_call IS NOT NULL AND c.call_id!=:own_call)))) LIMIT 1",
            {
                "subject": self.subject_id,
                "purpose": purpose,
                "require_prepared_authorization": int(require_prepared_authorization),
                "own_call": own_prepared_call,
            },
        ).fetchone()
        if row is not None:
            if row["status"] == "prepared" and row["purpose"] == purpose:
                raise SocialRoundWaiting("relationship_social_context_changed")
            raise SocialRoundWaiting("relationship_social_model_failed")
        if connection.execute(
            "SELECT 1 FROM model_attempts a JOIN model_calls c ON c.call_id=a.call_id "
            "WHERE c.subject_id=? AND c.purpose LIKE 'relationship_social:%' "
            "AND a.status IN ('authorized','executing') LIMIT 1",
            (self.subject_id,),
        ).fetchone():
            raise SocialRoundWaiting("relationship_social_model_failed")

    def may_reconsider(self, call: CallRecord, context_hash: str) -> bool | None:
        """None means unevaluated; False waits; True permits a NEW proposal."""
        receipt = self._receipt(call)
        if receipt is None:
            return None
        return receipt["context_hash"] != context_hash and self._time(self.clock()) >= self._time(
            receipt["reconsider_at"]
        )

    def reject(
        self, call: CallRecord, context_hash: str, reason: str, interval_seconds: float
    ) -> None:
        if self._receipt(call) is not None:
            return
        now = self._time(self.clock())
        # The receipt governs this hour/recipient round. The next hourly round
        # may reconsider unchanged evidence, within existing daily budgets.
        reconsider = min(
            now + timedelta(seconds=interval_seconds),
            now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1),
        )
        self.events.append(
            self.subject_id,
            "relationship_social_rejected",
            "social_supervisor",
            {
                "format": "social-rejection/v1",
                "model_call_id": call.call_id,
                "purpose": call.purpose,
                "request_hash": call.request_hash,
                "response_hash": call.response_hash,
                "context_hash": context_hash,
                "reason": reason,
                "reconsider_at": reconsider.isoformat(timespec="milliseconds"),
            },
            privacy_level="private",
            occurred_at=now.isoformat(timespec="milliseconds"),
            event_id=self._event_id(call.call_id),
        )

    def _receipt(self, call: CallRecord) -> dict[str, Any] | None:
        if call.subject_id != self.subject_id or call.status != "succeeded":
            raise IntegrityError("social rejection requires this subject's successful call")
        try:
            event = self.events.get(self._event_id(call.call_id))
        except NotFoundError:
            return None
        payload = event.payload
        expected = {
            "format": "social-rejection/v1",
            "model_call_id": call.call_id,
            "purpose": call.purpose,
            "request_hash": call.request_hash,
            "response_hash": call.response_hash,
        }
        digest = payload.get("context_hash")
        if (
            event.subject_id != self.subject_id
            or event.event_type != "relationship_social_rejected"
            or event.source != "social_supervisor"
            or event.privacy_level != "private"
            or set(payload) != set(expected) | {"context_hash", "reason", "reconsider_at"}
            or any(payload.get(key) != value for key, value in expected.items())
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(char not in "0123456789abcdef" for char in digest)
            or not isinstance(payload.get("reason"), str)
        ):
            raise IntegrityError("social rejection receipt is invalid")
        if self._time(payload.get("reconsider_at")) < self._time(event.occurred_at):
            raise IntegrityError("social rejection cooldown precedes rejection")
        return payload

    @staticmethod
    def _event_id(call_id: str) -> str:
        return "evt_relationship_social_rejected_" + call_id

    @staticmethod
    def _time(value: Any) -> datetime:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("timezone required")
            return parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError("social rejection time is invalid") from error
