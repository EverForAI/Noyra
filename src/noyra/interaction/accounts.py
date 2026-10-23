"""Independent public participant authentication; never accepts operator credentials."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from dataclasses import asdict
from typing import Any

from noyra.core.database import Database
from noyra.core.types import new_id, utc_now

from .participants import Participant, ParticipantStore

_KDF_SLOTS = threading.BoundedSemaphore(2)
_USERNAME = re.compile(r"[a-z0-9][a-z0-9_.-]{2,63}\Z")


class AccountError(ValueError):
    pass


class AccountRateLimited(AccountError):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def password_hash(password: str, salt: str | None = None) -> str:
    if not isinstance(password, str) or not 12 <= len(password) <= 256:
        raise AccountError("password_length_12_to_256")
    if not _KDF_SLOTS.acquire(False):
        raise AccountRateLimited("authentication_busy")
    try:
        salt = salt or secrets.token_hex(16)
        derived = hashlib.scrypt(
            password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1, maxmem=32 * 1024 * 1024
        )
        return salt + ":" + derived.hex()
    finally:
        _KDF_SLOTS.release()


def password_matches(password: str, encoded: str) -> bool:
    try:
        salt, _ = encoded.split(":", 1)
        return hmac.compare_digest(password_hash(password, salt), encoded)
    except (ValueError, TypeError):
        return False


class AccountStore:
    def __init__(self, database: Database, subject: str):
        self.database, self.subject = database, subject

    def throttle(self, client: str, operation: str, limit: int = 12) -> None:
        now = time.time()
        bucket = digest(operation + ":" + client)
        with self.database.transaction() as c:
            c.execute("DELETE FROM participant_auth_limits WHERE window_start<?", (now - 3600,))
            row = c.execute(
                "SELECT * FROM participant_auth_limits WHERE subject_id=? AND bucket=?",
                (self.subject, bucket),
            ).fetchone()
            if row and row["attempts"] >= limit:
                raise AccountRateLimited("account_rate_limited")
            if (
                not row
                and c.execute("SELECT COUNT(*) FROM participant_auth_limits").fetchone()[0] >= 10000
            ):
                raise AccountRateLimited("account_rate_limited")
            c.execute(
                "INSERT INTO participant_auth_limits VALUES(?,?,?,1) ON "
                "CONFLICT(subject_id,bucket) DO UPDATE SET attempts=attempts+1",
                (self.subject, bucket, now),
            )

    def register(
        self, username: str, password: str, name: str, *, kind: str = "human"
    ) -> dict[str, Any]:
        username = username.strip().lower()
        if (
            not _USERNAME.fullmatch(username)
            or kind not in {"human", "agent", "noyra"}
            or not 1 <= len(name.strip()) <= 128
        ):
            raise AccountError("invalid_account_details")
        encoded = password_hash(password)
        recovery = secrets.token_urlsafe(32)
        with self.database.transaction() as c:
            if c.execute(
                "SELECT 1 FROM participant_accounts WHERE subject_id=? AND username=?",
                (self.subject, username),
            ).fetchone():
                raise AccountError("account_name_unavailable")
            account = new_id("account")
            who = ParticipantStore.source(
                c,
                self.subject,
                "account",
                "local",
                account,
                label=name,
                entity_type=kind,
                authentication="account",
            )
            c.execute(
                "INSERT INTO participant_accounts VALUES(?,?,?,?,?)",
                (account, self.subject, who.entity_id, username, utc_now()),
            )
            c.execute(
                "INSERT INTO participant_credentials VALUES(?,?,?,?,?)",
                (account, self.subject, encoded, digest(recovery), utc_now()),
            )
        return {"username": username, "entity_id": who.entity_id, "recovery_code": recovery}

    def login(self, username: str, password: str) -> tuple[str, str, dict[str, Any]]:
        with self.database.connection() as c:
            row = c.execute(
                "SELECT a.account_id,p.password_digest FROM participant_accounts a JOIN "
                "participant_credentials p ON p.account_id=a.account_id AND "
                "p.subject_id=a.subject_id JOIN participant_entities e ON "
                "e.entity_id=a.entity_id AND e.subject_id=a.subject_id WHERE a.subject_id=? "
                "AND a.username=? AND e.status='active'",
                (self.subject, username.strip().lower()),
            ).fetchone()
        encoded = row["password_digest"] if row else ("00" * 16 + ":" + "00" * 64)
        if not password_matches(password, encoded) or row is None:
            raise AccountError("invalid_credentials")
        token = secrets.token_urlsafe(32)
        csrf = digest("csrf:" + token)
        now = time.time()
        with self.database.transaction() as c:
            current = c.execute(
                "SELECT password_digest FROM participant_credentials WHERE account_id=? "
                "AND subject_id=?",
                (row["account_id"], self.subject),
            ).fetchone()
            if current is None or not hmac.compare_digest(current[0], encoded):
                raise AccountError("invalid_credentials")
            c.execute("DELETE FROM participant_sessions WHERE expires_at<=?", (now,))
            sessions = c.execute(
                "SELECT session_digest FROM participant_sessions WHERE subject_id=? AND "
                "account_id=? ORDER BY created_at DESC",
                (self.subject, row["account_id"]),
            ).fetchall()
            for entry in sessions[7:]:
                c.execute("DELETE FROM participant_sessions WHERE session_digest=?", (entry[0],))
            c.execute(
                "INSERT INTO participant_sessions VALUES(?,?,?,?,?,?)",
                (digest(token), self.subject, row["account_id"], digest(csrf), now + 604800, now),
            )
        who = self.authenticate(token)
        assert who is not None
        return token, csrf, asdict(who)

    def authenticate(
        self, token: str, *, api: bool = False, csrf: str | None = None
    ) -> Participant | None:
        if not isinstance(token, str) or not 20 <= len(token) <= 256:
            return None
        with self.database.connection() as c:
            if api:
                row = c.execute(
                    "SELECT account_id FROM participant_api_credentials WHERE subject_id=? "
                    "AND credential_digest=?",
                    (self.subject, digest(token)),
                ).fetchone()
            else:
                row = c.execute(
                    "SELECT account_id,csrf_digest FROM participant_sessions WHERE "
                    "subject_id=? AND session_digest=? AND expires_at>?",
                    (self.subject, digest(token), time.time()),
                ).fetchone()
                if (
                    row
                    and csrf is not None
                    and not hmac.compare_digest(row["csrf_digest"], digest(csrf))
                ):
                    return None
            if row is None:
                return None
            source_channel = "api" if api else "account"
            entity = c.execute(
                "SELECT e.*,s.source_id,s.channel,s.authentication FROM participant_accounts "
                "a JOIN participant_entities e ON e.entity_id=a.entity_id AND "
                "e.subject_id=a.subject_id JOIN participant_sources s ON "
                "s.entity_id=e.entity_id AND s.subject_id=e.subject_id AND "
                "s.channel=? AND s.sender_id=a.account_id WHERE a.subject_id=? AND "
                "a.account_id=? AND e.status='active'",
                (source_channel, self.subject, row["account_id"]),
            ).fetchone()
            if entity is None:
                return None
            who = Participant(
                *(
                    str(entity[k])
                    for k in (
                        "entity_id",
                        "entity_type",
                        "display_name",
                        "source_id",
                        "channel",
                        "authentication",
                    )
                )
            )
            if api:
                from dataclasses import replace

                who = replace(who, authentication="credential")
            return who

    def logout(self, token: str) -> None:
        with self.database.transaction() as c:
            c.execute(
                "DELETE FROM participant_sessions WHERE subject_id=? AND session_digest=?",
                (self.subject, digest(token)),
            )

    def reset(self, username: str, recovery: str, password: str) -> str:
        encoded = password_hash(password)
        replacement = secrets.token_urlsafe(32)
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT a.account_id,p.recovery_digest FROM participant_accounts a JOIN "
                "participant_credentials p ON p.account_id=a.account_id AND "
                "p.subject_id=a.subject_id WHERE a.subject_id=? AND a.username=?",
                (self.subject, username.strip().lower()),
            ).fetchone()
            if row is None or not hmac.compare_digest(row["recovery_digest"], digest(recovery)):
                raise AccountError("invalid_credentials")
            c.execute(
                "UPDATE participant_credentials SET "
                "password_digest=?,recovery_digest=?,changed_at=? WHERE account_id=? AND "
                "subject_id=?",
                (encoded, digest(replacement), utc_now(), row["account_id"], self.subject),
            )
            c.execute(
                "DELETE FROM participant_sessions WHERE account_id=? AND subject_id=?",
                (row["account_id"], self.subject),
            )
            c.execute(
                "DELETE FROM participant_api_credentials WHERE account_id=? AND subject_id=?",
                (row["account_id"], self.subject),
            )
        return replacement

    def api_key(self, who: Participant, *, revoke: bool = False) -> str | None:
        with self.database.transaction() as c:
            row = c.execute(
                "SELECT account_id FROM participant_accounts WHERE subject_id=? AND entity_id=?",
                (self.subject, who.entity_id),
            ).fetchone()
            if row is None:
                raise AccountError("account_required")
            c.execute(
                "DELETE FROM participant_api_credentials WHERE subject_id=? AND account_id=?",
                (self.subject, row[0]),
            )
            if revoke:
                return None
            ParticipantStore.source(
                c,
                self.subject,
                "api",
                "local",
                row[0],
                entity_id=who.entity_id,
                authentication="credential",
            )
            token = "noyra_participant_" + secrets.token_urlsafe(32)
            c.execute(
                "INSERT INTO participant_api_credentials VALUES(?,?,?,?,?)",
                (digest(token), self.subject, row[0], "participant", utc_now()),
            )
            return token
