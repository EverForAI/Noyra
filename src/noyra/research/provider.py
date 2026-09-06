from __future__ import annotations

import os
from contextlib import suppress
from pathlib import Path
from typing import Any

from noyra.core.at_rest import validate_private_file, validate_private_root
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.secret_cleanup import SecretCleanupQueue
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .types import SearchProviderInput, SearchProviderRecord

_MAX_SEARCH_RESULTS = 20


class SearchProviderStore:
    """Operator-owned search resources; secret values stay outside SQLite and public APIs."""

    def __init__(self, database: Database, secret_dir: Path | str, *, repair_on_init: bool = True):
        self.database = database
        self.secret_dir = validate_private_root(secret_dir, create=True, label="search secret root")
        self.secret_cleanup = SecretCleanupQueue(database)
        if repair_on_init:
            self.secret_cleanup.repair(None, "search", self.secret_dir)

    def configure(
        self,
        subject_id: str,
        proposal: SearchProviderInput,
        *,
        actor: str,
    ) -> SearchProviderRecord:
        if not actor.strip() or actor == "subject":
            raise PermissionError("search resources are provided by an operator")
        config_id = new_id("searchcfg")
        key_reference = f"{config_id}.key"
        intent_id: str | None = None
        existing: Any | None = None
        try:
            intent_id = self.secret_cleanup.prepare_create(
                subject_id,
                "search",
                config_id,
                key_reference,
                content_hash({"api_key": proposal.api_key}),
            )
            self._write_secret(key_reference, proposal.api_key)
            self.secret_cleanup.mark_file_ready(intent_id)
            fingerprint = content_hash({"api_key": proposal.api_key})
            now = utc_now()
            state_hash = self._state_hash(
                subject_id,
                proposal.provider_type,
                proposal.label,
                key_reference,
                fingerprint,
                proposal.extras,
                proposal.rate_limit_per_hour,
                "active",
                now,
                None,
                None,
            )
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT * FROM search_provider_configs WHERE subject_id = ? "
                    "AND label = ? AND status = 'active'",
                    (subject_id, proposal.label),
                ).fetchone()
                if existing is not None:
                    self._revoke_connection(
                        connection,
                        existing,
                        reason="replaced by operator configuration",
                    )
                connection.execute(
                    """INSERT INTO search_provider_configs(
                        config_id, subject_id, provider_type, label, key_reference,
                        key_fingerprint, extras_json, rate_limit_per_hour, status,
                        state_hash, created_at, revoked_at, revoke_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?, NULL, NULL)""",
                    (
                        config_id,
                        subject_id,
                        proposal.provider_type,
                        proposal.label,
                        key_reference,
                        fingerprint,
                        canonical_json(proposal.extras),
                        proposal.rate_limit_per_hour,
                        state_hash,
                        now,
                    ),
                )
                self._insert_revision(
                    connection,
                    config_id,
                    "active",
                    "operator configured search resource",
                    now,
                )
        except Exception:
            self._abort_secret_create(intent_id, key_reference)
            raise
        self.secret_cleanup.mark_committed(intent_id)
        if existing is not None:
            try:
                self._secret_path(existing["key_reference"]).unlink(missing_ok=True)
                self.secret_cleanup.complete_delete(
                    str(existing["subject_id"]),
                    "search",
                    str(existing["config_id"]),
                    str(existing["key_reference"]),
                )
            except OSError as error:
                self.secret_cleanup.record_delete_failure(
                    str(existing["subject_id"]),
                    "search",
                    str(existing["config_id"]),
                    str(existing["key_reference"]),
                    error,
                )
        return self.get(config_id, subject_id=subject_id)

    def _abort_secret_create(self, intent_id: str | None, reference: str) -> None:
        cleanup_error: BaseException | None = None
        try:
            self._secret_path(reference).unlink(missing_ok=True)
            self._secret_path(f".{reference}.tmp").unlink(missing_ok=True)
        except BaseException as error:
            cleanup_error = error
        if intent_id is None:
            return
        try:
            if cleanup_error is None:
                self.secret_cleanup.mark_removed(intent_id)
            else:
                self.secret_cleanup.mark(intent_id, "failed", error=cleanup_error)
        except Exception:
            pass

    def revoke(
        self,
        config_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> SearchProviderRecord:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can revoke a search resource with a reason")
        with self.database.transaction() as connection:
            row = self._get_row(connection, config_id, subject_id=subject_id)
            if row["status"] == "revoked":
                return self._from_row(row)
            self._revoke_connection(connection, row, reason=reason)
        try:
            self._secret_path(row["key_reference"]).unlink(missing_ok=True)
            self.secret_cleanup.complete_delete(
                str(row["subject_id"]), "search", str(config_id), str(row["key_reference"])
            )
        except OSError as error:
            self.secret_cleanup.record_delete_failure(
                str(row["subject_id"]), "search", str(config_id), str(row["key_reference"]), error
            )
        return self.get(config_id, subject_id=subject_id)

    def _revoke_connection(self, connection: Any, row: Any, *, reason: str) -> None:
        now = utc_now()
        extras = self._extras(row["extras_json"])
        state_hash = self._state_hash(
            row["subject_id"],
            row["provider_type"],
            row["label"],
            row["key_reference"],
            row["key_fingerprint"],
            extras,
            int(row["rate_limit_per_hour"]),
            "revoked",
            row["created_at"],
            now,
            reason,
        )
        connection.execute(
            "UPDATE search_provider_configs SET status = 'revoked', state_hash = ?, "
            "revoked_at = ?, revoke_reason = ? WHERE config_id = ? AND subject_id = ?",
            (state_hash, now, reason, row["config_id"], row["subject_id"]),
        )
        self.secret_cleanup.prepare_delete(
            str(row["subject_id"]),
            "search",
            str(row["config_id"]),
            str(row["key_reference"]),
            connection=connection,
        )
        self._insert_revision(connection, row["config_id"], "revoked", reason, now)

    def active(self, subject_id: str) -> list[SearchProviderRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM search_provider_configs WHERE subject_id = ? "
                "AND status = 'active' ORDER BY label, created_at",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def list(self, subject_id: str) -> list[SearchProviderRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM search_provider_configs WHERE subject_id = ? "
                "ORDER BY created_at DESC, config_id DESC",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, config_id: str, *, subject_id: str) -> SearchProviderRecord:
        with self.database.connection() as connection:
            return self._from_row(self._get_row(connection, config_id, subject_id=subject_id))

    def api_key(self, config_id: str, *, subject_id: str) -> str:
        with self.database.connection() as connection:
            row = self._get_row(connection, config_id, subject_id=subject_id)
        if row["status"] != "active":
            raise PermissionError("search provider is not active")
        path = self._secret_path(row["key_reference"])
        try:
            value = path.read_text(encoding="utf-8")
        except OSError as error:
            raise IntegrityError("search provider secret is missing") from error
        if content_hash({"api_key": value}) != row["key_fingerprint"]:
            raise IntegrityError("search provider secret fingerprint mismatch")
        return value

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        with self.database.read_transaction() as connection:
            configs = connection.execute(
                "SELECT * FROM search_provider_configs WHERE subject_id = ? "
                "ORDER BY created_at, config_id",
                (subject_id,),
            ).fetchall()
            config_rows = {str(row["config_id"]): row for row in configs}
            verified_secrets = 0
            for row in configs:
                record = self._from_row(row)
                config_id = record.config_id
                reference = row["key_reference"]
                if (
                    not isinstance(reference, str)
                    or reference != f"{config_id}.key"
                    or not self._valid_hash(row["key_fingerprint"])
                ):
                    raise IntegrityError(f"search provider key metadata is invalid: {config_id}")
                try:
                    secret_path = self._secret_path(reference)
                except ValueError as error:
                    raise IntegrityError(
                        f"search provider secret reference is invalid: {config_id}"
                    ) from error
                if record.status == "active" or secret_path.exists():
                    try:
                        with secret_path.open("rb") as stream:
                            raw_secret = stream.read(4_097)
                    except OSError as error:
                        raise IntegrityError(
                            f"search provider secret is unavailable: {config_id}"
                        ) from error
                    if not raw_secret or len(raw_secret) > 4_096:
                        raise IntegrityError(f"search provider secret is invalid: {config_id}")
                    try:
                        secret = raw_secret.decode("utf-8")
                    except UnicodeError as error:
                        raise IntegrityError(
                            f"search provider secret is invalid: {config_id}"
                        ) from error
                    if (
                        not secret.strip()
                        or content_hash({"api_key": secret}) != row["key_fingerprint"]
                    ):
                        raise IntegrityError(
                            f"search provider secret fingerprint mismatch: {config_id}"
                        )
                    verified_secrets += 1

            revisions = connection.execute(
                "SELECT r.* FROM search_provider_revisions r "
                "JOIN search_provider_configs c ON c.config_id = r.config_id "
                "WHERE c.subject_id = ? ORDER BY r.config_id, r.rowid",
                (subject_id,),
            ).fetchall()
            grouped_revisions: dict[str, list[Any]] = {}
            for revision in revisions:
                config_id = str(revision["config_id"])
                grouped_revisions.setdefault(config_id, []).append(revision)
                status = revision["status"]
                reason = revision["reason"]
                created_at = revision["created_at"]
                if (
                    status not in {"active", "revoked"}
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or not isinstance(created_at, str)
                    or not created_at
                    or content_hash({"status": status, "reason": reason, "created_at": created_at})
                    != revision["state_hash"]
                ):
                    raise IntegrityError(
                        f"search provider revision is invalid: {revision['revision_id']}"
                    )
            for config_id, config in config_rows.items():
                history = grouped_revisions.get(config_id, [])
                expected_statuses = (
                    ("active",) if config["status"] == "active" else ("active", "revoked")
                )
                if tuple(row["status"] for row in history) != expected_statuses:
                    raise IntegrityError(f"search provider revision history mismatch: {config_id}")
                if history[0]["created_at"] != config["created_at"]:
                    raise IntegrityError(f"search provider revision history mismatch: {config_id}")
                if config["status"] == "revoked" and (
                    history[-1]["created_at"] != config["revoked_at"]
                    or history[-1]["reason"] != config["revoke_reason"]
                ):
                    raise IntegrityError(f"search provider revision history mismatch: {config_id}")

            uses = connection.execute(
                "SELECT u.*, c.subject_id AS config_subject_id, "
                "c.provider_type AS config_provider_type, a.subject_id AS action_subject_id, "
                "a.action_type, a.tool, a.input_hash, a.resource_cost_json "
                "FROM search_provider_uses u "
                "LEFT JOIN search_provider_configs c ON c.config_id = u.config_id "
                "LEFT JOIN actions a ON a.action_id = u.action_id "
                "WHERE u.subject_id = ? OR c.subject_id = ? OR a.subject_id = ? "
                "ORDER BY u.created_at, u.use_id",
                (subject_id, subject_id, subject_id),
            ).fetchall()
            for use in uses:
                use_id = str(use["use_id"])
                if (
                    use["subject_id"] != subject_id
                    or use["config_subject_id"] != subject_id
                    or use["action_subject_id"] != subject_id
                    or use["action_type"] != "search"
                    or use["tool"] != f"search_api:{use['config_provider_type']}"
                    or not self._valid_hash(use["query_hash"])
                    or not isinstance(use["created_at"], str)
                    or not use["created_at"]
                ):
                    raise IntegrityError(f"search provider use ownership mismatch: {use_id}")
                resource = self._strict_object(
                    use["resource_cost_json"], f"search provider use action: {use_id}"
                )
                if resource:
                    try:
                        limit = strict_int(resource.get("limit"))
                    except (TypeError, ValueError, OverflowError) as error:
                        raise IntegrityError(
                            f"search provider use action mismatch: {use_id}"
                        ) from error
                    expected_resource = {
                        "config_id": use["config_id"],
                        "query_hash": use["query_hash"],
                        "limit": limit,
                    }
                    action_matches = (
                        1 <= limit <= _MAX_SEARCH_RESULTS
                        and resource == expected_resource
                        and use["input_hash"] == content_hash(expected_resource)
                    )
                else:
                    action_matches = any(
                        use["input_hash"]
                        == content_hash(
                            {
                                "config_id": use["config_id"],
                                "query_hash": use["query_hash"],
                                "limit": limit,
                            }
                        )
                        for limit in range(1, _MAX_SEARCH_RESULTS + 1)
                    )
                if not action_matches:
                    raise IntegrityError(f"search provider use action mismatch: {use_id}")
        return {
            "search_provider_configs": len(configs),
            "search_provider_revisions": len(revisions),
            "search_provider_uses": len(uses),
            "search_provider_secrets": verified_secrets,
        }

    @staticmethod
    def _insert_revision(
        connection: Any,
        config_id: str,
        status: str,
        reason: str,
        created_at: str,
    ) -> None:
        connection.execute(
            """INSERT INTO search_provider_revisions(
                revision_id, config_id, status, reason, state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                new_id("searchrev"),
                config_id,
                status,
                reason,
                content_hash({"status": status, "reason": reason, "created_at": created_at}),
                created_at,
            ),
        )

    def _write_secret(self, reference: str, value: str) -> None:
        path = self._secret_path(reference)
        temporary = self._secret_path(f".{reference}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        descriptor = os.open(temporary, flags, 0o600)
        try:
            try:
                payload = memoryview(value.encode("utf-8"))
                while payload:
                    written = os.write(descriptor, payload)
                    if written <= 0:
                        raise OSError("secret write made no progress")
                    payload = payload[written:]
                os.fsync(descriptor)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        finally:
            os.close(descriptor)
        with suppress(OSError):
            os.chmod(temporary, 0o600)
        try:
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    def _secret_path(self, reference: str) -> Path:
        try:
            return validate_private_file(self.secret_dir, reference)
        except Exception as error:
            raise ValueError("search provider secret reference is invalid") from error

    @staticmethod
    def _get_row(connection: Any, config_id: str, *, subject_id: str) -> Any:
        row = connection.execute(
            "SELECT * FROM search_provider_configs WHERE config_id = ? AND subject_id = ?",
            (config_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"search provider not found: {config_id}")
        return row

    @classmethod
    def _from_row(cls, row: Any) -> SearchProviderRecord:
        config_id = row["config_id"]
        provider_type = row["provider_type"]
        label = row["label"]
        status = row["status"]
        created_at = row["created_at"]
        revoked_at = row["revoked_at"]
        revoke_reason = row["revoke_reason"]
        try:
            rate_limit = strict_int(row["rate_limit_per_hour"])
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"search provider rate limit is invalid: {config_id}") from error
        if (
            not isinstance(config_id, str)
            or not config_id
            or provider_type not in {"brave", "bing", "tavily", "serper"}
            or not isinstance(label, str)
            or not label.strip()
            or len(label) > 128
            or not 1 <= rate_limit <= 10_000
            or status not in {"active", "revoked"}
            or not isinstance(created_at, str)
            or not created_at
            or (status == "active" and (revoked_at is not None or revoke_reason is not None))
            or (
                status == "revoked"
                and (
                    not isinstance(revoked_at, str)
                    or not revoked_at
                    or not isinstance(revoke_reason, str)
                    or not revoke_reason.strip()
                )
            )
        ):
            raise IntegrityError(f"search provider durable state is invalid: {config_id}")
        extras = cls._extras(row["extras_json"])
        expected = cls._state_hash(
            row["subject_id"],
            provider_type,
            label,
            row["key_reference"],
            row["key_fingerprint"],
            extras,
            rate_limit,
            status,
            created_at,
            revoked_at,
            revoke_reason,
        )
        if expected != row["state_hash"]:
            raise IntegrityError("search provider state hash mismatch")
        return SearchProviderRecord(
            config_id,
            row["subject_id"],
            provider_type,
            label,
            row["key_fingerprint"],
            extras,
            rate_limit,
            status,
            created_at,
            revoked_at,
            revoke_reason,
        )

    @staticmethod
    def _extras(raw: object) -> dict[str, str]:
        if not isinstance(raw, str):
            raise IntegrityError("search provider extras JSON is invalid")
        try:
            value = strict_json_loads(raw)
        except (TypeError, ValueError) as error:
            raise IntegrityError("search provider extras JSON is invalid") from error
        if not isinstance(value, dict) or not all(
            isinstance(key, str) and isinstance(item, str) for key, item in value.items()
        ):
            raise IntegrityError("search provider extras are invalid")
        if (
            len(value) > 16
            or any(
                not key.strip() or not item.strip() or len(key) > 64 or len(item) > 2_048
                for key, item in value.items()
            )
            or canonical_json(value) != raw
        ):
            raise IntegrityError("search provider extras are invalid")
        return value

    @staticmethod
    def _strict_object(raw: object, context: str) -> dict[str, Any]:
        if not isinstance(raw, str):
            raise IntegrityError(f"{context} JSON is invalid")
        try:
            value = strict_json_loads(raw)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} JSON is invalid") from error
        if not isinstance(value, dict):
            raise IntegrityError(f"{context} JSON is invalid")
        return value

    @staticmethod
    def _valid_hash(value: object) -> bool:
        return (
            isinstance(value, str)
            and len(value) == 64
            and all(character in "0123456789abcdef" for character in value)
        )

    @staticmethod
    def _state_hash(
        subject_id: str,
        provider_type: str,
        label: str,
        key_reference: str,
        fingerprint: str,
        extras: dict[str, str],
        rate_limit: int,
        status: str,
        created_at: str,
        revoked_at: str | None,
        revoke_reason: str | None,
    ) -> str:
        return content_hash(
            {
                "subject_id": subject_id,
                "provider_type": provider_type,
                "label": label,
                "key_reference": key_reference,
                "key_fingerprint": fingerprint,
                "extras": extras,
                "rate_limit_per_hour": rate_limit,
                "status": status,
                "created_at": created_at,
                "revoked_at": revoked_at,
                "revoke_reason": revoke_reason,
            }
        )
