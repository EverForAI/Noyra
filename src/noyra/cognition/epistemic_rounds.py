"""Private terminal receipts, separate from successful epistemic business runs."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import content_hash, utc_now
from noyra.model import ModelLedger

from .epistemic_recovery import EpistemicRecovery

FORMAT = "epistemic-round-closed/v1"
PREFIX = "evt_epistemic_closed_"
REASONS = {"model_failed", "context_unavailable", "legacy_result_held"}


class EpistemicRoundExit:
    def __init__(self, database: Database, subject_id: str, clock: Callable[[], str] = utc_now):
        self.database, self.subject_id, self.clock = database, subject_id, clock
        self.events = EventStore(database)
        self.prefix = PREFIX + content_hash(subject_id) + ":"
        self.recovery = EpistemicRecovery(database, subject_id)

    @staticmethod
    def descriptor(call: Any) -> dict[str, Any]:
        return {
            "call_id": call.call_id,
            "status": call.status,
            "request_hash": call.request_hash,
            "response_hash": call.response_hash,
            "error_code": call.error_code,
            "completed_at": call.completed_at,
        }

    def evidence(self, connection: Any, trigger: str) -> list[dict[str, Any]]:
        purpose = f"epistemic_review:{trigger}"
        if self.recovery.blocked(connection, purpose, routed=True):
            raise IntegrityError("closed epistemic round bypasses reconciliation")
        calls = self.recovery.calls(connection, purpose)
        if not calls or any(call.status not in {"succeeded", "failed"} for call in calls):
            raise IntegrityError("closed epistemic round has unresolved model calls")
        return [self.descriptor(call) for call in calls]

    def close(self, trigger: str, reason: str) -> bool:
        if reason not in REASONS:
            raise ValueError("invalid epistemic exit reason")
        with self.database.transaction() as connection:
            if connection.execute(
                "SELECT 1 FROM events WHERE event_id=?", (self.prefix + trigger,)
            ).fetchone():
                self.read(connection, trigger)
                return False
            purpose = f"epistemic_review:{trigger}"
            ModelLedger._check_lifecycle(connection, self.subject_id, purpose)
            if self.recovery.blocked(connection, purpose, routed=True):
                return False
            calls = self.recovery.calls(connection, purpose)
            if not calls or any(call.status not in {"succeeded", "failed"} for call in calls):
                return False
            success = any(call.status == "succeeded" for call in calls)
            if (reason == "model_failed") == success:
                raise IntegrityError("epistemic exit reason contradicts model outcome")
            if connection.execute(
                "SELECT 1 FROM epistemic_review_runs WHERE subject_id=? "
                "AND trigger_observation_id=?",
                (self.subject_id, trigger),
            ).fetchone():
                return False
            row = connection.execute(
                "SELECT subject_id,status FROM observations WHERE observation_id=?", (trigger,)
            ).fetchone()
            if row is None or row["subject_id"] != self.subject_id:
                raise IntegrityError("epistemic exit has no owned observation")
            self.events._append_connection(
                connection,
                self.subject_id,
                "epistemic_round_closed",
                "epistemic_supervisor",
                {
                    "format": FORMAT,
                    "subject_id": self.subject_id,
                    "trigger_observation_id": trigger,
                    "reason": reason,
                    "calls": [self.descriptor(call) for call in calls],
                },
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=self.clock(),
                event_id=self.prefix + trigger,
            )
            return True

    def read(self, connection: Any, trigger: str) -> dict[str, Any]:
        row = connection.execute(
            "SELECT * FROM events WHERE event_id=?", (self.prefix + trigger,)
        ).fetchone()
        if row is None:
            raise IntegrityError("epistemic exit receipt is missing")
        event = self.events._from_row(row)
        value = event.payload
        if (
            event.subject_id != self.subject_id
            or event.event_type != "epistemic_round_closed"
            or event.source != "epistemic_supervisor"
            or event.privacy_level != "private"
            or set(value) != {"format", "subject_id", "trigger_observation_id", "reason", "calls"}
            or value["format"] != FORMAT
            or value["subject_id"] != self.subject_id
            or value["trigger_observation_id"] != trigger
            or not isinstance(value["reason"], str)
            or value["reason"] not in REASONS
            or not isinstance(value["calls"], list)
            or value["calls"] != self.evidence(connection, trigger)
        ):
            raise IntegrityError("epistemic exit receipt is invalid")
        success = any(call["status"] == "succeeded" for call in value["calls"])
        if (value["reason"] == "model_failed") == success:
            raise IntegrityError("epistemic exit reason does not match call evidence")
        observation = connection.execute(
            "SELECT subject_id FROM observations WHERE observation_id=?", (trigger,)
        ).fetchone()
        if observation is None or observation["subject_id"] != self.subject_id:
            raise IntegrityError("epistemic exit observation belongs to another subject")
        if connection.execute(
            "SELECT 1 FROM epistemic_review_runs WHERE subject_id=? AND trigger_observation_id=?",
            (self.subject_id, trigger),
        ).fetchone():
            raise IntegrityError("epistemic trigger has conflicting terminal outcomes")
        return value

    def verify_connection(self, connection: Any, *, full: bool = False) -> int:
        count = 0
        seen = set()
        # The reserved subject namespace is a primary-key range, so scheduling
        # never scans unrelated event history. Validate each receipt before it
        # can hide a trigger, using archive-aware payload reads.
        for row in connection.execute(
            "SELECT event_id FROM events WHERE event_id>=? AND event_id<? ORDER BY event_id",
            (self.prefix, self.prefix[:-1] + ";"),
        ):
            event_id = str(row["event_id"])
            if not event_id[len(self.prefix) :]:
                raise IntegrityError("epistemic exit event identity is invalid")
            self.read(connection, event_id[len(self.prefix) :])
            seen.add(event_id)
            count += 1
        if full:
            for row in connection.execute(
                "SELECT event_id FROM events WHERE subject_id=? "
                "AND event_type='epistemic_round_closed'",
                (self.subject_id,),
            ):
                if row["event_id"] not in seen:
                    raise IntegrityError("epistemic exit is outside its reserved namespace")
        return count
