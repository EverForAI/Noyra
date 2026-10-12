"""Anonymous reading and explicit operator publication, separate from private views."""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from noyra.core.database import Database, action_state_hash, behavior_log_state_hash
from noyra.core.deadline import remaining_seconds
from noyra.core.errors import IntegrityError, NotFoundError, PayloadLimitError
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.events import EventStore
from noyra.core.types import canonical_json, content_hash, strict_json_loads, utc_now

from .diary import PublicDiaryStore
from .posts import PublicPostStore
from .store import InteractionStore

ARCHIVE_CONTRACT = "public-contract-v2"
MAX_PUBLIC_SUMMARIES = 2_000
MAX_PUBLIC_SUMMARY_BYTES = 16_000_000
MAX_PUBLIC_REVISION_ROWS = 100_000
MAX_PUBLIC_REVISION_BYTES = 32_000_000
WITHDRAW_REVISION_RESERVE = 4_096
MAX_PUBLIC_SQL_STEPS = 250_000
MAX_PUBLIC_READ_BYTES = 128_000_000
MAX_PUBLIC_QUERY_SECONDS = 2.0
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


class PublicQueryBudgetExceeded(Exception):
    pass


class PublicationCapacityError(ValueError):
    pass


@dataclass
class _ReadBudget:
    deadline: float = field(
        default_factory=lambda: time.monotonic() + remaining_seconds(MAX_PUBLIC_QUERY_SECONDS)
    )
    steps: int = 0
    bytes_read: int = 0
    exhausted: bool = False

    def checkpoint(self, size: int = 0) -> None:
        self.bytes_read += size
        if self.bytes_read > MAX_PUBLIC_READ_BYTES or time.monotonic() >= self.deadline:
            self.exhausted = True
            raise PublicQueryBudgetExceeded("public read budget exceeded")

    def progress(self) -> int:
        self.steps += 1_000
        self.exhausted = self.steps > MAX_PUBLIC_SQL_STEPS or time.monotonic() >= self.deadline
        return int(self.exhausted)


_READ_BUDGET: ContextVar[_ReadBudget | None] = ContextVar(
    "public_archive_read_budget", default=None
)


@contextmanager
def _request_budget() -> Iterator[_ReadBudget]:
    previous = _READ_BUDGET.get()
    if previous is not None:
        previous.checkpoint()
        yield previous
        return
    try:
        budget = _ReadBudget()
    except TimeoutError as exc:
        raise PublicQueryBudgetExceeded("request deadline exhausted") from exc
    token = _READ_BUDGET.set(budget)
    try:
        yield budget
        budget.checkpoint()
    finally:
        _READ_BUDGET.reset(token)


@contextmanager
def _bounded_read(database: Database) -> Iterator[Any]:
    with _request_budget() as budget, database.read_transaction() as connection:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA busy_timeout=250")
        connection.set_progress_handler(budget.progress, 1_000)
        ascii_lower = str.maketrans("ABCDEFGHIJKLMNOPQRSTUVWXYZ", "abcdefghijklmnopqrstuvwxyz")

        def matches(text: str, needle: str) -> int:
            # Match SQLite lower()/instr semantics while accounting for every
            # scanned public value, including searches that return no rows.
            budget.checkpoint(len(text.encode()))
            return int(needle.translate(ascii_lower) in text.translate(ascii_lower))

        connection.create_function("public_text_match", 2, matches)
        try:
            yield connection
        except sqlite3.OperationalError as exc:
            if budget.exhausted or str(exc).lower() in {
                "interrupted",
                "database is locked",
                "database is busy",
            }:
                raise PublicQueryBudgetExceeded("public SQL read budget exceeded") from exc
            raise
        finally:
            connection.set_progress_handler(None, 0)


class _LifecycleReader:
    """Request-local, read-only verification; never perform cloud restores or writes."""

    def __init__(self, database: Database, connection: Any, subject_id: str):
        self.database, self.connection, self.subject_id = database, connection, subject_id
        self.archive: EventPayloadArchive | None = None
        self.segments: dict[str, dict[str, dict[str, Any]]] = {}

    def states(self, row: Any) -> tuple[str, str]:
        root = self.connection.execute(
            "SELECT * FROM event_chain_roots WHERE event_id=? AND subject_id=?",
            (row["event_id"], self.subject_id),
        ).fetchone()
        if (
            root is None
            or EventStore._chain_hash(row, root["sequence_number"], root["previous_root_hash"])
            != root["root_hash"]
        ):
            raise IntegrityError("public lifecycle metadata mismatch")
        key = row["payload_archive_key"]
        if key is None:
            if len(row["payload_json"].encode()) > 2_000_000:
                raise IntegrityError("public lifecycle payload exceeds read bound")
            try:
                payload = strict_json_loads(row["payload_json"])
            except (TypeError, ValueError) as exc:
                raise IntegrityError("public lifecycle payload invalid") from exc
        else:
            if key not in self.segments:
                if len(self.segments) >= 2:
                    raise PublicQueryBudgetExceeded("public lifecycle segment budget exceeded")
                if self.archive is None:
                    try:
                        self.archive = EventPayloadArchive(
                            self.database,
                            self.database.path.parent / "subject" / "cold",
                            subject_id=self.subject_id,
                            create_root=False,
                            cache_cloud_restores=False,
                        )
                    except (OSError, ValueError) as exc:
                        raise IntegrityError("public lifecycle archive unavailable") from exc
                try:
                    budget = _READ_BUDGET.get()
                    assert budget is not None
                    if budget.bytes_read >= MAX_PUBLIC_READ_BYTES:
                        raise PublicQueryBudgetExceeded("public archive byte budget exceeded")
                    self.segments[key] = self.archive.load_segment(
                        self.subject_id,
                        key,
                        connection=self.connection,
                        max_decompressed_bytes=min(
                            64_000_000, MAX_PUBLIC_READ_BYTES - budget.bytes_read
                        ),
                        max_events=5_000,
                        local_only=True,
                        consume_bytes=budget.checkpoint,
                    )
                except PayloadLimitError as exc:
                    raise IntegrityError("public lifecycle archive exceeds read bound") from exc
            payload = self.segments[key].get(row["event_id"])
        if not isinstance(payload, dict) or content_hash(payload) != row["payload_hash"]:
            raise IntegrityError("public lifecycle payload hash mismatch")
        old, new = payload.get("from"), payload.get("to")
        if (
            not isinstance(old, str)
            or not isinstance(new, str)
            or old not in LIFECYCLE_STATES
            or new not in LIFECYCLE_STATES
        ):
            raise IntegrityError("public lifecycle state unsupported")
        return old, new


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
    try:
        return parsed.astimezone(UTC).isoformat(timespec="milliseconds")
    except OverflowError as exc:
        raise ValueError("timestamp UTC conversion is outside supported range") from exc


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
        actor = _text(actor, 512, required=True)
        if actor == "subject":
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
                raise PublicationCapacityError("public summary capacity reached")
            state = {
                "publication_id": identifier,
                "subject_id": subject_id,
                **content,
                "status": "published",
                "revision": revision + 1,
                "published_at": now if old is None else old["published_at"],
                "updated_at": now,
            }
            self._revision_capacity(connection, subject_id, state, actor, old)
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
        actor = _text(actor, 512, required=True)
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
            self._revision_capacity(connection, subject_id, state, actor, row)
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
    def _revision_capacity(
        connection: Any, subject_id: str, state: dict[str, Any], actor: str, old: Any
    ) -> None:
        totals = connection.execute(
            "SELECT COUNT(*),COALESCE(SUM(length(CAST(r.publication_id||r.actor||r.status||"
            "r.state_hash||r.created_at AS BLOB))+8),0) FROM public_publication_revisions r "
            "JOIN public_publications p USING(publication_id) WHERE p.subject_id=?",
            (subject_id,),
        ).fetchone()
        active = connection.execute(
            "SELECT COUNT(*) FROM public_publications WHERE subject_id=? AND status='published'",
            (subject_id,),
        ).fetchone()[0]
        active += int(state["status"] == "published") - int(
            old is not None and old["status"] == "published"
        )
        size = (
            len(
                (
                    state["publication_id"]
                    + actor
                    + state["status"]
                    + "0" * 64
                    + state["updated_at"]
                ).encode()
            )
            + 8
        )
        # Each currently published row keeps one worst-case withdrawal slot.
        # Legacy databases already above the new cap may still withdraw; no
        # immutable history is discarded to make room for more publications.
        if state["status"] == "published" and (
            totals[0] + 1 + active > MAX_PUBLIC_REVISION_ROWS
            or totals[1] + size + active * WITHDRAW_REVISION_RESERVE > MAX_PUBLIC_REVISION_BYTES
        ):
            raise PublicationCapacityError(
                "public revision capacity reached; existing summaries may still be withdrawn"
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

    def admin_item(self, subject_id: str, identifier: str) -> dict[str, Any] | None:
        identifier = _text(identifier, 100, required=True)
        with self.database.read_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM public_publications WHERE subject_id=? AND publication_id=?",
                (subject_id, identifier),
            ).fetchone()
        return None if row is None else {**self._publication(row), "source_id": row["source_id"]}

    def admin_page(
        self, subject_id: str, *, limit: int = 20, cursor: str | None = None
    ) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 50:
            raise ValueError("invalid public management page size")
        clauses, values = ["subject_id=?"], [subject_id]
        if cursor:
            try:
                if len(cursor) > 1500:
                    raise ValueError("invalid public management cursor")
                data = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if not isinstance(data, dict) or data.get("subject") != subject_id:
                    raise ValueError("invalid public management subject")
                timestamp = _timestamp(data["time"])
                identifier = _text(data["id"], 100, required=True)
            except (KeyError, TypeError, binascii.Error, UnicodeError, json.JSONDecodeError) as exc:
                raise ValueError("invalid public management cursor") from exc
            clauses.append("(updated_at,publication_id)<(?,?)")
            values.extend([timestamp, identifier])
        with self.database.read_transaction() as connection:
            rows = connection.execute(
                f"SELECT * FROM public_publications WHERE {' AND '.join(clauses)} "
                "ORDER BY updated_at DESC,publication_id DESC LIMIT ?",
                (*values, limit + 1),
            ).fetchall()
        page = rows[:limit]
        next_cursor = None
        if len(rows) > limit:
            last = page[-1]
            next_cursor = base64.urlsafe_b64encode(
                canonical_json(
                    {
                        "subject": subject_id,
                        "time": last["updated_at"],
                        "id": last["publication_id"],
                    }
                ).encode()
            ).decode()
        return {
            "items": [{**self._publication(r), "source_id": r["source_id"]} for r in page],
            "next_cursor": next_cursor,
        }

    def overview(self, subject_id: str) -> dict[str, Any]:
        with _request_budget():
            return self._overview(subject_id)

    def _overview(self, subject_id: str) -> dict[str, Any]:
        with _bounded_read(self.database) as connection:
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
        with _request_budget():
            return self._page(
                subject_id,
                kind=kind,
                limit=limit,
                cursor=cursor,
                since=since,
                until=until,
                search=search,
                status=status,
                identifier=identifier,
            )

    def _page(
        self,
        subject_id: str,
        *,
        kind: str,
        limit: int,
        cursor: str | None,
        since: str | None,
        until: str | None,
        search: str,
        status: str,
        identifier: str | None,
    ) -> dict[str, Any]:
        if kind != "all" and kind not in TIMELINE_KINDS:
            raise ValueError("invalid public category")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("invalid public page size")
        search = _text(search, 120)
        status = _text(status, 500)
        identifier = _text(identifier, 256, required=True) if identifier is not None else None
        since = _timestamp(since) if since is not None else None
        until = _timestamp(until) if until is not None else None
        query = {
            "subject": subject_id,
            "kind": kind,
            "since": since,
            "until": until,
            "search": search,
            "status": status,
            "identifier": identifier,
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
        with _bounded_read(self.database) as connection:
            lifecycle = _LifecycleReader(self.database, connection, subject_id)
            # Internal verification fields never enter the allowlisted response.
            for category in sorted(TIMELINE_KINDS if kind == "all" else {kind}):
                spec = self._spec(category)
                table, columns, where, id_col, time_col, state_col, text_col = spec
                clauses = ["subject_id=?", where, f"{time_col}<=?"]
                values: list[Any] = [subject_id, anchor]
                if since:
                    clauses.append(f"{time_col}>=?")
                    values.append(since)
                if status and category != "lifecycle":
                    clauses.append(f"{state_col}=?")
                    values.append(status)
                if search:
                    clauses.append(f"public_text_match({text_col},?)=1")
                    values.append(search)
                if identifier:
                    clauses.append(f"{id_col}=?")
                    values.append(identifier)
                if key:
                    clauses.append(f"({time_col}, ?, {id_col}) < (?,?,?)")
                    values.extend([category, *key])
                sql = (
                    f"SELECT {columns} FROM {table} WHERE {' AND '.join(clauses)} "
                    f"ORDER BY {time_col} DESC,{id_col} DESC LIMIT ?"
                )
                values.append(limit + 1)
                base_values = values[:-1]
                scanned = 0
                matched = 0
                while True:
                    entries = []
                    for candidate in connection.execute(sql, values):
                        budget = _READ_BUDGET.get()
                        assert budget is not None
                        budget.checkpoint(
                            sum(len(v.encode()) for v in candidate if isinstance(v, str))
                        )
                        entries.append(candidate)
                    for entry in entries:
                        scanned += 1
                        if scanned > 2_000:
                            raise PublicQueryBudgetExceeded("public lifecycle scan budget exceeded")
                        if category == "lifecycle":
                            old, new = lifecycle.states(entry)
                            if status and new != status:
                                continue
                            entry = {**dict(entry), "old_state": old, "new_state": new}
                        item = self._item(category, entry, connection=connection)
                        # Lists contain readable excerpts; full public bodies are fetched
                        # only by identifier. Twenty large diaries must not make the
                        # entire archive exceed the HTTP response limit.
                        if identifier is None:
                            item["body"] = ""
                        items.append(item)
                        matched += 1
                        if matched >= limit + 1:
                            break
                    if category != "lifecycle" or matched >= limit + 1 or len(entries) < limit + 1:
                        break
                    last = entries[-1]
                    sql = (
                        f"SELECT {columns} FROM {table} WHERE {' AND '.join(clauses)} "
                        f"AND ({time_col},{id_col})<(?,?) "
                        f"ORDER BY {time_col} DESC,{id_col} DESC LIMIT ?"
                    )
                    values = [*base_values, last[time_col], last[id_col], limit + 1]
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
                "COALESCE(json_extract(details_json,'$.status'),'published')",
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
                "(SELECT r.*,l.subject_id AS log_subject_id FROM behavior_logs l "
                "JOIN behavior_log_revisions r ON r.log_id=l.log_id AND r.revision_number="
                "(SELECT MAX(r2.revision_number) FROM behavior_log_revisions r2 "
                "WHERE r2.log_id=l.log_id))",
                "*",
                "1=1",
                "log_id",
                "occurred_at",
                "result_status",
                "public_explanation",
            )
        if kind == "interaction":
            return (
                "interactions",
                "*",
                "direction='outgoing' AND substr(channel,1,7)='public:' COLLATE BINARY",
                "interaction_id",
                "created_at",
                "status",
                "content",
            )
        return (
            "events",
            "*",
            "event_type IN ('lifecycle_transition','lifecycle_recovery') "
            "AND privacy_level='public_status'",
            "event_id",
            "occurred_at",
            "'published'",
            "'状态变化'",
        )

    @classmethod
    def _item(cls, kind: str, row: Any, *, connection: Any) -> dict[str, Any]:
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
            InteractionStore._from_row(row)
            identifier, timestamp, body = row["interaction_id"], row["created_at"], row["content"]
            title, status = "公开交流", row["status"]
            details = {"interaction_kind": row["kind"]}
        elif kind == "behavior":
            if row["subject_id"] != row["log_subject_id"] or row[
                "state_hash"
            ] != behavior_log_state_hash(row):
                raise IntegrityError("public behavior revision mismatch")
            action = connection.execute(
                "SELECT * FROM actions WHERE action_id=? AND subject_id=?",
                (row["action_id"], row["subject_id"]),
            ).fetchone()
            revision = connection.execute(
                "SELECT * FROM action_revisions WHERE action_id=? AND subject_id=? "
                "ORDER BY revision_number DESC LIMIT 1",
                (row["action_id"], row["subject_id"]),
            ).fetchone()
            if (
                action is None
                or revision is None
                or (
                    revision["state_hash"] != action_state_hash(action)
                    or revision["status"] != row["result_status"]
                    or action["action_type"] != row["action_type"]
                    or action["tool"] != row["tool"]
                )
            ):
                raise IntegrityError("public behavior action mismatch")
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
                raise IntegrityError("public lifecycle state unsupported")
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
