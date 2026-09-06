from __future__ import annotations

from typing import Any

from .database import Database
from .errors import IntegrityError, InvalidTransitionError, NotFoundError
from .events import EventStore
from .types import RuntimeState, canonical_json, new_id, utc_now

ALLOWED_TRANSITIONS: dict[str, frozenset[str]] = {
    "booting": frozenset({"orienting", "active", "stopped"}),
    "orienting": frozenset({"active", "stopped"}),
    "active": frozenset({"paused", "winding_down", "stopped"}),
    "paused": frozenset({"active", "stopped"}),
    "winding_down": frozenset({"reflective_sleep", "stopped"}),
    "reflective_sleep": frozenset({"deep_sleep", "stopped"}),
    "deep_sleep": frozenset({"waking", "stopped"}),
    "waking": frozenset({"active", "stopped"}),
    "stopped": frozenset({"booting", "resetting"}),
    "resetting": frozenset({"booting", "stopped"}),
}


class LifecycleManager:
    def __init__(self, database: Database, event_store: EventStore, subject_id: str):
        self.database = database
        self.event_store = event_store
        self.subject_id = subject_id

    def ensure_initial(
        self, state: str = "stopped", reason: str = "initialization"
    ) -> RuntimeState:
        if state not in ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown lifecycle state: {state}")
        if not reason.strip():
            raise ValueError("initial lifecycle reason is required")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_state WHERE subject_id = ?", (self.subject_id,)
            ).fetchone()
            if row:
                return self._from_row(row)
            changed_at = utc_now()
            connection.execute(
                "INSERT INTO runtime_state(subject_id, state, reason, version, changed_at) "
                "VALUES (?, ?, ?, 0, ?)",
                (self.subject_id, state, reason, changed_at),
            )
            return RuntimeState(self.subject_id, state, reason, 0, changed_at)

    def current(self) -> RuntimeState:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_state WHERE subject_id = ?", (self.subject_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"lifecycle state not found: {self.subject_id}")
        return self._from_row(row)

    def transition(self, new_state: str, reason: str, *, actor: str = "runtime") -> RuntimeState:
        if new_state not in ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown lifecycle state: {new_state}")
        if not reason.strip():
            raise ValueError("transition reason is required")
        if not actor.strip():
            raise ValueError("transition actor is required")
        with self.database.transaction() as connection:
            return self._transition_connection(connection, new_state, reason, actor=actor)

    def safe_pause(
        self, reason: str = "integrity policy requested safe pause", *, actor: str
    ) -> RuntimeState:
        """Fail closed even when event-chain damage prevents a normal transition event."""
        current = self.current()
        if current.state == "paused":
            return current
        if current.state != "active":
            raise InvalidTransitionError(f"cannot safe-pause from state {current.state}")
        try:
            return self.transition("paused", reason, actor=actor)
        except IntegrityError as integrity_error:
            changed_at = utc_now()
            with self.database.transaction() as connection:
                row = connection.execute(
                    "SELECT * FROM runtime_state WHERE subject_id = ?", (self.subject_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError(
                        f"lifecycle state not found: {self.subject_id}"
                    ) from integrity_error
                if row["state"] == "paused":
                    return self._from_row(row)
                if row["state"] != "active":
                    raise InvalidTransitionError(
                        f"cannot safe-pause from state {row['state']}"
                    ) from integrity_error
                version = int(row["version"]) + 1
                connection.execute(
                    "UPDATE runtime_state SET state = 'paused', reason = ?, version = ?, "
                    "changed_at = ? WHERE subject_id = ?",
                    (reason, version, changed_at, self.subject_id),
                )
                connection.execute(
                    "INSERT INTO audit_records(audit_id, subject_id, action, actor, payload_json, "
                    "occurred_at) VALUES (?, ?, 'integrity_safe_pause_fallback', ?, ?, ?)",
                    (
                        new_id("audit"),
                        self.subject_id,
                        actor,
                        canonical_json(
                            {
                                "from": "active",
                                "to": "paused",
                                "reason": reason,
                                "version": version,
                            }
                        ),
                        changed_at,
                    ),
                )
                return RuntimeState(self.subject_id, "paused", reason, version, changed_at)

    def _transition_connection(
        self, connection: Any, new_state: str, reason: str, *, actor: str = "runtime"
    ) -> RuntimeState:
        """Transition while the caller owns an enclosing transaction.

        Sleep and recovery need lifecycle and domain records to commit atomically. This protected
        primitive deliberately shares the same validation as the public transition method.
        """
        if new_state not in ALLOWED_TRANSITIONS:
            raise ValueError(f"unknown lifecycle state: {new_state}")
        if not reason.strip():
            raise ValueError("transition reason is required")
        if not actor.strip():
            raise ValueError("transition actor is required")
        row = connection.execute(
            "SELECT * FROM runtime_state WHERE subject_id = ?", (self.subject_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"lifecycle state not found: {self.subject_id}")
        current = row["state"]
        if new_state not in ALLOWED_TRANSITIONS[current]:
            raise InvalidTransitionError(f"cannot transition {current} -> {new_state}")
        changed_at = utc_now()
        version = int(row["version"]) + 1
        connection.execute(
            "UPDATE runtime_state SET state = ?, reason = ?, version = ?, changed_at = ? "
            "WHERE subject_id = ?",
            (new_state, reason, version, changed_at, self.subject_id),
        )
        self.event_store._append_connection(
            connection,
            self.subject_id,
            "lifecycle_transition",
            actor,
            {"from": current, "to": new_state, "reason": reason, "version": version},
            privacy_level="public_status",
            causal_parent_ids=(),
            occurred_at=changed_at,
            event_id=None,
        )
        return RuntimeState(self.subject_id, new_state, reason, version, changed_at)

    def recover_for_restart(self, reason: str = "process restart recovery") -> RuntimeState:
        """Record a crash/restart boundary without pretending it was a normal transition."""
        if not reason.strip():
            raise ValueError("restart recovery reason is required")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_state WHERE subject_id = ?", (self.subject_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"lifecycle state not found: {self.subject_id}")
            current = self._from_row(row)
            if current.state == "booting":
                return current
            if current.state in {"stopped", "resetting"}:
                raise InvalidTransitionError(f"cannot recover restart from state {current.state}")
            if current.state == "paused":
                return current
            changed_at = utc_now()
            version = current.version + 1
            sleep_states = {
                "winding_down",
                "reflective_sleep",
                "deep_sleep",
                "waking",
            }
            recovered_state = current.state if current.state in sleep_states else "booting"
            connection.execute(
                "UPDATE runtime_state SET state = ?, reason = ?, version = ?, "
                "changed_at = ? WHERE subject_id = ?",
                (recovered_state, reason, version, changed_at, self.subject_id),
            )
            self.event_store._append_connection(
                connection,
                self.subject_id,
                "lifecycle_recovery",
                "supervisor",
                {
                    "from": current.state,
                    "to": recovered_state,
                    "reason": reason,
                    "version": version,
                },
                privacy_level="public_status",
                causal_parent_ids=(),
                occurred_at=changed_at,
                event_id=None,
            )
            return RuntimeState(self.subject_id, recovered_state, reason, version, changed_at)

    @staticmethod
    def _from_row(row: Any) -> RuntimeState:
        state = row["state"]
        version = int(row["version"])
        if state not in ALLOWED_TRANSITIONS or version < 0:
            raise IntegrityError(f"invalid persisted lifecycle state: {state!r} v{version}")
        return RuntimeState(row["subject_id"], state, row["reason"], version, row["changed_at"])
