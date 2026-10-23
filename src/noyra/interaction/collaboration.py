"""Participant-owned views over existing posts, interactions and bounty workflows."""

from __future__ import annotations

from typing import Any

from noyra.core.types import content_hash, new_id, utc_now
from noyra.wallet.economy_types import SubmissionInput

from .participants import Participant
from .store import InteractionStore
from .types import PublicPostInput


def _text(payload: dict[str, Any], key: str, maximum: int = 20000) -> str:
    value = payload.get(key, "")
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= maximum:
        raise ValueError("invalid_" + key)
    return value.strip()


class ParticipantCollaboration:
    def __init__(self, owner: Any):
        self.owner = owner
        self.db = owner.kernel.database
        self.subject = owner.kernel.subject_id

    def opportunities(self) -> list[dict[str, Any]]:
        rows = self.owner.wallet_economy.list_bounties(self.subject, status="published", limit=50)
        return [
            {
                k: getattr(row, k)
                for k in (
                    "bounty_id",
                    "title",
                    "description",
                    "acceptance_criteria",
                    "network_id",
                    "asset_id",
                    "reward_amount",
                    "opens_at",
                    "expires_at",
                    "max_submissions",
                    "reward_slots",
                    "status",
                )
            }
            for row in rows
        ]

    def read(self, path: str, query: dict[str, list[str]], who: Participant) -> dict[str, Any]:
        if set(query) - {"before"}:
            raise ValueError("invalid_query")
        before = query.get("before", ["\uffff"])[0]
        if len(before) > 100:
            raise ValueError("invalid_query")
        view = path.removeprefix("/api/participants/")
        if view == "opportunities":
            return {"items": self.opportunities()}
        with self.db.connection() as c:
            if view == "posts":
                rows = c.execute(
                    "SELECT "
                    "p.post_id,p.kind,p.title,p.content,p.status,p.created_at,p.updated_at "
                    "FROM participant_post_links l JOIN public_posts p ON "
                    "p.post_id=l.post_id AND p.subject_id=l.subject_id WHERE "
                    "l.subject_id=? AND l.entity_id=? AND p.post_id<? ORDER BY p.post_id "
                    "DESC LIMIT 51",
                    (self.subject, who.entity_id, before),
                ).fetchall()
                return self._page(rows, "post_id")
            if view == "applications":
                rows = c.execute(
                    "SELECT "
                    "a.application_id,a.bounty_id,a.content,a.status,a.created_at,b.title "
                    "FROM participant_applications a JOIN wallet_bounties b ON "
                    "b.bounty_id=a.bounty_id AND b.subject_id=a.subject_id WHERE "
                    "a.subject_id=? AND a.entity_id=? AND a.application_id<? ORDER BY "
                    "a.application_id DESC LIMIT 51",
                    (self.subject, who.entity_id, before),
                ).fetchall()
                return self._page(rows, "application_id")
            if view == "submissions":
                rows = c.execute(
                    "SELECT "
                    "s.submission_id,s.bounty_id,s.content,s.status,s.created_at,s.recipien"
                    "t_address,s.decided_at,b.title,b.reward_amount,b.network_id,b.asset_id"
                    ",o.status AS payment_status,o.order_id FROM participant_submissions l "
                    "JOIN wallet_bounty_submissions s ON s.submission_id=l.submission_id AN"
                    "D s.subject_id=l.subject_id JOIN wallet_bounties b ON b.bounty_id=s.bo"
                    "unty_id AND b.subject_id=s.subject_id LEFT JOIN wallet_payment_orders "
                    "o ON o.submission_id=s.submission_id AND o.subject_id=s.subject_id WHE"
                    "RE l.subject_id=? AND l.entity_id=? AND s.submission_id<? ORDER BY s.s"
                    "ubmission_id DESC LIMIT 51",
                    (self.subject, who.entity_id, before),
                ).fetchall()
                return self._page(rows, "submission_id")
            if view == "messages":
                rows = c.execute(
                    "SELECT "
                    "i.interaction_id,i.direction,i.content,i.status,i.created_at,i.related"
                    "_interaction_id FROM participant_message_links m JOIN interactions i O"
                    "N i.interaction_id=m.interaction_id AND i.subject_id=m.subject_id WHER"
                    "E m.subject_id=? AND m.entity_id=? AND m.visibility='private' AND m.co"
                    "nversation_key=? AND i.interaction_id<? ORDER BY i.interaction_id DESC"
                    " LIMIT 51",
                    (self.subject, who.entity_id, "participant:" + who.entity_id, before),
                ).fetchall()
                return self._page(rows, "interaction_id")
            if view == "notifications":
                records = self._notifications(c, who)
                return {"items": records}
        raise LookupError("unknown_participant_route")

    @staticmethod
    def _page(rows: Any, key: str) -> dict[str, Any]:
        return {
            "items": [dict(r) for r in rows[:50]],
            "next_cursor": rows[49][key] if len(rows) > 50 else None,
        }

    def _notifications(self, c: Any, who: Participant) -> list[dict[str, Any]]:
        results = []
        for view, key in [("posts", "post_id"), ("submissions", "submission_id")]:
            for row in self.read("/api/participants/" + view, {}, who)["items"]:
                state = row["status"] + ":" + str(row.get("payment_status") or "")
                if row["status"] in {"submitted", "pending_review"}:
                    continue
                results.append(
                    {
                        "notification_id": content_hash([view, row[key], state]),
                        "kind": view,
                        "title": row["title"],
                        "status": state,
                        "related_id": row[key],
                    }
                )
        for row in self.read("/api/participants/messages", {}, who)["items"]:
            if row["direction"] == "outgoing":
                results.append(
                    {
                        "notification_id": content_hash(["message", row["interaction_id"]]),
                        "kind": "messages",
                        "title": "Noyra",
                        "status": "reply",
                        "related_id": row["interaction_id"],
                    }
                )
        for row in results:
            row["read"] = bool(
                c.execute(
                    "SELECT 1 FROM participant_notification_reads WHERE subject_id=? AND "
                    "entity_id=? AND notification_id=?",
                    (self.subject, who.entity_id, row["notification_id"]),
                ).fetchone()
            )
        return results[:100]

    def write(
        self, path: str, payload: dict[str, Any], who: Participant, client: str
    ) -> dict[str, Any]:
        view = path.removeprefix("/api/participants/")
        allowed = {
            "messages": {"content", "idempotency_key"},
            "posts": {
                "kind",
                "title",
                "content",
                "idempotency_key",
                "captcha_id",
                "captcha_answer",
            },
            "apply": {"bounty_id", "content", "idempotency_key"},
            "submit": {
                "bounty_id",
                "content",
                "evidence",
                "recipient_address",
                "consent_version",
                "idempotency_key",
            },
            "withdraw": {"kind", "id"},
            "notification-read": {"notification_id"},
        }
        if view not in allowed:
            raise LookupError("unknown_participant_route")
        if set(payload) - allowed[view]:
            raise ValueError("unaccepted_participant_fields")
        if view in {"messages", "posts", "apply", "submit"}:
            identity = _text(payload, "idempotency_key", 128)
            key = "participant:" + content_hash([who.entity_id, view, identity])
        if view == "messages":
            content = _text(payload, "content", 10000)
            with self.db.connection() as c:
                if (
                    c.execute(
                        "SELECT COUNT(*) FROM participant_message_links WHERE subject_id=?",
                        (self.subject,),
                    ).fetchone()[0]
                    >= 10000
                ):
                    raise ValueError("message_capacity_reached")
            record = InteractionStore(self.db).receive(
                self.subject,
                "participant",
                who.entity_id,
                content,
                idempotency_key=key,
                participant=who,
                participant_conversation="participant:" + who.entity_id,
                participant_private=True,
            )
            return {"interaction_id": record.interaction_id, "status": record.status}
        if view == "posts":
            # PublicPostStore keeps its own CAPTCHA/rate/capacity and idempotent admission.
            from .posts import PublicPostCaptchaError

            captcha_error = None
            with self.db.transaction() as c:
                try:
                    post = self.owner.public_posts.create(
                        self.subject,
                        PublicPostInput(
                            kind=payload.get("kind", "post"),
                            title=_text(payload, "title", 240),
                            content=_text(payload, "content"),
                            author_label=who.display_name,
                        ),
                        idempotency_key=key,
                        client_ip=client,
                        captcha_id=payload.get("captcha_id"),
                        captcha_answer=payload.get("captcha_answer"),
                        _connection=c,
                    )
                except PublicPostCaptchaError as error:
                    captcha_error = error
                if captcha_error is None:
                    existing = c.execute(
                        "SELECT entity_id FROM participant_post_links WHERE post_id=?",
                        (post.post_id,),
                    ).fetchone()
                    if existing and existing[0] != who.entity_id:
                        raise PermissionError("post_owner_mismatch")
                    c.execute(
                        "INSERT OR IGNORE INTO participant_post_links VALUES(?,?,?,?)",
                        (post.post_id, self.subject, who.entity_id, utc_now()),
                    )
            if captcha_error is not None:
                raise captcha_error
            return {"post_id": post.post_id, "status": post.status}
        if view in {"apply", "submit"}:
            bounty_id = _text(payload, "bounty_id", 128)
            bounty = self.owner.wallet_economy.get_bounty(bounty_id, self.subject)
            if bounty.status != "published" or bounty.opens_at > utc_now():
                raise ValueError("collaboration_not_open")
            content = _text(payload, "content")
            with self.db.transaction() as c:
                count = c.execute(
                    "SELECT COUNT(*) FROM participant_applications WHERE subject_id=?",
                    (self.subject,),
                ).fetchone()[0]
                if count >= 10000:
                    raise ValueError("collaboration_capacity_reached")
                if view == "apply":
                    existing = c.execute(
                        "SELECT * FROM participant_applications WHERE subject_id=? AND "
                        "entity_id=? AND idempotency_key=?",
                        (self.subject, who.entity_id, key),
                    ).fetchone()
                    if existing:
                        if existing["bounty_id"] != bounty_id or existing["content"] != content:
                            raise ValueError("idempotency_conflict")
                        return {
                            "application_id": existing["application_id"],
                            "status": existing["status"],
                        }
                    aid = new_id("application")
                    c.execute(
                        "INSERT INTO participant_applications VALUES(?,?,?,?,?,?,?,?)",
                        (
                            aid,
                            self.subject,
                            who.entity_id,
                            bounty_id,
                            content,
                            "submitted",
                            key,
                            utc_now(),
                        ),
                    )
                    return {"application_id": aid, "status": "submitted"}
                submission = self.owner.wallet_economy.submit_to_bounty(
                    bounty_id,
                    self.subject,
                    SubmissionInput(
                        counterparty=who.entity_id,
                        content=content,
                        evidence=payload.get("evidence", []),
                        recipient_address=payload.get("recipient_address", ""),
                        idempotency_key=key,
                        consent_version=payload.get("consent_version", 1),
                    ),
                    client_ip=client,
                    _connection=c,
                )
                workflow = c.execute(
                    "SELECT * FROM wallet_reward_workflows WHERE subject_id=? AND "
                    "bounty_id=? AND status='open'",
                    (self.subject, bounty_id),
                ).fetchone()
                if workflow:
                    self.owner.wallet_rewards._link_submission(
                        c,
                        workflow,
                        submission.submission_id,
                        source_type="public_form",
                        source_content_hash=content_hash(content),
                        claimed_network_id=bounty.network_id,
                        actor="participant",
                    )
                c.execute(
                    "INSERT OR IGNORE INTO participant_submissions VALUES(?,?,?,?)",
                    (submission.submission_id, self.subject, who.entity_id, utc_now()),
                )
            return {"submission_id": submission.submission_id, "status": submission.status}
        if view == "withdraw":
            identifier = _text(payload, "id", 128)
            if payload.get("kind") == "application":
                with self.db.transaction() as c:
                    changed = c.execute(
                        "UPDATE participant_applications SET status='withdrawn' WHERE "
                        "application_id=? AND subject_id=? AND entity_id=? AND status='submitted'",
                        (identifier, self.subject, who.entity_id),
                    ).rowcount
                    if not changed:
                        raise LookupError("application_unavailable")
            elif payload.get("kind") == "submission":
                with self.db.connection() as c:
                    if not c.execute(
                        "SELECT 1 FROM participant_submissions WHERE submission_id=? AND "
                        "subject_id=? AND entity_id=?",
                        (identifier, self.subject, who.entity_id),
                    ).fetchone():
                        raise LookupError("submission_unavailable")
                self.owner.wallet_economy.withdraw_submission(
                    identifier, self.subject, actor="participant:" + who.entity_id
                )
            else:
                raise ValueError("invalid_withdrawal")
            return {"ok": True}
        if view == "notification-read":
            identifier = _text(payload, "notification_id", 64)
            with self.db.transaction() as c:
                if not any(n["notification_id"] == identifier for n in self._notifications(c, who)):
                    raise LookupError("notification_unavailable")
                c.execute(
                    "INSERT OR IGNORE INTO participant_notification_reads VALUES(?,?,?,?)",
                    (self.subject, who.entity_id, identifier, utc_now()),
                )
            return {"ok": True}
        raise LookupError("unknown_participant_route")
