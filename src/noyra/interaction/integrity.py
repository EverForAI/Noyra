from __future__ import annotations

from typing import Any

from noyra.core.database import Database, public_post_identity_hash
from noyra.core.errors import IntegrityError
from noyra.core.payload_codec import decompress_text
from noyra.core.types import content_hash, strict_json_loads

from .diary import PublicDiaryStore
from .store import InteractionStore


class InteractionIntegrity:
    """Verify interaction decisions and the public diary privacy boundary."""

    def __init__(self, database: Database):
        self.database = database

    def verify(self, subject_id: str) -> dict[str, int]:
        counts: dict[str, int] = {}
        with self.database.read_transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise IntegrityError("interaction state contains broken foreign keys")
            interactions = connection.execute(
                "SELECT * FROM interactions WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in interactions:
                InteractionStore._from_row(row)
                if row["related_interaction_id"] is not None:
                    related = connection.execute(
                        "SELECT subject_id FROM interactions WHERE interaction_id = ?",
                        (row["related_interaction_id"],),
                    ).fetchone()
                    if related is None or related["subject_id"] != subject_id:
                        raise IntegrityError(
                            "interaction relationship crosses subject boundary: "
                            f"{row['interaction_id']}"
                        )
                decisions = connection.execute(
                    "SELECT * FROM interaction_decisions WHERE interaction_id = ? ORDER BY rowid",
                    (row["interaction_id"],),
                ).fetchall()
                expected_from = "offered"
                for decision in decisions:
                    expected_hash = content_hash(
                        {
                            "from_status": decision["from_status"],
                            "to_status": decision["to_status"],
                            "rationale": decision["rationale"],
                            "actor": decision["actor"],
                        }
                    )
                    if (
                        decision["from_status"] != expected_from
                        or decision["actor"] != "subject"
                        or decision["state_hash"] != expected_hash
                    ):
                        raise IntegrityError(
                            f"interaction decision mismatch: {row['interaction_id']}"
                        )
                    expected_from = decision["to_status"]
                if row["direction"] == "incoming":
                    if decisions and expected_from != row["status"]:
                        raise IntegrityError(
                            f"interaction current decision mismatch: {row['interaction_id']}"
                        )
                    if not decisions and row["status"] != "offered":
                        raise IntegrityError(
                            f"interaction missing decision history: {row['interaction_id']}"
                        )
                elif decisions or row["status"] != "sent":
                    raise IntegrityError(
                        f"outgoing interaction state mismatch: {row['interaction_id']}"
                    )
            counts["interactions"] = len(interactions)
            decisions = connection.execute(
                """SELECT interaction_decisions.* FROM interaction_decisions
                   JOIN interactions ON interactions.interaction_id =
                       interaction_decisions.interaction_id
                   WHERE interactions.subject_id = ?""",
                (subject_id,),
            ).fetchall()
            counts["interaction_decisions"] = len(decisions)

            bindings = connection.execute(
                "SELECT b.*, t.subject_id AS transport_subject, t.channel AS transport_channel "
                "FROM interaction_bindings b LEFT JOIN interaction_transports t "
                "ON t.transport_id = b.transport_id WHERE b.subject_id = ?",
                (subject_id,),
            ).fetchall()
            for binding in bindings:
                if (
                    binding["transport_subject"] != subject_id
                    or binding["transport_channel"] != binding["channel"]
                    or binding["role"] not in {"creator", "participant"}
                ):
                    raise IntegrityError(f"inbound binding mismatch: {binding['binding_id']}")
            counts["interaction_bindings"] = len(bindings)

            inbound_events = connection.execute(
                "SELECT e.*, t.subject_id AS transport_subject, t.channel AS transport_channel "
                "FROM interaction_inbound_events e LEFT JOIN interaction_transports t "
                "ON t.transport_id = e.transport_id WHERE e.subject_id = ?",
                (subject_id,),
            ).fetchall()
            for event in inbound_events:
                try:
                    reply_version = int(event["reply_context_version"])
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"inbound reply context version is invalid: {event['event_id']}"
                    ) from error
                reply_selector = event["reply_selector"]
                if (
                    event["transport_subject"] != subject_id
                    or event["transport_channel"] != event["channel"]
                    or not event["external_account_id"]
                    or reply_version not in {0, 1}
                    or (
                        reply_version == 0
                        and (event["external_thread_id"] is not None or reply_selector is not None)
                    )
                    or (
                        reply_version == 1
                        and (
                            (
                                event["channel"] == "qq"
                                and reply_selector
                                not in {"qq:user", "qq:group", "qq:channel", "qq:dm"}
                            )
                            or (event["channel"] == "feishu" and reply_selector != "feishu:chat_id")
                            or (
                                event["channel"] not in {"qq", "feishu"}
                                and reply_selector is not None
                            )
                        )
                    )
                    or (
                        event["status"] == "processed"
                        and (event["interaction_id"] is None or event["processed_at"] is None)
                    )
                    or (
                        event["status"] == "received"
                        and (
                            event["processed_at"] is not None or event["interaction_id"] is not None
                        )
                    )
                    or (
                        event["status"] == "rejected"
                        and (event["processed_at"] is None or event["interaction_id"] is not None)
                    )
                ):
                    raise IntegrityError(f"inbound event mismatch: {event['event_id']}")
                if event["external_thread_id"] is not None:
                    linked_thread = connection.execute(
                        "SELECT 1 FROM interaction_threads WHERE subject_id = ? "
                        "AND transport_id = ? AND external_account_id = ? "
                        "AND conversation_id = ? AND external_thread_id = ?",
                        (
                            subject_id,
                            event["transport_id"],
                            event["external_account_id"],
                            event["conversation_id"],
                            event["external_thread_id"],
                        ),
                    ).fetchone()
                    if linked_thread is None:
                        raise IntegrityError(f"inbound reply thread mismatch: {event['event_id']}")
                if event["interaction_id"] is not None:
                    linked = connection.execute(
                        "SELECT subject_id, direction, channel, counterparty, content_hash "
                        "FROM interactions "
                        "WHERE interaction_id = ?",
                        (event["interaction_id"],),
                    ).fetchone()
                    if (
                        linked is None
                        or linked["subject_id"] != subject_id
                        or linked["direction"] != "incoming"
                        or linked["channel"] != event["channel"]
                        or linked["counterparty"]
                        != (
                            event["external_sender_id"]
                            if event["channel"] == "email"
                            else event["conversation_id"]
                        )
                        or linked["content_hash"] != event["content_hash"]
                    ):
                        raise IntegrityError(f"inbound interaction mismatch: {event['event_id']}")
            counts["interaction_inbound_events"] = len(inbound_events)

            threads = connection.execute(
                "SELECT t.*, p.subject_id AS transport_subject, p.channel AS transport_channel "
                "FROM interaction_threads t LEFT JOIN interaction_transports p "
                "ON p.transport_id = t.transport_id WHERE t.subject_id = ?",
                (subject_id,),
            ).fetchall()
            for thread in threads:
                if (
                    thread["transport_subject"] != subject_id
                    or thread["transport_channel"] != thread["channel"]
                    or not thread["external_account_id"]
                ):
                    raise IntegrityError(f"interaction thread mismatch: {thread['thread_id']}")
            counts["interaction_threads"] = len(threads)

            social_runs = connection.execute(
                "SELECT * FROM relationship_social_runs WHERE subject_id = ?",
                (subject_id,),
            ).fetchall()
            for row in social_runs:
                self._verify_social_run(connection, subject_id, row)
            counts["relationship_social_runs"] = len(social_runs)

            entries = connection.execute(
                "SELECT * FROM public_diary_entries WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for row in entries:
                PublicDiaryStore._from_row(row)
                sleep = connection.execute(
                    "SELECT subject_id, status FROM sleep_runs WHERE sleep_id = ?",
                    (row["source_sleep_id"],),
                ).fetchone()
                if (
                    sleep is None
                    or sleep["subject_id"] != subject_id
                    or sleep["status"] != "complete"
                ):
                    raise IntegrityError(f"public diary source mismatch: {row['entry_id']}")
                reflection = connection.execute(
                    "SELECT public_diary_candidate FROM sleep_reflections WHERE sleep_id = ?",
                    (row["source_sleep_id"],),
                ).fetchone()
                if reflection is None:
                    raise IntegrityError(f"public diary reflection missing: {row['entry_id']}")
                if (
                    reflection["public_diary_candidate"] is not None
                    and reflection["public_diary_candidate"] != row["body"]
                ):
                    raise IntegrityError(f"public diary candidate mismatch: {row['entry_id']}")
            counts["public_diary_entries"] = len(entries)

            posts = connection.execute(
                "SELECT * FROM public_posts WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for post in posts:
                if (
                    content_hash(post["content"]) != post["content_hash"]
                    or public_post_identity_hash(post) != post["identity_hash"]
                    or post["status"]
                    not in {
                        "draft",
                        "pending_review",
                        "published",
                        "rejected",
                        "archived",
                    }
                    or (post["status"] == "published" and post["published_at"] is None)
                    or (
                        post["status"] in {"draft", "pending_review", "rejected"}
                        and post["published_at"] is not None
                    )
                ):
                    raise IntegrityError(f"public post mismatch: {post['post_id']}")
                history = connection.execute(
                    "SELECT * FROM public_post_moderation_events "
                    "WHERE subject_id = ? AND post_id = ? ORDER BY revision",
                    (subject_id, post["post_id"]),
                ).fetchall()
                if history and history[0]["from_status"] != "pending_review":
                    raise IntegrityError(f"public post moderation mismatch: {post['post_id']}")
                expected_status = "pending_review"
                previous_event_id: str | None = None
                for expected_revision, event in enumerate(history, start=1):
                    state = {
                        "event_id": event["event_id"],
                        "subject_id": event["subject_id"],
                        "post_id": event["post_id"],
                        "from_status": event["from_status"],
                        "to_status": event["to_status"],
                        "actor": event["actor"],
                        "reason": event["reason"],
                        "revision": event["revision"],
                        "previous_event_id": event["previous_event_id"],
                        "idempotency_key": event["idempotency_key"],
                        "created_at": event["created_at"],
                    }
                    allowed = {
                        "pending_review": {"published", "rejected"},
                        "published": {"archived"},
                        "rejected": {"archived"},
                    }
                    if (
                        event["from_status"] != expected_status
                        or event["revision"] != expected_revision
                        or event["previous_event_id"] != previous_event_id
                        or not isinstance(event["idempotency_key"], str)
                        or not event["idempotency_key"].strip()
                        or not event["actor"].strip()
                        or not event["reason"].strip()
                        or event["to_status"] not in allowed.get(expected_status, set())
                        or event["state_hash"] != content_hash(state)
                    ):
                        raise IntegrityError(f"public post moderation mismatch: {post['post_id']}")
                    expected_status = event["to_status"]
                    previous_event_id = str(event["event_id"])
                if expected_status != post["status"] and not (
                    not history and post["status"] in {"draft", "pending_review"}
                ):
                    raise IntegrityError(
                        f"public post moderation state mismatch: {post['post_id']}"
                    )
            counts["public_posts"] = len(posts)

            controls = connection.execute(
                "SELECT * FROM public_post_controls WHERE subject_id = ?", (subject_id,)
            ).fetchall()
            for control in controls:
                state = {
                    key: control[key]
                    for key in (
                        "subject_id",
                        "rate_limit_per_hour",
                        "queue_cap",
                        "captcha_ttl_seconds",
                        "captcha_max_attempts",
                        "captcha_mode",
                        "storage_cap_bytes",
                        "captcha_issue_limit_per_hour",
                        "captcha_global_rate_per_minute",
                        "updated_at",
                        "updated_by",
                    )
                }
                if not control["updated_by"].strip() or control["state_hash"] != content_hash(
                    state
                ):
                    raise IntegrityError("public post controls integrity mismatch")
            counts["public_post_controls"] = len(controls)
        return counts

    @staticmethod
    def _verify_social_run(connection: Any, subject_id: str, row: Any) -> None:
        proposal = InteractionIntegrity._json_value(
            row["proposal_json"], "social proposal JSON is invalid"
        )
        evidence_value = InteractionIntegrity._json_value(
            row["evidence_event_ids_json"], "social evidence JSON is invalid"
        )
        if not isinstance(proposal, dict) or content_hash(proposal) != row["proposal_hash"]:
            raise IntegrityError(f"social proposal mismatch: {row['social_id']}")
        if not isinstance(evidence_value, list) or not all(
            isinstance(item, str) for item in evidence_value
        ):
            raise IntegrityError(f"social evidence is invalid: {row['social_id']}")
        evidence = tuple(evidence_value)
        expected = content_hash(
            {
                "relationship_id": row["relationship_id"],
                "model_call_id": row["model_call_id"],
                "interaction_id": row["interaction_id"],
                "disposition": row["disposition"],
                "channel": row["channel"],
                "counterparty": row["counterparty"],
                "topic": row["topic"],
                "rationale": row["rationale"],
                "proposal_hash": row["proposal_hash"],
                "evidence_event_ids": list(evidence),
                "created_at": row["created_at"],
            }
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"social state mismatch: {row['social_id']}")
        relationship = connection.execute(
            "SELECT subject_id, entity_type, entity_key FROM relationships "
            "WHERE relationship_id = ?",
            (row["relationship_id"],),
        ).fetchone()
        if (
            relationship is None
            or relationship["subject_id"] != subject_id
            or relationship["entity_type"] != "human"
            or relationship["entity_key"] != row["counterparty"]
        ):
            raise IntegrityError(f"social relationship mismatch: {row['social_id']}")
        model_call = connection.execute(
            "SELECT subject_id, status, response_json, response_hash FROM model_calls "
            "WHERE call_id = ?",
            (row["model_call_id"],),
        ).fetchone()
        if model_call is None or model_call["subject_id"] != subject_id:
            raise IntegrityError(f"social model call mismatch: {row['social_id']}")
        if model_call["status"] != "succeeded" or model_call["response_json"] is None:
            raise IntegrityError(f"social model call is incomplete: {row['social_id']}")
        try:
            response_json = decompress_text(model_call["response_json"])
        except TypeError as error:
            raise IntegrityError(f"social model response is invalid: {row['social_id']}") from error
        response = InteractionIntegrity._json_value(
            response_json or "null", "social model response JSON is invalid"
        )
        if content_hash(response) != model_call["response_hash"]:
            raise IntegrityError(f"social model response mismatch: {row['social_id']}")
        content = response.get("content") if isinstance(response, dict) else None
        if (
            not isinstance(content, str)
            or InteractionIntegrity._json_value(content, "social model content JSON is invalid")
            != proposal
        ):
            raise IntegrityError(f"social proposal differs from model call: {row['social_id']}")
        if evidence:
            placeholders = ",".join("?" for _ in evidence)
            events = connection.execute(
                f"SELECT event_id FROM events WHERE subject_id = ? "
                f"AND event_id IN ({placeholders})",
                (subject_id, *evidence),
            ).fetchall()
            if len(events) != len(evidence):
                raise IntegrityError(f"social evidence mismatch: {row['social_id']}")
        interaction_id = row["interaction_id"]
        sends = row["disposition"] in {"contact", "request_help"}
        if sends != (interaction_id is not None):
            raise IntegrityError(f"social interaction linkage mismatch: {row['social_id']}")
        if interaction_id is not None:
            interaction = connection.execute(
                "SELECT subject_id, direction, kind, channel, counterparty, content, rationale "
                "FROM interactions WHERE interaction_id = ?",
                (interaction_id,),
            ).fetchone()
            expected_kind = (
                "help_request" if row["disposition"] == "request_help" else "subject_message"
            )
            if (
                interaction is None
                or interaction["subject_id"] != subject_id
                or interaction["direction"] != "outgoing"
                or interaction["kind"] != expected_kind
                or interaction["channel"] != row["channel"]
                or interaction["counterparty"] != row["counterparty"]
                or interaction["content"] != proposal.get("content")
                or interaction["rationale"] != row["rationale"]
            ):
                raise IntegrityError(f"social interaction mismatch: {row['social_id']}")

    @staticmethod
    def _json_value(value: Any, message: str) -> Any:
        if not isinstance(value, str):
            raise IntegrityError(message)
        try:
            return strict_json_loads(value)
        except (TypeError, ValueError, UnicodeError) as error:
            raise IntegrityError(message) from error
