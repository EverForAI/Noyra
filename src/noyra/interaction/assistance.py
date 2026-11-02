"""Free public help and private, participant-bound replies; no payment dependency."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, strict_json_loads, utc_now

from .expression import ExpressionProposal, ExpressionStore

_REPLY_INDEX_VERSION = 1
_REPLY_INDEX_BACKFILL_BATCH = 8
_REPLY_INDEX_PAGE_SIZE = 64


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

    def _reply_cursor_key(self, request_id: str) -> str:
        return "assistance_reply_cursor:" + content_hash([self.subject, request_id])

    def _reply_index_prefix(self, request_id: str) -> str:
        return (
            f"assistance_reply_index:v{_REPLY_INDEX_VERSION}:"
            f"{content_hash(self.subject)}:{content_hash(request_id)}:"
        )

    def _reply_backfill_key(self) -> str:
        return f"assistance_reply_backfill:v{_REPLY_INDEX_VERSION}:{content_hash(self.subject)}"

    @staticmethod
    def _reply_sort_time(value: str) -> str:
        try:
            parsed = datetime.fromisoformat(value)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("naive timestamp")
            return parsed.astimezone(UTC).strftime("%Y%m%dT%H%M%S.%fZ")
        except (TypeError, ValueError) as error:
            raise IntegrityError("assistance reply timestamp is invalid") from error

    def _reply_index_key(self, request_id: str, created_at: str, interaction_id: str) -> str:
        if not interaction_id.startswith("int_") or ":" in interaction_id:
            raise IntegrityError("assistance reply interaction identity is invalid")
        return (
            self._reply_index_prefix(request_id)
            + self._reply_sort_time(created_at)
            + ":"
            + interaction_id
        )

    def _put_reply_index_connection(
        self,
        connection: Any,
        request_id: str,
        interaction_id: str,
        entity_id: str,
        created_at: str,
        event_id: str,
        payload_hash: str,
    ) -> None:
        key = self._reply_index_key(request_id, created_at, interaction_id)
        handled = connection.execute(
            "SELECT 1 FROM events WHERE subject_id=? "
            "AND event_id='evt_help_considered_'||? LIMIT 1",
            (self.subject, interaction_id),
        ).fetchone()
        if handled is not None:
            connection.execute("DELETE FROM schema_meta WHERE key=?", (key,))
            return
        record = {
            "version": _REPLY_INDEX_VERSION,
            "subject_id": self.subject,
            "request_id": request_id,
            "interaction_id": interaction_id,
            "entity_id": entity_id,
            "created_at": created_at,
            "event_id": event_id,
            "payload_hash": payload_hash,
        }
        value = canonical_json({"record": record, "record_hash": content_hash(record)})
        existing = connection.execute(
            "SELECT value FROM schema_meta WHERE key=?", (key,)
        ).fetchone()
        if existing is not None:
            if existing[0] != value:
                raise IntegrityError("assistance reply index conflicts with immutable evidence")
            return
        connection.execute("INSERT INTO schema_meta(key,value) VALUES(?,?)", (key, value))

    def _reply_backfill_state(self, connection: Any) -> tuple[str, str] | None:
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key=?", (self._reply_backfill_key(),)
        ).fetchone()
        if row is None:
            return None
        value = strict_json_loads(row[0])
        if not isinstance(value, dict) or set(value) != {
            "version",
            "subject_id",
            "cursor_event_id",
            "upper_event_id",
            "state_hash",
        }:
            raise IntegrityError("assistance reply backfill state is invalid")
        state = {
            key: value[key]
            for key in ("version", "subject_id", "cursor_event_id", "upper_event_id")
        }
        if (
            type(state["version"]) is not int
            or state["version"] != _REPLY_INDEX_VERSION
            or state["subject_id"] != self.subject
            or type(state["cursor_event_id"]) is not str
            or type(state["upper_event_id"]) is not str
            or state["cursor_event_id"] > state["upper_event_id"]
            or value["state_hash"] != content_hash(state)
        ):
            raise IntegrityError("assistance reply backfill state hash or bounds are invalid")
        return state["cursor_event_id"], state["upper_event_id"]

    def _write_reply_backfill_state(
        self, connection: Any, cursor_event_id: str, upper_event_id: str
    ) -> None:
        state = {
            "version": _REPLY_INDEX_VERSION,
            "subject_id": self.subject,
            "cursor_event_id": cursor_event_id,
            "upper_event_id": upper_event_id,
        }
        value = dict(state, state_hash=content_hash(state))
        connection.execute(
            "INSERT INTO schema_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (self._reply_backfill_key(), canonical_json(value)),
        )

    def advance_reply_index(self, *, scan_limit: int = _REPLY_INDEX_BACKFILL_BATCH) -> bool:
        """Backfill legacy reply links in bounded, restartable batches."""
        limit = max(1, min(scan_limit, 64))
        with self.database.connection() as connection:
            state = self._reply_backfill_state(connection)
        if state is None:
            with self.database.transaction() as connection:
                state = self._reply_backfill_state(connection)
                if state is None:
                    upper = str(
                        connection.execute(
                            "SELECT COALESCE(MAX(event_id),'') FROM events WHERE subject_id=?",
                            (self.subject,),
                        ).fetchone()[0]
                    )
                    state = ("", upper)
                    self._write_reply_backfill_state(connection, "", upper)
        assert state is not None
        if state[0] >= state[1]:
            return True

        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT event_id FROM events WHERE subject_id=? "
                "AND event_type='assistance_reply' AND event_id>? AND event_id<=? "
                "ORDER BY event_id LIMIT ?",
                (self.subject, state[0], state[1], limit),
            ).fetchall()
            scan_to = state[1] if len(rows) < limit else str(rows[-1]["event_id"])
            events = []
            event_store = EventStore(self.database)
            for entry in rows:
                event = connection.execute(
                    "SELECT * FROM events WHERE subject_id=? AND event_id=? "
                    "AND event_type='assistance_reply'",
                    (self.subject, entry["event_id"]),
                ).fetchone()
                if event is None:
                    raise IntegrityError("assistance reply event disappeared during backfill")
                payload = event_store.payload_from_row(event, connection=connection)
                if (
                    set(payload) != {"request_id", "interaction_id", "entity_id"}
                    or not all(isinstance(payload[key], str) and payload[key] for key in payload)
                    or content_hash(payload) != event["payload_hash"]
                    or event["event_id"]
                    != "evt_help_reply_" + content_hash(payload["interaction_id"])
                ):
                    raise IntegrityError("legacy assistance reply evidence is invalid")
                request = connection.execute(
                    "SELECT status FROM autonomous_project_assistance_requests "
                    "WHERE subject_id=? AND request_id=?",
                    (self.subject, payload["request_id"]),
                ).fetchone()
                message = connection.execute(
                    "SELECT i.created_at,i.direction,p.entity_id,p.visibility "
                    "FROM interactions i JOIN participant_message_links p "
                    "ON p.subject_id=i.subject_id AND p.interaction_id=i.interaction_id "
                    "WHERE i.subject_id=? AND i.interaction_id=?",
                    (self.subject, payload["interaction_id"]),
                ).fetchone()
                if (
                    request is None
                    or message is None
                    or message["direction"] != "incoming"
                    or message["visibility"] != "private"
                    or message["entity_id"] != payload["entity_id"]
                ):
                    raise IntegrityError("legacy assistance reply ownership is invalid")
                handled = connection.execute(
                    "SELECT 1 FROM events WHERE subject_id=? "
                    "AND event_id='evt_help_considered_'||? LIMIT 1",
                    (self.subject, payload["interaction_id"]),
                ).fetchone()
                if request["status"] == "open" and handled is None:
                    events.append((payload, message["created_at"], event["payload_hash"]))

        with self.database.transaction() as connection:
            current = self._reply_backfill_state(connection)
            if current is None or current[1] != state[1]:
                raise IntegrityError("assistance reply backfill checkpoint changed")
            for payload, created_at, payload_hash in events:
                self._put_reply_index_connection(
                    connection,
                    payload["request_id"],
                    payload["interaction_id"],
                    payload["entity_id"],
                    created_at,
                    "evt_help_reply_" + content_hash(payload["interaction_id"]),
                    payload_hash,
                )
            self._write_reply_backfill_state(connection, max(current[0], scan_to), current[1])
            updated = self._reply_backfill_state(connection)
        assert updated is not None
        return updated[0] >= updated[1]

    def advance_reply(self, request_id: str, reply: Any) -> None:
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO schema_meta(key,value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (
                    self._reply_cursor_key(request_id),
                    canonical_json([str(reply["created_at"]), str(reply["interaction_id"])]),
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
                "SELECT r.request_id,p.* "
                "FROM autonomous_project_assistance_requests r JOIN actions a ON "
                "a.subject_id=r.subject_id "
                "AND a.idempotency_key='expression:help:'||r.request_id AND a.status='succeeded' "
                "JOIN public_publications p ON p.subject_id=r.subject_id "
                "AND p.publication_id=json_extract(a.result_json,'$.publication_id') "
                "WHERE r.subject_id=? AND r.status='open' AND p.status='published' "
                "ORDER BY r.created_at,r.request_id LIMIT 50",
                (self.subject,),
            ).fetchall()
        from .public_archive import PublicArchive

        result = []
        for row in rows:
            with self.database.connection() as connection:
                publication = {
                    key: value for key, value in dict(row).items() if key != "request_id"
                }
                item = PublicArchive._publication(publication, connection=connection)
            result.append(
                {
                    "request_id": row["request_id"],
                    "publication_id": item["id"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "body": item["body"],
                    "published_at": item["occurred_at"],
                }
            )
        return result

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
        self._link_reply_connection(connection, request_id, interaction_id, entity_id)

    def _link_reply_connection(
        self, connection: Any, request_id: str, interaction_id: str, entity_id: str
    ) -> None:
        # External routes are authenticated by InboundStore; public callers
        # retain link_reply's publication check above.
        request = connection.execute(
            "SELECT 1 FROM autonomous_project_assistance_requests "
            "WHERE subject_id=? AND request_id=? AND status='open'",
            (self.subject, request_id),
        ).fetchone()
        if request is None:
            raise PermissionError("assistance request unavailable")
        source = connection.execute(
            "SELECT i.created_at FROM participant_message_links p JOIN interactions i "
            "ON i.interaction_id=p.interaction_id AND i.subject_id=p.subject_id "
            "WHERE p.subject_id=? AND p.interaction_id=? AND p.entity_id=? "
            "AND p.visibility='private' AND i.direction='incoming'",
            (self.subject, interaction_id, entity_id),
        ).fetchone()
        if source is None:
            raise PermissionError("help reply participant ownership mismatch")
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
        event = EventStore(self.database)._append_connection(
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
        self._put_reply_index_connection(
            connection,
            request_id,
            interaction_id,
            entity_id,
            str(source["created_at"]),
            event_id,
            event.payload_hash,
        )

    def _decode_reply_index(self, key: str, value: str, request_id: str) -> dict[str, Any]:
        entry = strict_json_loads(value)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"record", "record_hash"}
            or not isinstance(entry["record"], dict)
            or not isinstance(entry["record_hash"], str)
        ):
            raise IntegrityError("assistance reply index record is invalid")
        record = entry["record"]
        fields = {
            "version",
            "subject_id",
            "request_id",
            "interaction_id",
            "entity_id",
            "created_at",
            "event_id",
            "payload_hash",
        }
        if set(record) != fields:
            raise IntegrityError("assistance reply index fields are invalid")
        if (
            type(record["version"]) is not int
            or record["version"] != _REPLY_INDEX_VERSION
            or any(
                not isinstance(record[name], str) or not record[name]
                for name in fields - {"version"}
            )
            or record["subject_id"] != self.subject
            or record["request_id"] != request_id
            or entry["record_hash"] != content_hash(record)
            or key
            != self._reply_index_key(request_id, record["created_at"], record["interaction_id"])
            or record["event_id"] != "evt_help_reply_" + content_hash(record["interaction_id"])
        ):
            raise IntegrityError("assistance reply index integrity mismatch")
        return record

    def _delete_reply_index_connection(
        self, connection: Any, request_id: str, interaction_id: str, created_at: str
    ) -> None:
        connection.execute(
            "DELETE FROM schema_meta WHERE key=?",
            (self._reply_index_key(request_id, created_at, interaction_id),),
        )

    def discard_reply(self, request_id: str, interaction_id: str) -> None:
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT created_at FROM interactions WHERE subject_id=? AND interaction_id=?",
                (self.subject, interaction_id),
            ).fetchone()
            if row is not None:
                self._delete_reply_index_connection(
                    connection, request_id, interaction_id, str(row["created_at"])
                )

    def quarantine_reply(self, request_id: str, interaction_id: str) -> None:
        with self.database.transaction() as connection:
            event = connection.execute(
                "SELECT * FROM events WHERE subject_id=? AND event_id=? "
                "AND event_type='assistance_reply'",
                (self.subject, "evt_help_reply_" + content_hash(interaction_id)),
            ).fetchone()
            if event is None:
                raise PermissionError("help reply evidence is unavailable")
            source = EventStore(self.database).payload_from_row(event, connection=connection)
            if (
                source.get("request_id") != request_id
                or source.get("interaction_id") != interaction_id
                or content_hash(source) != event["payload_hash"]
            ):
                raise IntegrityError("help reply quarantine source mismatch")
            EventStore(self.database)._append_connection(
                connection,
                self.subject,
                "assistance_reply_quarantined",
                "subject",
                {"interaction_id": interaction_id, "request_id": request_id},
                privacy_level="private",
                causal_parent_ids=(),
                occurred_at=utc_now(),
                event_id="evt_help_considered_" + interaction_id,
            )
            row = connection.execute(
                "SELECT created_at FROM interactions WHERE subject_id=? AND interaction_id=?",
                (self.subject, interaction_id),
            ).fetchone()
            if row is not None:
                self._delete_reply_index_connection(
                    connection, request_id, interaction_id, str(row["created_at"])
                )

    def replies(self, request_id: str, *, limit: int = 1) -> list[Any]:
        cursor_key = self._reply_cursor_key(request_id)
        prefix = self._reply_index_prefix(request_id)
        upper = prefix + "g"
        with self.database.connection() as connection:
            saved = connection.execute(
                "SELECT value FROM schema_meta WHERE key=?", (cursor_key,)
            ).fetchone()
            after = ("", "")
            if saved is not None:
                value = strict_json_loads(saved[0])
                if (
                    not isinstance(value, list)
                    or len(value) != 2
                    or any(not isinstance(item, str) for item in value)
                ):
                    raise IntegrityError("assistance reply cursor is invalid")
                try:
                    cursor_time = datetime.fromisoformat(value[0])
                except ValueError as error:
                    raise IntegrityError("assistance reply cursor is invalid") from error
                if cursor_time.tzinfo is None or cursor_time.utcoffset() is None:
                    raise IntegrityError("assistance reply cursor is not timezone-aware")
                after = (value[0], value[1])
        after_key = prefix if not after[0] else self._reply_index_key(request_id, *after)
        with self.database.connection() as connection:
            index_rows = connection.execute(
                "SELECT key,value FROM schema_meta WHERE key>? AND key<? ORDER BY key LIMIT ?",
                (after_key, upper, max(1, min(limit, _REPLY_INDEX_PAGE_SIZE))),
            ).fetchall()
            if not index_rows and saved is not None:
                index_rows = connection.execute(
                    "SELECT key,value FROM schema_meta WHERE key>=? AND key<? ORDER BY key LIMIT ?",
                    (prefix, upper, max(1, min(limit, _REPLY_INDEX_PAGE_SIZE))),
                ).fetchall()
            results: list[Any] = []
            last_record: dict[str, Any] | None = None
            stale: list[tuple[str, str, str]] = []
            event_store = EventStore(self.database)
            for index_row in index_rows:
                record = self._decode_reply_index(
                    str(index_row["key"]), str(index_row["value"]), request_id
                )
                last_record = record
                request = connection.execute(
                    "SELECT status FROM autonomous_project_assistance_requests "
                    "WHERE subject_id=? AND request_id=?",
                    (self.subject, request_id),
                ).fetchone()
                message = connection.execute(
                    "SELECT i.*,p.entity_id AS linked_entity,p.visibility FROM interactions i "
                    "JOIN participant_message_links p ON p.subject_id=i.subject_id "
                    "AND p.interaction_id=i.interaction_id WHERE i.subject_id=? "
                    "AND i.interaction_id=?",
                    (self.subject, record["interaction_id"]),
                ).fetchone()
                event = connection.execute(
                    "SELECT * FROM events WHERE subject_id=? AND event_id=? "
                    "AND event_type='assistance_reply'",
                    (self.subject, record["event_id"]),
                ).fetchone()
                if request is None or message is None or event is None:
                    raise IntegrityError("assistance reply index references missing evidence")
                payload = event_store.payload_from_row(event, connection=connection)
                if (
                    content_hash(payload) != event["payload_hash"]
                    or event["payload_hash"] != record["payload_hash"]
                    or payload.get("request_id") != request_id
                    or payload.get("interaction_id") != record["interaction_id"]
                    or payload.get("entity_id") != record["entity_id"]
                    or message["direction"] != "incoming"
                    or message["linked_entity"] != record["entity_id"]
                    or message["visibility"] != "private"
                ):
                    raise IntegrityError("assistance reply index contradicts event or participant")
                handled = connection.execute(
                    "SELECT 1 FROM events WHERE subject_id=? "
                    "AND event_id='evt_help_considered_'||? LIMIT 1",
                    (self.subject, record["interaction_id"]),
                ).fetchone()
                if request["status"] != "open" or handled is not None:
                    stale.append((request_id, record["interaction_id"], record["created_at"]))
                    continue
                if message["status"] in {"rejected", "silent", "expired"}:
                    stale.append((request_id, record["interaction_id"], record["created_at"]))
                    continue
                if message["status"] != "accepted":
                    continue
                item = dict(message)
                item["event_id"] = record["event_id"]
                results.append(item)
                if len(results) >= max(1, min(limit, _REPLY_INDEX_PAGE_SIZE)):
                    break

            if stale or last_record is not None:
                with self.database.transaction() as write_connection:
                    for stale_request, interaction_id, created_at in stale:
                        self._delete_reply_index_connection(
                            write_connection, stale_request, interaction_id, created_at
                        )
                    if last_record is not None:
                        write_connection.execute(
                            "INSERT INTO schema_meta(key,value) VALUES(?,?) "
                            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                            (
                                cursor_key,
                                canonical_json(
                                    [last_record["created_at"], last_record["interaction_id"]]
                                ),
                            ),
                        )
            return results

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
                "SELECT i.status,i.created_at,p.visibility,p.entity_id FROM interactions i "
                "JOIN participant_message_links p ON p.subject_id=i.subject_id "
                "AND p.interaction_id=i.interaction_id WHERE i.subject_id=? AND i.interaction_id=?",
                (self.subject, interaction_id),
            ).fetchone()
            if (
                request is None
                or message is None
                or message["status"] != "accepted"
                or message["visibility"] != "private"
            ):
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
                or payload.get("entity_id") != message["entity_id"]
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
            self._delete_reply_index_connection(
                connection, request_id, interaction_id, str(message["created_at"])
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
                prefix = self._reply_index_prefix(request_id)
                connection.execute(
                    "DELETE FROM schema_meta WHERE key>=? AND key<?",
                    (prefix, prefix + "g"),
                )
