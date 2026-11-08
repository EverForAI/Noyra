"""Read-only communication progress, separate from durable send intent."""

from __future__ import annotations

from typing import Any

from noyra.core.delivery_state import verified_delivery_status


def communication_view(connection: Any, interaction: Any) -> dict[str, Any]:
    if interaction["direction"] != "outgoing":
        return {
            "communication_status": "not_applicable",
            "delivery_reason": "incoming_message",
            "delivery_statuses": [],
            "deliveries_truncated": False,
        }
    rows = connection.execute(
        "SELECT delivery_id,subject_id,status FROM interaction_deliveries "
        "WHERE subject_id=? AND interaction_id=? ORDER BY rowid DESC LIMIT 11",
        (interaction["subject_id"], interaction["interaction_id"]),
    ).fetchall()
    statuses = [verified_delivery_status(connection, row) for row in rows[:10]]
    truncated = len(rows) > 10
    if truncated:
        state, reason = "delivery_sample_incomplete", "more_delivery_records"
    elif statuses:
        state = next(
            status
            for status in ("unknown", "sending", "queued", "failed", "cancelled", "delivered")
            if status in statuses
        )
        reason = "verified_delivery_state"
    elif interaction["channel"] == "web" and interaction["kind"] != "help_request":
        state, reason = "local_available", "local_message_recorded"
    else:
        # No row is evidence of no scheduled delivery, not evidence that no
        # transport exists or that a provider accepted the message.
        state, reason = "not_scheduled", "no_delivery_record"
    return {
        "communication_status": state,
        "delivery_reason": reason,
        "delivery_statuses": statuses,
        "deliveries_truncated": truncated,
    }
