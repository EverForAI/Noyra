"""Free public help and private, participant-bound replies; no payment dependency."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, strict_json_loads, utc_now

from .expression import ExpressionProposal, ExpressionStore


class AssistanceStore:
    def __init__(self, database: Database, subject: str):
        self.database, self.subject = database, subject

    def pending(self) -> list[Any]:
        with self.database.connection() as connection:
            saved = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?", ("assistance_cursor:" + self.subject,)
            ).fetchone()
            after = ("", "")
            if saved is not None:
                value = strict_json_loads(saved[0])
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or any(not isinstance(item, str) for item in value)
                ):
                    raise IntegrityError("assistance service cursor is invalid")
                datetime.fromisoformat(value[0])
                after = (value[0], value[1])
            rows = connection.execute(
                "SELECT * FROM autonomous_project_assistance_requests "
                "WHERE subject_id=? AND status='open' AND (created_at,request_id)>(?,?) "
                "ORDER BY created_at,request_id LIMIT 16",
                (self.subject, *after),
            ).fetchall()
            if not rows and saved is not None:
                rows = connection.execute(
                    "SELECT * FROM autonomous_project_assistance_requests "
                    "WHERE subject_id=? AND status='open' ORDER BY created_at,request_id LIMIT 16",
                    (self.subject,),
                ).fetchall()
        return list(rows)

    def advance(self, request: Any) -> None:
        # A waiting/unknown reply yields to other help requests. Advancing this
        # cursor changes neither the request nor any model attempt identity.
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    "assistance_cursor:" + self.subject,
                    canonical_json([request["created_at"], request["request_id"]]),
                ),
            )

    def action(self, request: Any) -> Any:
        summary = str(request["public_summary"])
        # The private description and project internals never enter public
        # writing or external contact context.
        proposal = ExpressionProposal(
            operation="write",
            title="Noyra 想请教一个问题",
            summary=summary[:1_000],
            body=summary,
            reason="Ask for help without offering payment",
        )
        action = ExpressionStore(self.database, self.subject).prepare(
            proposal, "help:" + str(request["request_id"])
        )
        return action, proposal

    def public(self) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT r.request_id,p.publication_id,p.title,p.summary,p.body,p.published_at "
                "FROM autonomous_project_assistance_requests r JOIN actions a ON "
                "a.subject_id=r.subject_id "
                "AND a.idempotency_key='expression:help:'||r.request_id AND a.status='succeeded' "
                "JOIN public_publications p ON p.subject_id=r.subject_id "
                "AND p.publication_id=json_extract(a.result_json,'$.publication_id') "
                "WHERE r.subject_id=? AND r.status='open' AND p.status='published' "
                "ORDER BY r.created_at,r.request_id LIMIT 50",
                (self.subject,),
            ).fetchall()
        return [dict(row) for row in rows]

    def link_reply(
        self, connection: Any, request_id: str, interaction_id: str, entity_id: str
    ) -> None:
        row = connection.execute(
            "SELECT r.request_id FROM autonomous_project_assistance_requests r "
            "JOIN actions a ON a.subject_id=r.subject_id AND "
            "a.idempotency_key='expression:help:'||r.request_id "
            "JOIN public_publications p ON p.subject_id=r.subject_id "
            "AND p.publication_id=json_extract(a.result_json,'$.publication_id') "
            "WHERE r.subject_id=? AND r.request_id=? AND r.status='open' "
            "AND a.status='succeeded' AND p.status='published'",
            (self.subject, request_id),
        ).fetchone()
        if row is None:
            raise PermissionError("public assistance request unavailable")
        payload = {
            "request_id": request_id,
            "interaction_id": interaction_id,
            "entity_id": entity_id,
        }
        event_id = "evt_help_reply_" + content_hash(interaction_id)
        existing = connection.execute(
            "SELECT * FROM events WHERE event_id=?", (event_id,)
        ).fetchone()
        if existing is not None and existing["payload_hash"] != content_hash(payload):
            raise ValueError("message already replies to a different help request")
        EventStore(self.database)._append_connection(
            connection,
            self.subject,
            "assistance_reply",
            "participant",
            payload,
            privacy_level="private",
            causal_parent_ids=(),
            occurred_at=utc_now(),
            event_id=event_id,
        )

    def replies(self, request_id: str) -> list[Any]:
        with self.database.connection() as connection:
            # Match the exact request from the immutable link, while the
            # participant's ordinary message stays in its own private thread.
            return list(
                connection.execute(
                    "SELECT i.*,e.event_id FROM events e JOIN interactions i "
                    "ON i.subject_id=e.subject_id AND "
                    "i.interaction_id=json_extract(e.payload_json,'$.interaction_id') "
                    "WHERE e.subject_id=? AND e.event_type='assistance_reply' AND "
                    "json_valid(e.payload_json) "
                    "AND json_extract(e.payload_json,'$.request_id')=? AND i.direction='incoming' "
                    "AND i.status='accepted' AND NOT EXISTS(SELECT 1 "
                    "FROM events d "
                    "WHERE d.event_id='evt_help_considered_'||i.interaction_id) "
                    "ORDER BY i.created_at,i.interaction_id LIMIT 1",
                    (self.subject, request_id),
                ).fetchall()
            )

    def settle(self, request_id: str, interaction_id: str, call_id: str, disposition: str) -> None:
        if disposition not in {"continue", "wait", "end"}:
            raise ValueError("invalid help response disposition")
        with self.database.transaction() as connection:
            request = connection.execute(
                "SELECT * FROM autonomous_project_assistance_requests "
                "WHERE subject_id=? AND request_id=? AND status='open'",
                (self.subject, request_id),
            ).fetchone()
            message = connection.execute(
                "SELECT i.status,p.visibility FROM interactions i "
                "JOIN participant_message_links p ON p.subject_id=i.subject_id "
                "AND p.interaction_id=i.interaction_id WHERE i.subject_id=? AND i.interaction_id=?",
                (self.subject, interaction_id),
            ).fetchone()
            if request is None or message is None or message["status"] != "accepted":
                raise PermissionError("help reply is unavailable")
            link = connection.execute(
                "SELECT * FROM events WHERE event_id=? AND subject_id=?",
                ("evt_help_reply_" + content_hash(interaction_id), self.subject),
            ).fetchone()
            if link is None:
                raise PermissionError("help reply has no request linkage")
            payload = EventStore(self.database).payload_from_row(link, connection=connection)
            if (
                payload.get("request_id") != request_id
                or payload.get("interaction_id") != interaction_id
                or content_hash(payload) != link["payload_hash"]
            ):
                raise IntegrityError("help reply source mismatch")
            from noyra.cognition.assistance import HelpReplyChoice
            from noyra.model import ModelLedger

            call = ModelLedger(self.database).get_call(call_id)
            if (
                call.subject_id != self.subject
                or call.status != "succeeded"
                or call.purpose != "expression_help_reply:" + interaction_id
            ):
                raise PermissionError("help reply decision is unavailable")
            choice = HelpReplyChoice.model_validate_json(
                str((call.response or {}).get("content", ""))
            )
            if choice.disposition != disposition:
                raise PermissionError("help decision does not match model evidence")
            now = utc_now()
            payload = {
                "request_id": request_id,
                "interaction_id": interaction_id,
                "model_call_id": call_id,
                "disposition": disposition,
                "reason": choice.reason,
            }
            EventStore(self.database)._append_connection(
                connection,
                self.subject,
                "assistance_reply_considered",
                "subject",
                payload,
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=now,
                event_id="evt_help_considered_" + interaction_id,
            )
            if disposition != "wait":
                state = {
                    key: request[key]
                    for key in (
                        "subject_id",
                        "project_id",
                        "phase_id",
                        "request_kind",
                        "title",
                        "description",
                        "public_summary",
                        "status",
                        "created_at",
                        "updated_at",
                    )
                }
                state.update(
                    status="resolved" if disposition == "continue" else "withdrawn", updated_at=now
                )
                connection.execute(
                    "UPDATE autonomous_project_assistance_requests SET status=?,updated_at=?,"
                    "state_hash=? WHERE request_id=? AND subject_id=?",
                    (state["status"], now, content_hash(state), request_id, self.subject),
                )
