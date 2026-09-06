from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from noyra.core.at_rest import validate_private_file, validate_private_root
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.secret_cleanup import SecretCleanupQueue
from noyra.core.types import content_hash, new_id, strict_finite_float, strict_int, utc_now

from .embedding import EmbeddingSettings


class EmbeddingResourceInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=128)
    base_url: str
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr
    dimensions: int | None = Field(default=None, ge=1, le=16_384)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    daily_call_limit: int = Field(default=1_000, ge=0)
    daily_token_limit: int = Field(default=5_000_000, ge=0)
    daily_cost_limit_microusd: int = Field(default=1_000_000, ge=0)
    input_cost_microusd_per_million: int = Field(default=20_000, ge=0)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_cooldown_seconds: float = Field(default=60, ge=1, le=3_600)

    @field_validator("label", "model")
    @classmethod
    def validate_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("embedding resource text cannot be blank")
        return value

    def settings(
        self,
        api_key: SecretStr | None = None,
        *,
        resource_id: str | None = None,
    ) -> EmbeddingSettings:
        return EmbeddingSettings(
            base_url=self.base_url,
            model=self.model,
            api_key=api_key or self.api_key,
            dimensions=self.dimensions,
            timeout_seconds=self.timeout_seconds,
            resource_id=resource_id,
            daily_call_limit=self.daily_call_limit,
            daily_token_limit=self.daily_token_limit,
            daily_cost_limit_microusd=self.daily_cost_limit_microusd,
            input_cost_microusd_per_million=self.input_cost_microusd_per_million,
            circuit_failure_threshold=self.circuit_failure_threshold,
            circuit_cooldown_seconds=self.circuit_cooldown_seconds,
        )


@dataclass(frozen=True)
class EmbeddingResourceRecord:
    config_id: str
    subject_id: str
    label: str
    base_url: str
    model: str
    dimensions: int | None
    timeout_seconds: float
    daily_call_limit: int
    daily_token_limit: int
    daily_cost_limit_microusd: int
    input_cost_microusd_per_million: int
    circuit_failure_threshold: int
    circuit_cooldown_seconds: float
    key_fingerprint: str
    status: str
    created_at: str
    updated_at: str
    revoked_at: str | None


class EmbeddingResourceStore:
    """Independent embedding resource pool; secrets never enter SQLite."""

    def __init__(
        self,
        database: Database,
        secret_dir: Path | str,
        *,
        repair_on_init: bool = True,
    ):
        self.database = database
        self.secret_dir = validate_private_root(
            secret_dir, create=True, label="embedding secret root"
        )
        self.secret_cleanup = SecretCleanupQueue(database)
        if repair_on_init:
            self.secret_cleanup.repair(None, "embedding", self.secret_dir)

    def configure(
        self,
        subject_id: str,
        proposal: EmbeddingResourceInput,
        *,
        actor: str,
    ) -> EmbeddingResourceRecord:
        if not actor.strip() or actor == "subject":
            raise PermissionError("embedding resources are provided by an operator")
        settings = proposal.settings()
        config_id = new_id("embedding")
        now = utc_now()
        key_path = self._secret_path(config_id)
        intent_id: str | None = None
        try:
            intent_id = self.secret_cleanup.prepare_create(
                subject_id,
                "embedding",
                config_id,
                key_path.name,
                content_hash({"api_key": settings.api_key.get_secret_value()}),
            )
            self._write_secret(key_path, settings.api_key.get_secret_value())
            self.secret_cleanup.mark_file_ready(intent_id)
            fingerprint = content_hash({"api_key": settings.api_key.get_secret_value()})
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT config_id FROM embedding_resources WHERE subject_id = ? "
                    "AND label = ? AND status != 'revoked'",
                    (subject_id, proposal.label),
                ).fetchone()
                if existing is not None:
                    raise ValueError("embedding resource label already exists")
                connection.execute(
                    "INSERT INTO embedding_resources(\n"
                    "config_id, subject_id, label, base_url, model, "
                    "dimensions, timeout_seconds, daily_call_limit, daily_token_limit, "
                    "daily_cost_limit_microusd, input_cost_microusd_per_million, "
                    "circuit_failure_threshold, circuit_cooldown_seconds, key_fingerprint, "
                    "status, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)",
                    (
                        config_id,
                        subject_id,
                        proposal.label,
                        settings.base_url,
                        settings.model,
                        settings.dimensions,
                        settings.timeout_seconds,
                        settings.daily_call_limit,
                        settings.daily_token_limit,
                        settings.daily_cost_limit_microusd,
                        settings.input_cost_microusd_per_million,
                        settings.circuit_failure_threshold,
                        settings.circuit_cooldown_seconds,
                        fingerprint,
                        now,
                        now,
                    ),
                )
        except Exception:
            self._abort_secret_create(intent_id, key_path)
            raise
        self.secret_cleanup.mark_committed(intent_id)
        return self.get(config_id, subject_id=subject_id)

    def _abort_secret_create(self, intent_id: str | None, path: Path) -> None:
        cleanup_error: BaseException | None = None
        try:
            path.unlink(missing_ok=True)
            path.with_name(f".{path.name}.tmp").unlink(missing_ok=True)
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

    def list(self, subject_id: str) -> list[EmbeddingResourceRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM embedding_resources WHERE subject_id = ? "
                "ORDER BY created_at DESC, config_id DESC",
                (subject_id,),
            ).fetchall()
        return [self._from_row(row) for row in rows]

    def get(self, config_id: str, *, subject_id: str) -> EmbeddingResourceRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM embedding_resources WHERE config_id = ? AND subject_id = ?",
                (config_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"embedding resource not found: {config_id}")
        return self._from_row(row)

    def active_settings(self, subject_id: str) -> EmbeddingSettings | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM embedding_resources WHERE subject_id = ? AND status = 'active' "
                "ORDER BY updated_at DESC, config_id DESC LIMIT 1",
                (subject_id,),
            ).fetchone()
        if row is None:
            return None
        record = self._from_row(row)
        key_path = self._secret_path(record.config_id)
        try:
            secret = key_path.read_text(encoding="utf-8")
        except OSError as error:
            raise IntegrityError("embedding resource secret is unavailable") from error
        if content_hash({"api_key": secret}) != record.key_fingerprint:
            raise IntegrityError("embedding resource secret fingerprint mismatch")
        return EmbeddingSettings(
            base_url=record.base_url,
            model=record.model,
            api_key=SecretStr(secret),
            dimensions=record.dimensions,
            timeout_seconds=record.timeout_seconds,
            resource_id=record.config_id,
            daily_call_limit=record.daily_call_limit,
            daily_token_limit=record.daily_token_limit,
            daily_cost_limit_microusd=record.daily_cost_limit_microusd,
            input_cost_microusd_per_million=record.input_cost_microusd_per_million,
            circuit_failure_threshold=record.circuit_failure_threshold,
            circuit_cooldown_seconds=record.circuit_cooldown_seconds,
        )

    def call_authorizer(
        self,
        subject_id: str,
        settings: EmbeddingSettings,
    ) -> Callable[[], None]:
        """Bind an already-built provider to live resource status and secret state."""
        config_id = settings.resource_id
        if config_id is None:
            raise ValueError("managed embedding settings require a resource identifier")

        def authorize() -> None:
            with self.database.connection() as connection:
                row = connection.execute(
                    "SELECT subject_id, base_url, model, key_fingerprint, status "
                    "FROM embedding_resources WHERE config_id = ? AND subject_id = ?",
                    (config_id, subject_id),
                ).fetchone()
            if row is None or row["status"] != "active":
                raise PermissionError("embedding resource is no longer active")
            expected_fingerprint = content_hash({"api_key": settings.api_key.get_secret_value()})
            if (
                row["base_url"] != settings.base_url
                or row["model"] != settings.model
                or row["key_fingerprint"] != expected_fingerprint
            ):
                raise IntegrityError("embedding provider no longer matches its resource")
            try:
                current_secret = self._secret_path(config_id).read_text(encoding="utf-8")
            except OSError as error:
                raise IntegrityError("embedding resource secret is unavailable") from error
            if content_hash({"api_key": current_secret}) != expected_fingerprint:
                raise IntegrityError("embedding resource secret fingerprint mismatch")

        return authorize

    def disable(
        self,
        config_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> EmbeddingResourceRecord:
        return self._change_status(
            config_id, "disabled", reason=reason, actor=actor, subject_id=subject_id
        )

    def enable(
        self,
        config_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> EmbeddingResourceRecord:
        return self._change_status(
            config_id, "active", reason=reason, actor=actor, subject_id=subject_id
        )

    def revoke(
        self,
        config_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> EmbeddingResourceRecord:
        record = self._change_status(
            config_id, "revoked", reason=reason, actor=actor, subject_id=subject_id
        )
        try:
            self._secret_path(config_id).unlink(missing_ok=True)
            self.secret_cleanup.complete_delete(
                record.subject_id, "embedding", config_id, f"{config_id}.key"
            )
        except OSError as error:
            self.secret_cleanup.record_delete_failure(
                record.subject_id, "embedding", config_id, f"{config_id}.key", error
            )
        return record

    def cleanup_health(self, subject_id: str) -> dict[str, int | str | None]:
        return self.secret_cleanup.health(subject_id, "embedding")

    def _change_status(
        self,
        config_id: str,
        status: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> EmbeddingResourceRecord:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("embedding resource status requires an operator")
        if status not in {"active", "disabled", "revoked"}:
            raise ValueError("embedding resource status is invalid")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM embedding_resources WHERE config_id = ? AND subject_id = ?",
                (config_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"embedding resource not found: {config_id}")
            if row["status"] == "revoked" and status != "revoked":
                raise ValueError("revoked embedding resource cannot be enabled")
            connection.execute(
                "UPDATE embedding_resources SET status = ?, updated_at = ?, revoked_at = ? "
                "WHERE config_id = ? AND subject_id = ?",
                (status, now, now if status == "revoked" else None, config_id, subject_id),
            )
            if status == "revoked":
                self.secret_cleanup.prepare_delete(
                    str(row["subject_id"]),
                    "embedding",
                    str(config_id),
                    f"{config_id}.key",
                    connection=connection,
                )
        return self.get(config_id, subject_id=subject_id)

    def _secret_path(self, config_id: str) -> Path:
        if not config_id or "/" in config_id or "\\" in config_id or config_id.startswith("."):
            raise ValueError("invalid embedding resource identifier")
        return validate_private_file(self.secret_dir, f"{config_id}.key")

    @staticmethod
    def _write_secret(path: Path, value: str) -> None:
        temporary = path.with_name(f".{path.name}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            try:
                view = memoryview(value.encode("utf-8"))
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("embedding secret write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        finally:
            os.close(descriptor)
        try:
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _from_row(row: Any) -> EmbeddingResourceRecord:
        try:
            dimensions = None if row["dimensions"] is None else strict_int(row["dimensions"])
            timeout_seconds = strict_finite_float(row["timeout_seconds"])
            daily_call_limit = strict_int(row["daily_call_limit"])
            daily_token_limit = strict_int(row["daily_token_limit"])
            daily_cost_limit_microusd = strict_int(row["daily_cost_limit_microusd"])
            input_cost_microusd_per_million = strict_int(row["input_cost_microusd_per_million"])
            circuit_failure_threshold = strict_int(row["circuit_failure_threshold"])
            circuit_cooldown_seconds = strict_finite_float(row["circuit_cooldown_seconds"])
        except (KeyError, TypeError, ValueError) as error:
            raise IntegrityError("embedding resource numeric state is invalid") from error
        if dimensions is not None and not 1 <= dimensions <= 16_384:
            raise IntegrityError("embedding resource dimensions are invalid")
        if not 0 < timeout_seconds <= 300:
            raise IntegrityError("embedding resource timeout is invalid")
        if (
            min(
                daily_call_limit,
                daily_token_limit,
                daily_cost_limit_microusd,
                input_cost_microusd_per_million,
            )
            < 0
        ):
            raise IntegrityError("embedding resource budget is invalid")
        if not 1 <= circuit_failure_threshold <= 20:
            raise IntegrityError("embedding resource circuit threshold is invalid")
        if not 1 <= circuit_cooldown_seconds <= 3_600:
            raise IntegrityError("embedding resource circuit cooldown is invalid")
        status = row["status"]
        if status not in {"active", "disabled", "revoked"}:
            raise IntegrityError("embedding resource status is invalid")
        text_fields = (
            "config_id",
            "subject_id",
            "label",
            "base_url",
            "model",
            "key_fingerprint",
            "created_at",
            "updated_at",
        )
        if any(not isinstance(row[field], str) or not row[field].strip() for field in text_fields):
            raise IntegrityError("embedding resource text state is invalid")
        if not row["base_url"].startswith("https://"):
            raise IntegrityError("embedding resource endpoint is invalid")
        return EmbeddingResourceRecord(
            row["config_id"],
            row["subject_id"],
            row["label"],
            row["base_url"],
            row["model"],
            dimensions,
            timeout_seconds,
            daily_call_limit,
            daily_token_limit,
            daily_cost_limit_microusd,
            input_cost_microusd_per_million,
            circuit_failure_threshold,
            circuit_cooldown_seconds,
            row["key_fingerprint"],
            status,
            row["created_at"],
            row["updated_at"],
            row["revoked_at"],
        )
