from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import math
import secrets
import shutil
import struct
import threading
import time
import zlib
from collections import deque
from collections.abc import Mapping
from contextlib import nullcontext
from typing import Any

from noyra.core.database import Database, public_post_identity_hash
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import content_hash, new_id, utc_now

from .types import PublicPostInput, PublicPostRecord


class PublicPostAbuseError(ValueError):
    """Base error for durable public-post admission checks."""


class PublicPostCaptchaError(PublicPostAbuseError):
    pass


class PublicPostRateLimitError(PublicPostAbuseError):
    pass


class PublicPostQueueFullError(PublicPostAbuseError):
    pass


class PublicPostCapacityError(PublicPostAbuseError):
    """Durable public-post admission storage capacity was exhausted."""


class PublicPostConflictError(PublicPostAbuseError):
    """A moderation precondition or idempotency contract did not match."""


# These are deliberately hard upper bounds for ephemeral admission evidence.
# They prevent a botnet with many source addresses from turning CAPTCHA/rate
# limiting into an unbounded SQLite write stream.  The normal service request
# and post limits are much lower; the caps are an emergency backstop.
DEFAULT_RATE_EVENT_CAP = 100_000
DEFAULT_CAPTCHA_ISSUE_EVENT_CAP = 100_000
DEFAULT_CAPTCHA_CHALLENGE_CAP = 5_000
DEFAULT_POST_CAP = 100_000
DEFAULT_POST_STORAGE_CAP_BYTES = 250_000_000
DEFAULT_CAPTCHA_ISSUE_LIMIT_PER_HOUR = 30
DEFAULT_CAPTCHA_GLOBAL_RATE_PER_MINUTE = 300
DEFAULT_CAPTCHA_ACTIVE_PER_CLIENT = 3
_PROCESS_IP_HASH_KEY = secrets.token_bytes(32)


class PublicPostStore:
    """Moderated public writing; drafts and rejected content never enter the public view."""

    def __init__(
        self,
        database: Database,
        *,
        rate_limit_per_hour: int = 10,
        queue_cap: int = 1_000,
        rate_event_cap: int = DEFAULT_RATE_EVENT_CAP,
        captcha_issue_event_cap: int = DEFAULT_CAPTCHA_ISSUE_EVENT_CAP,
        captcha_challenge_cap: int = DEFAULT_CAPTCHA_CHALLENGE_CAP,
        post_cap: int = DEFAULT_POST_CAP,
        storage_cap_bytes: int = DEFAULT_POST_STORAGE_CAP_BYTES,
        minimum_free_bytes: int = 10_000_000,
        captcha_issue_limit_per_hour: int = DEFAULT_CAPTCHA_ISSUE_LIMIT_PER_HOUR,
        captcha_global_rate_per_minute: int = DEFAULT_CAPTCHA_GLOBAL_RATE_PER_MINUTE,
        captcha_active_per_client: int = DEFAULT_CAPTCHA_ACTIVE_PER_CLIENT,
    ):
        if (
            type(rate_limit_per_hour) is not int
            or type(queue_cap) is not int
            or rate_limit_per_hour < 1
            or queue_cap < 1
        ):
            raise ValueError("invalid public post abuse limits")
        for name, value in (
            ("rate_event_cap", rate_event_cap),
            ("captcha_issue_event_cap", captcha_issue_event_cap),
            ("captcha_challenge_cap", captcha_challenge_cap),
            ("post_cap", post_cap),
            ("storage_cap_bytes", storage_cap_bytes),
            ("minimum_free_bytes", minimum_free_bytes),
            ("captcha_issue_limit_per_hour", captcha_issue_limit_per_hour),
            ("captcha_global_rate_per_minute", captcha_global_rate_per_minute),
            ("captcha_active_per_client", captcha_active_per_client),
        ):
            if type(value) is not int or value < 1:
                raise ValueError(f"{name} is invalid")
        self.database = database
        self.rate_limit_per_hour = rate_limit_per_hour
        self.queue_cap = queue_cap
        self.rate_event_cap = rate_event_cap
        self.captcha_issue_event_cap = captcha_issue_event_cap
        self.captcha_challenge_cap = captcha_challenge_cap
        self.post_cap = post_cap
        self.storage_cap_bytes = storage_cap_bytes
        self.minimum_free_bytes = minimum_free_bytes
        self.captcha_issue_limit_per_hour = captcha_issue_limit_per_hour
        self.captcha_global_rate_per_minute = captcha_global_rate_per_minute
        self.captcha_active_per_client = captcha_active_per_client
        self._ip_hash_key = hmac.new(
            _PROCESS_IP_HASH_KEY,
            str(database.path).encode("utf-8"),
            hashlib.sha256,
        ).digest()
        self._captcha_gate_lock = threading.Lock()
        self._captcha_issue_times: deque[float] = deque()

    def create(
        self,
        subject_id: str,
        proposal: PublicPostInput,
        *,
        idempotency_key: str | None = None,
        client_ip: str | None = None,
        captcha_id: str | None = None,
        captcha_answer: str | None = None,
        rate_limit_per_hour: int | None = None,
        queue_cap: int | None = None,
        storage_cap_bytes: int | None = None,
        author_provenance: str = "visitor",
        _connection: Any | None = None,
    ) -> PublicPostRecord:
        if author_provenance not in {"visitor", "subject", "operator", "verified_channel"}:
            raise ValueError("public post author provenance is invalid")
        trusted_author = author_provenance != "visitor"
        client_ip = "trusted-author" if trusted_author else self._client_bucket(client_ip)
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 256
        ):
            raise ValueError("public post idempotency key is invalid")
        key = idempotency_key or content_hash(
            {
                "kind": proposal.kind,
                "title": proposal.title,
                "content": proposal.content,
                "author_label": proposal.author_label,
            }
        )
        legacy_key = (
            None
            if idempotency_key is not None
            else content_hash(
                {"kind": proposal.kind, "title": proposal.title, "content": proposal.content}
            )
        )
        # Idempotent retries should not consume a fresh CAPTCHA or count
        # against the sender. The transactional re-check below still closes
        # the race where two new requests use the same key concurrently.
        read_scope = self.database.connection() if _connection is None else nullcontext(_connection)
        with read_scope as connection:
            existing = connection.execute(
                "SELECT * FROM public_posts WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, key),
            ).fetchone()
            if existing is None and legacy_key is not None:
                existing = connection.execute(
                    "SELECT * FROM public_posts WHERE subject_id = ? AND idempotency_key = ?",
                    (subject_id, legacy_key),
                ).fetchone()
                if existing is not None:
                    key = legacy_key
        if existing is not None:
            if content_hash(existing["content"]) != existing["content_hash"]:
                raise IntegrityError("public post content integrity check failed")
            expected_hash = content_hash(proposal.content)
            if (
                existing["kind"] != proposal.kind
                or existing["title"] != proposal.title
                or existing["content_hash"] != expected_hash
                or existing["author_label"] != proposal.author_label
                or existing["author_provenance"] != author_provenance
            ):
                raise ValueError("public post idempotency key identifies different content")
            return self._record(existing)
        prospective_bytes = self._post_bytes(proposal)
        self._ensure_disk_headroom(prospective_bytes)
        now = utc_now()
        post_id = new_id("post")
        captcha_error: PublicPostCaptchaError | None = None
        write_scope = (
            self.database.transaction() if _connection is None else nullcontext(_connection)
        )
        with write_scope as connection:
            existing = connection.execute(
                "SELECT * FROM public_posts WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, key),
            ).fetchone()
            if existing is None and legacy_key is not None:
                existing = connection.execute(
                    "SELECT * FROM public_posts WHERE subject_id = ? AND idempotency_key = ?",
                    (subject_id, legacy_key),
                ).fetchone()
                if existing is not None:
                    key = legacy_key
            if existing is not None:
                if content_hash(existing["content"]) != existing["content_hash"]:
                    raise IntegrityError("public post content integrity check failed")
                expected_hash = content_hash(proposal.content)
                if (
                    existing["kind"] != proposal.kind
                    or existing["title"] != proposal.title
                    or existing["content_hash"] != expected_hash
                    or existing["author_label"] != proposal.author_label
                    or existing["author_provenance"] != author_provenance
                ):
                    raise ValueError("public post idempotency key identifies different content")
                return self._record(existing)
            effective_queue_cap = self.queue_cap if queue_cap is None else queue_cap
            effective_storage_cap = (
                self.storage_cap_bytes if storage_cap_bytes is None else storage_cap_bytes
            )
            if trusted_author:
                self._check_post_capacity(
                    connection,
                    subject_id=subject_id,
                    queue_cap=effective_queue_cap,
                    storage_cap_bytes=effective_storage_cap,
                    prospective_bytes=prospective_bytes,
                )
            else:
                self._admit_abuse_checked(
                    connection,
                    subject_id=subject_id,
                    client_ip=client_ip,
                    rate_limit_per_hour=(
                        self.rate_limit_per_hour
                        if rate_limit_per_hour is None
                        else rate_limit_per_hour
                    ),
                    queue_cap=effective_queue_cap,
                    storage_cap_bytes=effective_storage_cap,
                    prospective_bytes=prospective_bytes,
                )
                try:
                    self._verify_captcha(
                        connection,
                        subject_id=subject_id,
                        client_ip=client_ip,
                        challenge_id=captcha_id,
                        answer=captcha_answer,
                    )
                except PublicPostCaptchaError as error:
                    captcha_error = error
            if captcha_error is not None:
                # Commit the failed-attempt counter.  The exception is raised
                # after the transaction has closed.
                pass
            else:
                if not trusted_author:
                    self._record_rate_event(connection, subject_id, client_ip)
                identity = {
                    "post_id": post_id,
                    "subject_id": subject_id,
                    "kind": proposal.kind,
                    "title": proposal.title,
                    "content_hash": content_hash(proposal.content),
                    "author_label": proposal.author_label,
                    "author_provenance": author_provenance,
                    "idempotency_key": key,
                    "created_at": now,
                }
                connection.execute(
                    """INSERT INTO public_posts(
                        post_id, subject_id, kind, title, content, content_hash,
                        author_label, status, idempotency_key, created_at, updated_at, published_at,
                        identity_hash, author_provenance
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'pending_review', ?, ?, ?, NULL, ?, ?)""",
                    (
                        post_id,
                        subject_id,
                        proposal.kind,
                        proposal.title,
                        proposal.content,
                        content_hash(proposal.content),
                        proposal.author_label,
                        key,
                        now,
                        now,
                        content_hash(identity),
                        author_provenance,
                    ),
                )
        if captcha_error is not None:
            raise captcha_error
        if _connection is not None:
            row = _connection.execute(
                "SELECT * FROM public_posts WHERE post_id = ? AND subject_id = ?",
                (post_id, subject_id),
            ).fetchone()
            if row is None:
                raise IntegrityError("public post transaction did not persist the post")
            return self._record(row)
        return self.get(post_id, subject_id=subject_id)

    def issue_captcha(
        self,
        subject_id: str,
        client_ip: str,
        *,
        ttl_seconds: int = 300,
        max_attempts: int = 5,
        mode: str = "alphanumeric",
        issue_limit_per_hour: int | None = None,
        global_rate_per_minute: int | None = None,
        queue_cap: int | None = None,
        storage_cap_bytes: int | None = None,
    ) -> dict[str, Any]:
        client_ip = self._client_bucket(client_ip)
        issue_limit = (
            self.captcha_issue_limit_per_hour
            if issue_limit_per_hour is None
            else issue_limit_per_hour
        )
        global_rate = (
            self.captcha_global_rate_per_minute
            if global_rate_per_minute is None
            else global_rate_per_minute
        )
        effective_queue_cap = self.queue_cap if queue_cap is None else queue_cap
        effective_storage_cap = (
            self.storage_cap_bytes if storage_cap_bytes is None else storage_cap_bytes
        )
        if (
            type(ttl_seconds) is not int
            or type(max_attempts) is not int
            or type(issue_limit) is not int
            or type(global_rate) is not int
            or not 30 <= ttl_seconds <= 3_600
            or not 1 <= max_attempts <= 20
            or not 1 <= issue_limit <= 100_000
            or not 1 <= global_rate <= 100_000
        ):
            raise ValueError("invalid CAPTCHA limits")
        alphabets = {
            "letters": "ABCDEFGHJKLMNPQRSTUVWXYZ",
            "digits": "23456789",
            "alphanumeric": "ABCDEFGHJKLMNPQRSTUVWXYZ23456789",
        }
        alphabet = alphabets.get(mode)
        if alphabet is None:
            raise ValueError("invalid CAPTCHA mode")
        answer = "".join(secrets.choice(alphabet) for _ in range(6))
        challenge_id = new_id("captcha")
        salt = secrets.token_hex(16)
        now = time.time()
        answer_hash = self._captcha_hash(answer, salt)
        self._ensure_disk_headroom(1)
        self._preflight_post_capacity(
            subject_id,
            queue_cap=effective_queue_cap,
            storage_cap_bytes=effective_storage_cap,
        )
        self._reserve_captcha_issue_slot(now, global_rate)
        with self.database.transaction() as connection:
            cutoff = now - 3600
            minute_cutoff = now - 60
            connection.execute(
                "DELETE FROM public_post_captcha_issue_events "
                "WHERE subject_id = ? AND issued_at <= ?",
                (subject_id, cutoff),
            )
            connection.execute(
                "DELETE FROM public_post_rate_events WHERE subject_id = ? AND occurred_at <= ?",
                (subject_id, cutoff),
            )
            connection.execute(
                """DELETE FROM public_post_captcha_challenges
                   WHERE subject_id = ? AND (expires_at <= ? OR consumed_at IS NOT NULL)""",
                (subject_id, now),
            )
            issue_count = connection.execute(
                """SELECT COUNT(*) AS count FROM public_post_captcha_issue_events
                   WHERE subject_id = ? AND client_ip = ? AND issued_at > ?""",
                (subject_id, client_ip, cutoff),
            ).fetchone()
            if issue_count is not None and int(issue_count["count"]) >= issue_limit:
                raise PublicPostRateLimitError("CAPTCHA issue rate limit exceeded")
            global_issue_count = connection.execute(
                "SELECT COUNT(*) AS count FROM public_post_captcha_issue_events "
                "WHERE subject_id = ? AND issued_at > ?",
                (subject_id, minute_cutoff),
            ).fetchone()
            if global_issue_count is not None and int(global_issue_count["count"]) >= global_rate:
                raise PublicPostRateLimitError("CAPTCHA global issue rate limit exceeded")
            client_active = connection.execute(
                "SELECT COUNT(*) AS count FROM public_post_captcha_challenges "
                "WHERE subject_id = ? AND client_ip = ? AND expires_at > ? "
                "AND consumed_at IS NULL",
                (subject_id, client_ip, now),
            ).fetchone()
            if (
                client_active is not None
                and int(client_active["count"]) >= self.captcha_active_per_client
            ):
                raise PublicPostRateLimitError("CAPTCHA active challenge limit exceeded")
            active_count = connection.execute(
                """SELECT COUNT(*) AS count FROM public_post_captcha_challenges
                   WHERE subject_id = ? AND expires_at > ? AND consumed_at IS NULL""",
                (subject_id, now),
            ).fetchone()
            if (
                active_count is not None
                and int(active_count["count"]) >= self.captcha_challenge_cap
            ):
                raise PublicPostQueueFullError("CAPTCHA challenge capacity is full")
            issue_total = connection.execute(
                "SELECT COUNT(*) AS count FROM public_post_captcha_issue_events "
                "WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
            if (
                issue_total is not None
                and int(issue_total["count"]) >= self.captcha_issue_event_cap
            ):
                raise PublicPostCapacityError("CAPTCHA issue evidence capacity is full")
            self._check_post_capacity(
                connection,
                subject_id=subject_id,
                queue_cap=effective_queue_cap,
                storage_cap_bytes=effective_storage_cap,
                prospective_bytes=0,
            )
            connection.execute(
                "INSERT INTO public_post_captcha_issue_events("
                "event_id, subject_id, client_ip, issued_at) VALUES (?, ?, ?, ?)",
                (new_id("captcha-issue"), subject_id, client_ip, now),
            )
            connection.execute(
                """INSERT INTO public_post_captcha_challenges(
                    challenge_id, subject_id, client_ip, answer_hash, answer_salt,
                    attempts, max_attempts, issued_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, NULL)""",
                (
                    challenge_id,
                    subject_id,
                    client_ip,
                    answer_hash,
                    salt,
                    max_attempts,
                    now,
                    now + ttl_seconds,
                ),
            )
        return {
            "challenge_id": challenge_id,
            "captcha_id": challenge_id,
            "image": self._captcha_image(answer),
            "expires_at": now + ttl_seconds,
        }

    def get(self, post_id: str, *, subject_id: str) -> PublicPostRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM public_posts WHERE post_id = ? AND subject_id = ?",
                (post_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"public post not found: {post_id}")
        return self._record(row)

    def controls(self, subject_id: str, *, defaults: Mapping[str, Any]) -> dict[str, Any]:
        """Return operator overrides, falling back to the process defaults."""
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT subject_id, rate_limit_per_hour, queue_cap, captcha_ttl_seconds, "
                "captcha_max_attempts, captcha_mode, storage_cap_bytes, "
                "captcha_issue_limit_per_hour, captcha_global_rate_per_minute, "
                "updated_at, updated_by, state_hash "
                "FROM public_post_controls WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        if row is None:
            return {
                "rate_limit_per_hour": int(defaults["rate_limit_per_hour"]),
                "queue_cap": int(defaults["queue_cap"]),
                "captcha_ttl_seconds": int(defaults["captcha_ttl_seconds"]),
                "captcha_max_attempts": int(defaults["captcha_max_attempts"]),
                "captcha_mode": str(defaults["captcha_mode"]),
                "storage_cap_bytes": int(defaults.get("storage_cap_bytes", self.storage_cap_bytes)),
                "captcha_issue_limit_per_hour": int(
                    defaults.get("captcha_issue_limit_per_hour", self.captcha_issue_limit_per_hour)
                ),
                "captcha_global_rate_per_minute": int(
                    defaults.get(
                        "captcha_global_rate_per_minute",
                        self.captcha_global_rate_per_minute,
                    )
                ),
                "updated_at": None,
                "updated_by": None,
                "source": "environment",
            }
        state = {
            "subject_id": row["subject_id"],
            "rate_limit_per_hour": row["rate_limit_per_hour"],
            "queue_cap": row["queue_cap"],
            "captcha_ttl_seconds": row["captcha_ttl_seconds"],
            "captcha_max_attempts": row["captcha_max_attempts"],
            "captcha_mode": row["captcha_mode"],
            "storage_cap_bytes": row["storage_cap_bytes"],
            "captcha_issue_limit_per_hour": row["captcha_issue_limit_per_hour"],
            "captcha_global_rate_per_minute": row["captcha_global_rate_per_minute"],
            "updated_at": row["updated_at"],
            "updated_by": row["updated_by"],
        }
        if not isinstance(row["updated_by"], str) or not row["updated_by"].strip():
            raise IntegrityError("public post controls integrity mismatch")
        if row["state_hash"] != content_hash(state):
            raise IntegrityError("public post controls integrity mismatch")
        return {
            "rate_limit_per_hour": int(row["rate_limit_per_hour"]),
            "queue_cap": int(row["queue_cap"]),
            "captcha_ttl_seconds": int(row["captcha_ttl_seconds"]),
            "captcha_max_attempts": int(row["captcha_max_attempts"]),
            "captcha_mode": str(row["captcha_mode"]),
            "storage_cap_bytes": int(row["storage_cap_bytes"]),
            "captcha_issue_limit_per_hour": int(row["captcha_issue_limit_per_hour"]),
            "captcha_global_rate_per_minute": int(row["captcha_global_rate_per_minute"]),
            "updated_at": str(row["updated_at"]),
            "updated_by": str(row["updated_by"]),
            "source": "admin",
        }

    def configure_controls(
        self,
        subject_id: str,
        *,
        rate_limit_per_hour: int,
        queue_cap: int,
        captcha_ttl_seconds: int,
        captcha_max_attempts: int,
        captcha_mode: str,
        storage_cap_bytes: int | None = None,
        captcha_issue_limit_per_hour: int | None = None,
        captcha_global_rate_per_minute: int | None = None,
        actor: str,
    ) -> dict[str, Any]:
        storage_cap_bytes = (
            self.storage_cap_bytes if storage_cap_bytes is None else storage_cap_bytes
        )
        captcha_issue_limit_per_hour = (
            self.captcha_issue_limit_per_hour
            if captcha_issue_limit_per_hour is None
            else captcha_issue_limit_per_hour
        )
        captcha_global_rate_per_minute = (
            self.captcha_global_rate_per_minute
            if captcha_global_rate_per_minute is None
            else captcha_global_rate_per_minute
        )
        if not actor.strip() or actor.strip().casefold() == "subject":
            raise PermissionError("public-post controls require an operator")
        if not 1 <= rate_limit_per_hour <= 100_000:
            raise ValueError("public post rate limit is invalid")
        if not 1 <= queue_cap <= self.post_cap:
            raise ValueError("public post queue cap is invalid")
        if not 30 <= captcha_ttl_seconds <= 3_600:
            raise ValueError("CAPTCHA TTL is invalid")
        if not 1 <= captcha_max_attempts <= 20:
            raise ValueError("CAPTCHA attempts are invalid")
        if captcha_mode not in {"letters", "digits", "alphanumeric"}:
            raise ValueError("CAPTCHA mode is invalid")
        if not 1_000_000 <= storage_cap_bytes <= self.storage_cap_bytes:
            raise ValueError("public post storage cap is invalid")
        if not 1 <= captcha_issue_limit_per_hour <= 100_000:
            raise ValueError("CAPTCHA issue limit is invalid")
        if not 1 <= captcha_global_rate_per_minute <= 100_000:
            raise ValueError("CAPTCHA global rate is invalid")
        now = utc_now()
        state = {
            "subject_id": subject_id,
            "rate_limit_per_hour": rate_limit_per_hour,
            "queue_cap": queue_cap,
            "captcha_ttl_seconds": captcha_ttl_seconds,
            "captcha_max_attempts": captcha_max_attempts,
            "captcha_mode": captcha_mode,
            "storage_cap_bytes": storage_cap_bytes,
            "captcha_issue_limit_per_hour": captcha_issue_limit_per_hour,
            "captcha_global_rate_per_minute": captcha_global_rate_per_minute,
            "updated_at": now,
            "updated_by": actor,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO public_post_controls(
                    subject_id, rate_limit_per_hour, queue_cap, captcha_ttl_seconds,
                    captcha_max_attempts, captcha_mode, storage_cap_bytes,
                    captcha_issue_limit_per_hour, captcha_global_rate_per_minute,
                    updated_at, updated_by, state_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(subject_id) DO UPDATE SET
                    rate_limit_per_hour = excluded.rate_limit_per_hour,
                    queue_cap = excluded.queue_cap,
                    captcha_ttl_seconds = excluded.captcha_ttl_seconds,
                    captcha_max_attempts = excluded.captcha_max_attempts,
                    captcha_mode = excluded.captcha_mode,
                    storage_cap_bytes = excluded.storage_cap_bytes,
                    captcha_issue_limit_per_hour = excluded.captcha_issue_limit_per_hour,
                    captcha_global_rate_per_minute = excluded.captcha_global_rate_per_minute,
                    updated_at = excluded.updated_at,
                    updated_by = excluded.updated_by,
                    state_hash = excluded.state_hash""",
                (
                    subject_id,
                    rate_limit_per_hour,
                    queue_cap,
                    captcha_ttl_seconds,
                    captcha_max_attempts,
                    captcha_mode,
                    storage_cap_bytes,
                    captcha_issue_limit_per_hour,
                    captcha_global_rate_per_minute,
                    now,
                    actor,
                    content_hash(state),
                ),
            )
        return self.controls(subject_id, defaults=state)

    def public(self, subject_id: str, *, limit: int = 100) -> list[PublicPostRecord]:
        bounded = max(1, min(limit, 1_000))
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM public_posts WHERE subject_id = ? AND status = 'published' "
                "ORDER BY published_at DESC, post_id DESC LIMIT ?",
                (subject_id, bounded),
            ).fetchall()
        return [self._record(row) for row in rows]

    def admin(
        self,
        subject_id: str,
        *,
        limit: int = 100,
        status: str | None = None,
        cursor: str | None = None,
    ) -> list[PublicPostRecord]:
        bounded = max(1, min(limit, 1_000))
        if status is not None and status not in {
            "draft",
            "pending_review",
            "published",
            "rejected",
            "archived",
        }:
            raise ValueError("invalid public post status filter")
        cursor_created_at: str | None = None
        cursor_post_id: str | None = None
        if cursor is not None:
            if not isinstance(cursor, str) or len(cursor) > 512 or "|" not in cursor:
                raise ValueError("invalid public post cursor")
            cursor_created_at, cursor_post_id = cursor.rsplit("|", 1)
            if not cursor_created_at or not cursor_post_id:
                raise ValueError("invalid public post cursor")
        clauses = ["subject_id = ?"]
        parameters: list[Any] = [subject_id]
        if status is not None:
            clauses.append("status = ?")
            parameters.append(status)
        if cursor_created_at is not None and cursor_post_id is not None:
            clauses.append("(created_at < ? OR (created_at = ? AND post_id < ?))")
            parameters.extend((cursor_created_at, cursor_created_at, cursor_post_id))
        parameters.append(bounded)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM public_posts WHERE "
                + " AND ".join(clauses)
                + " ORDER BY created_at DESC, post_id DESC LIMIT ?",
                tuple(parameters),
            ).fetchall()
        return [self._record(row) for row in rows]

    def moderate(
        self,
        post_id: str,
        *,
        subject_id: str,
        status: str,
        actor: str,
        reason: str,
        expected_status: str | None = None,
        idempotency_key: str | None = None,
    ) -> PublicPostRecord:
        if status not in {"published", "rejected", "archived"}:
            raise ValueError("invalid public post moderation")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 256:
            raise ValueError("invalid public post moderation actor")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 2_000:
            raise ValueError("invalid public post moderation reason")
        if expected_status is not None and expected_status not in {
            "pending_review",
            "published",
            "rejected",
        }:
            raise ValueError("invalid public post moderation precondition")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 256
        ):
            raise ValueError("invalid public post moderation idempotency key")
        now = utc_now()
        event_id = new_id("post-moderation")
        operation_key = idempotency_key.strip() if idempotency_key is not None else event_id
        replayed = False
        with self.database.transaction() as connection:
            existing_event = connection.execute(
                "SELECT * FROM public_post_moderation_events "
                "WHERE subject_id = ? AND idempotency_key = ?",
                (subject_id, operation_key),
            ).fetchone()
            if existing_event is not None:
                if (
                    existing_event["post_id"] != post_id
                    or existing_event["to_status"] != status
                    or existing_event["actor"] != actor
                    or existing_event["reason"] != reason
                    or (
                        expected_status is not None
                        and existing_event["from_status"] != expected_status
                    )
                ):
                    raise PublicPostConflictError(
                        "public post moderation idempotency key identifies another operation"
                    )
                replayed = True
            row = connection.execute(
                "SELECT * FROM public_posts WHERE post_id = ? AND subject_id = ?",
                (post_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"public post not found: {post_id}")
            if public_post_identity_hash(row) != row["identity_hash"]:
                raise IntegrityError("public post identity integrity check failed")
            if replayed:
                return self._record(row)
            if expected_status is not None and row["status"] != expected_status:
                raise PublicPostConflictError("public post moderation state changed")
            allowed = {
                "pending_review": {"published", "rejected"},
                "published": {"archived"},
                "rejected": {"archived"},
                "draft": {"pending_review"},
                "archived": set(),
            }
            if status not in allowed.get(str(row["status"]), set()):
                raise ValueError("invalid public post status transition")
            previous = connection.execute(
                "SELECT event_id, revision, to_status "
                "FROM public_post_moderation_events "
                "WHERE subject_id = ? AND post_id = ? "
                "ORDER BY revision DESC LIMIT 1",
                (subject_id, post_id),
            ).fetchone()
            revision = 1 if previous is None else int(previous["revision"]) + 1
            previous_event_id = None if previous is None else str(previous["event_id"])
            published_at = now if status == "published" else row["published_at"]
            connection.execute(
                "UPDATE public_posts SET status = ?, updated_at = ?, published_at = ? "
                "WHERE post_id = ?",
                (status, now, published_at, post_id),
            )
            state = {
                "event_id": event_id,
                "subject_id": subject_id,
                "post_id": post_id,
                "from_status": row["status"],
                "to_status": status,
                "actor": actor,
                "reason": reason,
                "revision": revision,
                "previous_event_id": previous_event_id,
                "idempotency_key": operation_key,
                "created_at": now,
            }
            connection.execute(
                """INSERT INTO public_post_moderation_events(
                    event_id, subject_id, post_id, from_status, to_status,
                    actor, reason, state_hash, created_at, revision,
                    previous_event_id, idempotency_key
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    state["event_id"],
                    subject_id,
                    post_id,
                    row["status"],
                    status,
                    actor,
                    reason,
                    content_hash(state),
                    now,
                    revision,
                    previous_event_id,
                    operation_key,
                ),
            )
        return self.get(post_id, subject_id=subject_id)

    @staticmethod
    def _record(row: Any) -> PublicPostRecord:
        if content_hash(row["content"]) != row["content_hash"]:
            raise IntegrityError("public post content integrity check failed")
        if public_post_identity_hash(row) != row["identity_hash"]:
            raise IntegrityError("public post identity integrity check failed")
        return PublicPostRecord(
            row["post_id"],
            row["subject_id"],
            row["kind"],
            row["title"],
            row["content"],
            row["author_label"],
            row["author_provenance"],
            row["status"],
            row["created_at"],
            row["updated_at"],
            row["published_at"],
        )

    def _admit_abuse_checked(
        self,
        connection: Any,
        *,
        subject_id: str,
        client_ip: str,
        rate_limit_per_hour: int,
        queue_cap: int,
        storage_cap_bytes: int,
        prospective_bytes: int,
    ) -> None:
        if (
            type(rate_limit_per_hour) is not int
            or type(queue_cap) is not int
            or rate_limit_per_hour < 1
            or queue_cap < 1
            or storage_cap_bytes < 1_000_000
            or prospective_bytes < 0
        ):
            raise ValueError("invalid public post abuse limits")
        cutoff = time.time() - 3600
        # Prune before checking the evidence budget.  Otherwise one stale
        # burst can permanently block admission until a later request happens
        # to trigger garbage collection.
        connection.execute(
            "DELETE FROM public_post_rate_events WHERE subject_id = ? AND occurred_at <= ?",
            (subject_id, cutoff),
        )
        self._check_post_capacity(
            connection,
            subject_id=subject_id,
            queue_cap=queue_cap,
            storage_cap_bytes=storage_cap_bytes,
            prospective_bytes=prospective_bytes,
        )
        recent = connection.execute(
            "SELECT COUNT(*) AS count FROM public_post_rate_events WHERE subject_id = ? "
            "AND client_ip = ? AND occurred_at > ?",
            (subject_id, client_ip, cutoff),
        ).fetchone()
        if recent is not None and int(recent["count"]) >= rate_limit_per_hour:
            raise PublicPostRateLimitError("public post rate limit exceeded")
        total_rate_events = connection.execute(
            "SELECT COUNT(*) AS count FROM public_post_rate_events WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        if total_rate_events is not None and int(total_rate_events["count"]) >= self.rate_event_cap:
            raise PublicPostCapacityError("public post rate evidence capacity is full")

    @staticmethod
    def _record_rate_event(connection: Any, subject_id: str, client_ip: str) -> None:
        connection.execute(
            "INSERT INTO public_post_rate_events(event_id, subject_id, client_ip, occurred_at) "
            "VALUES (?, ?, ?, ?)",
            (new_id("post-rate"), subject_id, client_ip, time.time()),
        )

    def _preflight_post_capacity(
        self, subject_id: str, *, queue_cap: int, storage_cap_bytes: int
    ) -> None:
        """Reject an already-full site without first taking SQLite's write lock."""
        with self.database.connection() as connection:
            self._check_post_capacity(
                connection,
                subject_id=subject_id,
                queue_cap=queue_cap,
                storage_cap_bytes=storage_cap_bytes,
                prospective_bytes=0,
            )

    def _check_post_capacity(
        self,
        connection: Any,
        *,
        subject_id: str,
        queue_cap: int,
        storage_cap_bytes: int,
        prospective_bytes: int,
    ) -> None:
        total = connection.execute(
            "SELECT COUNT(*) AS count FROM public_posts WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        if total is not None and int(total["count"]) >= self.post_cap:
            raise PublicPostCapacityError("public post storage capacity is full")
        pending = connection.execute(
            "SELECT COUNT(*) AS count FROM public_posts "
            "WHERE subject_id = ? AND status = 'pending_review'",
            (subject_id,),
        ).fetchone()
        if pending is not None and int(pending["count"]) >= queue_cap:
            raise PublicPostQueueFullError("public post moderation queue is full")
        usage = connection.execute(
            "SELECT COALESCE(SUM("
            "length(CAST(title AS BLOB)) + length(CAST(content AS BLOB)) + "
            "length(CAST(author_label AS BLOB))"
            "), 0) AS byte_size FROM public_posts WHERE subject_id = ?",
            (subject_id,),
        ).fetchone()
        used_bytes = 0 if usage is None else int(usage["byte_size"])
        if used_bytes + prospective_bytes > storage_cap_bytes:
            raise PublicPostCapacityError("public post byte capacity is full")

    def usage(self, subject_id: str, *, storage_cap_bytes: int | None = None) -> dict[str, int]:
        cap = self.storage_cap_bytes if storage_cap_bytes is None else storage_cap_bytes
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT COUNT(*) AS post_count, "
                "SUM(CASE WHEN status = 'pending_review' THEN 1 ELSE 0 END) AS pending_count, "
                "COALESCE(SUM(length(CAST(title AS BLOB)) + "
                "length(CAST(content AS BLOB)) + length(CAST(author_label AS BLOB))), 0) "
                "AS byte_size FROM public_posts WHERE subject_id = ?",
                (subject_id,),
            ).fetchone()
        return {
            "post_count": int(row["post_count"]),
            "pending_count": int(row["pending_count"] or 0),
            "byte_size": int(row["byte_size"]),
            "storage_cap_bytes": int(cap),
        }

    def verify_captcha(
        self,
        subject_id: str,
        *,
        client_ip: str,
        challenge_id: str | None,
        answer: str | None,
    ) -> None:
        """Validate and consume a challenge in its own durable transaction."""
        client_ip = self._client_bucket(client_ip)
        error: PublicPostCaptchaError | None = None
        with self.database.transaction() as connection:
            try:
                self._verify_captcha(
                    connection,
                    subject_id=subject_id,
                    client_ip=client_ip,
                    challenge_id=challenge_id,
                    answer=answer,
                )
            except PublicPostCaptchaError as caught:
                # Failed answers increment the durable attempt counter before
                # the public error is raised outside the committing scope.
                error = caught
        if error is not None:
            raise error

    def _verify_captcha(
        self,
        connection: Any,
        *,
        subject_id: str,
        client_ip: str,
        challenge_id: str | None,
        answer: str | None,
    ) -> None:
        if not isinstance(challenge_id, str) or not challenge_id.strip():
            raise PublicPostCaptchaError("CAPTCHA challenge is required")
        if not isinstance(answer, str) or not answer.strip():
            raise PublicPostCaptchaError("CAPTCHA answer is required")
        if len(answer) > 128:
            raise PublicPostCaptchaError("CAPTCHA answer is invalid")
        row = connection.execute(
            "SELECT * FROM public_post_captcha_challenges "
            "WHERE challenge_id = ? AND subject_id = ?",
            (challenge_id, subject_id),
        ).fetchone()
        now = time.time()
        if row is None or row["client_ip"] != client_ip:
            raise PublicPostCaptchaError("CAPTCHA challenge is invalid")
        if row["consumed_at"] is not None or float(row["expires_at"]) <= now:
            raise PublicPostCaptchaError("CAPTCHA challenge has expired")
        if int(row["attempts"]) >= int(row["max_attempts"]):
            raise PublicPostCaptchaError("CAPTCHA attempt limit exceeded")
        valid = hmac.compare_digest(
            self._captcha_hash(answer.strip().upper(), row["answer_salt"]), row["answer_hash"]
        )
        if not valid:
            attempts = int(row["attempts"]) + 1
            consumed = now if attempts >= int(row["max_attempts"]) else None
            updated = connection.execute(
                "UPDATE public_post_captcha_challenges SET attempts = ?, consumed_at = ? "
                "WHERE challenge_id = ? AND subject_id = ? AND client_ip = ? "
                "AND consumed_at IS NULL AND attempts = ?",
                (attempts, consumed, challenge_id, subject_id, client_ip, int(row["attempts"])),
            ).rowcount
            if updated != 1:
                raise PublicPostCaptchaError("CAPTCHA challenge is no longer valid")
            raise PublicPostCaptchaError("CAPTCHA answer is incorrect")
        updated = connection.execute(
            "UPDATE public_post_captcha_challenges SET attempts = attempts + 1, consumed_at = ? "
            "WHERE challenge_id = ? AND subject_id = ? AND client_ip = ? "
            "AND consumed_at IS NULL AND attempts = ?",
            (now, challenge_id, subject_id, client_ip, int(row["attempts"])),
        ).rowcount
        if updated != 1:
            raise PublicPostCaptchaError("CAPTCHA challenge is no longer valid")

    @staticmethod
    def _normalize_client_ip(client_ip: str | None) -> str:
        if not isinstance(client_ip, str) or not client_ip.strip():
            raise ValueError("public post client IP is required")
        # The HTTP handler supplies a socket peer address.  Canonicalising here
        # also makes direct callers and IPv4-mapped IPv6 representations share
        # one rate-limit bucket and rejects unbounded attacker-controlled keys.
        try:
            address = ipaddress.ip_address(client_ip.strip())
            if getattr(address, "scope_id", None) is not None:
                raise ValueError("scoped IPv6 addresses are not accepted")
            mapped = getattr(address, "ipv4_mapped", None)
            normalized = mapped if mapped is not None else address
            if isinstance(normalized, ipaddress.IPv6Address):
                normalized = ipaddress.ip_network(f"{normalized}/64", strict=False).network_address
            value = str(normalized)
            if len(value) > 64:
                raise ValueError("client IP is too long")
            return value
        except ValueError as error:
            raise ValueError("public post client IP is invalid") from error

    def _client_bucket(self, client_ip: str | None) -> str:
        canonical = self._normalize_client_ip(client_ip)
        return hmac.new(
            self._ip_hash_key,
            canonical.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()

    def _reserve_captcha_issue_slot(self, now: float, limit: int) -> None:
        """Apply a cheap in-process global gate before any SQLite writer is opened."""
        cutoff = now - 60
        with self._captcha_gate_lock:
            while self._captcha_issue_times and self._captcha_issue_times[0] <= cutoff:
                self._captcha_issue_times.popleft()
            if len(self._captcha_issue_times) >= limit:
                raise PublicPostRateLimitError("CAPTCHA global issue rate limit exceeded")
            self._captcha_issue_times.append(now)

    def _ensure_disk_headroom(self, prospective_bytes: int) -> None:
        try:
            free_bytes = shutil.disk_usage(self.database.path.parent).free
        except OSError as error:
            raise PublicPostCapacityError("public post storage availability is unknown") from error
        if free_bytes - prospective_bytes < self.minimum_free_bytes:
            raise PublicPostCapacityError("public post storage pressure is critical")

    @staticmethod
    def _post_bytes(proposal: PublicPostInput) -> int:
        return sum(
            len(value.encode("utf-8"))
            for value in (proposal.title, proposal.content, proposal.author_label)
        )

    @staticmethod
    def _captcha_hash(answer: str, salt: str) -> str:
        return hashlib.sha256((salt + ":" + answer).encode("utf-8")).hexdigest()

    @staticmethod
    def _captcha_image(answer: str) -> str:
        """Return a small raster CAPTCHA without embedding the answer in markup.

        Pillow is deliberately not a runtime dependency.  The bundled 5x7
        bitmap glyphs are enough for a human challenge, while the PNG payload
        prevents an HTML/SVG parser from recovering the answer from source.
        """
        glyphs = {
            "A": ("01110", "10001", "10001", "11111", "10001", "10001", "10001"),
            "B": ("11110", "10001", "10001", "11110", "10001", "10001", "11110"),
            "C": ("01111", "10000", "10000", "10000", "10000", "10000", "01111"),
            "D": ("11110", "10001", "10001", "10001", "10001", "10001", "11110"),
            "E": ("11111", "10000", "10000", "11110", "10000", "10000", "11111"),
            "F": ("11111", "10000", "10000", "11110", "10000", "10000", "10000"),
            "G": ("01111", "10000", "10000", "10111", "10001", "10001", "01111"),
            "H": ("10001", "10001", "10001", "11111", "10001", "10001", "10001"),
            "J": ("00111", "00010", "00010", "00010", "00010", "10010", "01100"),
            "K": ("10001", "10010", "10100", "11000", "10100", "10010", "10001"),
            "L": ("10000", "10000", "10000", "10000", "10000", "10000", "11111"),
            "M": ("10001", "11011", "10101", "10101", "10001", "10001", "10001"),
            "N": ("10001", "11001", "10101", "10011", "10001", "10001", "10001"),
            "P": ("11110", "10001", "10001", "11110", "10000", "10000", "10000"),
            "Q": ("01110", "10001", "10001", "10001", "10101", "10010", "01101"),
            "R": ("11110", "10001", "10001", "11110", "10100", "10010", "10001"),
            "S": ("01111", "10000", "10000", "01110", "00001", "00001", "11110"),
            "T": ("11111", "00100", "00100", "00100", "00100", "00100", "00100"),
            "U": ("10001", "10001", "10001", "10001", "10001", "10001", "01110"),
            "V": ("10001", "10001", "10001", "10001", "10001", "01010", "00100"),
            "W": ("10001", "10001", "10001", "10101", "10101", "11011", "10001"),
            "X": ("10001", "10001", "01010", "00100", "01010", "10001", "10001"),
            "Y": ("10001", "10001", "01010", "00100", "00100", "00100", "00100"),
            "Z": ("11111", "00001", "00010", "00100", "01000", "10000", "11111"),
            "2": ("01110", "10001", "00001", "00010", "00100", "01000", "11111"),
            "3": ("11110", "00001", "00001", "01110", "00001", "00001", "11110"),
            "4": ("00010", "00110", "01010", "10010", "11111", "00010", "00010"),
            "5": ("11111", "10000", "10000", "11110", "00001", "00001", "11110"),
            "6": ("01110", "10000", "10000", "11110", "10001", "10001", "01110"),
            "7": ("11111", "00001", "00010", "00100", "01000", "01000", "01000"),
            "8": ("01110", "10001", "10001", "01110", "10001", "10001", "01110"),
            "9": ("01110", "10001", "10001", "01111", "00001", "00001", "01110"),
        }
        width, height = 240, 82
        background = secrets.choice(
            ((242, 246, 248), (247, 243, 238), (239, 245, 241), (245, 241, 247))
        )
        pixels = bytearray(list(background) * width * height)

        def put(x: int, y: int, color: tuple[int, int, int]) -> None:
            if 0 <= x < width and 0 <= y < height:
                offset = (y * width + x) * 3
                pixels[offset : offset + 3] = bytes(color)

        # Soft background texture frustrates simple connected-component
        # segmentation without making the challenge hard to read.
        for _ in range(520):
            x = secrets.randbelow(width)
            y = secrets.randbelow(height)
            color = secrets.choice(
                ((184, 196, 204), (205, 188, 198), (190, 204, 188), (215, 207, 183))
            )
            put(x, y, color)

        # Each character receives independent scale, rotation, shear and
        # placement.  The previous fixed 5x7 grid was perfectly recoverable by
        # a trivial threshold/template matcher.
        cursor = 13 + secrets.randbelow(8)
        for char in answer:
            glyph = glyphs[char]
            scale_x = 4.2 + secrets.randbelow(17) / 10
            scale_y = 4.5 + secrets.randbelow(18) / 10
            angle = math.radians(secrets.randbelow(35) - 17)
            shear = (secrets.randbelow(41) - 20) / 100
            center_x = cursor + 14 + secrets.randbelow(7) - 3
            center_y = 39 + secrets.randbelow(13) - 6
            color = secrets.choice(((28, 43, 60), (40, 35, 68), (24, 62, 65), (67, 38, 47)))
            cosine, sine = math.cos(angle), math.sin(angle)
            for row, bits in enumerate(glyph):
                for column, bit in enumerate(bits):
                    if bit == "1":
                        for dy in range(6):
                            for dx in range(6):
                                local_x = (column - 2 + (dx + 0.5) / 6) * scale_x
                                local_y = (row - 3 + (dy + 0.5) / 6) * scale_y
                                local_x += shear * local_y
                                rotated_x = local_x * cosine - local_y * sine
                                rotated_y = local_x * sine + local_y * cosine
                                put(
                                    round(center_x + rotated_x),
                                    round(center_y + rotated_y),
                                    color,
                                )
            cursor += 34 + secrets.randbelow(8) - 3

        def line(
            x0: int, y0: int, x1: int, y1: int, color: tuple[int, int, int], width_: int
        ) -> None:
            steps = max(abs(x1 - x0), abs(y1 - y0), 1)
            for step in range(steps + 1):
                x = round(x0 + (x1 - x0) * step / steps)
                y = round(y0 + (y1 - y0) * step / steps)
                for offset_y in range(-width_, width_ + 1):
                    for offset_x in range(-width_, width_ + 1):
                        if offset_x * offset_x + offset_y * offset_y <= width_ * width_ + 1:
                            put(x + offset_x, y + offset_y, color)

        # Crossing strokes are drawn after glyphs so a single dark threshold
        # no longer cleanly separates six independent templates.
        for _ in range(5):
            line(
                -5,
                8 + secrets.randbelow(height - 16),
                width + 5,
                8 + secrets.randbelow(height - 16),
                secrets.choice(((74, 91, 105), (98, 70, 88), (70, 102, 92))),
                secrets.randbelow(2) + 1,
            )
        for _ in range(180):
            put(
                secrets.randbelow(width),
                secrets.randbelow(height),
                secrets.choice(((38, 48, 58), (75, 61, 81), (56, 79, 70))),
            )

        def chunk(kind: bytes, payload: bytes) -> bytes:
            checksum = struct.pack(">I", zlib.crc32(kind + payload) & 0xFFFFFFFF)
            return struct.pack(">I", len(payload)) + kind + payload + checksum

        scanlines = b"".join(
            b"\x00" + pixels[row * width * 3 : (row + 1) * width * 3] for row in range(height)
        )
        header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
        png = (
            b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(scanlines, 6))
            + chunk(b"IEND", b"")
        )
        return "data:image/png;base64," + base64.b64encode(png).decode("ascii")
