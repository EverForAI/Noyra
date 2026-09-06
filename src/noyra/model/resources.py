from __future__ import annotations

import base64
import builtins
import ipaddress
import json
import os
import re
import threading
import time
from collections.abc import Callable, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, ClassVar, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from noyra.core.at_rest import validate_private_file, validate_private_root
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError, RuntimeOwnershipError
from noyra.core.locking import ProcessLock
from noyra.core.secret_cleanup import SecretCleanupQueue
from noyra.core.types import (
    content_hash,
    new_id,
    strict_bool,
    strict_finite_float,
    strict_int,
    utc_now,
)

from .config import OpenAICompatibleSettings
from .errors import (
    BudgetExhaustedError,
    ConfigurationError,
    ModelCallStateError,
    ProviderCallError,
    StructuredOutputError,
)
from .gateway import ModelGateway
from .ledger import ModelLedger
from .openai_compatible import OpenAICompatibleProvider
from .types import (
    BudgetLimits,
    CallRecord,
    GatewayResult,
    ModelMessage,
    ModelPricing,
    OutputT,
    RetryPolicy,
)

CognitivePool = Literal["economy", "deep"]

# SQLite INTEGER values are signed 64-bit integers.  Python's unbounded
# integers must be bounded before they reach either pydantic's public API or
# sqlite3's parameter binding, otherwise a large operator supplied value can
# fail with an implementation-level OverflowError.
SQLITE_INT64_MAX = 2**63 - 1
MICROUSD_SCALE = Decimal("1000000")
MAX_USD = Decimal(SQLITE_INT64_MAX) / MICROUSD_SCALE
# Public resource views are intentionally page-shaped.  The upper bound keeps
# operator/UI reads from turning a large resource table into an unbounded
# Python allocation while preserving the existing list-returning API.
MAX_RESOURCE_PAGE_SIZE = 1_000

_CANONICAL_INTEGER = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_CANONICAL_USD = re.compile(r"(?:0|[1-9][0-9]*)(?:\.[0-9]+)?\Z")


def _normalize_public_model_url(value: str) -> str:
    """Return one canonical, operator-safe URL for a remote model endpoint.

    ``OpenAICompatibleSettings`` is the source of truth for URL syntax and
    endpoint policy.  This small wrapper keeps the stricter resource-store
    contract in one place and turns parser/provider-specific failures into a
    stable ``ValueError`` for both pydantic input validation and persistence
    validation callers.
    """

    if type(value) is not str:
        raise ValueError("operator model resources require a public HTTPS endpoint")
    try:
        normalized = OpenAICompatibleSettings(
            base_url=value,
            model="validation",
            api_key=SecretStr("validation"),
        ).base_url
        parsed = urlsplit(normalized)
        # Accessing ``port`` forces urllib's parser to reject malformed port
        # values, which otherwise can survive the settings model untouched.
        _ = parsed.port
    except (TypeError, ValueError) as error:
        raise ValueError("operator model resources require a public HTTPS endpoint") from error

    hostname = parsed.hostname
    if parsed.scheme != "https" or hostname is None:
        raise ValueError("operator model resources require a public HTTPS endpoint")
    if hostname.casefold().rstrip(".") == "localhost":
        raise ValueError("operator model resources cannot target localhost")
    try:
        literal = ipaddress.ip_address(hostname.split("%", 1)[0])
    except ValueError:
        return normalized
    if not literal.is_global:
        raise ValueError("operator model resources require a public endpoint")
    return normalized


def _parse_budget_integer(value: Any) -> int:
    """Parse one budget integer without pydantic or sqlite coercion."""

    if isinstance(value, bool):
        raise ValueError("boolean values are not valid budget integers")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        if _CANONICAL_INTEGER.fullmatch(value) is None:
            raise ValueError("budget integers must be canonical decimal values")
        parsed = int(value, 10)
    else:
        raise ValueError("budget integers must be Python integers or canonical strings")
    if parsed < 0 or parsed > SQLITE_INT64_MAX:
        raise ValueError(f"budget integer must be between 0 and {SQLITE_INT64_MAX}")
    return parsed


def _parse_budget_usd(value: Any) -> Decimal:
    """Parse USD budgets exactly at SQLite's micro-USD precision.

    Floats are accepted for compatibility with existing JSON clients, but are
    converted through their shortest decimal representation.  Values that
    would require rounding to micro-USD are rejected instead of being rounded
    upward and accidentally consuming a larger budget.
    """

    if isinstance(value, bool):
        raise ValueError("boolean values are not valid USD budgets")
    try:
        if isinstance(value, Decimal):
            parsed = value
        elif isinstance(value, int):
            parsed = Decimal(value)
        elif isinstance(value, float):
            # str(float) avoids importing the binary approximation into the
            # accounting value while still preserving the established JSON API.
            parsed = Decimal(str(value))
        elif isinstance(value, str):
            if _CANONICAL_USD.fullmatch(value) is None:
                raise ValueError("USD budgets must be canonical decimal values")
            parsed = Decimal(value)
        else:
            raise ValueError("USD budgets must be Decimal, numeric, or canonical strings")

        if not parsed.is_finite():
            raise ValueError("USD budgets must be finite")
        if parsed < 0:
            raise ValueError("USD budgets cannot be negative")
        scaled = parsed * MICROUSD_SCALE
        if scaled != scaled.to_integral_value():
            raise ValueError("USD budgets cannot have more than six decimal places")
        if scaled > SQLITE_INT64_MAX:
            raise ValueError(f"USD budget exceeds the maximum of {MAX_USD} USD")
        return parsed
    except ArithmeticError as error:
        raise ValueError("USD budget is outside the supported numeric range") from error


class _ModelProbeOutput(BaseModel):
    """Small structured response used by the explicit operator probe."""

    model_config = ConfigDict(extra="ignore", strict=True)

    ok: bool


class CognitiveResourceGroupInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pool: CognitivePool
    label: str = Field(min_length=1, max_length=128)
    provider_type: Literal["openai_compatible"] = "openai_compatible"
    base_url: str
    model: str = Field(min_length=1, max_length=256)
    api_keys: tuple[SecretStr, ...] = Field(min_length=1, max_length=64)
    priority: int = Field(default=100, ge=0, le=1000)
    weight: int = Field(default=100, ge=1, le=1000)
    daily_attempts: int = Field(default=100, ge=0)
    daily_input_tokens: int = Field(default=100_000, ge=0)
    daily_output_tokens: int = Field(default=20_000, ge=0)
    daily_cost_limit_usd: Decimal = Field(default=Decimal("5"), ge=0)
    input_usd_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    output_usd_per_million: Decimal = Field(default=Decimal("0"), ge=0)
    max_attempts: int = Field(default=3, ge=1, le=10)

    @field_validator(
        "priority",
        "weight",
        "daily_attempts",
        "daily_input_tokens",
        "daily_output_tokens",
        "max_attempts",
        mode="before",
    )
    @classmethod
    def validate_budget_integers(cls, value: Any) -> int:
        return _parse_budget_integer(value)

    @field_validator(
        "daily_cost_limit_usd",
        "input_usd_per_million",
        "output_usd_per_million",
        mode="before",
    )
    @classmethod
    def validate_budget_usd(cls, value: Any) -> Decimal:
        return _parse_budget_usd(value)

    @field_validator("label", "model")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("cognitive resource text cannot be blank")
        return normalized

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _normalize_public_model_url(value)

    @model_validator(mode="after")
    def validate_keys(self) -> CognitiveResourceGroupInput:
        fingerprints = [content_hash({"key": key.get_secret_value()}) for key in self.api_keys]
        if any(not key.get_secret_value().strip() for key in self.api_keys):
            raise ValueError("cognitive resource key cannot be blank")
        if len(fingerprints) != len(set(fingerprints)):
            raise ValueError("duplicate cognitive resource keys are not allowed")
        return self


class CognitiveResourceGroupUpdate(BaseModel):
    """Mutable, non-secret routing and budget controls for one resource.

    Provider URL, model name and API keys intentionally remain immutable after
    creation.  Replacing those values through a new resource plus an audited
    revoke avoids changing the route underneath an in-flight call and keeps
    the secret lifecycle simple.  This model is therefore safe to expose from
    the management UI as a patch-like operation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    priority: int | None = Field(default=None, ge=0, le=1_000)
    weight: int | None = Field(default=None, ge=1, le=1_000)
    daily_attempts: int | None = Field(default=None, ge=0)
    daily_input_tokens: int | None = Field(default=None, ge=0)
    daily_output_tokens: int | None = Field(default=None, ge=0)
    daily_cost_limit_usd: Decimal | None = Field(default=None, ge=0)
    input_usd_per_million: Decimal | None = Field(default=None, ge=0)
    output_usd_per_million: Decimal | None = Field(default=None, ge=0)
    max_attempts: int | None = Field(default=None, ge=1, le=10)

    @field_validator(
        "priority",
        "weight",
        "daily_attempts",
        "daily_input_tokens",
        "daily_output_tokens",
        "max_attempts",
        mode="before",
    )
    @classmethod
    def validate_budget_integers(cls, value: Any) -> int | None:
        if value is None:
            return None
        return _parse_budget_integer(value)

    @field_validator(
        "daily_cost_limit_usd",
        "input_usd_per_million",
        "output_usd_per_million",
        mode="before",
    )
    @classmethod
    def validate_budget_usd(cls, value: Any) -> Decimal | None:
        if value is None:
            return None
        return _parse_budget_usd(value)

    @model_validator(mode="after")
    def require_change(self) -> CognitiveResourceGroupUpdate:
        if not any(value is not None for value in self.model_dump(mode="python").values()):
            raise ValueError("cognitive resource update must change at least one field")
        return self


@dataclass(frozen=True)
class CognitiveResourceGroupRecord:
    group_id: str
    subject_id: str
    pool: str
    label: str
    provider_type: str
    base_url: str
    model: str
    priority: int
    weight: int
    daily_attempts: int
    daily_input_tokens: int
    daily_output_tokens: int
    daily_cost_microusd: int
    input_microusd_per_million: int
    output_microusd_per_million: int
    max_attempts: int
    status: str
    key_count: int
    available_key_count: int
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CognitiveResourceKeyRecord:
    key_id: str
    group_id: str
    subject_id: str
    key_fingerprint: str
    status: str
    selection_count: int
    consecutive_failures: int
    cooldown_until: str | None
    last_selected_at: str | None
    last_success_at: str | None
    last_failure_at: str | None
    created_at: str


class CognitiveResourceStore:
    """Operator-provided model pools with secrets outside the runtime database."""

    def __init__(self, database: Database, secret_dir: Path | str, *, repair_on_init: bool = True):
        self.database = database
        self.secret_dir = validate_private_root(
            secret_dir, create=True, label="cognitive secret root"
        )
        self.secret_cleanup = SecretCleanupQueue(database)
        if repair_on_init:
            self.secret_cleanup.repair(None, "cognitive", self.secret_dir)

    def configure(
        self,
        subject_id: str,
        proposal: CognitiveResourceGroupInput,
        *,
        actor: str,
    ) -> CognitiveResourceGroupRecord:
        if not actor.strip() or actor == "subject":
            raise PermissionError("cognitive resources are provided by an operator")
        group_id = new_id("cgroup")
        now = utc_now()
        key_rows: list[tuple[str, str, str, str]] = []
        intent_ids: dict[str, str] = {}
        pending_secret_keys: list[tuple[str, str]] = []
        try:
            values = self._values(subject_id, proposal, now)
            self._validate_group_values(values, "active")
        except IntegrityError:
            raise
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as error:
            raise IntegrityError("cognitive resource group durable state is invalid") from error
        try:
            for key in proposal.api_keys:
                key_id = new_id("ckey")
                reference = f"{key_id}.key"
                raw = key.get_secret_value()
                fingerprint = content_hash({"api_key": raw})
                intent_ids[reference] = self.secret_cleanup.prepare_create(
                    subject_id, "cognitive", key_id, reference, fingerprint
                )
                pending_secret_keys.append((key_id, reference))
                self._write_secret(reference, raw)
                self.secret_cleanup.mark_file_ready(intent_ids[reference])
                key_rows.append((key_id, reference, fingerprint, raw))
            group_hash = self._group_hash(group_id, values, "active")
            with self.database.transaction() as connection:
                existing = connection.execute(
                    "SELECT group_id FROM cognitive_resource_groups WHERE subject_id = ? "
                    "AND pool = ? AND label = ?",
                    (subject_id, proposal.pool, proposal.label),
                ).fetchone()
                if existing is not None:
                    raise ValueError("cognitive resource label already exists in this pool")
                connection.execute(
                    """INSERT INTO cognitive_resource_groups(
                        group_id, subject_id, pool, label, provider_type, base_url, model,
                        priority, weight, daily_attempts, daily_input_tokens,
                        daily_output_tokens, daily_cost_microusd,
                        input_microusd_per_million, output_microusd_per_million,
                        max_attempts, status, state_hash, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                              'active', ?, ?, ?)""",
                    (
                        group_id,
                        *values,
                        group_hash,
                        now,
                        now,
                    ),
                )
                self._insert_group_revision(
                    connection, group_id, "active", "operator configured cognitive resource", now
                )
                for key_id, reference, fingerprint, _ in key_rows:
                    key_hash = self._key_hash(
                        key_id,
                        group_id,
                        subject_id,
                        reference,
                        fingerprint,
                        "active",
                        0,
                        0,
                        None,
                        None,
                        None,
                        None,
                        now,
                    )
                    connection.execute(
                        """INSERT INTO cognitive_resource_keys(
                            key_id, group_id, subject_id, key_reference, key_fingerprint,
                            status, selection_count, consecutive_failures, cooldown_until,
                            last_selected_at, last_success_at, last_failure_at, state_hash,
                            created_at
                        ) VALUES (?, ?, ?, ?, ?, 'active', 0, 0, NULL, NULL, NULL, NULL, ?, ?)""",
                        (key_id, group_id, subject_id, reference, fingerprint, key_hash, now),
                    )
                    self._insert_key_event(
                        connection, key_id, subject_id, "configured", "operator_configured", now
                    )
        except Exception:
            for _, reference in pending_secret_keys:
                self._abort_secret_create(intent_ids.get(reference), reference)
            raise
        for _, reference, _, _ in key_rows:
            self.secret_cleanup.mark_committed(intent_ids[reference])
        return self.get(group_id, subject_id=subject_id)

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

    def disable(
        self,
        group_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> CognitiveResourceGroupRecord:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can disable a cognitive resource")
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_group_row(connection, group_id, subject_id=subject_id)
            if row["status"] == "revoked":
                return self._from_group_row(connection, row)
            values = self._row_values(row)
            state_hash = self._group_hash(group_id, values, "disabled")
            connection.execute(
                "UPDATE cognitive_resource_groups SET status = 'disabled', state_hash = ?, "
                "updated_at = ? WHERE group_id = ? AND subject_id = ?",
                (state_hash, now, group_id, subject_id),
            )
            self._insert_group_revision(connection, group_id, "disabled", reason, now)
        return self.get(group_id, subject_id=subject_id)

    def enable(
        self,
        group_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> CognitiveResourceGroupRecord:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can enable a cognitive resource")
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_group_row(connection, group_id, subject_id=subject_id)
            if row["status"] == "revoked":
                raise ValueError("revoked cognitive resources cannot be enabled")
            values = self._row_values(row)
            state_hash = self._group_hash(group_id, values, "active")
            connection.execute(
                "UPDATE cognitive_resource_groups SET status = 'active', state_hash = ?, "
                "updated_at = ? WHERE group_id = ? AND subject_id = ?",
                (state_hash, now, group_id, subject_id),
            )
            self._insert_group_revision(connection, group_id, "active", reason, now)
        return self.get(group_id, subject_id=subject_id)

    def update(
        self,
        group_id: str,
        proposal: CognitiveResourceGroupUpdate,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> CognitiveResourceGroupRecord:
        """Atomically update non-secret routing and budget controls.

        The current status is preserved and a normal group revision is
        appended, so integrity verification can distinguish a deliberate
        budget change from an out-of-band row mutation.  A revoked resource
        is immutable; callers should create a replacement resource instead.
        """

        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can update a cognitive resource")
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_group_row(connection, group_id, subject_id=subject_id)
            if row["status"] == "revoked":
                raise ValueError("revoked cognitive resources cannot be updated")
            try:
                current = list(self._row_values(row))
                # _row_values is intentionally kept in the same order as the
                # INSERT statement.  Keep this map explicit so a future schema
                # field cannot be silently updated at the wrong offset.
                positions = {
                    "priority": 6,
                    "weight": 7,
                    "daily_attempts": 8,
                    "daily_input_tokens": 9,
                    "daily_output_tokens": 10,
                    "daily_cost_limit_usd": 11,
                    "input_usd_per_million": 12,
                    "output_usd_per_million": 13,
                    "max_attempts": 14,
                }
                for field, position in positions.items():
                    value = getattr(proposal, field)
                    if value is None:
                        continue
                    if field.endswith("_usd") or field.endswith("_usd_per_million"):
                        current[position] = self._usd_to_microusd(value)
                    else:
                        current[position] = value
                values = tuple(current)
                self._validate_group_values(values, row["status"])
                stored_values = tuple(self._row_values(row))
            except IntegrityError:
                raise
            except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as error:
                raise IntegrityError(
                    f"cognitive resource group durable state is invalid: {group_id}"
                ) from error
            if values == stored_values:
                raise ValueError("cognitive resource update has no changes")
            state_hash = self._group_hash(group_id, values, row["status"])
            connection.execute(
                "UPDATE cognitive_resource_groups SET priority = ?, weight = ?, "
                "daily_attempts = ?, daily_input_tokens = ?, daily_output_tokens = ?, "
                "daily_cost_microusd = ?, input_microusd_per_million = ?, "
                "output_microusd_per_million = ?, max_attempts = ?, state_hash = ?, "
                "updated_at = ? WHERE group_id = ? AND subject_id = ?",
                (
                    values[6],
                    values[7],
                    values[8],
                    values[9],
                    values[10],
                    values[11],
                    values[12],
                    values[13],
                    values[14],
                    state_hash,
                    now,
                    group_id,
                    subject_id,
                ),
            )
            self._insert_group_revision(connection, group_id, row["status"], reason, now)
        return self.get(group_id, subject_id=subject_id)

    def revoke(
        self,
        group_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> CognitiveResourceGroupRecord:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can revoke a cognitive resource")
        now = utc_now()
        references: list[tuple[str, str]] = []
        with self.database.transaction() as connection:
            row = self._get_group_row(connection, group_id, subject_id=subject_id)
            if row["status"] == "revoked":
                return self._from_group_row(connection, row)
            values = self._row_values(row)
            state_hash = self._group_hash(group_id, values, "revoked")
            connection.execute(
                "UPDATE cognitive_resource_groups SET status = 'revoked', state_hash = ?, "
                "updated_at = ? WHERE group_id = ? AND subject_id = ?",
                (state_hash, now, group_id, subject_id),
            )
            self._insert_group_revision(connection, group_id, "revoked", reason, now)
            keys = connection.execute(
                "SELECT * FROM cognitive_resource_keys WHERE group_id = ? ORDER BY rowid",
                (group_id,),
            )
            for key in keys:
                references.append((str(key["key_id"]), str(key["key_reference"])))
                self._update_key_connection(
                    connection,
                    key,
                    status="revoked",
                    event_type="revoked",
                    reason_code="group_revoked",
                    now=now,
                )
                self.secret_cleanup.prepare_delete(
                    str(row["subject_id"]),
                    "cognitive",
                    str(key["key_id"]),
                    str(key["key_reference"]),
                    connection=connection,
                )
        for key_id, reference in references:
            try:
                self._secret_path(reference).unlink(missing_ok=True)
                self.secret_cleanup.complete_delete(
                    str(row["subject_id"]), "cognitive", key_id, reference
                )
            except OSError as error:
                self.secret_cleanup.record_delete_failure(
                    str(row["subject_id"]), "cognitive", key_id, reference, error
                )
        return self.get(group_id, subject_id=subject_id)

    def list(
        self,
        subject_id: str,
        *,
        pool: str | None = None,
        limit: int | None = None,
        offset: int = 0,
    ) -> builtins.list[CognitiveResourceGroupRecord]:
        page_limit = self._page_limit(limit)
        page_offset = self._page_offset(offset)
        with self.database.connection() as connection:
            if pool is None:
                rows = connection.execute(
                    "SELECT * FROM cognitive_resource_groups WHERE subject_id = ? "
                    "ORDER BY pool, priority, label, group_id LIMIT ? OFFSET ?",
                    (subject_id, page_limit, page_offset),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM cognitive_resource_groups WHERE subject_id = ? AND pool = ? "
                    "ORDER BY priority, label, group_id LIMIT ? OFFSET ?",
                    (subject_id, pool, page_limit, page_offset),
                ).fetchall()
            return [self._from_group_row(connection, row) for row in rows]

    def active(
        self, subject_id: str, pool: CognitivePool
    ) -> builtins.list[CognitiveResourceGroupRecord]:
        return [record for record in self.list(subject_id, pool=pool) if record.status == "active"]

    def get(self, group_id: str, *, subject_id: str) -> CognitiveResourceGroupRecord:
        with self.database.connection() as connection:
            return self._from_group_row(
                connection, self._get_group_row(connection, group_id, subject_id=subject_id)
            )

    def keys(
        self,
        group_id: str,
        *,
        subject_id: str,
        limit: int | None = None,
        offset: int = 0,
    ) -> builtins.list[CognitiveResourceKeyRecord]:
        page_limit = self._page_limit(limit)
        page_offset = self._page_offset(offset)
        with self.database.connection() as connection:
            self._get_group_row(connection, group_id, subject_id=subject_id)
            rows = connection.execute(
                "SELECT * FROM cognitive_resource_keys WHERE group_id = ? AND subject_id = ? "
                "ORDER BY selection_count, created_at, key_id LIMIT ? OFFSET ?",
                (group_id, subject_id, page_limit, page_offset),
            ).fetchall()
            return [self._from_key_row(row) for row in rows]

    def select_key(self, group_id: str, *, subject_id: str) -> CognitiveResourceKeyRecord | None:
        now = utc_now()
        with self.database.transaction() as connection:
            group = self._get_group_row(connection, group_id, subject_id=subject_id)
            if group["status"] != "active":
                return None
            chosen = connection.execute(
                "SELECT k.* FROM cognitive_resource_keys AS k "
                "JOIN cognitive_resource_groups AS g ON g.group_id = k.group_id "
                "WHERE k.group_id = ? AND k.subject_id = ? AND g.subject_id = ? "
                "AND (k.status = 'active' OR (k.status = 'cooldown' "
                "AND k.cooldown_until IS NOT NULL AND k.cooldown_until <= ?)) "
                "ORDER BY k.selection_count, k.created_at, k.key_id LIMIT 1",
                (group_id, subject_id, subject_id, now),
            ).fetchone()
            if chosen is None:
                return None
            status = "active"
            selection_count = int(chosen["selection_count"]) + 1
            state_hash = self._key_hash(
                chosen["key_id"],
                chosen["group_id"],
                chosen["subject_id"],
                chosen["key_reference"],
                chosen["key_fingerprint"],
                status,
                selection_count,
                int(chosen["consecutive_failures"]),
                None,
                now,
                chosen["last_success_at"],
                chosen["last_failure_at"],
                chosen["created_at"],
            )
            connection.execute(
                "UPDATE cognitive_resource_keys SET status = ?, selection_count = ?, "
                "cooldown_until = NULL, last_selected_at = ?, state_hash = ? "
                "WHERE key_id = ? AND group_id = ? AND subject_id = ?",
                (
                    status,
                    selection_count,
                    now,
                    state_hash,
                    chosen["key_id"],
                    group_id,
                    subject_id,
                ),
            )
            self._insert_key_event(
                connection,
                chosen["key_id"],
                chosen["subject_id"],
                "selected",
                "least_used_key_selected",
                now,
            )
            row = connection.execute(
                "SELECT k.* FROM cognitive_resource_keys AS k "
                "JOIN cognitive_resource_groups AS g ON g.group_id = k.group_id "
                "WHERE k.key_id = ? AND k.group_id = ? AND k.subject_id = ? "
                "AND g.subject_id = ?",
                (chosen["key_id"], group_id, subject_id, subject_id),
            ).fetchone()
        return self._from_key_row(row)

    def api_key(self, key_id: str, *, subject_id: str) -> str:
        with self.database.connection() as connection:
            row = self._get_key_row(connection, key_id, subject_id=subject_id)
        if row["status"] == "revoked":
            raise PermissionError("cognitive resource key is revoked")
        try:
            value = self._secret_path(row["key_reference"]).read_text(encoding="utf-8")
        except OSError as error:
            raise IntegrityError("cognitive resource secret is missing") from error
        if content_hash({"api_key": value}) != row["key_fingerprint"]:
            raise IntegrityError("cognitive resource secret fingerprint mismatch")
        return value

    def record_success(self, key_id: str, *, subject_id: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_key_row(connection, key_id, subject_id=subject_id)
            # Revocation is a terminal operator decision.  A provider call
            # that was already in flight may report success after the key was
            # revoked; never let that late callback resurrect the credential.
            if row["status"] == "revoked":
                return
            self._update_key_connection(
                connection,
                row,
                status="active",
                event_type="succeeded",
                reason_code="provider_call_succeeded",
                now=now,
                consecutive_failures=0,
                last_success_at=now,
                cooldown_until=None,
            )

    def record_failure(
        self,
        key_id: str,
        reason_code: str,
        *,
        cooldown_seconds: int,
        subject_id: str,
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            row = self._get_key_row(connection, key_id, subject_id=subject_id)
            # See record_success: late provider failures must not move a
            # revoked key back into a live cooldown state.
            if row["status"] == "revoked":
                return
            failures = int(row["consecutive_failures"]) + 1
            cooldown_until = (
                datetime.now(UTC) + timedelta(seconds=max(1, cooldown_seconds))
            ).isoformat(timespec="milliseconds")
            self._update_key_connection(
                connection,
                row,
                status="cooldown",
                event_type="cooled_down",
                reason_code=reason_code,
                now=now,
                consecutive_failures=failures,
                last_failure_at=now,
                cooldown_until=cooldown_until,
            )

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        group_states: dict[str, dict[str, str]] = {}
        key_states: dict[str, CognitiveResourceKeyRecord] = {}
        revisions_by_group: dict[str, dict[str, Any]] = {}
        events_by_key: dict[str, dict[str, Any]] = {}
        groups_count = 0
        keys_count = 0
        group_revisions_count = 0
        key_events_count = 0
        allowed_events = {
            "configured",
            "selected",
            "succeeded",
            "failed",
            "cooled_down",
            "recovered",
            "revoked",
        }
        with self.database.read_transaction() as connection:
            for row in connection.execute(
                """SELECT g.*, g.rowid AS storage_rowid
                   FROM cognitive_resource_groups g
                   WHERE g.subject_id = ? OR EXISTS (
                       SELECT 1 FROM cognitive_resource_keys k
                       WHERE k.group_id = g.group_id AND k.subject_id = ?
                   )
                   ORDER BY g.rowid""",
                (subject_id, subject_id),
            ):
                groups_count += 1
                group_id = self._routing_text(row["group_id"], "cognitive resource group id")
                row_subject = self._routing_text(
                    row["subject_id"], f"cognitive resource group {group_id} subject"
                )
                status = self._routing_text(
                    row["status"], f"cognitive resource group {group_id} status"
                )
                created_at = self._resource_timestamp(
                    row["created_at"], f"cognitive resource group {group_id} created time"
                )
                updated_at = self._resource_timestamp(
                    row["updated_at"], f"cognitive resource group {group_id} updated time"
                )
                if row_subject != subject_id or updated_at < created_at:
                    raise IntegrityError(
                        f"cognitive resource group ownership is invalid: {group_id}"
                    )
                try:
                    values = self._row_values(row)
                    self._validate_group_values(values, status)
                except IntegrityError:
                    raise
                except (KeyError, TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"cognitive resource group durable state is invalid: {group_id}"
                    ) from error
                state_hash = self._routing_text(
                    row["state_hash"], f"cognitive resource group {group_id} state hash"
                )
                if self._group_hash(group_id, values, status) != state_hash:
                    raise IntegrityError(f"cognitive resource group hash mismatch: {group_id}")
                group_states[group_id] = {
                    "status": status,
                    "created_at": created_at,
                    "updated_at": updated_at,
                }

            for row in connection.execute(
                """SELECT k.*, k.rowid AS storage_rowid,
                          g.subject_id AS referenced_group_subject_id
                   FROM cognitive_resource_keys k
                   LEFT JOIN cognitive_resource_groups g ON g.group_id = k.group_id
                   WHERE k.subject_id = ? OR g.subject_id = ?
                   ORDER BY k.rowid""",
                (subject_id, subject_id),
            ):
                keys_count += 1
                key_id = self._routing_text(row["key_id"], "cognitive resource key id")
                group_id = self._routing_text(
                    row["group_id"], f"cognitive resource key {key_id} group"
                )
                row_subject = self._routing_text(
                    row["subject_id"], f"cognitive resource key {key_id} subject"
                )
                if (
                    row_subject != subject_id
                    or row["referenced_group_subject_id"] != subject_id
                    or group_id not in group_states
                ):
                    raise IntegrityError(f"cognitive resource key ownership is invalid: {key_id}")
                try:
                    record = self._from_key_row(row)
                except IntegrityError:
                    raise
                except (KeyError, TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"cognitive resource key durable state is invalid: {key_id}"
                    ) from error
                self._resource_timestamp(
                    record.created_at, f"cognitive resource key {key_id} created time"
                )
                if (record.status == "cooldown") != (record.cooldown_until is not None):
                    raise IntegrityError(
                        f"cognitive resource key cooldown state is invalid: {key_id}"
                    )
                if record.cooldown_until is not None:
                    self._resource_timestamp(
                        record.cooldown_until, f"cognitive resource key {key_id} cooldown time"
                    )
                for field, value in (
                    ("last selected", record.last_selected_at),
                    ("last success", record.last_success_at),
                    ("last failure", record.last_failure_at),
                ):
                    if value is not None:
                        self._resource_timestamp(
                            value, f"cognitive resource key {key_id} {field} time"
                        )
                key_states[key_id] = record

            for row in connection.execute(
                """SELECT r.*, r.rowid AS storage_rowid,
                          g.subject_id AS referenced_group_subject_id
                   FROM cognitive_resource_group_revisions r
                   JOIN cognitive_resource_groups g ON g.group_id = r.group_id
                   WHERE g.subject_id = ? OR EXISTS (
                       SELECT 1 FROM cognitive_resource_keys k
                       WHERE k.group_id = g.group_id AND k.subject_id = ?
                   )
                   ORDER BY r.rowid""",
                (subject_id, subject_id),
            ):
                group_revisions_count += 1
                revision_id = self._routing_text(row["revision_id"], "cognitive group revision id")
                group_id = self._routing_text(
                    row["group_id"], f"cognitive group revision {revision_id} group"
                )
                status = self._routing_text(
                    row["status"], f"cognitive group revision {revision_id} status"
                )
                reason = self._routing_text(
                    row["reason"], f"cognitive group revision {revision_id} reason"
                )
                created_at = self._resource_timestamp(
                    row["created_at"], f"cognitive group revision {revision_id} created time"
                )
                if (
                    group_id not in group_states
                    or row["referenced_group_subject_id"] != subject_id
                    or status not in {"active", "disabled", "revoked"}
                ):
                    raise IntegrityError(
                        f"cognitive resource group revision is invalid: {revision_id}"
                    )
                expected = content_hash(
                    {
                        "group_id": group_id,
                        "status": status,
                        "reason": reason,
                        "created_at": created_at,
                    }
                )
                if (
                    self._routing_text(
                        row["state_hash"], f"cognitive group revision {revision_id} state hash"
                    )
                    != expected
                ):
                    raise IntegrityError(
                        f"cognitive resource group revision hash mismatch: {revision_id}"
                    )
                state = revisions_by_group.get(group_id)
                if state is None:
                    state = {
                        "count": 0,
                        "first_status": status,
                        "first_created_at": created_at,
                        "previous_time": "",
                        "revoked": False,
                        "sequence_invalid": False,
                        "history_missing": (
                            status != "active" or created_at != group_states[group_id]["created_at"]
                        ),
                    }
                    revisions_by_group[group_id] = state
                elif state["revoked"] or created_at < state["previous_time"]:
                    state["sequence_invalid"] = True
                state["count"] += 1
                state["latest_status"] = status
                state["latest_created_at"] = created_at
                state["previous_time"] = created_at
                state["revoked"] = bool(state["revoked"] or status == "revoked")

            for row in connection.execute(
                """SELECT e.*, e.rowid AS storage_rowid,
                          k.subject_id AS referenced_key_subject_id,
                          k.group_id AS referenced_key_group_id,
                          g.subject_id AS referenced_group_subject_id
                   FROM cognitive_resource_key_events e
                   LEFT JOIN cognitive_resource_keys k ON k.key_id = e.key_id
                   LEFT JOIN cognitive_resource_groups g ON g.group_id = k.group_id
                   WHERE e.subject_id = ? OR k.subject_id = ? OR g.subject_id = ?
                   ORDER BY e.rowid""",
                (subject_id, subject_id, subject_id),
            ):
                key_events_count += 1
                event_id = self._routing_text(row["event_id"], "cognitive resource key event id")
                key_id = self._routing_text(
                    row["key_id"], f"cognitive resource key event {event_id} key"
                )
                row_subject = self._routing_text(
                    row["subject_id"], f"cognitive resource key event {event_id} subject"
                )
                event_type = self._routing_text(
                    row["event_type"], f"cognitive resource key event {event_id} type"
                )
                reason_code = self._routing_text(
                    row["reason_code"], f"cognitive resource key event {event_id} reason"
                )
                created_at = self._resource_timestamp(
                    row["created_at"],
                    f"cognitive resource key event {event_id} created time",
                )
                if (
                    row_subject != subject_id
                    or row["referenced_key_subject_id"] != subject_id
                    or row["referenced_group_subject_id"] != subject_id
                    or key_id not in key_states
                    or row["referenced_key_group_id"] != key_states[key_id].group_id
                    or event_type not in allowed_events
                ):
                    raise IntegrityError(
                        f"cognitive resource key event ownership is invalid: {event_id}"
                    )
                expected = content_hash(
                    {
                        "key_id": key_id,
                        "subject_id": row_subject,
                        "event_type": event_type,
                        "reason_code": reason_code,
                        "created_at": created_at,
                    }
                )
                if (
                    self._routing_text(
                        row["state_hash"],
                        f"cognitive resource key event {event_id} state hash",
                    )
                    != expected
                ):
                    raise IntegrityError(f"cognitive resource key event hash mismatch: {event_id}")
                state = events_by_key.get(key_id)
                if state is None:
                    state = {
                        "count": 0,
                        "first_event_type": event_type,
                        "first_created_at": created_at,
                        "previous_time": "",
                        "revoked": False,
                        "sequence_invalid": False,
                        "history_missing": (
                            event_type != "configured"
                            or created_at != key_states[key_id].created_at
                        ),
                        "selected": 0,
                        "consecutive_failures": 0,
                        "status": "active",
                        "last_selected": None,
                        "last_success": None,
                        "last_failure": None,
                    }
                    events_by_key[key_id] = state
                else:
                    if state["revoked"] or created_at < state["previous_time"]:
                        state["sequence_invalid"] = True
                    if event_type == "configured":
                        state["history_missing"] = True
                state["count"] += 1
                if event_type == "selected":
                    state["selected"] += 1
                    state["last_selected"] = created_at
                    state["status"] = "active"
                elif event_type == "succeeded":
                    state["consecutive_failures"] = 0
                    state["last_success"] = created_at
                    state["status"] = "active"
                elif event_type in {"failed", "cooled_down"}:
                    state["consecutive_failures"] += 1
                    state["last_failure"] = created_at
                    if event_type == "cooled_down":
                        state["status"] = "cooldown"
                elif event_type == "recovered":
                    state["consecutive_failures"] = 0
                    state["status"] = "active"
                elif event_type == "revoked":
                    state["status"] = "revoked"
                    state["revoked"] = True
                state["previous_time"] = created_at

        for group_id, group in group_states.items():
            state = revisions_by_group.get(group_id)
            if (
                state is None
                or state["history_missing"]
                or state["count"] == 0
                or state["first_status"] != "active"
                or state["first_created_at"] != group["created_at"]
            ):
                raise IntegrityError(f"cognitive resource group history is missing: {group_id}")
            if state["sequence_invalid"]:
                raise IntegrityError(
                    f"cognitive resource group revision sequence is invalid: {group_id}"
                )
            if (
                state["latest_status"] != group["status"]
                or state["latest_created_at"] != group["updated_at"]
            ):
                raise IntegrityError(f"cognitive resource group current state mismatch: {group_id}")

        for key_id, key in key_states.items():
            state = events_by_key.get(key_id)
            if (
                state is None
                or state["history_missing"]
                or state["count"] == 0
                or state["first_event_type"] != "configured"
                or state["first_created_at"] != key.created_at
            ):
                raise IntegrityError(f"cognitive resource key history is missing: {key_id}")
            if state["sequence_invalid"]:
                raise IntegrityError(f"cognitive resource key event sequence is invalid: {key_id}")
            if (
                key.selection_count != state["selected"]
                or key.consecutive_failures != state["consecutive_failures"]
                or key.last_selected_at != state["last_selected"]
                or key.last_success_at != state["last_success"]
                or key.last_failure_at != state["last_failure"]
                or key.status != state["status"]
            ):
                raise IntegrityError(f"cognitive resource key current state mismatch: {key_id}")

        return {
            "cognitive_resource_groups": groups_count,
            "cognitive_resource_keys": keys_count,
            "cognitive_resource_group_revisions": group_revisions_count,
            "cognitive_resource_key_events": key_events_count,
        }

    def verify_routing_integrity(self, subject_id: str) -> dict[str, int]:
        """Verify immutable route history and mutable wait queues without writing."""
        decision_ids: set[str] = set()
        attempts_by_decision: dict[str, dict[int, dict[str, Any]]] = {}
        outcome_decisions: set[str] = set()
        tasks_by_id: dict[str, dict[str, Any]] = {}
        revisions_by_task: dict[str, dict[str, Any]] = {}
        counts = {
            "cognitive_route_decisions": 0,
            "cognitive_route_attempts": 0,
            "cognitive_route_outcomes": 0,
            "waiting_cognitive_tasks": 0,
            "waiting_cognitive_task_revisions": 0,
        }
        route_pools = {
            "economy_model": "economy",
            "deep_model": "deep",
            "search_api": "search",
            "browser": "browser",
            "embedding": "embedding",
        }

        with self.database.read_transaction() as connection:
            for row in connection.execute(
                """SELECT g.*, g.rowid AS storage_rowid
                   FROM cognitive_resource_groups g
                   WHERE g.subject_id = ? OR EXISTS (
                       SELECT 1 FROM cognitive_resource_keys k
                       WHERE k.group_id = g.group_id AND k.subject_id = ?
                   )
                   ORDER BY g.rowid""",
                (subject_id, subject_id),
            ):
                try:
                    values = self._row_values(row)
                    self._validate_group_values(values, row["status"])
                except IntegrityError:
                    raise
                except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as error:
                    raise IntegrityError(
                        "cognitive resource group durable state is invalid"
                    ) from error

            for row in connection.execute(
                """SELECT d.*, d.rowid AS storage_rowid,
                          g.subject_id AS referenced_group_subject_id,
                          g.pool AS referenced_group_pool,
                          k.subject_id AS referenced_key_subject_id,
                          k.group_id AS referenced_key_group_id
                   FROM cognitive_route_decisions d
                   LEFT JOIN cognitive_resource_groups g ON g.group_id = d.group_id
                   LEFT JOIN cognitive_resource_keys k ON k.key_id = d.key_id
                   WHERE d.subject_id = ? OR g.subject_id = ? OR k.subject_id = ?
                   ORDER BY d.rowid""",
                (subject_id, subject_id, subject_id),
            ):
                counts["cognitive_route_decisions"] += 1
                decision_id = self._routing_text(row["decision_id"], "route decision id")
                row_subject = self._routing_text(
                    row["subject_id"], f"route decision {decision_id} subject"
                )
                purpose = self._routing_text(
                    row["purpose"], f"route decision {decision_id} purpose"
                )
                task_kind = self._routing_text(
                    row["task_kind"], f"route decision {decision_id} task kind"
                )
                selected_route = self._routing_text(
                    row["selected_route"], f"route decision {decision_id} selected route"
                )
                pool = self._routing_optional_text(
                    row["pool"], f"route decision {decision_id} pool"
                )
                group_id = self._routing_optional_text(
                    row["group_id"], f"route decision {decision_id} group"
                )
                key_id = self._routing_optional_text(
                    row["key_id"], f"route decision {decision_id} key"
                )
                importance = self._routing_unit_float(
                    row["importance"], f"route decision {decision_id} importance"
                )
                risk = self._routing_unit_float(row["risk"], f"route decision {decision_id} risk")
                ambiguity = self._routing_unit_float(
                    row["ambiguity"], f"route decision {decision_id} ambiguity"
                )
                reason_code = self._routing_text(
                    row["reason_code"], f"route decision {decision_id} reason"
                )
                created_at = self._routing_text(
                    row["created_at"], f"route decision {decision_id} created time"
                )
                state_hash = self._routing_text(
                    row["state_hash"], f"route decision {decision_id} state hash"
                )
                if (
                    row_subject != subject_id
                    or selected_route
                    not in {
                        "rule",
                        "economy_model",
                        "deep_model",
                        "search_api",
                        "browser",
                        "embedding",
                        "wait",
                    }
                    or pool not in {None, "economy", "deep", "search", "browser", "embedding"}
                    or route_pools.get(selected_route, pool) != pool
                    or task_kind != purpose.split(":", 1)[0]
                    or (key_id is not None and group_id is None)
                ):
                    raise IntegrityError(f"cognitive route decision is invalid: {decision_id}")
                if group_id is not None and (
                    row["referenced_group_subject_id"] != subject_id
                    or row["referenced_group_pool"] != pool
                ):
                    raise IntegrityError(f"cognitive route decision group mismatch: {decision_id}")
                if key_id is not None and (
                    row["referenced_key_subject_id"] != subject_id
                    or row["referenced_key_group_id"] != group_id
                ):
                    raise IntegrityError(f"cognitive route decision key mismatch: {decision_id}")
                expected = content_hash(
                    {
                        "decision_id": decision_id,
                        "subject_id": row_subject,
                        "purpose": purpose,
                        "task_kind": task_kind,
                        "selected_route": selected_route,
                        "pool": pool,
                        "group_id": group_id,
                        "key_id": key_id,
                        "importance": importance,
                        "risk": risk,
                        "ambiguity": ambiguity,
                        "reason_code": reason_code,
                        "created_at": created_at,
                    }
                )
                if state_hash != expected:
                    raise IntegrityError(f"cognitive route decision hash mismatch: {decision_id}")
                decision_ids.add(decision_id)

            for row in connection.execute(
                """SELECT a.*, a.rowid AS storage_rowid,
                          d.subject_id AS referenced_decision_subject_id,
                          d.pool AS referenced_decision_pool,
                          d.group_id AS decision_group_id,
                          d.key_id AS decision_key_id,
                          g.subject_id AS referenced_group_subject_id,
                          g.pool AS referenced_group_pool,
                          k.subject_id AS referenced_key_subject_id,
                          k.group_id AS referenced_key_group_id
                   FROM cognitive_route_attempts a
                   LEFT JOIN cognitive_route_decisions d ON d.decision_id = a.decision_id
                   LEFT JOIN cognitive_resource_groups g ON g.group_id = a.group_id
                   LEFT JOIN cognitive_resource_keys k ON k.key_id = a.key_id
                   WHERE a.subject_id = ? OR d.subject_id = ? OR g.subject_id = ?
                      OR k.subject_id = ?
                   ORDER BY a.rowid""",
                (subject_id, subject_id, subject_id, subject_id),
            ):
                counts["cognitive_route_attempts"] += 1
                attempt_id = self._routing_text(row["attempt_id"], "route attempt id")
                row_subject = self._routing_text(
                    row["subject_id"], f"route attempt {attempt_id} subject"
                )
                decision_id = self._routing_text(
                    row["decision_id"], f"route attempt {attempt_id} decision"
                )
                group_id = self._routing_text(row["group_id"], f"route attempt {attempt_id} group")
                key_id = self._routing_text(row["key_id"], f"route attempt {attempt_id} key")
                attempt_number = self._routing_nonnegative_int(
                    row["attempt_number"], f"route attempt {attempt_id} number", minimum=1
                )
                outcome = self._routing_text(row["outcome"], f"route attempt {attempt_id} outcome")
                reason_code = self._routing_text(
                    row["reason_code"], f"route attempt {attempt_id} reason"
                )
                latency_ms = self._routing_nonnegative_int(
                    row["latency_ms"], f"route attempt {attempt_id} latency"
                )
                created_at = self._routing_text(
                    row["created_at"], f"route attempt {attempt_id} created time"
                )
                state_hash = self._routing_text(
                    row["state_hash"], f"route attempt {attempt_id} state hash"
                )
                if (
                    row_subject != subject_id
                    or row["referenced_decision_subject_id"] != subject_id
                    or decision_id not in decision_ids
                    or row["referenced_group_subject_id"] != subject_id
                    or row["referenced_key_subject_id"] != subject_id
                    or row["referenced_key_group_id"] != group_id
                    or row["referenced_group_pool"] != row["referenced_decision_pool"]
                    or (
                        row["decision_group_id"] is not None
                        and row["decision_group_id"] != group_id
                    )
                    or (row["decision_key_id"] is not None and row["decision_key_id"] != key_id)
                    or outcome not in {"selected", "succeeded", "failed", "unknown"}
                ):
                    raise IntegrityError(
                        f"cognitive route attempt ownership is invalid: {attempt_id}"
                    )
                expected = content_hash(
                    {
                        "subject_id": row_subject,
                        "decision_id": decision_id,
                        "group_id": group_id,
                        "key_id": key_id,
                        "attempt_number": attempt_number,
                        "outcome": outcome,
                        "reason_code": reason_code,
                        "latency_ms": latency_ms,
                        "created_at": created_at,
                    }
                )
                if state_hash != expected:
                    raise IntegrityError(f"cognitive route attempt hash mismatch: {attempt_id}")
                self._routing_nonnegative_int(
                    row["storage_rowid"], f"route attempt {attempt_id} row id", minimum=1
                )
                by_number = attempts_by_decision.setdefault(decision_id, {})
                record = by_number.get(attempt_number)
                if record is None:
                    by_number[attempt_number] = {
                        "count": 1,
                        "first_outcome": outcome,
                        "second_outcome": None,
                    }
                elif record["count"] == 1:
                    record["count"] = 2
                    record["second_outcome"] = outcome
                else:
                    record["count"] += 1

            for row in connection.execute(
                """SELECT o.*, o.rowid AS storage_rowid,
                          d.subject_id AS referenced_decision_subject_id
                   FROM cognitive_route_outcomes o
                   LEFT JOIN cognitive_route_decisions d ON d.decision_id = o.decision_id
                   WHERE o.subject_id = ? OR d.subject_id = ?
                   ORDER BY o.rowid""",
                (subject_id, subject_id),
            ):
                counts["cognitive_route_outcomes"] += 1
                outcome_id = self._routing_text(row["outcome_id"], "route outcome id")
                row_subject = self._routing_text(
                    row["subject_id"], f"route outcome {outcome_id} subject"
                )
                decision_id = self._routing_text(
                    row["decision_id"], f"route outcome {outcome_id} decision"
                )
                outcome = self._routing_text(row["outcome"], f"route outcome {outcome_id} outcome")
                changed_state = self._routing_bool(
                    row["result_changed_state"],
                    f"route outcome {outcome_id} changed-state flag",
                )
                input_tokens = self._routing_nonnegative_int(
                    row["input_tokens"], f"route outcome {outcome_id} input tokens"
                )
                output_tokens = self._routing_nonnegative_int(
                    row["output_tokens"], f"route outcome {outcome_id} output tokens"
                )
                cost_microusd = self._routing_nonnegative_int(
                    row["cost_microusd"], f"route outcome {outcome_id} cost"
                )
                latency_ms = self._routing_nonnegative_int(
                    row["latency_ms"], f"route outcome {outcome_id} latency"
                )
                reason_code = self._routing_text(
                    row["reason_code"], f"route outcome {outcome_id} reason"
                )
                created_at = self._routing_text(
                    row["created_at"], f"route outcome {outcome_id} created time"
                )
                state_hash = self._routing_text(
                    row["state_hash"], f"route outcome {outcome_id} state hash"
                )
                if (
                    row_subject != subject_id
                    or row["referenced_decision_subject_id"] != subject_id
                    or decision_id not in decision_ids
                    or outcome not in {"succeeded", "failed", "deferred", "cached"}
                ):
                    raise IntegrityError(
                        f"cognitive route outcome ownership is invalid: {outcome_id}"
                    )
                expected = content_hash(
                    {
                        "subject_id": row_subject,
                        "decision_id": decision_id,
                        "outcome": outcome,
                        "result_changed_state": changed_state,
                        "input_tokens": input_tokens,
                        "output_tokens": output_tokens,
                        "cost_microusd": cost_microusd,
                        "latency_ms": latency_ms,
                        "reason_code": reason_code,
                        "created_at": created_at,
                    }
                )
                if state_hash != expected:
                    raise IntegrityError(f"cognitive route outcome hash mismatch: {outcome_id}")
                outcome_decisions.add(decision_id)

            for row in connection.execute(
                "SELECT *, rowid AS storage_rowid FROM waiting_cognitive_tasks "
                "WHERE subject_id = ? ORDER BY rowid",
                (subject_id,),
            ):
                counts["waiting_cognitive_tasks"] += 1
                task_id = self._routing_text(row["task_id"], "waiting cognitive task id")
                row_subject = self._routing_text(
                    row["subject_id"], f"waiting cognitive task {task_id} subject"
                )
                pool = self._routing_text(row["pool"], f"waiting cognitive task {task_id} pool")
                purpose = self._routing_text(
                    row["purpose"], f"waiting cognitive task {task_id} purpose"
                )
                status = self._routing_text(
                    row["status"], f"waiting cognitive task {task_id} status"
                )
                reason_code = self._routing_text(
                    row["reason_code"], f"waiting cognitive task {task_id} reason"
                )
                retry_count = self._routing_nonnegative_int(
                    row["retry_count"], f"waiting cognitive task {task_id} retry count"
                )
                next_retry_at = self._routing_text(
                    row["next_retry_at"], f"waiting cognitive task {task_id} next retry"
                )
                first_waited_at = self._routing_text(
                    row["first_waited_at"], f"waiting cognitive task {task_id} first wait"
                )
                updated_at = self._routing_text(
                    row["updated_at"], f"waiting cognitive task {task_id} updated time"
                )
                state_hash = self._routing_text(
                    row["state_hash"], f"waiting cognitive task {task_id} state hash"
                )
                if (
                    row_subject != subject_id
                    or pool not in {"economy", "deep", "search", "browser", "embedding"}
                    or status not in {"waiting", "resolved", "cancelled"}
                ):
                    raise IntegrityError(f"waiting cognitive task is invalid: {task_id}")
                expected = content_hash(
                    {
                        "task_id": task_id,
                        "subject_id": row_subject,
                        "pool": pool,
                        "purpose": purpose,
                        "status": status,
                        "reason_code": reason_code,
                        "retry_count": retry_count,
                        "next_retry_at": next_retry_at,
                        "first_waited_at": first_waited_at,
                        "updated_at": updated_at,
                    }
                )
                if state_hash != expected:
                    raise IntegrityError(f"waiting cognitive task hash mismatch: {task_id}")
                tasks_by_id[task_id] = {
                    "status": status,
                    "reason_code": reason_code,
                    "retry_count": retry_count,
                    "next_retry_at": next_retry_at,
                    "first_waited_at": first_waited_at,
                    "updated_at": updated_at,
                }

            for row in connection.execute(
                """SELECT r.*, r.rowid AS storage_rowid
                   FROM waiting_cognitive_task_revisions r
                   JOIN waiting_cognitive_tasks t ON t.task_id = r.task_id
                   WHERE t.subject_id = ? ORDER BY r.rowid""",
                (subject_id,),
            ):
                counts["waiting_cognitive_task_revisions"] += 1
                revision_id = self._routing_text(row["revision_id"], "waiting task revision id")
                task_id = self._routing_text(
                    row["task_id"], f"waiting task revision {revision_id} task"
                )
                status = self._routing_text(
                    row["status"], f"waiting task revision {revision_id} status"
                )
                reason_code = self._routing_text(
                    row["reason_code"], f"waiting task revision {revision_id} reason"
                )
                retry_count = self._routing_nonnegative_int(
                    row["retry_count"], f"waiting task revision {revision_id} retry count"
                )
                next_retry_at = self._routing_text(
                    row["next_retry_at"], f"waiting task revision {revision_id} next retry"
                )
                created_at = self._routing_text(
                    row["created_at"], f"waiting task revision {revision_id} created time"
                )
                state_hash = self._routing_text(
                    row["state_hash"], f"waiting task revision {revision_id} state hash"
                )
                if task_id not in tasks_by_id or status not in {
                    "waiting",
                    "resolved",
                    "cancelled",
                }:
                    raise IntegrityError(
                        f"waiting cognitive task revision is invalid: {revision_id}"
                    )
                expected = content_hash(
                    {
                        "task_id": task_id,
                        "status": status,
                        "reason_code": reason_code,
                        "retry_count": retry_count,
                        "next_retry_at": next_retry_at,
                        "created_at": created_at,
                    }
                )
                if state_hash != expected:
                    raise IntegrityError(
                        f"waiting cognitive task revision hash mismatch: {revision_id}"
                    )
                self._routing_nonnegative_int(
                    row["storage_rowid"],
                    f"waiting task revision {revision_id} row id",
                    minimum=1,
                )
                state = revisions_by_task.get(task_id)
                if state is None:
                    revisions_by_task[task_id] = {
                        "count": 1,
                        "first_created_at": created_at,
                        "previous_status": status,
                        "previous_retry_count": retry_count,
                        "previous_next_retry_at": next_retry_at,
                        "sequence_invalid": status != "waiting" or retry_count != 1,
                        "latest_status": status,
                        "latest_reason_code": reason_code,
                        "latest_retry_count": retry_count,
                        "latest_next_retry_at": next_retry_at,
                        "latest_created_at": created_at,
                    }
                else:
                    if status == "waiting":
                        valid = (
                            state["previous_status"] == "waiting"
                            and retry_count == state["previous_retry_count"] + 1
                        )
                    else:
                        valid = (
                            state["previous_status"] == "waiting"
                            and retry_count == state["previous_retry_count"]
                            and next_retry_at == state["previous_next_retry_at"]
                        )
                    if not valid:
                        state["sequence_invalid"] = True
                    state["count"] += 1
                    state["previous_status"] = status
                    state["previous_retry_count"] = retry_count
                    state["previous_next_retry_at"] = next_retry_at
                    state["latest_status"] = status
                    state["latest_reason_code"] = reason_code
                    state["latest_retry_count"] = retry_count
                    state["latest_next_retry_at"] = next_retry_at
                    state["latest_created_at"] = created_at

        for decision_id, attempt_history in attempts_by_decision.items():
            numbers = sorted(attempt_history)
            if numbers != list(range(1, len(numbers) + 1)):
                raise IntegrityError(f"cognitive route attempt sequence is invalid: {decision_id}")
            active_numbers: list[int] = []
            for number in numbers:
                record = attempt_history[number]
                count = record["count"]
                if (
                    count not in {1, 2}
                    or record["first_outcome"] != "selected"
                    or (count == 2 and record["second_outcome"] == "selected")
                ):
                    raise IntegrityError(
                        f"cognitive route attempt lifecycle is invalid: {decision_id}"
                    )
                if count == 1:
                    active_numbers.append(number)
            if len(active_numbers) > 1 or (
                active_numbers
                and (active_numbers[0] != numbers[-1] or decision_id in outcome_decisions)
            ):
                raise IntegrityError(f"cognitive route attempt lifecycle is invalid: {decision_id}")

        for task_id, task in tasks_by_id.items():
            state = revisions_by_task.get(task_id)
            if state is None:
                raise IntegrityError(f"waiting cognitive task history is missing: {task_id}")
            if state["sequence_invalid"]:
                raise IntegrityError(
                    f"waiting cognitive task revision sequence is invalid: {task_id}"
                )
            if (
                task["status"] != state["latest_status"]
                or task["reason_code"] != state["latest_reason_code"]
                or task["retry_count"] != state["latest_retry_count"]
                or task["next_retry_at"] != state["latest_next_retry_at"]
                or task["first_waited_at"] != state["first_created_at"]
                or task["updated_at"] != state["latest_created_at"]
            ):
                raise IntegrityError(f"waiting cognitive task current state mismatch: {task_id}")

        return counts

    @staticmethod
    def _routing_text(value: object, context: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise IntegrityError(f"{context} is invalid")
        return value

    @classmethod
    def _routing_optional_text(cls, value: object, context: str) -> str | None:
        if value is None:
            return None
        return cls._routing_text(value, context)

    @staticmethod
    def _routing_nonnegative_int(value: object, context: str, *, minimum: int = 0) -> int:
        try:
            parsed = strict_int(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if parsed < minimum:
            raise IntegrityError(f"{context} is invalid")
        return parsed

    @staticmethod
    def _routing_unit_float(value: object, context: str) -> float:
        try:
            parsed = strict_finite_float(value)
        except (TypeError, ValueError, OverflowError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        if not 0.0 <= parsed <= 1.0:
            raise IntegrityError(f"{context} is invalid")
        return parsed

    @staticmethod
    def _routing_bool(value: object, context: str) -> bool:
        try:
            return strict_bool(value)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"{context} is invalid") from error

    @classmethod
    def _resource_timestamp(cls, value: object, context: str) -> str:
        text = cls._routing_text(value, context)
        if len(text) > 128:
            raise IntegrityError(f"{context} is invalid")
        try:
            parsed = datetime.fromisoformat(text)
            tzinfo = parsed.tzinfo
            if tzinfo is None or parsed.utcoffset() is None:
                raise IntegrityError(f"{context} is invalid")
        except (AttributeError, OverflowError, TypeError, ValueError) as error:
            raise IntegrityError(f"{context} is invalid") from error
        return text

    @staticmethod
    def _page_limit(value: int | None) -> int:
        if value is None:
            return MAX_RESOURCE_PAGE_SIZE
        if type(value) is not int:
            raise TypeError("resource page limit must be an integer")
        return max(1, min(value, MAX_RESOURCE_PAGE_SIZE))

    @staticmethod
    def _page_offset(value: int) -> int:
        if type(value) is not int:
            raise TypeError("resource page offset must be an integer")
        return max(0, value)

    def add_keys(
        self,
        group_id: str,
        api_keys: Sequence[SecretStr],
        *,
        actor: str,
        subject_id: str,
    ) -> CognitiveResourceGroupRecord:
        if not actor.strip() or actor == "subject" or not api_keys:
            raise PermissionError("only an operator can add cognitive resource keys")
        now = utc_now()
        created: list[tuple[str, str, str]] = []
        intent_ids: dict[str, str] = {}
        with self.database.connection() as connection:
            group = self._get_group_row(connection, group_id, subject_id=subject_id)
            if group["status"] == "revoked":
                # A revoked group is terminal.  In particular, do not create
                # a new secret under it: the group revoke path has already
                # completed its one-time cleanup and will not revisit keys
                # added afterwards.
                raise ValueError("revoked cognitive resources cannot receive new keys")
        try:
            if len(api_keys) > 64:
                raise ValueError("a cognitive resource group cannot contain more than 64 keys")
            incoming: set[str] = set()
            for secret in api_keys:
                raw = secret.get_secret_value()
                if not raw.strip():
                    raise ValueError("cognitive resource key cannot be blank")
                fingerprint = content_hash({"api_key": raw})
                if fingerprint in incoming:
                    raise ValueError("duplicate cognitive resource key is not allowed")
                incoming.add(fingerprint)
                key_id = new_id("ckey")
                reference = f"{key_id}.key"
                intent_ids[reference] = self.secret_cleanup.prepare_create(
                    str(group["subject_id"]),
                    "cognitive",
                    key_id,
                    reference,
                    fingerprint,
                )
                self._write_secret(reference, raw)
                self.secret_cleanup.mark_file_ready(intent_ids[reference])
                created.append((key_id, reference, fingerprint))
            with self.database.transaction() as connection:
                # The group may have been revoked while secrets were being
                # prepared.  Re-read its status under the writer lock so a
                # concurrent revoke cannot leave an active key and an
                # orphaned secret attached to a terminal group.
                group = self._get_group_row(connection, group_id, subject_id=subject_id)
                if group["status"] == "revoked":
                    raise ValueError("revoked cognitive resources cannot receive new keys")
                existing_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM cognitive_resource_keys WHERE group_id = ?",
                        (group_id,),
                    ).fetchone()[0]
                )
                if existing_count + len(created) > 64:
                    raise ValueError("a cognitive resource group cannot contain more than 64 keys")
                # Re-check under the writer transaction.  The initial read is
                # only a fast-path; without this second check two concurrent
                # operator requests could commit the same fingerprint into a
                # group because the secrets are prepared before the DB write.
                for key_id, reference, fingerprint in created:
                    duplicate = connection.execute(
                        "SELECT 1 FROM cognitive_resource_keys WHERE group_id = ? "
                        "AND key_fingerprint = ? LIMIT 1",
                        (group_id, fingerprint),
                    ).fetchone()
                    if duplicate is not None:
                        raise ValueError("duplicate cognitive resource key is not allowed")
                    state_hash = self._key_hash(
                        key_id,
                        group_id,
                        group["subject_id"],
                        reference,
                        fingerprint,
                        "active",
                        0,
                        0,
                        None,
                        None,
                        None,
                        None,
                        now,
                    )
                    connection.execute(
                        "INSERT INTO cognitive_resource_keys("
                        "key_id, group_id, subject_id, key_reference, key_fingerprint, status, "
                        "selection_count, consecutive_failures, cooldown_until, last_selected_at, "
                        "last_success_at, last_failure_at, state_hash, created_at) "
                        "VALUES (?, ?, ?, ?, ?, 'active', 0, 0, NULL, NULL, NULL, NULL, ?, ?)",
                        (
                            key_id,
                            group_id,
                            group["subject_id"],
                            reference,
                            fingerprint,
                            state_hash,
                            now,
                        ),
                    )
                    self._insert_key_event(
                        connection,
                        key_id,
                        group["subject_id"],
                        "configured",
                        "operator_added_key",
                        now,
                    )
        except Exception:
            for reference, intent_id in intent_ids.items():
                self._abort_secret_create(intent_id, reference)
            raise
        for _, reference, _ in created:
            self.secret_cleanup.mark_committed(intent_ids[reference])
        return self.get(group_id, subject_id=subject_id)

    def revoke_key(
        self,
        key_id: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> CognitiveResourceKeyRecord:
        """Revoke one credential without deleting its audit history.

        The secret file is removed only after the durable row and append-only
        key event are committed.  If filesystem cleanup fails, the cleanup
        queue retains the intent and startup repair can finish it; the key is
        already unusable because its durable status is revoked.
        """

        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("only an operator can revoke a cognitive resource key")
        reference = ""
        with self.database.transaction() as connection:
            row = self._get_key_row(connection, key_id, subject_id=subject_id)
            if row["status"] == "revoked":
                return self._from_key_row(row)
            reference = str(row["key_reference"])
            now = utc_now()
            self._update_key_connection(
                connection,
                row,
                status="revoked",
                event_type="revoked",
                reason_code=reason,
                now=now,
            )
            self.secret_cleanup.prepare_delete(
                subject_id,
                "cognitive",
                key_id,
                reference,
                connection=connection,
            )
        try:
            self._secret_path(reference).unlink(missing_ok=True)
            self.secret_cleanup.complete_delete(subject_id, "cognitive", key_id, reference)
        except OSError as error:
            self.secret_cleanup.record_delete_failure(
                subject_id, "cognitive", key_id, reference, error
            )
        with self.database.connection() as connection:
            return self._from_key_row(self._get_key_row(connection, key_id, subject_id=subject_id))

    def _update_key_connection(
        self,
        connection: Any,
        row: Any,
        *,
        status: str,
        event_type: str,
        reason_code: str,
        now: str,
        consecutive_failures: int | None = None,
        last_success_at: str | None = None,
        last_failure_at: str | None = None,
        cooldown_until: str | None = None,
    ) -> None:
        failures = (
            int(row["consecutive_failures"])
            if consecutive_failures is None
            else consecutive_failures
        )
        success_at = row["last_success_at"] if last_success_at is None else last_success_at
        failure_at = row["last_failure_at"] if last_failure_at is None else last_failure_at
        state_hash = self._key_hash(
            row["key_id"],
            row["group_id"],
            row["subject_id"],
            row["key_reference"],
            row["key_fingerprint"],
            status,
            int(row["selection_count"]),
            failures,
            cooldown_until,
            row["last_selected_at"],
            success_at,
            failure_at,
            row["created_at"],
        )
        connection.execute(
            "UPDATE cognitive_resource_keys SET status = ?, consecutive_failures = ?, "
            "cooldown_until = ?, last_success_at = ?, last_failure_at = ?, state_hash = ? "
            "WHERE key_id = ?",
            (
                status,
                failures,
                cooldown_until,
                success_at,
                failure_at,
                state_hash,
                row["key_id"],
            ),
        )
        self._insert_key_event(
            connection, row["key_id"], row["subject_id"], event_type, reason_code, now
        )

    @staticmethod
    def _insert_group_revision(
        connection: Any, group_id: str, status: str, reason: str, now: str
    ) -> None:
        payload = {"group_id": group_id, "status": status, "reason": reason, "created_at": now}
        connection.execute(
            "INSERT INTO cognitive_resource_group_revisions("
            "revision_id, group_id, status, reason, state_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (new_id("cgrouprev"), group_id, status, reason, content_hash(payload), now),
        )

    @staticmethod
    def _insert_key_event(
        connection: Any,
        key_id: str,
        subject_id: str,
        event_type: str,
        reason_code: str,
        now: str,
    ) -> None:
        payload = {
            "key_id": key_id,
            "subject_id": subject_id,
            "event_type": event_type,
            "reason_code": reason_code,
            "created_at": now,
        }
        connection.execute(
            "INSERT INTO cognitive_resource_key_events("
            "event_id, key_id, subject_id, event_type, reason_code, state_hash, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                new_id("ckeyevt"),
                key_id,
                subject_id,
                event_type,
                reason_code,
                content_hash(payload),
                now,
            ),
        )

    @staticmethod
    def _get_group_row(connection: Any, group_id: str, *, subject_id: str | None = None) -> Any:
        if subject_id is None:
            row = connection.execute(
                "SELECT * FROM cognitive_resource_groups WHERE group_id = ?", (group_id,)
            ).fetchone()
        else:
            row = connection.execute(
                "SELECT * FROM cognitive_resource_groups WHERE group_id = ? AND subject_id = ?",
                (group_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"cognitive resource group not found: {group_id}")
        return row

    @staticmethod
    def _get_key_row(connection: Any, key_id: str, *, subject_id: str) -> Any:
        row = connection.execute(
            "SELECT k.* FROM cognitive_resource_keys AS k "
            "JOIN cognitive_resource_groups AS g ON g.group_id = k.group_id "
            "WHERE k.key_id = ? AND k.subject_id = ? AND g.subject_id = ?",
            (key_id, subject_id, subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"cognitive resource key not found: {key_id}")
        return row

    @classmethod
    def _from_group_row(cls, connection: Any, row: Any) -> CognitiveResourceGroupRecord:
        values = cls._row_values(row)
        cls._validate_group_values(values, row["status"])
        if cls._group_hash(row["group_id"], values, row["status"]) != row["state_hash"]:
            raise IntegrityError(f"cognitive resource group hash mismatch: {row['group_id']}")
        now = utc_now()
        counts = connection.execute(
            "SELECT COUNT(*) AS total, SUM(CASE WHEN status = 'active' OR "
            "(status = 'cooldown' AND cooldown_until IS NOT NULL AND cooldown_until <= ?) "
            "THEN 1 ELSE 0 END) AS available "
            "FROM cognitive_resource_keys WHERE group_id = ? AND subject_id = ?",
            (now, row["group_id"], row["subject_id"]),
        ).fetchone()
        return CognitiveResourceGroupRecord(
            row["group_id"],
            row["subject_id"],
            row["pool"],
            row["label"],
            row["provider_type"],
            row["base_url"],
            row["model"],
            values[6],
            values[7],
            values[8],
            values[9],
            values[10],
            values[11],
            values[12],
            values[13],
            values[14],
            row["status"],
            int(counts["total"]),
            int(counts["available"] or 0),
            row["created_at"],
            row["updated_at"],
        )

    @classmethod
    def _from_key_row(cls, row: Any) -> CognitiveResourceKeyRecord:
        selection_count = strict_int(row["selection_count"])
        consecutive_failures = strict_int(row["consecutive_failures"])
        if selection_count < 0 or consecutive_failures < 0:
            raise IntegrityError(f"cognitive resource key counters are invalid: {row['key_id']}")
        if row["status"] not in {"active", "cooldown", "revoked"}:
            raise IntegrityError(f"cognitive resource key status is invalid: {row['key_id']}")
        expected = cls._key_hash(
            row["key_id"],
            row["group_id"],
            row["subject_id"],
            row["key_reference"],
            row["key_fingerprint"],
            row["status"],
            selection_count,
            consecutive_failures,
            row["cooldown_until"],
            row["last_selected_at"],
            row["last_success_at"],
            row["last_failure_at"],
            row["created_at"],
        )
        if expected != row["state_hash"]:
            raise IntegrityError(f"cognitive resource key hash mismatch: {row['key_id']}")
        return CognitiveResourceKeyRecord(
            row["key_id"],
            row["group_id"],
            row["subject_id"],
            row["key_fingerprint"],
            row["status"],
            selection_count,
            consecutive_failures,
            row["cooldown_until"],
            row["last_selected_at"],
            row["last_success_at"],
            row["last_failure_at"],
            row["created_at"],
        )

    @staticmethod
    def _values(
        subject_id: str, proposal: CognitiveResourceGroupInput, now: str
    ) -> tuple[Any, ...]:
        return (
            subject_id,
            proposal.pool,
            proposal.label,
            proposal.provider_type,
            proposal.base_url,
            proposal.model,
            proposal.priority,
            proposal.weight,
            proposal.daily_attempts,
            proposal.daily_input_tokens,
            proposal.daily_output_tokens,
            CognitiveResourceStore._usd_to_microusd(proposal.daily_cost_limit_usd),
            CognitiveResourceStore._usd_to_microusd(proposal.input_usd_per_million),
            CognitiveResourceStore._usd_to_microusd(proposal.output_usd_per_million),
            proposal.max_attempts,
        )

    @staticmethod
    def _row_values(row: Any) -> tuple[Any, ...]:
        try:
            return (
                row["subject_id"],
                row["pool"],
                row["label"],
                row["provider_type"],
                row["base_url"],
                row["model"],
                row["priority"],
                row["weight"],
                row["daily_attempts"],
                row["daily_input_tokens"],
                row["daily_output_tokens"],
                row["daily_cost_microusd"],
                row["input_microusd_per_million"],
                row["output_microusd_per_million"],
                row["max_attempts"],
            )
        except (KeyError, IndexError, TypeError, ValueError, ArithmeticError) as error:
            raise IntegrityError("cognitive resource group durable state is invalid") from error

    @staticmethod
    def _validate_group_values(values: tuple[Any, ...], status: Any) -> None:
        if type(values) is not tuple or len(values) != 15:
            raise IntegrityError("cognitive resource group durable state is invalid")
        (
            subject_id,
            pool,
            label,
            provider_type,
            base_url,
            model,
            priority,
            weight,
            daily_attempts,
            daily_input_tokens,
            daily_output_tokens,
            daily_cost_microusd,
            input_microusd_per_million,
            output_microusd_per_million,
            max_attempts,
        ) = values
        text_values = (subject_id, label, base_url, model)
        if any(type(value) is not str or not value.strip() for value in text_values):
            raise IntegrityError("cognitive resource group durable state is invalid")
        if (
            type(pool) is not str
            or pool not in {"economy", "deep"}
            or type(provider_type) is not str
            or provider_type != "openai_compatible"
            or type(status) is not str
            or status not in {"active", "disabled", "revoked"}
        ):
            raise IntegrityError("cognitive resource group durable state is invalid")
        try:
            normalized_url = _normalize_public_model_url(base_url)
        except (TypeError, ValueError) as error:
            raise IntegrityError("cognitive resource group durable state is invalid") from error
        if normalized_url != base_url:
            raise IntegrityError("cognitive resource group durable state is invalid")
        numeric_values = (
            priority,
            weight,
            daily_attempts,
            daily_input_tokens,
            daily_output_tokens,
            daily_cost_microusd,
            input_microusd_per_million,
            output_microusd_per_million,
            max_attempts,
        )
        if any(
            type(value) is not int or value < 0 or value > SQLITE_INT64_MAX
            for value in numeric_values
        ):
            raise IntegrityError("cognitive resource group durable state is invalid")
        if (
            not 0 <= priority <= 1_000
            or not 1 <= weight <= 1_000
            or min(
                daily_attempts,
                daily_input_tokens,
                daily_output_tokens,
                daily_cost_microusd,
                input_microusd_per_million,
                output_microusd_per_million,
            )
            < 0
            or not 1 <= max_attempts <= 10
        ):
            raise IntegrityError("cognitive resource group durable state is invalid")

    @staticmethod
    def _group_hash(group_id: str, values: tuple[Any, ...], status: str) -> str:
        return content_hash({"group_id": group_id, "values": list(values), "status": status})

    @staticmethod
    def _key_hash(
        key_id: str,
        group_id: str,
        subject_id: str,
        reference: str,
        fingerprint: str,
        status: str,
        selection_count: int,
        failures: int,
        cooldown_until: str | None,
        last_selected_at: str | None,
        last_success_at: str | None,
        last_failure_at: str | None,
        created_at: str,
    ) -> str:
        return content_hash(
            {
                "key_id": key_id,
                "group_id": group_id,
                "subject_id": subject_id,
                "key_reference": reference,
                "key_fingerprint": fingerprint,
                "status": status,
                "selection_count": selection_count,
                "consecutive_failures": failures,
                "cooldown_until": cooldown_until,
                "last_selected_at": last_selected_at,
                "last_success_at": last_success_at,
                "last_failure_at": last_failure_at,
                "created_at": created_at,
            }
        )

    def _write_secret(self, reference: str, value: str) -> None:
        path = self._secret_path(reference)
        temporary = self._secret_path(f".{reference}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
            raise ValueError("cognitive resource secret reference is invalid") from error

    @staticmethod
    def _usd_to_microusd(value: Decimal) -> int:
        parsed = _parse_budget_usd(value)
        return int(parsed * MICROUSD_SCALE)


@dataclass(frozen=True)
class CognitiveRouteDecisionRecord:
    decision_id: str
    selected_route: str
    pool: str | None
    group_id: str | None
    key_id: str | None
    reason_code: str
    created_at: str


@dataclass(frozen=True)
class _LogicalRouteState:
    """Durable state for one routed logical request.

    The physical ``model_calls`` row is the source of truth.  A routed request
    may have several *known failed* physical calls while failover is in
    progress, but an ``unknown`` (or still in-flight) row fences the logical
    request.  ``retry`` is only returned after the ledger has recorded an
    explicit ``prepare_unknown_retry`` authorization; it carries the original
    group/key/idempotency tuple so a retry cannot silently fail over to a new
    credential.
    """

    call: CallRecord
    group_id: str
    key_id: str
    disposition: str


class RoutedModelGateway(ModelGateway):
    """Routes each cognition purpose to one isolated model pool with bounded failover."""

    _logical_route_locks: ClassVar[dict[tuple[str, str, str], tuple[threading.Lock, int]]] = {}
    _logical_route_locks_guard: ClassVar[threading.Lock] = threading.Lock()

    _ECONOMY_PREFIXES = (
        "world_cognition:",
        "interaction_cognition:",
        "research_plan:",
        "research_model_search:",
        "research_assessment:",
        "autonomous_project_formation:",
        "autonomous_project_review:",
    )

    def __init__(
        self,
        database: Database,
        subject_id: str,
        resources: CognitiveResourceStore,
        *,
        legacy_gateway: ModelGateway | None = None,
        provider_factory: Callable[[OpenAICompatibleSettings], Any] = OpenAICompatibleProvider,
        cooldown_seconds: int = 300,
        wait_base_seconds: int = 300,
        wait_max_seconds: int = 21_600,
        max_group_failovers: int = 4,
        capture_model_io: bool = False,
        capture_model_io_getter: Callable[[], bool] | None = None,
        enforce_training_policy: bool = False,
    ):
        routed_model = legacy_gateway.model if legacy_gateway is not None else "routed"
        super().__init__(
            _NoopProvider(),
            ModelLedger(database),
            model=routed_model,
            limits=BudgetLimits(0, 0, 0, 0),
            pricing=ModelPricing(),
            retry_policy=RetryPolicy(max_attempts=1),
            resource_pool="deep",
            capture_model_io=capture_model_io,
            capture_model_io_getter=capture_model_io_getter,
            enforce_training_policy=enforce_training_policy,
        )
        self.database = database
        self.subject_id = subject_id
        self.resources = resources
        self.legacy_gateway = legacy_gateway
        self.provider_factory = provider_factory
        self.cooldown_seconds = cooldown_seconds
        self.wait_base_seconds = wait_base_seconds
        self.wait_max_seconds = wait_max_seconds
        self.max_group_failovers = max_group_failovers
        self.capture_model_io = capture_model_io
        self.capture_model_io_getter = capture_model_io_getter
        self.enforce_training_policy = enforce_training_policy
        self.limits = self._aggregate_limits("deep")

    @property
    def configured_model(self) -> str:
        return self.legacy_gateway.model if self.legacy_gateway is not None else self.model

    @classmethod
    def _try_claim_logical_route(cls, key: tuple[str, str, str]) -> threading.Lock | None:
        with cls._logical_route_locks_guard:
            entry = cls._logical_route_locks.get(key)
            if entry is None:
                lock = threading.Lock()
                users = 0
            else:
                lock, users = entry
            cls._logical_route_locks[key] = (lock, users + 1)
        if lock.acquire(blocking=False):
            return lock
        cls._release_logical_route(key, lock, acquired=False)
        return None

    @classmethod
    def _release_logical_route(
        cls,
        key: tuple[str, str, str],
        lock: threading.Lock,
        *,
        acquired: bool,
    ) -> None:
        if acquired:
            lock.release()
        with cls._logical_route_locks_guard:
            entry = cls._logical_route_locks.get(key)
            if entry is None or entry[0] is not lock:
                return
            users = entry[1] - 1
            if users == 0:
                del cls._logical_route_locks[key]
            else:
                cls._logical_route_locks[key] = (lock, users)

    async def complete_structured(
        self,
        subject_id: str,
        purpose: str,
        messages: Sequence[ModelMessage],
        output_type: type[OutputT],
        *,
        idempotency_key: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> GatewayResult[OutputT]:
        """Serialize one logical request and fail closed on a concurrent owner.

        The lock spans durable route discovery, physical call preparation, and
        the provider request.  It is deliberately non-blocking: a second
        worker must not wait and then issue a duplicate side effect after the
        first worker's outcome becomes ambiguous.
        """
        lock_key = (subject_id, purpose, idempotency_key)
        lock = self._try_claim_logical_route(lock_key)
        if lock is None:
            raise ProviderCallError(
                "routed_model_call_in_progress",
                retryable=False,
                outcome_unknown=True,
            )
        route_digest = content_hash(
            {
                "subject_id": subject_id,
                "purpose": purpose,
                "idempotency_key": idempotency_key,
            }
        )
        process_lock = ProcessLock(
            self.database.path.with_name(f".{self.database.path.name}.model-route-locks")
            / f"{route_digest}.lock"
        )
        try:
            try:
                process_lock.acquire()
            except RuntimeOwnershipError as error:
                raise ProviderCallError(
                    "routed_model_call_in_progress",
                    retryable=False,
                    outcome_unknown=True,
                ) from error
            return await self._complete_structured_locked(
                subject_id,
                purpose,
                messages,
                output_type,
                idempotency_key=idempotency_key,
                max_output_tokens=max_output_tokens,
                temperature=temperature,
            )
        finally:
            process_lock.release()
            self._release_logical_route(lock_key, lock, acquired=True)

    async def _complete_structured_locked(
        self,
        subject_id: str,
        purpose: str,
        messages: Sequence[ModelMessage],
        output_type: type[OutputT],
        *,
        idempotency_key: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
    ) -> GatewayResult[OutputT]:
        if subject_id != self.subject_id:
            raise PermissionError("routed model gateway cannot serve another subject")
        pool = self.pool_for_purpose(purpose)
        self.limits = self._aggregate_limits(pool)
        decision = self._record_decision(
            purpose,
            pool,
            selected_route=f"{pool}_model",
            group_id=None,
            key_id=None,
            reason_code="purpose_classified_locally",
        )
        # A routed logical request is fenced by its first ambiguous physical
        # call.  This check must happen before cache recovery, retry timers, or
        # key selection: a later key can never prove that the first provider
        # did not receive the request.
        logical_state = self._logical_route_state(purpose, pool, idempotency_key)
        if logical_state is not None:
            if logical_state.disposition == "retry":
                forced_state = logical_state
            else:
                self._record_unavailable(
                    pool,
                    purpose,
                    "model_outcome_unknown_quarantined",
                    force=True,
                )
                self._finish_decision(
                    decision.decision_id,
                    "deferred",
                    "model_outcome_unknown_quarantined",
                )
                raise ProviderCallError(
                    "routed_model_call_quarantined",
                    retryable=False,
                    outcome_unknown=True,
                )
        else:
            forced_state = None
        recovered = self._recover_existing(
            purpose,
            pool,
            idempotency_key,
            output_type,
        )
        if recovered is not None and forced_state is None:
            self._resolve_wait(pool, purpose)
            self._finish_decision(
                decision.decision_id,
                "cached",
                "idempotent_route_recovered",
                result=recovered,
            )
            return recovered
        if forced_state is None and not self._retry_due(pool, purpose):
            self._finish_decision(
                decision.decision_id,
                "deferred",
                "resource_retry_not_due",
            )
            raise ProviderCallError(
                f"{pool}_pool_waiting",
                retryable=False,
                outcome_unknown=False,
            )
        groups = self.resources.active(subject_id, pool)
        if forced_state is not None and not groups:
            self._finish_decision(
                decision.decision_id,
                "deferred",
                "unknown_retry_route_unavailable",
            )
            raise ProviderCallError(
                "routed_unknown_retry_route_unavailable",
                retryable=False,
                outcome_unknown=True,
            )
        if not groups and self.legacy_gateway is not None:
            if self.legacy_gateway.resource_pool != pool:
                self._record_unavailable(pool, purpose, "pool_not_configured")
                self._finish_decision(
                    decision.decision_id,
                    "deferred",
                    "pool_not_configured",
                )
                raise ProviderCallError(
                    f"{pool}_pool_unavailable", retryable=False, outcome_unknown=False
                )
            groups = []
        if not groups:
            if self.legacy_gateway is None:
                self._record_unavailable(pool, purpose, "pool_not_configured")
                self._finish_decision(decision.decision_id, "deferred", "pool_not_configured")
                raise ProviderCallError(
                    f"{pool}_pool_unavailable", retryable=False, outcome_unknown=False
                )
            started = time.monotonic()
            try:
                result = await self.legacy_gateway.complete_structured(
                    subject_id,
                    purpose,
                    messages,
                    output_type,
                    idempotency_key=idempotency_key,
                    max_output_tokens=max_output_tokens,
                    temperature=temperature,
                )
            except Exception:
                self._record_unavailable(pool, purpose, "legacy_pool_failed")
                self._finish_decision(
                    decision.decision_id,
                    "failed",
                    "legacy_pool_failed",
                    latency_ms=int((time.monotonic() - started) * 1000),
                )
                raise
            self._resolve_wait(pool, purpose)
            self._finish_decision(
                decision.decision_id,
                "cached" if result.cached else "succeeded",
                "legacy_pool_succeeded",
                result=result,
                latency_ms=int((time.monotonic() - started) * 1000),
            )
            return result

        if forced_state is not None:
            # Explicit unknown retry is bound to the original route.  Do not
            # call ``select_key`` (which increments selection_count), rotate a
            # credential, or allow failover to another group.
            try:
                forced_group = self.resources.get(forced_state.group_id, subject_id=self.subject_id)
                forced_key = next(
                    key
                    for key in self.resources.keys(
                        forced_state.group_id, subject_id=self.subject_id
                    )
                    if key.key_id == forced_state.key_id
                )
            except (LookupError, IntegrityError, NotFoundError, StopIteration) as error:
                self._finish_decision(
                    decision.decision_id,
                    "deferred",
                    "unknown_retry_route_unavailable",
                )
                raise ProviderCallError(
                    "routed_unknown_retry_route_unavailable",
                    retryable=False,
                    outcome_unknown=True,
                ) from error
            if (
                forced_group.pool != pool
                or forced_group.status != "active"
                or forced_key.status == "revoked"
            ):
                self._finish_decision(
                    decision.decision_id,
                    "deferred",
                    "unknown_retry_route_unavailable",
                )
                raise ProviderCallError(
                    "routed_unknown_retry_route_unavailable",
                    retryable=False,
                    outcome_unknown=True,
                )
            groups = [forced_group]
        else:
            groups = self._weighted_group_order(groups)
        terminal_error: Exception | None = None
        route_in_progress = False
        attempts = 0
        for group in groups:
            if attempts >= self.max_group_failovers:
                break
            tried_keys: set[str] = set()
            while attempts < self.max_group_failovers:
                if forced_state is not None:
                    key = forced_key if attempts == 0 else None
                else:
                    key = self.resources.select_key(group.group_id, subject_id=self.subject_id)
                if key is None or key.key_id in tried_keys:
                    break
                tried_keys.add(key.key_id)
                attempts += 1
                routed_idempotency = (
                    forced_state.call.idempotency_key
                    if forced_state is not None
                    else (
                        f"{self._route_prefix(idempotency_key, pool)}{group.group_id}:"
                        f"key:{key.key_id}:selection:{key.selection_count}"
                    )
                )
                decision = self._assign_decision(decision, group.group_id, key.key_id)
                attempt_number = attempts
                started = time.monotonic()
                self._record_attempt(
                    decision.decision_id,
                    group.group_id,
                    key.key_id,
                    attempt_number,
                    "selected",
                    "resource_selected",
                    0,
                )
                try:
                    gateway = self._gateway(group, key)
                except Exception as error:
                    latency = int((time.monotonic() - started) * 1000)
                    self._record_attempt(
                        decision.decision_id,
                        group.group_id,
                        key.key_id,
                        attempt_number,
                        "failed",
                        type(error).__name__,
                        latency,
                    )
                    terminal_error = error
                    if forced_state is not None:
                        break
                    continue
                try:
                    result = await gateway.complete_structured(
                        subject_id,
                        purpose,
                        messages,
                        output_type,
                        idempotency_key=routed_idempotency,
                        max_output_tokens=max_output_tokens,
                        temperature=temperature,
                    )
                except (ProviderCallError, StructuredOutputError) as error:
                    latency = int((time.monotonic() - started) * 1000)
                    self.resources.record_failure(
                        key.key_id,
                        type(error).__name__,
                        cooldown_seconds=self.cooldown_seconds,
                        subject_id=self.subject_id,
                    )
                    self._record_attempt(
                        decision.decision_id,
                        group.group_id,
                        key.key_id,
                        attempt_number,
                        (
                            "unknown"
                            if isinstance(error, ProviderCallError) and error.outcome_unknown
                            else "failed"
                        ),
                        getattr(error, "code", type(error).__name__),
                        latency,
                    )
                    terminal_error = error
                    if forced_state is not None or (
                        isinstance(error, ProviderCallError) and error.outcome_unknown
                    ):
                        break
                except (BudgetExhaustedError, ModelCallStateError) as error:
                    latency = int((time.monotonic() - started) * 1000)
                    self._record_attempt(
                        decision.decision_id,
                        group.group_id,
                        key.key_id,
                        attempt_number,
                        "failed",
                        type(error).__name__,
                        latency,
                    )
                    terminal_error = error
                    if isinstance(error, ModelCallStateError):
                        # Another worker may have claimed the same logical
                        # request between route inspection and durable
                        # preparation.  This is not a provider failure and
                        # must never trigger credential failover.
                        route_in_progress = True
                    break
                else:
                    latency = int((time.monotonic() - started) * 1000)
                    self.resources.record_success(key.key_id, subject_id=self.subject_id)
                    self._record_attempt(
                        decision.decision_id,
                        group.group_id,
                        key.key_id,
                        attempt_number,
                        "succeeded",
                        "provider_call_succeeded",
                        latency,
                    )
                    self._resolve_wait(pool, purpose)
                    self._finish_decision(
                        decision.decision_id,
                        "cached" if result.cached else "succeeded",
                        "resource_pool_succeeded",
                        result=result,
                        latency_ms=latency,
                    )
                    return result
                finally:
                    await self._close_gateway(gateway)
            if isinstance(terminal_error, ProviderCallError) and terminal_error.outcome_unknown:
                break
            if forced_state is not None:
                # An explicitly-authorized retry is still pinned to the
                # original route even when it has a known terminal failure.
                break
            if isinstance(terminal_error, BudgetExhaustedError):
                continue
            if isinstance(terminal_error, ModelCallStateError):
                break
        if isinstance(terminal_error, ProviderCallError) and terminal_error.outcome_unknown:
            self._record_unavailable(
                pool,
                purpose,
                "model_outcome_unknown_quarantined",
                force=True,
            )
            self._finish_decision(
                decision.decision_id,
                "deferred",
                "model_outcome_unknown_quarantined",
            )
            raise terminal_error
        if route_in_progress:
            self._record_unavailable(
                pool,
                purpose,
                "routed_model_call_in_progress",
                force=True,
            )
            self._finish_decision(
                decision.decision_id,
                "deferred",
                "routed_model_call_in_progress",
            )
            raise ProviderCallError(
                "routed_model_call_in_progress",
                retryable=False,
                outcome_unknown=True,
            ) from terminal_error
        self._record_unavailable(pool, purpose, "all_pool_resources_unavailable")
        self._finish_decision(decision.decision_id, "failed", "all_pool_resources_unavailable")
        if terminal_error is not None:
            raise terminal_error
        raise ProviderCallError(f"{pool}_pool_unavailable", retryable=False, outcome_unknown=False)

    @classmethod
    def pool_for_purpose(cls, purpose: str) -> CognitivePool:
        if purpose.startswith(cls._ECONOMY_PREFIXES):
            return "economy"
        return "deep"

    def limits_for_pool(self, pool: CognitivePool) -> BudgetLimits:
        return self._aggregate_limits(pool)

    def _logical_route_state(
        self, purpose: str, pool: CognitivePool, idempotency_key: str
    ) -> _LogicalRouteState | None:
        """Return the durable fence for a routed logical request.

        Physical calls intentionally include the selected group/key in their
        idempotency key so ordinary, *known* provider failures can fail over.
        That suffix is also the durable binding needed to stop an ambiguous
        call from rotating credentials on the next tick.  We inspect all
        matching physical rows in creation order and fail closed if any row is
        unknown or still in flight.  A prepared row is admitted only when the
        append-only operator authorization audit exists.
        """

        prefix = self._route_prefix(idempotency_key, pool)
        legacy_prefix = self._legacy_route_prefix(idempotency_key, pool)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_calls WHERE subject_id = ? AND purpose = ? "
                "AND resource_pool = ? AND ("
                "substr(idempotency_key, 1, ?) = ? OR "
                "substr(idempotency_key, 1, ?) = ?) "
                "ORDER BY created_at, call_id LIMIT 257",
                (
                    self.subject_id,
                    purpose,
                    pool,
                    len(prefix),
                    prefix,
                    len(legacy_prefix),
                    legacy_prefix,
                ),
            ).fetchall()
            if len(rows) > 256:
                row = rows[0]
                _matched, metadata = self._route_metadata(
                    str(row["idempotency_key"]), idempotency_key, pool
                )
                return _LogicalRouteState(
                    self.ledger._call_from_row(row),
                    metadata[0] if metadata else "",
                    metadata[1] if metadata else "",
                    "quarantine",
                )
            if not rows:
                return None
            call_ids = [str(row["call_id"]) for row in rows]
            placeholders = ",".join("?" for _ in call_ids)
            audit_rows = connection.execute(
                "SELECT action, payload_json FROM audit_records "
                "WHERE subject_id = ? AND action IN "
                "('model_unknown_retry_authorized', 'model_unknown_retry_cancelled') "
                "AND json_valid(payload_json) "
                f"AND json_extract(payload_json, '$.call_id') IN ({placeholders}) "
                "ORDER BY occurred_at, audit_id",
                (self.subject_id, *call_ids),
            ).fetchall()

        authorized_call_ids: set[str] = set()
        cancelled_call_ids: set[str] = set()
        for audit_row in audit_rows:
            try:
                payload = json.loads(str(audit_row["payload_json"]))
            except (TypeError, ValueError):
                # Integrity verification owns malformed audit payloads.  A
                # malformed authorization must never grant a retry here.
                continue
            if isinstance(payload, dict) and isinstance(payload.get("call_id"), str):
                call_id = payload["call_id"]
                if audit_row["action"] == "model_unknown_retry_cancelled":
                    cancelled_call_ids.add(call_id)
                else:
                    authorized_call_ids.add(call_id)

        retry_state: _LogicalRouteState | None = None
        for row in rows:
            physical_key = str(row["idempotency_key"])
            matched, metadata = self._route_metadata(physical_key, idempotency_key, pool)
            if not matched:
                continue
            if metadata is None or str(row["resource_group_id"]) != metadata[0]:
                # A malformed/mismatched physical route is not safe to reuse.
                # Treat it as quarantined rather than selecting another key.
                call = self.ledger._call_from_row(row)
                return _LogicalRouteState(
                    call,
                    metadata[0] if metadata else "",
                    metadata[1] if metadata else "",
                    "quarantine",
                )
            group_id, key_id = metadata
            call = self.ledger._call_from_row(row)
            status = str(row["status"])
            if status == "unknown" or status == "executing":
                return _LogicalRouteState(call, group_id, key_id, "quarantine")
            if status == "prepared":
                if call.call_id in authorized_call_ids:
                    if retry_state is not None:
                        # Two independently authorized prepared rows mean the
                        # logical request already forked; do not guess which
                        # physical route is safe to execute.
                        return _LogicalRouteState(call, group_id, key_id, "quarantine")
                    retry_state = _LogicalRouteState(call, group_id, key_id, "retry")
                    continue
                return _LogicalRouteState(call, group_id, key_id, "quarantine")
            if (
                status == "failed"
                and call.call_id in authorized_call_ids
                and call.call_id not in cancelled_call_ids
            ):
                # One explicitly-authorized physical retry is terminal when
                # it fails.  A later tick cannot treat the durable audit row
                # as unlimited permission to rotate credentials and call
                # again.  By contrast, operator reconciliation to a known
                # failed outcome releases the ambiguity fence.
                return _LogicalRouteState(call, group_id, key_id, "quarantine")
        return retry_state

    @staticmethod
    def _route_prefix(idempotency_key: str, pool: CognitivePool) -> str:
        encoded = base64.urlsafe_b64encode(idempotency_key.encode("utf-8")).decode("ascii")
        return f"noyra-route-v2:{encoded}:pool:{pool}:group:"

    @staticmethod
    def _legacy_route_prefix(idempotency_key: str, pool: CognitivePool) -> str:
        return f"{idempotency_key}:pool:{pool}:group:"

    @classmethod
    def _route_metadata(
        cls,
        physical_key: str,
        idempotency_key: str,
        pool: CognitivePool,
    ) -> tuple[bool, tuple[str, str] | None]:
        """Match and parse v2 routes without delimiter-prefix collisions.

        Legacy routes remain readable, but their logical component is recovered
        from the final pool marker and must equal the requested key exactly.
        This prevents a legacy key containing ``:pool:`` from masquerading as a
        shorter logical key.
        """
        prefix = cls._route_prefix(idempotency_key, pool)
        if physical_key.startswith(prefix):
            return True, cls._parse_route_suffix(physical_key[len(prefix) :])
        legacy_prefix = cls._legacy_route_prefix(idempotency_key, pool)
        if not physical_key.startswith(legacy_prefix):
            return False, None
        marker = f":pool:{pool}:group:"
        marker_index = physical_key.rfind(marker)
        if (
            marker_index < 0
            or physical_key[:marker_index] != idempotency_key
            or physical_key.find(marker) != marker_index
        ):
            # The row passed the broad legacy prefix query but its delimiter
            # structure is not an exact legacy route for this logical key.
            # Return a *matched malformed* result so the caller quarantines it
            # instead of silently failing over to another provider route.
            return True, None
        return True, cls._parse_route_suffix(physical_key[marker_index + len(marker) :])

    @staticmethod
    def _parse_route_suffix(remainder: str) -> tuple[str, str] | None:
        group_id, marker, remainder = remainder.partition(":key:")
        if not marker or not group_id:
            return None
        key_id, marker, selection = remainder.rpartition(":selection:")
        if not marker or not key_id or not selection.isdigit():
            return None
        return group_id, key_id

    def _retry_due(self, pool: CognitivePool, purpose: str) -> bool:
        now = utc_now()
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT status, next_retry_at FROM waiting_cognitive_tasks "
                "WHERE subject_id = ? AND pool = ? AND purpose = ?",
                (self.subject_id, pool, purpose),
            ).fetchone()
        return row is None or row["status"] != "waiting" or row["next_retry_at"] <= now

    def _recover_existing(
        self,
        purpose: str,
        pool: CognitivePool,
        idempotency_key: str,
        output_type: type[OutputT],
    ) -> GatewayResult[OutputT] | None:
        prefix = self._route_prefix(idempotency_key, pool)
        legacy_prefix = self._legacy_route_prefix(idempotency_key, pool)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM model_calls WHERE subject_id = ? AND purpose = ? "
                "AND resource_pool = ? AND ("
                "substr(idempotency_key, 1, ?) = ? OR "
                "substr(idempotency_key, 1, ?) = ?) "
                "AND status = 'succeeded' ORDER BY completed_at DESC LIMIT 257",
                (
                    self.subject_id,
                    purpose,
                    pool,
                    len(prefix),
                    prefix,
                    len(legacy_prefix),
                    legacy_prefix,
                ),
            ).fetchall()
        matching = []
        for row in rows:
            matched, metadata = self._route_metadata(
                str(row["idempotency_key"]), idempotency_key, pool
            )
            if matched and metadata is not None:
                matching.append(row)
        if not matching:
            return None
        return ModelGateway._cached_result(self.ledger._call_from_row(matching[0]), output_type)

    async def test_resource(self, group_id: str, *, subject_id: str) -> dict[str, Any]:
        """Run one explicit, budgeted probe against exactly one resource.

        The probe is separate from routed cognition: it never silently fails
        over to another group, and the normal model ledger records its cost and
        provider outcome. It is only invoked by an operator action.
        """
        group = self.resources.get(group_id, subject_id=subject_id)
        if group.status != "active":
            raise ValueError("model resource must be active for testing")
        key = self.resources.select_key(group.group_id, subject_id=subject_id)
        if key is None:
            raise ProviderCallError(
                "model_resource_key_unavailable", retryable=False, outcome_unknown=False
            )
        # A probe is one deliberate billable attempt. It must not inherit a
        # resource's normal cognition retry policy and quietly spend several
        # attempts from a single operator click.
        gateway = self._gateway(group, key, max_attempts=1)
        started = time.monotonic()
        purpose = f"operator_model_test:{group.group_id}"
        idempotency_key = f"{purpose}:{new_id('probe')}"
        messages = (
            ModelMessage(
                role="system",
                content=(
                    "You are responding to a connectivity probe. Return only JSON "
                    '{"ok":true}; do not perform any action.'
                ),
            ),
            ModelMessage(role="user", content="Connectivity probe."),
        )
        try:
            result = await gateway.complete_structured(
                subject_id,
                purpose,
                messages,
                _ModelProbeOutput,
                idempotency_key=idempotency_key,
                max_output_tokens=64,
                temperature=0,
            )
        except ProviderCallError as error:
            self.resources.record_failure(
                key.key_id,
                getattr(error, "code", type(error).__name__),
                cooldown_seconds=self.cooldown_seconds,
                subject_id=subject_id,
            )
            raise
        except StructuredOutputError:
            # The provider was reachable and the ledger has a known terminal
            # failure. Keep the credential healthy; invalid structured output
            # is a provider/model contract issue, not proof that the API key is
            # bad.
            raise
        except (BudgetExhaustedError, ModelCallStateError):
            raise
        else:
            self.resources.record_success(key.key_id, subject_id=subject_id)
            return {
                "group_id": group.group_id,
                "model": group.model,
                "key_id": key.key_id,
                "status": "succeeded",
                "ok": result.output.ok,
                "cached": result.cached,
                "call_id": result.call_id,
                "latency_ms": int((time.monotonic() - started) * 1000),
            }
        finally:
            await self._close_gateway(gateway)

    def pool_status(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        pools: tuple[CognitivePool, CognitivePool] = ("economy", "deep")
        reset_at = (
            (datetime.now(UTC) + timedelta(days=1))
            .replace(hour=0, minute=0, second=0, microsecond=0)
            .isoformat()
        )
        for pool in pools:
            groups = self.resources.active(self.subject_id, pool)
            limits = self._aggregate_limits(pool)
            usage = self.ledger.budget_status(
                self.subject_id,
                limits,
                resource_pool=pool,
            )
            with self.database.connection() as connection:
                pending_waits = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM waiting_cognitive_tasks "
                        "WHERE subject_id = ? AND pool = ? AND status = 'waiting'",
                        (self.subject_id, pool),
                    ).fetchone()[0]
                )
            result[pool] = {
                "status": "healthy"
                if any(group.available_key_count > 0 for group in groups)
                else "unavailable",
                "group_count": len(groups),
                "key_count": sum(group.key_count for group in groups),
                "available_key_count": sum(group.available_key_count for group in groups),
                "today_attempts": usage.attempts,
                "today_input_tokens": usage.input_tokens,
                "today_output_tokens": usage.output_tokens,
                "today_cost_microusd": usage.cost_microusd,
                "pressure": usage.pressure,
                "pending_wait_count": pending_waits,
                "budget_reset_at_utc": reset_at,
            }
        return result

    async def aclose(self) -> None:
        if self.legacy_gateway is not None:
            close = getattr(self.legacy_gateway.provider, "aclose", None)
            if close is not None:
                await close()

    def _gateway(
        self,
        group: CognitiveResourceGroupRecord,
        key: CognitiveResourceKeyRecord,
        *,
        max_attempts: int | None = None,
    ) -> ModelGateway:
        settings = OpenAICompatibleSettings(
            base_url=group.base_url,
            model=group.model,
            api_key=SecretStr(self.resources.api_key(key.key_id, subject_id=self.subject_id)),
        )
        provider = self.provider_factory(settings)
        return ModelGateway(
            provider,
            self.ledger,
            model=group.model,
            limits=self._limits_for_group(group),
            pricing=ModelPricing(
                group.input_microusd_per_million,
                group.output_microusd_per_million,
            ),
            retry_policy=RetryPolicy(max_attempts=max_attempts or group.max_attempts),
            resource_pool=group.pool,
            resource_group_id=group.group_id,
            pool_limits=self._aggregate_limits(group.pool),
            capture_model_io=self.capture_model_io,
            capture_model_io_getter=self.capture_model_io_getter,
            enforce_training_policy=self.enforce_training_policy,
        )

    def _weighted_group_order(
        self, groups: Sequence[CognitiveResourceGroupRecord]
    ) -> builtins.list[CognitiveResourceGroupRecord]:
        """Choose the least-used weighted share within each priority tier."""
        if not groups:
            return []
        group_ids = [group.group_id for group in groups]
        placeholders = ",".join("?" for _ in group_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT group_id, COUNT(*) AS selections FROM cognitive_route_attempts "
                f"WHERE subject_id = ? AND group_id IN ({placeholders}) "
                "AND outcome = 'selected' GROUP BY group_id",
                (self.subject_id, *group_ids),
            ).fetchall()
        selections = {str(row["group_id"]): int(row["selections"]) for row in rows}
        return sorted(
            groups,
            key=lambda group: (
                group.priority,
                selections.get(group.group_id, 0) / group.weight,
                group.label,
            ),
        )

    async def _close_gateway(self, gateway: ModelGateway) -> None:
        close = getattr(gateway.provider, "aclose", None)
        if close is not None:
            await close()

    def _record_decision(
        self,
        purpose: str,
        pool: str,
        *,
        selected_route: str,
        group_id: str | None,
        key_id: str | None,
        reason_code: str,
    ) -> CognitiveRouteDecisionRecord:
        now = utc_now()
        decision_id = new_id("croute")
        payload = {
            "decision_id": decision_id,
            "subject_id": self.subject_id,
            "purpose": purpose,
            "task_kind": purpose.split(":", 1)[0],
            "selected_route": selected_route,
            "pool": pool,
            "group_id": group_id,
            "key_id": key_id,
            "importance": 0.5,
            "risk": 0.3 if pool == "economy" else 0.8,
            "ambiguity": 0.4,
            "reason_code": reason_code,
            "created_at": now,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO cognitive_route_decisions(
                    decision_id, subject_id, purpose, task_kind, selected_route, pool,
                    group_id, key_id, importance, risk, ambiguity, reason_code, state_hash,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    decision_id,
                    self.subject_id,
                    purpose,
                    payload["task_kind"],
                    selected_route,
                    pool,
                    group_id,
                    key_id,
                    payload["importance"],
                    payload["risk"],
                    payload["ambiguity"],
                    reason_code,
                    content_hash(payload),
                    now,
                ),
            )
        return CognitiveRouteDecisionRecord(
            decision_id, selected_route, pool, group_id, key_id, reason_code, now
        )

    def _assign_decision(
        self, decision: CognitiveRouteDecisionRecord, group_id: str, key_id: str
    ) -> CognitiveRouteDecisionRecord:
        # The immutable decision is the pool choice. Concrete attempts are recorded separately.
        return CognitiveRouteDecisionRecord(
            decision.decision_id,
            decision.selected_route,
            decision.pool,
            group_id,
            key_id,
            decision.reason_code,
            decision.created_at,
        )

    def _record_attempt(
        self,
        decision_id: str,
        group_id: str,
        key_id: str,
        attempt_number: int,
        outcome: str,
        reason_code: str,
        latency_ms: int,
    ) -> None:
        now = utc_now()
        payload = {
            "subject_id": self.subject_id,
            "decision_id": decision_id,
            "group_id": group_id,
            "key_id": key_id,
            "attempt_number": attempt_number,
            "outcome": outcome,
            "reason_code": reason_code,
            "latency_ms": latency_ms,
            "created_at": now,
        }
        with self.database.transaction() as connection:
            connection.execute(
                """INSERT INTO cognitive_route_attempts(
                    attempt_id, subject_id, decision_id, group_id, key_id, attempt_number,
                    outcome, reason_code, latency_ms, state_hash, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("crouteattempt"),
                    self.subject_id,
                    decision_id,
                    group_id,
                    key_id,
                    attempt_number,
                    outcome,
                    reason_code,
                    latency_ms,
                    content_hash(payload),
                    now,
                ),
            )

    def _finish_decision(
        self,
        decision_id: str,
        outcome: str,
        reason_code: str,
        *,
        result: GatewayResult[Any] | None = None,
        latency_ms: int = 0,
    ) -> None:
        now = utc_now()
        input_tokens = 0 if result is None else result.usage.input_tokens
        output_tokens = 0 if result is None else result.usage.output_tokens
        cost = 0 if result is None else result.cost_microusd
        payload = {
            "subject_id": self.subject_id,
            "decision_id": decision_id,
            "outcome": outcome,
            "result_changed_state": False,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cost_microusd": cost,
            "latency_ms": latency_ms,
            "reason_code": reason_code,
            "created_at": now,
        }
        with self.database.transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM cognitive_route_outcomes WHERE decision_id = ?", (decision_id,)
            ).fetchone()
            if exists is not None:
                return
            connection.execute(
                """INSERT INTO cognitive_route_outcomes(
                    outcome_id, subject_id, decision_id, outcome, result_changed_state,
                    input_tokens, output_tokens, cost_microusd, latency_ms, reason_code,
                    state_hash, created_at
                ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    new_id("crouteout"),
                    self.subject_id,
                    decision_id,
                    outcome,
                    input_tokens,
                    output_tokens,
                    cost,
                    latency_ms,
                    reason_code,
                    content_hash(payload),
                    now,
                ),
            )

    def _record_unavailable(
        self, pool: str, purpose: str, reason_code: str, *, force: bool = False
    ) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM waiting_cognitive_tasks WHERE subject_id = ? AND pool = ? "
                "AND purpose = ? AND status = 'waiting'",
                (self.subject_id, pool, purpose),
            ).fetchone()
            if row is not None and row["reason_code"] == reason_code and row["status"] == "waiting":
                return
            if row is not None and row["next_retry_at"] > now and not force:
                return
            if row is None:
                row = connection.execute(
                    "SELECT * FROM waiting_cognitive_tasks WHERE subject_id = ? AND pool = ? "
                    "AND purpose = ?",
                    (self.subject_id, pool, purpose),
                ).fetchone()
            retry_count = 1 if row is None else int(row["retry_count"]) + 1
            delay = min(self.wait_max_seconds, self.wait_base_seconds * (2 ** (retry_count - 1)))
            next_retry = (datetime.now(UTC) + timedelta(seconds=delay)).isoformat(
                timespec="milliseconds"
            )
            task_id = new_id("cwait") if row is None else str(row["task_id"])
            payload = {
                "task_id": task_id,
                "subject_id": self.subject_id,
                "pool": pool,
                "purpose": purpose,
                "status": "waiting",
                "reason_code": reason_code,
                "retry_count": retry_count,
                "next_retry_at": next_retry,
                "first_waited_at": now if row is None else row["first_waited_at"],
                "updated_at": now,
            }
            if row is None:
                connection.execute(
                    """INSERT INTO waiting_cognitive_tasks(
                        task_id, subject_id, pool, purpose, status, reason_code,
                        retry_count, next_retry_at, first_waited_at, updated_at, state_hash
                    ) VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id,
                        self.subject_id,
                        pool,
                        purpose,
                        reason_code,
                        retry_count,
                        next_retry,
                        now,
                        now,
                        content_hash(payload),
                    ),
                )
            else:
                connection.execute(
                    "UPDATE waiting_cognitive_tasks SET status = 'waiting', reason_code = ?, "
                    "retry_count = ?, next_retry_at = ?, updated_at = ?, state_hash = ? "
                    "WHERE task_id = ?",
                    (
                        reason_code,
                        retry_count,
                        next_retry,
                        now,
                        content_hash(payload),
                        task_id,
                    ),
                )
            self._insert_wait_revision(
                connection,
                task_id,
                "waiting",
                reason_code,
                retry_count,
                next_retry,
                now,
            )

    def _resolve_wait(self, pool: str, purpose: str) -> None:
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM waiting_cognitive_tasks WHERE subject_id = ? AND pool = ? "
                "AND purpose = ? AND status = 'waiting'",
                (self.subject_id, pool, purpose),
            ).fetchone()
            if row is None:
                return
            payload = {
                "task_id": row["task_id"],
                "subject_id": self.subject_id,
                "pool": pool,
                "purpose": purpose,
                "status": "resolved",
                "reason_code": "resource_recovered",
                "retry_count": int(row["retry_count"]),
                "next_retry_at": row["next_retry_at"],
                "first_waited_at": row["first_waited_at"],
                "updated_at": now,
            }
            connection.execute(
                "UPDATE waiting_cognitive_tasks SET status = 'resolved', "
                "reason_code = 'resource_recovered', updated_at = ?, state_hash = ? "
                "WHERE task_id = ?",
                (now, content_hash(payload), row["task_id"]),
            )
            self._insert_wait_revision(
                connection,
                row["task_id"],
                "resolved",
                "resource_recovered",
                int(row["retry_count"]),
                row["next_retry_at"],
                now,
            )

    @staticmethod
    def _insert_wait_revision(
        connection: Any,
        task_id: str,
        status: str,
        reason_code: str,
        retry_count: int,
        next_retry_at: str,
        now: str,
    ) -> None:
        payload = {
            "task_id": task_id,
            "status": status,
            "reason_code": reason_code,
            "retry_count": retry_count,
            "next_retry_at": next_retry_at,
            "created_at": now,
        }
        connection.execute(
            """INSERT INTO waiting_cognitive_task_revisions(
                revision_id, task_id, status, reason_code, retry_count, next_retry_at,
                state_hash, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                new_id("cwaitrev"),
                task_id,
                status,
                reason_code,
                retry_count,
                next_retry_at,
                content_hash(payload),
                now,
            ),
        )

    def _aggregate_limits(self, pool: str) -> BudgetLimits:
        if pool not in {"economy", "deep"}:
            raise ValueError("cognitive pool is invalid")
        typed_pool = cast(CognitivePool, pool)
        groups = self.resources.active(self.subject_id, typed_pool) if self.resources else []
        if not groups and self.legacy_gateway is not None:
            return self.legacy_gateway.limits
        return BudgetLimits(
            sum(group.daily_attempts for group in groups),
            sum(group.daily_input_tokens for group in groups),
            sum(group.daily_output_tokens for group in groups),
            sum(group.daily_cost_microusd for group in groups),
        )

    @staticmethod
    def _limits_for_group(group: CognitiveResourceGroupRecord) -> BudgetLimits:
        """Return the hard budget for one provider group.

        Group limits are intentionally independent from the pool aggregate.  The
        routed gateway checks both limits before authorizing an attempt.
        """
        return BudgetLimits(
            group.daily_attempts,
            group.daily_input_tokens,
            group.daily_output_tokens,
            group.daily_cost_microusd,
        )


class _NoopProvider:
    name = "routed"

    async def complete(self, request: Any) -> Any:
        raise ProviderCallError(
            "routed_gateway_provider_is_not_directly_callable",
            retryable=False,
            outcome_unknown=False,
        )

    async def aclose(self) -> None:
        return None


def resource_groups_from_env(pool: CognitivePool) -> tuple[CognitiveResourceGroupInput, ...]:
    raw = os.getenv(f"NOYRA_{pool.upper()}_MODEL_GROUPS_JSON", "[]")
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"invalid {pool} model groups JSON") from error
    if not isinstance(payload, list):
        raise ConfigurationError(f"{pool} model groups must be a JSON array")
    groups: list[CognitiveResourceGroupInput] = []
    try:
        for item in payload:
            if not isinstance(item, dict):
                raise ValueError("model group must be an object")
            normalized = dict(item)
            normalized["pool"] = pool
            raw_keys = normalized.get("api_keys", [])
            if not isinstance(raw_keys, list):
                raise ValueError("model group api_keys must be a JSON array")
            if any(type(value) is not str for value in raw_keys):
                raise ValueError("model group api_keys must contain only strings")
            normalized["api_keys"] = tuple(SecretStr(value) for value in raw_keys)
            groups.append(CognitiveResourceGroupInput.model_validate(normalized))
    except (TypeError, ValueError) as error:
        raise ConfigurationError(f"invalid {pool} model group configuration") from error
    return tuple(groups)
