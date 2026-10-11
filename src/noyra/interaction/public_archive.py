"""Anonymous reading and explicit operator publication, separate from private views."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import canonical_json, content_hash, utc_now

from .diary import PublicDiaryStore
from .posts import PublicPostStore

ARCHIVE_CONTRACT = "public-contract-v2"
MAX_PUBLIC_SUMMARIES = 2_000
MAX_PUBLIC_SUMMARY_BYTES = 16_000_000
PUBLIC_KINDS = frozenset({"goal", "project", "prediction", "research", "sleep", "update"})
TIMELINE_KINDS = PUBLIC_KINDS | {"diary", "behavior", "interaction", "post", "lifecycle"}
LIFECYCLE_STATES = frozenset(
    {
        "booting",
        "orienting",
        "active",
        "paused",
        "winding_down",
        "reflective_sleep",
        "deep_sleep",
        "waking",
        "stopped",
        "resetting",
    }
)
SOURCE_TABLES = {
    "goal": ("goals", "goal_id"),
    "project": ("autonomous_projects", "project_id"),
    "prediction": ("predictions", "prediction_id"),
    "research": ("research_search_runs", "research_id"),
    "sleep": ("sleep_runs", "sleep_id"),
}


class PublicationConflict(ValueError):
    pass


def _text(value: Any, maximum: int, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise ValueError("invalid public text")
    value.encode("utf-8")  # Reject lone surrogates before storage or HTTP serialization.
    if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
        raise ValueError("invalid public control character")
    if required and not value.strip():
        raise ValueError("public text required")
    return value.strip()


def _timestamp(value: Any) -> str:
    text = _text(value, 40, required=True)
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return parsed.astimezone(UTC).isoformat(timespec="milliseconds")


def _details(kind: str, value: Any) -> dict[str, Any]:
    allowed = {
        "goal": {"status", "progress", "next_step"},
        "project": {"status", "progress", "next_step", "phases", "resource_state"},
        "prediction": {"status", "probability", "target_at", "outcome"},
        "research": {"status", "sources", "uncertainty"},
        "sleep": {"status", "started_at", "completed_at", "reason_category"},
        "update": {"status"},
    }
    if not isinstance(value, dict) or set(value) - allowed[kind]:
        raise ValueError("invalid public detail fields")
    result: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"progress", "probability"}:
            if type(item) not in {int, float} or not math.isfinite(item) or not 0 <= item <= 1:
                raise ValueError("invalid public probability or progress")
            result[key] = item
        elif key in {"target_at", "started_at", "completed_at"}:
            result[key] = _timestamp(item)
        elif key == "outcome":
            if item not in {"pending", "supported", "not_supported", "partial", "cancelled"}:
                raise ValueError("invalid public outcome")
            result[key] = item
        elif key == "resource_state":
            if item not in {"normal", "limited", "paused", "undisclosed"}:
                raise ValueError("invalid public resource state")
            result[key] = item
        elif key == "reason_category":
            if item not in {"periodic", "resource", "maintenance", "undisclosed"}:
                raise ValueError("invalid public sleep reason")
            result[key] = item
        elif key in {"sources", "phases"}:
            if not isinstance(item, list) or len(item) > 10:
                raise ValueError("invalid public detail list")
            result[key] = []
            for entry in item:
                fields = {"title", "url"} if key == "sources" else {"title", "status"}
                if not isinstance(entry, dict) or set(entry) != fields:
                    raise ValueError("invalid public list entry")
                clean = {k: _text(v, 500, required=True) for k, v in entry.items()}
                if key == "sources":
                    url = urlsplit(clean["url"])
                    if url.scheme != "https" or not url.hostname or url.username or url.password:
                        raise ValueError("public source must be an HTTPS link without credentials")
                result[key].append(clean)
        else:
            result[key] = _text(item, 500, required=True)
    if (
        "started_at" in result
        and "completed_at" in result
        and result["completed_at"] < result["started_at"]
    ):
        raise ValueError("invalid sleep interval")
    return result


class PublicArchive:
    def __init__(self, database: Database):
        self.database = database

    def publish(self, subject_id: str, payload: dict[str, Any], *, actor: str) -> dict[str, Any]:
        allowed = {
            "publication_id",
            "kind",
            "source_id",
            "title",
            "summary",
            "body",
            "details",
            "expected_revision",
            "confirm_public",
        }
        if (
            not isinstance(payload, dict)
            or set(payload) - allowed
            or payload.get("confirm_public") is not True
        ):
            raise ValueError("explicit public confirmation required")
        if not actor or actor == "subject":
            raise PermissionError("operator public summary required")
        identifier = _text(payload.get("publication_id"), 100, required=True)
        if not re.fullmatch(r"[a-zA-Z0-9_-]{8,100}", identifier):
            raise ValueError("invalid publication identifier")
        kind = payload.get("kind")
        if not isinstance(kind, str) or kind not in PUBLIC_KINDS:
            raise ValueError("invalid public kind")
        expected = payload.get("expected_revision")
        if type(expected) is not int or expected < 0:
            raise ValueError("public revision required")
        source = payload.get("source_id")
        if source is not None:
            source = _text(source, 256, required=True)
            if kind not in SOURCE_TABLES:
                raise ValueError("update cannot bind a private source")
        title = _text(payload.get("title"), 240, required=True)
        summary = _text(payload.get("summary"), 1000, required=True)
        body = _text(payload.get("body", ""), 20_000)
        details = _details(kind, payload.get("details", {}))
        now = utc_now()
        with self.database.transaction() as connection:
            old = connection.execute(
                "SELECT * FROM public_publications WHERE publication_id=?", (identifier,)
            ).fetchone()
            if old is not None and old["subject_id"] != subject_id:
                raise NotFoundError("public summary not found")
            if source is not None:
                table, column = SOURCE_TABLES[kind]
                if (
                    connection.execute(
                        f"SELECT 1 FROM {table} WHERE {column}=? AND subject_id=?",
                        (source, subject_id),
                    ).fetchone()
                    is None
                ):
                    raise NotFoundError("source not found")
            content = {
                "kind": kind,
                "source_id": source,
                "title": title,
                "summary": summary,
                "body": body,
                "details_json": canonical_json(details),
            }
            if old is not None:
                self._verify(old)
                if old["status"] == "published" and all(old[k] == v for k, v in content.items()):
                    return self._publication(old)  # An identical network retry creates no revision.
                if old["kind"] != kind or old["source_id"] != source:
                    raise PublicationConflict("publication source cannot change")
            revision = 0 if old is None else int(old["revision"])
            if revision != expected:
                raise PublicationConflict("publication revision changed")
            totals = connection.execute(
                "SELECT COUNT(*),COALESCE(SUM(length(CAST("
                "title||summary||body||details_json AS BLOB))),0) "
                "FROM public_publications WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            old_bytes = (
                0
                if old is None
                else len(
                    (old["title"] + old["summary"] + old["body"] + old["details_json"]).encode()
                )
            )
            new_bytes = len((title + summary + body + canonical_json(details)).encode())
            if (old is None and totals[0] >= MAX_PUBLIC_SUMMARIES) or (
                totals[1] - old_bytes + new_bytes > MAX_PUBLIC_SUMMARY_BYTES
            ):
                raise ValueError("public summary capacity reached")
            state = {
                "publication_id": identifier,
                "subject_id": subject_id,
                **content,
                "status": "published",
                "revision": revision + 1,
                "published_at": now if old is None else old["published_at"],
                "updated_at": now,
            }
            digest = content_hash(state)
            if old is None:
                columns = ",".join(state) + ",state_hash"
                placeholders = ",".join("?" for _ in range(len(state) + 1))
                connection.execute(
                    f"INSERT INTO public_publications({columns}) VALUES({placeholders})",
                    (*state.values(), digest),
                )
            else:
                connection.execute(
                    "UPDATE public_publications SET title=?,summary=?,body=?,details_json=?,"
                    "status='published',revision=?,updated_at=?,state_hash=? "
                    "WHERE publication_id=?",
                    (
                        title,
                        summary,
                        body,
                        canonical_json(details),
                        revision + 1,
                        now,
                        digest,
                        identifier,
                    ),
                )
            connection.execute(
                "INSERT INTO public_publication_revisions VALUES(?,?,?,?,?,?)",
                (identifier, revision + 1, actor, "published", digest, now),
            )
        return self._publication({**state, "state_hash": digest})

    def withdraw(self, subject_id: str, identifier: str, expected: int, *, actor: str) -> None:
        if type(expected) is not int or not actor or actor == "subject":
            raise ValueError("operator revision required")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM public_publications WHERE publication_id=? AND subject_id=?",
                (identifier, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError("public summary not found")
            self._verify(row)
            if row["status"] == "withdrawn":
                return
            if row["revision"] != expected:
                raise PublicationConflict("publication revision changed")
            state = dict(row)
            state.pop("state_hash")
            state.update(status="withdrawn", revision=expected + 1, updated_at=utc_now())
            digest = content_hash(state)
            connection.execute(
                "UPDATE public_publications SET status='withdrawn',revision=?,updated_at=?,"
                "state_hash=? WHERE publication_id=?",
                (expected + 1, state["updated_at"], digest, identifier),
            )
            connection.execute(
                "INSERT INTO public_publication_revisions VALUES(?,?,?,?,?,?)",
                (identifier, expected + 1, actor, "withdrawn", digest, state["updated_at"]),
            )

    @staticmethod
    def _verify(row: Any) -> None:
        state = dict(row)
        digest = state.pop("state_hash")
        if content_hash(state) != digest:
            raise IntegrityError("public summary integrity mismatch")

    @classmethod
    def _publication(cls, row: Any) -> dict[str, Any]:
        cls._verify(row)
        return {
            "id": row["publication_id"],
            "kind": row["kind"],
            "title": row["title"],
            "summary": row["summary"],
            "body": row["body"],
            "status": row["status"],
            "details": json.loads(row["details_json"]),
            "revision": row["revision"],
            "occurred_at": row["published_at"],
            "updated_at": row["updated_at"],
            "provenance": "operator_summary",
        }

    def admin(self, subject_id: str) -> list[dict[str, Any]]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM public_publications WHERE subject_id=? "
                "ORDER BY updated_at DESC,publication_id DESC LIMIT 100",
                (subject_id,),
            )
            return [{**self._publication(r), "source_id": r["source_id"]} for r in rows]

    def overview(self, subject_id: str) -> dict[str, Any]:
        with self.database.read_transaction() as connection:
            row = connection.execute(
                "SELECT i.personal_name,i.project_name,r.state,r.changed_at "
                "FROM subject_identity i JOIN runtime_state r ON r.subject_id=i.subject_id "
                "WHERE i.subject_id=?",
                (subject_id,),
            ).fetchone()
            if row is None or row["state"] not in LIFECYCLE_STATES:
                raise NotFoundError("public state not found")
            count = connection.execute(
                "SELECT COUNT(*) FROM public_diary_entries WHERE subject_id=?",
                (subject_id,),
            ).fetchone()[0]
            sleep_time = connection.execute(
                "SELECT MAX(completed_at) FROM sleep_runs WHERE subject_id=? AND status='complete'",
                (subject_id,),
            ).fetchone()[0]
        return {
            "contract": ARCHIVE_CONTRACT,
            "generated_at": utc_now(),
            "display_name": row["personal_name"] or row["project_name"],
            "lifecycle": {"state": row["state"], "changed_at": row["changed_at"]},
            "public_diary_count": count,
            "last_sleep_completed_at": sleep_time,
            "recent": self.page(subject_id, limit=3)["items"],
            "goals": self.page(subject_id, kind="goal", limit=2)["items"],
            "projects": self.page(subject_id, kind="project", limit=2)["items"],
        }

    def page(
        self,
        subject_id: str,
        *,
        kind: str = "all",
        limit: int = 20,
        cursor: str | None = None,
        since: str | None = None,
        until: str | None = None,
        search: str = "",
        status: str = "",
        identifier: str | None = None,
    ) -> dict[str, Any]:
        if kind != "all" and kind not in TIMELINE_KINDS:
            raise ValueError("invalid public category")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid public page size")
        search = _text(search, 120)
        status = _text(status, 64)
        if status and not re.fullmatch(r"[a-z][a-z_]{0,63}", status):
            raise ValueError("invalid public status")
        since = _timestamp(since) if since is not None else None
        until = _timestamp(until) if until is not None else None
        query = {
            "subject": subject_id,
            "kind": kind,
            "since": since,
            "until": until,
            "search": search,
            "status": status,
        }
        query_hash = content_hash(query)
        key = None
        anchor = until or utc_now()
        if cursor:
            try:
                if len(cursor) > 1500:
                    raise ValueError("invalid public cursor")
                decoded = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(decoded, dict) or decoded.get("query") != query_hash:
                    raise ValueError("public cursor belongs to another query")
                anchor = _timestamp(decoded["anchor"])
                key = decoded["key"]
                if not isinstance(key, list) or len(key) != 3:
                    raise ValueError("invalid public cursor key")
                key = tuple(_text(item, 256, required=True) for item in key)
                _timestamp(key[0])
                if key[1] not in TIMELINE_KINDS:
                    raise ValueError("invalid public cursor category")
            except (KeyError, TypeError, binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid public cursor") from exc
        if since and since > anchor:
            raise ValueError("invalid public interval")
        items: list[dict[str, Any]] = []
        with self.database.read_transaction() as connection:
            # Only public fields are read. No private_state/goals_view/projects_view calls.
            for category in sorted(TIMELINE_KINDS if kind == "all" else {kind}):
                spec = self._spec(category)
                table, columns, where, id_col, time_col, state_col, text_col = spec
                clauses = ["subject_id=?", where, f"{time_col}<=?"]
                values: list[Any] = [subject_id, anchor]
                if since:
                    clauses.append(f"{time_col}>=?")
                    values.append(since)
                if status:
                    clauses.append(f"{state_col}=?")
                    values.append(status)
                if search:
                    clauses.append(f"instr(lower({text_col}), lower(?))>0")
                    values.append(search)
                if identifier:
                    clauses.append(f"{id_col}=?")
                    values.append(_text(identifier, 256, required=True))
                if key:
                    clauses.append(f"({time_col}, ?, {id_col}) < (?,?,?)")
                    values.extend([category, *key])
                sql = (
                    f"SELECT {columns} FROM {table} WHERE {' AND '.join(clauses)} "
                    f"ORDER BY {time_col} DESC,{id_col} DESC LIMIT ?"
                )
                values.append(limit + 1)
                for entry in connection.execute(sql, values):
                    item = self._item(category, entry)
                    if item is not None:
                        items.append(item)
        items.sort(key=lambda item: (item["occurred_at"], item["kind"], item["id"]), reverse=True)
        page = items[:limit]
        next_cursor = None
        if len(items) > limit:
            last = page[-1]
            data = {
                "query": query_hash,
                "anchor": anchor,
                "key": [last["occurred_at"], last["kind"], last["id"]],
            }
            next_cursor = base64.urlsafe_b64encode(canonical_json(data).encode()).decode()
        return {
            "contract": ARCHIVE_CONTRACT,
            "generated_at": utc_now(),
            "items": page,
            "next_cursor": next_cursor,
        }

    @staticmethod
    def _spec(kind: str) -> tuple[str, str, str, str, str, str, str]:
        if kind in PUBLIC_KINDS:
            return (
                "public_publications",
                "*",
                f"status='published' AND kind='{kind}'",
                "publication_id",
                "published_at",
                "json_extract(details_json,'$.status')",
                "title || ' ' || summary || ' ' || body",
            )
        if kind == "diary":
            return (
                "public_diary_entries",
                "*",
                "1=1",
                "entry_id",
                "created_at",
                "'published'",
                "title || ' ' || body",
            )
        if kind == "post":
            return (
                "public_posts",
                "*",
                "status='published'",
                "post_id",
                "published_at",
                "status",
                "title || ' ' || content",
            )
        if kind == "behavior":
            # Do not return prior revisions that may have been further redacted.
            return (
                "(SELECT l.subject_id,l.log_id,l.action_type,l.tool,r.* FROM behavior_logs l "
                "JOIN behavior_log_revisions r ON r.log_id=l.log_id AND r.revision_number="
                "(SELECT MAX(r2.revision_number) FROM behavior_log_revisions r2 "
                "WHERE r2.log_id=l.log_id))",
                "log_id,action_type,tool,occurred_at,result_status,public_explanation,"
                "side_effect_summary,resource_summary,public_target,redaction_reason",
                "1=1",
                "log_id",
                "occurred_at",
                "result_status",
                "public_explanation",
            )
        if kind == "interaction":
            return (
                "interactions",
                "interaction_id,kind,content,status,created_at",
                "direction='outgoing' AND channel LIKE 'public:%'",
                "interaction_id",
                "created_at",
                "status",
                "content",
            )
        return (
            "events",
            "event_id,occurred_at,json_extract(payload_json,'$.from') AS old_state,"
            "json_extract(payload_json,'$.to') AS new_state",
            "event_type IN ('lifecycle_transition','lifecycle_recovery') "
            "AND privacy_level='public_status' AND json_valid(payload_json)",
            "event_id",
            "occurred_at",
            "json_extract(payload_json,'$.to')",
            "'状态变化'",
        )

    @classmethod
    def _item(cls, kind: str, row: Any) -> dict[str, Any] | None:
        if kind in PUBLIC_KINDS:
            return cls._publication(row)
        details: dict[str, Any] = {}
        status, provenance, title = "published", "subject", ""
        if kind == "diary":
            diary = PublicDiaryStore._from_row(row)
            identifier, timestamp, title, body = (
                diary.entry_id,
                diary.created_at,
                diary.title,
                diary.body,
            )
        elif kind == "post":
            post = PublicPostStore._record(row)
            if post.published_at is None:
                raise IntegrityError("published post has no publication time")
            identifier, timestamp, title, body = (
                post.post_id,
                post.published_at,
                post.title,
                post.content,
            )
            provenance = post.author_provenance
            details = {"author_label": post.author_label, "post_kind": post.kind}
        elif kind == "interaction":
            identifier, timestamp, body = row["interaction_id"], row["created_at"], row["content"]
            title, status = "公开交流", row["status"]
            details = {"interaction_kind": row["kind"]}
        elif kind == "behavior":
            identifier, timestamp, body = (
                row["log_id"],
                row["occurred_at"],
                row["public_explanation"],
            )
            title, status, provenance = row["action_type"], row["result_status"], "runtime"
            details = {
                k: row[k]
                for k in (
                    "tool",
                    "side_effect_summary",
                    "resource_summary",
                    "public_target",
                    "redaction_reason",
                )
            }
        else:
            if row["old_state"] not in LIFECYCLE_STATES or row["new_state"] not in LIFECYCLE_STATES:
                return None
            identifier, timestamp, body = row["event_id"], row["occurred_at"], ""
            title, status, provenance = "生命周期变化", row["new_state"], "runtime"
            details = {"from": row["old_state"], "to": row["new_state"]}
        return {
            "id": identifier,
            "kind": kind,
            "title": title,
            "body": body,
            "summary": body[:220],
            "status": status,
            "details": details,
            "occurred_at": timestamp,
            "updated_at": timestamp,
            "provenance": provenance,
        }
