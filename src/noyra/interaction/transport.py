from __future__ import annotations

import asyncio
import builtins
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import smtplib
import ssl
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from noyra.core.admission import OperationInvalidated, accounting_scope, assert_current_lease
from noyra.core.at_rest import validate_private_file, validate_private_root
from noyra.core.database import Database
from noyra.core.errors import IntegrityError, InvalidTransitionError, NotFoundError
from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    HTTPResponseLimitError,
    PublicDNSAsyncHTTPTransport,
    _PublicDNSAsyncBackend,
    read_bounded_response,
    validate_response_headers,
)
from noyra.core.secret_cleanup import SecretCleanupQueue
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .outbound import OUTBOUND_ADAPTERS

TransportChannel = Literal["telegram", "wechat", "qq", "feishu", "email", "webhook"]
DeliveryReconciliationOutcome = Literal[
    "delivered", "failed", "cancelled", "unknown", "unsupported", "error"
]
TRANSPORT_MAX_RESPONSE_BYTES = DEFAULT_MAX_RESPONSE_BYTES
TRANSPORT_TOTAL_TIMEOUT_SECONDS = 20.0
TRANSPORT_SECRET_MAX_BYTES = 16_000_000
DELIVERY_EVIDENCE_MAX_BYTES = 16_384
SMTP_TIMEOUT_SECONDS = 20.0
PROVIDER_TOKEN_SKEW_SECONDS = 60.0
LOGGER = logging.getLogger("noyra.interaction.transport")


class DeliveryOutcomeUnknown(RuntimeError):
    def __init__(self, message: str, *, provider_message_id: str | None = None):
        super().__init__(message)
        self.provider_message_id = provider_message_id


class _PublicDNSBackend(_PublicDNSAsyncBackend):
    """Compatibility alias for the shared public-DNS backend."""

    @staticmethod
    def _public_addresses(host: str, port: int) -> list[str]:
        from noyra.core.http import public_addresses

        try:
            return public_addresses(host, port)
        except OSError as error:
            message = str(error).replace("HTTP endpoint", "transport endpoint")
            raise OSError(message) from error


class TransportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    channel: TransportChannel
    label: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2_000)
    settings: dict[str, str | int | bool] = Field(default_factory=dict)
    credentials: dict[str, SecretStr] = Field(default_factory=dict)

    @field_validator("label")
    @classmethod
    def validate_label(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("transport label cannot be blank")
        return value.strip()

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = cls.normalize_endpoint(value)
        parsed = urlsplit(value)
        if parsed.scheme not in {"https", "smtp", "smtps"} or not parsed.hostname:
            raise ValueError("transport endpoint must use https, smtp or smtps")
        if parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("transport endpoint cannot target localhost")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("transport endpoint must use a public address")
        return value

    @staticmethod
    def normalize_endpoint(value: str) -> str:
        parsed = urlsplit(value.strip())
        if (
            parsed.scheme not in {"https", "smtp", "smtps"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise ValueError("transport endpoint must be an absolute URL without credentials")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("transport endpoint port is invalid") from error
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("transport endpoint port is invalid")
        host = parsed.hostname.casefold().rstrip(".")
        if ":" in host:
            host = f"[{host}]"
        netloc = host
        if port is not None and not (
            (parsed.scheme == "https" and port == 443)
            or (parsed.scheme == "smtp" and port == 25)
            or (parsed.scheme == "smtps" and port == 465)
        ):
            netloc += f":{port}"
        path = parsed.path
        if path == "/":
            path = ""
        return urlunsplit((parsed.scheme.casefold(), netloc, path, parsed.query, ""))

    @model_validator(mode="after")
    def validate_channel_endpoint(self) -> TransportInput:
        parsed = urlsplit(self.endpoint)
        scheme = parsed.scheme
        if self.channel == "email" and scheme not in {"smtp", "smtps"}:
            raise ValueError("email transport requires smtp or smtps")
        if self.channel != "email" and scheme != "https":
            raise ValueError("messaging transports require https")
        official_hosts = {
            "telegram": {"api.telegram.org"},
            "qq": {"api.sgroup.qq.com"},
            "wechat": {"api.weixin.qq.com"},
            "feishu": {"open.feishu.cn", "open.larksuite.com"},
        }
        allowed = official_hosts.get(self.channel)
        hostname = parsed.hostname or ""
        if allowed is not None and hostname.casefold().rstrip(".") not in allowed:
            raise ValueError(f"{self.channel} transport endpoint must use an official API host")
        return self


@dataclass(frozen=True)
class TransportRecord:
    transport_id: str
    subject_id: str
    channel: str
    label: str
    endpoint: str
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DeliveryRecord:
    delivery_id: str
    interaction_id: str
    subject_id: str
    transport_id: str
    idempotency_key: str
    status: str
    provider_message_id: str | None
    attempts: int
    last_error: str | None
    next_retry_at: str | None
    created_at: str
    updated_at: str
    original_status: str | None = None
    reconciliation_id: str | None = None
    reconciliation_source: str | None = None
    reconciliation_reason: str | None = None
    reconciled_at: str | None = None


@dataclass(frozen=True)
class ProviderStatusEvidence:
    outcome: DeliveryReconciliationOutcome
    provider_status: str
    provider_message_id: str | None = None
    details: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class DeliveryReconciliationRecord:
    reconciliation_id: str
    delivery_id: str
    subject_id: str
    sequence: int
    source: str
    outcome: str
    provider_message_id: str | None
    provider_status: str | None
    evidence: Mapping[str, Any]
    evidence_hash: str
    actor: str
    reason: str
    state_hash: str
    created_at: str


DeliveryStatusLookup = Callable[
    [DeliveryRecord, TransportRecord, Mapping[str, Any], Mapping[str, Any]],
    Awaitable[ProviderStatusEvidence],
]


class TransportStore:
    """Operator-managed channel configuration with secrets outside SQLite."""

    def __init__(self, database: Database, secret_dir: Path | str, *, repair_on_init: bool = True):
        self.database = database
        self.secret_dir = validate_private_root(
            secret_dir, create=True, label="transport secret root"
        )
        self.secret_cleanup = SecretCleanupQueue(database)
        if repair_on_init:
            self.secret_cleanup.repair(None, "transport", self.secret_dir)
            self._upgrade_legacy_endpoints()

    def configure(
        self, subject_id: str, proposal: TransportInput, *, actor: str
    ) -> TransportRecord:
        if not actor.strip() or actor == "subject":
            raise PermissionError("only an operator can configure transports")
        transport_id = new_id("transport")
        now = utc_now()
        secret_reference = f"{transport_id}.json"
        payload = {
            "endpoint": TransportInput.normalize_endpoint(proposal.endpoint),
            "credentials": {
                key: value.get_secret_value() for key, value in proposal.credentials.items()
            },
        }
        intent_id: str | None = None
        try:
            intent_id = self.secret_cleanup.prepare_create(
                subject_id,
                "transport",
                transport_id,
                secret_reference,
            )
            self._write_secret(secret_reference, canonical_json(payload))
            self.secret_cleanup.mark_file_ready(intent_id)
            private_endpoint = TransportInput.normalize_endpoint(proposal.endpoint)
            endpoint = self._endpoint_origin(private_endpoint)
            endpoint_digest = self._endpoint_digest(private_endpoint)
            state_hash = content_hash(
                {
                    "transport_id": transport_id,
                    "subject_id": subject_id,
                    "channel": proposal.channel,
                    "label": proposal.label,
                    "endpoint": endpoint,
                    "endpoint_contract": "digest_v1",
                    "endpoint_digest": endpoint_digest,
                    "secret_reference": secret_reference,
                    "config": proposal.settings,
                    "status": "active",
                }
            )
        except Exception:
            self._abort_secret_create(intent_id, secret_reference)
            raise
        try:
            with self.database.transaction() as connection:
                connection.execute(
                    "INSERT INTO interaction_transports(transport_id, subject_id, channel, label, "
                    "endpoint, endpoint_contract, endpoint_digest, secret_reference, config_json, "
                    "status, state_hash, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'digest_v1', ?, ?, ?, 'active', ?, ?, ?)",
                    (
                        transport_id,
                        subject_id,
                        proposal.channel,
                        proposal.label,
                        endpoint,
                        endpoint_digest,
                        secret_reference,
                        canonical_json(proposal.settings),
                        state_hash,
                        now,
                        now,
                    ),
                )
        except Exception:
            self._abort_secret_create(intent_id, secret_reference)
            raise
        self.secret_cleanup.mark_committed(intent_id)
        return self.get(transport_id, subject_id=subject_id)

    def _abort_secret_create(self, intent_id: str | None, reference: str) -> None:
        """Remove a partially published transport secret and close its intent."""
        cleanup_error: BaseException | None = None
        try:
            self._secret_path(reference).unlink(missing_ok=True)
            self._secret_path(f".{reference}.tmp").unlink(missing_ok=True)
        except BaseException as caught:  # cleanup must never hide the original failure
            cleanup_error = caught
        if intent_id is None:
            return
        try:
            if cleanup_error is None:
                self.secret_cleanup.mark_removed(intent_id)
            else:
                self.secret_cleanup.mark(intent_id, "failed", error=cleanup_error)
        except Exception:
            # The durable intent remains for startup reconciliation.
            pass

    def list(
        self, subject_id: str, *, channel: str | None = None
    ) -> builtins.list[TransportRecord]:
        with self.database.connection() as connection:
            if channel is None:
                rows = connection.execute(
                    "SELECT * FROM interaction_transports WHERE subject_id = ? "
                    "ORDER BY channel, label",
                    (subject_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM interaction_transports WHERE subject_id = ? AND channel = ? "
                    "ORDER BY label",
                    (subject_id, channel),
                ).fetchall()
        return [self._record(row) for row in rows]

    def active_for(self, subject_id: str, channel: str) -> builtins.list[TransportRecord]:
        return [
            record for record in self.list(subject_id, channel=channel) if record.status == "active"
        ]

    def get(self, transport_id: str, *, subject_id: str) -> TransportRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_transports WHERE transport_id = ? AND subject_id = ?",
                (transport_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"transport not found: {transport_id}")
        return self._record(row)

    def settings(self, transport_id: str, *, subject_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT transport_id, config_json FROM interaction_transports "
                "WHERE transport_id = ? AND subject_id = ?",
                (transport_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"transport not found: {transport_id}")
        return self._config(row)

    def disable(
        self, transport_id: str, *, reason: str, actor: str, subject_id: str
    ) -> TransportRecord:
        return self._set_status(
            transport_id, "disabled", reason=reason, actor=actor, subject_id=subject_id
        )

    def enable(
        self, transport_id: str, *, reason: str, actor: str, subject_id: str
    ) -> TransportRecord:
        return self._set_status(
            transport_id, "active", reason=reason, actor=actor, subject_id=subject_id
        )

    def revoke(
        self, transport_id: str, *, reason: str, actor: str, subject_id: str
    ) -> TransportRecord:
        record = self._set_status(
            transport_id, "revoked", reason=reason, actor=actor, subject_id=subject_id
        )
        with self.database.connection() as connection:
            reference = connection.execute(
                "SELECT secret_reference FROM interaction_transports "
                "WHERE transport_id = ? AND subject_id = ?",
                (transport_id, subject_id),
            ).fetchone()[0]
        if reference:
            try:
                self._secret_path(str(reference)).unlink(missing_ok=True)
                self.secret_cleanup.complete_delete(
                    str(record.subject_id), "transport", str(transport_id), str(reference)
                )
            except OSError as error:
                self.secret_cleanup.record_delete_failure(
                    str(record.subject_id), "transport", str(transport_id), str(reference), error
                )
        return record

    def secret(self, transport_id: str, *, subject_id: str) -> dict[str, Any]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT secret_reference, status, channel, endpoint, endpoint_contract, "
                "endpoint_digest "
                "FROM interaction_transports "
                "WHERE transport_id = ? AND subject_id = ?",
                (transport_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"transport not found: {transport_id}")
        if row["status"] == "revoked":
            raise PermissionError("transport secret is unavailable")
        reference = row["secret_reference"]
        if not isinstance(reference, str) or reference != f"{transport_id}.json":
            raise IntegrityError("transport secret reference is invalid")
        try:
            payload = json.loads(self._secret_path(reference).read_text())
        except (OSError, ValueError) as error:
            raise IntegrityError("transport secret is missing or invalid") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("credentials"), dict):
            raise IntegrityError("transport secret payload is invalid")
        endpoint = payload.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise IntegrityError("transport secret endpoint is missing")
        try:
            normalized_endpoint = TransportInput.normalize_endpoint(endpoint)
            TransportInput(
                channel=cast(TransportChannel, str(row["channel"])),
                label="delivery endpoint validation",
                endpoint=normalized_endpoint,
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError("transport secret endpoint is invalid") from error
        if not self._endpoint_matches_durable(
            str(row["endpoint"]),
            normalized_endpoint,
            str(row["endpoint_contract"]),
            row["endpoint_digest"],
        ):
            raise IntegrityError("transport secret endpoint does not match transport")
        return payload

    def _upgrade_legacy_endpoints(self, subject_id: str | None = None) -> int:
        """Bind pre-v50 private endpoints into the durable transport hash.

        Legacy releases intentionally hashed only the endpoint origin in
        SQLite while dispatch used the complete endpoint from the protected
        secret file.  Schema 50 marks those rows explicitly.  Upgrade only a
        row whose old state hash is valid and whose private endpoint has exactly
        the historical durable origin, then atomically bind a digest of the
        complete endpoint without copying webhook keys or query secrets into SQLite.
        A historically valid endpoint rejected by the current provider policy
        is migrated as terminally revoked and its secret is crash-safely removed.
        """
        with self.database.connection() as connection:
            if subject_id is None:
                rows = connection.execute(
                    "SELECT * FROM interaction_transports "
                    "WHERE endpoint_contract = 'legacy_origin' "
                    "ORDER BY subject_id, transport_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM interaction_transports WHERE subject_id = ? "
                    "AND endpoint_contract = 'legacy_origin' ORDER BY transport_id",
                    (subject_id,),
                ).fetchall()
        upgraded = 0
        for row in rows:
            transport_id = str(row["transport_id"])
            channel = str(row["channel"])
            # Verify the legacy durable state before consulting the formerly
            # authoritative private endpoint.
            self._record(row)
            reference = row["secret_reference"]
            if not isinstance(reference, str) or reference != f"{transport_id}.json":
                raise IntegrityError(
                    f"legacy transport secret reference is invalid: {transport_id} ({channel})"
                )
            quarantine = False
            legacy_private_endpoint: str | None = None
            if row["status"] == "revoked":
                # Revocation has already removed the private file and is
                # terminal.  Bind a digest of the remaining durable origin so
                # no un-hashed legacy marker survives.
                try:
                    normalized_endpoint = TransportInput.normalize_endpoint(str(row["endpoint"]))
                except ValueError as error:
                    raise IntegrityError(
                        f"revoked legacy transport endpoint is invalid: {transport_id} ({channel})"
                    ) from error
            else:
                try:
                    payload = self._read_legacy_secret_payload(reference)
                except (OSError, UnicodeError, TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"legacy transport secret is missing or invalid: {transport_id} ({channel})"
                    ) from error
                if not isinstance(payload, dict) or not isinstance(
                    payload.get("credentials"), dict
                ):
                    raise IntegrityError(
                        f"legacy transport secret payload is invalid: {transport_id} ({channel})"
                    )
                if any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in payload["credentials"].items()
                ):
                    raise IntegrityError(
                        f"legacy transport secret credentials are invalid: "
                        f"{transport_id} ({channel})"
                    )
                endpoint = payload.get("endpoint")
                if not isinstance(endpoint, str):
                    raise IntegrityError(
                        f"legacy transport secret endpoint is missing: {transport_id} ({channel})"
                    )
                legacy_private_endpoint = endpoint
                try:
                    legacy_origin = self._legacy_endpoint_origin(endpoint, channel)
                except (TypeError, ValueError) as error:
                    raise IntegrityError(
                        f"legacy transport secret endpoint is invalid: {transport_id} ({channel})"
                    ) from error
                if not isinstance(row["endpoint"], str) or legacy_origin != row["endpoint"]:
                    raise IntegrityError(
                        f"legacy transport secret endpoint origin mismatch: "
                        f"{transport_id} ({channel})"
                    )
                try:
                    normalized_endpoint = TransportInput.normalize_endpoint(endpoint)
                    # Re-apply the current channel host/scheme contract before
                    # any legacy provider credential can be used again.
                    TransportInput(
                        channel=cast(TransportChannel, str(row["channel"])),
                        label="legacy endpoint validation",
                        endpoint=normalized_endpoint,
                    )
                except (TypeError, ValueError):
                    quarantine = True
                else:
                    quarantine = not self._endpoint_matches_durable(
                        str(row["endpoint"]), normalized_endpoint, "legacy_origin", None
                    )
            config = self._config(row)
            if quarantine:
                assert legacy_private_endpoint is not None
                endpoint_origin = str(row["endpoint"])
                endpoint_digest = self._endpoint_digest(legacy_private_endpoint)
                status = "revoked"
                updated_at = utc_now()
            else:
                endpoint_origin = self._endpoint_origin(normalized_endpoint)
                endpoint_digest = self._endpoint_digest(normalized_endpoint)
                status = str(row["status"])
                updated_at = str(row["updated_at"])
            state_hash = content_hash(
                {
                    "transport_id": transport_id,
                    "subject_id": row["subject_id"],
                    "channel": channel,
                    "label": row["label"],
                    "endpoint": endpoint_origin,
                    "endpoint_contract": "digest_v1",
                    "endpoint_digest": endpoint_digest,
                    "secret_reference": reference,
                    "config": config,
                    "status": status,
                }
            )
            with self.database.transaction() as connection:
                current = connection.execute(
                    "SELECT * FROM interaction_transports WHERE transport_id = ?",
                    (transport_id,),
                ).fetchone()
                if current is None or any(
                    current[key] != row[key]
                    for key in row.keys()  # noqa: SIM118 - sqlite3.Row iteration yields values
                ):
                    raise IntegrityError(
                        f"legacy transport changed during endpoint upgrade: "
                        f"{transport_id} ({channel})"
                    )
                # BEGIN IMMEDIATE now fences concurrent SQLite writers.  Re-run
                # the historical hash/contract checks on this locked row before
                # changing its status or publishing a delete intent.
                self._record(current)
                changed = connection.execute(
                    "UPDATE interaction_transports SET endpoint = ?, "
                    "endpoint_contract = 'digest_v1', endpoint_digest = ?, status = ?, "
                    "state_hash = ?, updated_at = ? "
                    "WHERE transport_id = ? AND endpoint = ? AND state_hash = ? "
                    "AND secret_reference = ? AND status = ? "
                    "AND endpoint_contract = 'legacy_origin'",
                    (
                        endpoint_origin,
                        endpoint_digest,
                        status,
                        state_hash,
                        updated_at,
                        transport_id,
                        row["endpoint"],
                        row["state_hash"],
                        reference,
                        row["status"],
                    ),
                ).rowcount
                if changed != 1:
                    raise IntegrityError(
                        f"legacy transport changed during endpoint upgrade: "
                        f"{transport_id} ({channel})"
                    )
                if quarantine:
                    self.secret_cleanup.prepare_delete(
                        str(row["subject_id"]),
                        "transport",
                        transport_id,
                        reference,
                        connection=connection,
                    )
            if quarantine:
                LOGGER.warning(
                    "legacy transport %s (%s) was revoked because its endpoint no longer "
                    "satisfies current transport policy; reconfigure this transport with a "
                    "current endpoint before delivery can resume",
                    transport_id,
                    channel,
                )
                try:
                    self._secret_path(reference).unlink(missing_ok=True)
                    self.secret_cleanup.complete_delete(
                        str(row["subject_id"]), "transport", transport_id, reference
                    )
                except OSError as error:
                    self.secret_cleanup.record_delete_failure(
                        str(row["subject_id"]),
                        "transport",
                        transport_id,
                        reference,
                        error,
                    )
            upgraded += 1
        return upgraded

    def delivery_counterparty(self, transport_id: str, interaction: Any, *, subject_id: str) -> str:
        """Resolve a configured recipient for a web-originated help request."""
        counterparty = str(interaction["counterparty"])
        if interaction["channel"] != "web" or interaction["kind"] != "help_request":
            return counterparty
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT transport_id, channel, config_json FROM interaction_transports "
                "WHERE transport_id = ? AND subject_id = ?",
                (transport_id, subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"transport not found: {transport_id}")
        config = self._config(row)
        for key in ("recipient", "chat_id", "target", "to"):
            value = config.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip()
        if row["channel"] in {"telegram", "email"}:
            raise ValueError(f"{row['channel']} help-request transport requires recipient settings")
        return counterparty

    def verify_integrity(self, subject_id: str) -> dict[str, int]:
        records = self.list(subject_id)
        for transport_record in records:
            if transport_record.status != "revoked":
                self.secret(transport_record.transport_id, subject_id=subject_id)
        with self.database.connection() as connection:
            deliveries = connection.execute(
                "SELECT d.delivery_id FROM interaction_deliveries d "
                "JOIN interactions i ON i.interaction_id = d.interaction_id "
                "JOIN interaction_transports t ON t.transport_id = d.transport_id "
                "WHERE d.subject_id = ? AND (i.subject_id != d.subject_id "
                "OR t.subject_id != d.subject_id)",
                (subject_id,),
            ).fetchall()
            count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM interaction_deliveries WHERE subject_id = ?",
                    (subject_id,),
                ).fetchone()[0]
            )
            reconciliation_rows = connection.execute(
                "SELECT r.*, d.status AS delivery_status, d.subject_id AS delivery_subject_id "
                "FROM interaction_delivery_reconciliations r "
                "LEFT JOIN interaction_deliveries d ON d.delivery_id = r.delivery_id "
                "WHERE r.subject_id = ? OR d.subject_id = ? "
                "ORDER BY r.delivery_id, r.sequence",
                (subject_id, subject_id),
            ).fetchall()
        if deliveries:
            raise IntegrityError("transport delivery crosses subject boundary")
        sequences: dict[str, int] = {}
        terminal: set[str] = set()
        for row in reconciliation_rows:
            reconciliation_record = DeliveryDispatcher._reconciliation_record(row)
            if (
                reconciliation_record.subject_id != subject_id
                or row["delivery_subject_id"] != subject_id
                or row["delivery_status"] != "unknown"
            ):
                raise IntegrityError("delivery reconciliation ownership or baseline is invalid")
            expected_sequence = sequences.get(reconciliation_record.delivery_id, 0) + 1
            if (
                reconciliation_record.sequence != expected_sequence
                or reconciliation_record.delivery_id in terminal
            ):
                raise IntegrityError("delivery reconciliation sequence is invalid")
            sequences[reconciliation_record.delivery_id] = reconciliation_record.sequence
            if reconciliation_record.outcome in {"delivered", "failed", "cancelled"}:
                terminal.add(reconciliation_record.delivery_id)
        return {
            "interaction_transports": len(records),
            "interaction_deliveries": count,
            "interaction_delivery_reconciliations": len(reconciliation_rows),
        }

    def _set_status(
        self,
        transport_id: str,
        status: str,
        *,
        reason: str,
        actor: str,
        subject_id: str,
    ) -> TransportRecord:
        if (
            status not in {"active", "disabled", "revoked"}
            or not actor.strip()
            or actor == "subject"
        ):
            raise PermissionError("only an operator can change transport status")
        if not reason.strip():
            raise ValueError("transport status reason is required")
        now = utc_now()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_transports WHERE transport_id = ? AND subject_id = ?",
                (transport_id, subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"transport not found: {transport_id}")
            if row["status"] == "revoked" and status != "revoked":
                # Revocation is terminal: it schedules deletion of the
                # credential file, so re-enabling the same row could publish
                # an active transport whose secret no longer exists.  A new
                # configure operation is required to restore a channel.
                raise ValueError("revoked transports cannot be enabled or disabled")
            # Never let a status operation silently bless altered durable
            # transport state with a freshly computed hash.
            self._record(row)
            if row["endpoint_contract"] != "digest_v1":
                raise IntegrityError("transport endpoint upgrade is required")
            config = self._config(row)
            state = {
                "transport_id": transport_id,
                "subject_id": row["subject_id"],
                "channel": row["channel"],
                "label": row["label"],
                "endpoint": row["endpoint"],
                "endpoint_contract": "digest_v1",
                "endpoint_digest": row["endpoint_digest"],
                "secret_reference": row["secret_reference"],
                "config": config,
                "status": status,
            }
            state_hash = content_hash(state)
            connection.execute(
                "UPDATE interaction_transports SET status = ?, state_hash = ?, updated_at = ? "
                "WHERE transport_id = ? AND subject_id = ?",
                (status, state_hash, now, transport_id, subject_id),
            )
            if status == "revoked" and row["secret_reference"]:
                self.secret_cleanup.prepare_delete(
                    str(row["subject_id"]),
                    "transport",
                    str(transport_id),
                    str(row["secret_reference"]),
                    connection=connection,
                )
        return self.get(transport_id, subject_id=subject_id)

    @staticmethod
    def _endpoint_origin(endpoint: str) -> str:
        parsed = urlsplit(endpoint)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        port = f":{parsed.port}" if parsed.port else ""
        return f"{parsed.scheme}://{host}{port}"

    @staticmethod
    def _legacy_endpoint_origin(endpoint: Any, channel: Any) -> str:
        """Validate a schema-49 endpoint under its historical input contract."""
        if not isinstance(endpoint, str) or not 1 <= len(endpoint) <= 2_000:
            raise ValueError("legacy transport endpoint is invalid")
        if channel not in {"telegram", "wechat", "qq", "feishu", "email", "webhook"}:
            raise ValueError("legacy transport channel is invalid")
        parsed = urlsplit(endpoint)
        if parsed.scheme not in {"https", "smtp", "smtps"} or not parsed.hostname:
            raise ValueError("legacy transport endpoint scheme or host is invalid")
        if parsed.hostname.casefold() in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("legacy transport endpoint cannot target localhost")
        try:
            address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError("legacy transport endpoint must use a public address")
        if channel == "email" and parsed.scheme not in {"smtp", "smtps"}:
            raise ValueError("legacy email transport requires smtp or smtps")
        if channel != "email" and parsed.scheme != "https":
            raise ValueError("legacy messaging transport requires https")
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("legacy transport endpoint port is invalid") from error
        host = parsed.hostname
        if ":" in host:
            host = f"[{host}]"
        port_suffix = f":{port}" if port else ""
        return f"{parsed.scheme}://{host}{port_suffix}"

    @staticmethod
    def _endpoint_digest(endpoint: str) -> str:
        return content_hash({"endpoint": endpoint})

    @staticmethod
    def _endpoint_matches_durable(
        durable_endpoint: str,
        normalized_secret_endpoint: str,
        endpoint_contract: str,
        endpoint_digest: Any,
    ) -> bool:
        if endpoint_contract not in {"legacy_origin", "digest_v1"}:
            return False
        try:
            normalized_durable = TransportInput.normalize_endpoint(durable_endpoint)
        except ValueError:
            return False
        if normalized_durable != TransportStore._endpoint_origin(normalized_secret_endpoint):
            return False
        if endpoint_contract == "legacy_origin":
            return endpoint_digest is None
        if not isinstance(endpoint_digest, str) or len(endpoint_digest) != 64:
            return False
        return hmac.compare_digest(
            endpoint_digest,
            TransportStore._endpoint_digest(normalized_secret_endpoint),
        )

    def _secret_path(self, reference: str) -> Path:
        try:
            return validate_private_file(self.secret_dir, reference)
        except Exception as error:
            raise ValueError("transport secret reference is invalid") from error

    def _read_legacy_secret_payload(self, reference: str) -> Any:
        with self._secret_path(reference).open("rb") as stream:
            raw = stream.read(TRANSPORT_SECRET_MAX_BYTES + 1)
        if len(raw) > TRANSPORT_SECRET_MAX_BYTES:
            raise ValueError("legacy transport secret is too large")
        return strict_json_loads(raw.decode("utf-8"))

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
                        raise OSError("transport secret write made no progress")
                    payload = payload[written:]
                os.fsync(descriptor)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
        finally:
            os.close(descriptor)
        try:
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise

    @staticmethod
    def _record(row: Any) -> TransportRecord:
        config = TransportStore._config(row)
        endpoint_contract = row["endpoint_contract"]
        if endpoint_contract not in {"legacy_origin", "digest_v1"}:
            raise IntegrityError(f"transport endpoint contract is invalid: {row['transport_id']}")
        state = {
            "transport_id": row["transport_id"],
            "subject_id": row["subject_id"],
            "channel": row["channel"],
            "label": row["label"],
            "endpoint": row["endpoint"],
            "config": config,
            "status": row["status"],
        }
        # Schema-49 rows keep their historical hash until the guarded startup
        # upgrader binds the private endpoint digest.  Every new/digest row
        # covers the contract marker, endpoint digest and deterministic secret
        # reference without exposing a webhook key or query secret in SQLite.
        if endpoint_contract == "digest_v1":
            endpoint_digest = row["endpoint_digest"]
            if not isinstance(endpoint_digest, str) or len(endpoint_digest) != 64:
                raise IntegrityError(f"transport endpoint digest is invalid: {row['transport_id']}")
            secret_reference = row["secret_reference"]
            if not isinstance(secret_reference, str):
                raise IntegrityError(
                    f"transport secret reference is invalid: {row['transport_id']}"
                )
            state["endpoint_contract"] = "digest_v1"
            state["endpoint_digest"] = endpoint_digest
            state["secret_reference"] = secret_reference
        elif row["endpoint_digest"] is not None:
            raise IntegrityError(
                f"legacy transport endpoint digest is invalid: {row['transport_id']}"
            )
        expected = content_hash(state)
        if expected != row["state_hash"]:
            raise IntegrityError(f"transport state hash mismatch: {row['transport_id']}")
        if endpoint_contract == "digest_v1" and secret_reference != f"{row['transport_id']}.json":
            raise IntegrityError(f"transport secret reference is invalid: {row['transport_id']}")
        return TransportRecord(
            str(row["transport_id"]),
            str(row["subject_id"]),
            str(row["channel"]),
            str(row["label"]),
            str(row["endpoint"]),
            str(row["status"]),
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    @staticmethod
    def _config(row: Any) -> dict[str, Any]:
        transport_id = row["transport_id"]
        raw_config = row["config_json"]
        if not isinstance(raw_config, str):
            raise IntegrityError(f"transport settings are invalid: {transport_id}")
        try:
            config = strict_json_loads(raw_config)
        except (TypeError, ValueError) as error:
            raise IntegrityError(f"transport settings are invalid: {transport_id}") from error
        if not isinstance(config, dict):
            raise IntegrityError(f"transport settings are invalid: {transport_id}")
        return config


class DeliveryDispatcher:
    """Delivers subject-created interactions with explicit outcome states."""

    def __init__(
        self,
        database: Database,
        transports: TransportStore,
        *,
        client: httpx.AsyncClient | None = None,
        status_lookup: DeliveryStatusLookup | None = None,
        status_client: httpx.AsyncClient | None = None,
    ):
        self.database = database
        self.transports = transports
        self._owns_client = client is None
        self._client = client
        self._status_lookup = status_lookup
        self._status_client = status_client
        self._recovered_subjects: set[str] = set()
        self._closed = False
        self._worker_tasks: set[asyncio.Task[Any]] = set()
        self._provider_tokens: dict[tuple[str, str], tuple[str, float]] = {}
        self._provider_token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        self._closed = True
        for worker in tuple(self._worker_tasks):
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    async def _run_blocking(self, function: Any, /, *args: Any) -> Any:
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        self._worker_tasks.add(worker)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise
        finally:
            if worker.done():
                self._worker_tasks.discard(worker)

    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                transport=PublicDNSAsyncHTTPTransport(),
                timeout=httpx.Timeout(20, connect=8),
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=8),
                follow_redirects=False,
                trust_env=False,
            )
        return self._client

    async def _provider_credentials(
        self, transport: TransportRecord, credentials: Mapping[str, Any]
    ) -> dict[str, Any]:
        """Resolve short-lived native provider tokens without persisting them."""
        resolved = dict(credentials)
        # Provider routing is configuration, not a credential.  Keep the
        # selected Feishu/Lark API origin in the resolved, in-memory map so
        # token acquisition can use the same region as message delivery.
        if transport.channel == "feishu" and self.transports is not None:
            settings = self.transports.settings(
                transport.transport_id, subject_id=transport.subject_id
            )
            api_origin = settings.get("api_origin")
            if isinstance(api_origin, str) and api_origin.strip():
                resolved["api_origin"] = api_origin.strip()
        if (
            transport.channel == "qq"
            and credentials.get("app_id")
            and credentials.get("app_secret")
        ):
            resolved["access_token"] = await self._provider_token(
                transport,
                transport.transport_id,
                "qq",
                resolved,
            )
        elif (
            transport.channel == "feishu"
            and credentials.get("app_id")
            and credentials.get("app_secret")
        ):
            resolved["tenant_access_token"] = await self._provider_token(
                transport,
                transport.transport_id,
                "feishu",
                resolved,
            )
        elif (
            transport.channel == "wechat"
            and credentials.get("app_id")
            and credentials.get("app_secret")
        ):
            resolved["access_token"] = await self._provider_token(
                transport,
                transport.transport_id,
                "wechat",
                resolved,
            )
        return resolved

    async def _provider_token(
        self,
        transport: TransportRecord,
        transport_id: str,
        provider: str,
        credentials: Mapping[str, Any],
    ) -> str:
        cache_key = (transport_id, provider)
        now = time.time()
        async with self._provider_token_lock:
            cached = self._provider_tokens.get(cache_key)
            if cached is not None and cached[1] > now + PROVIDER_TOKEN_SKEW_SECONDS:
                return cached[0]
            if provider == "qq":
                endpoint = "https://bots.qq.com/app/getAppAccessToken"
                method = "POST"
                request_kwargs: dict[str, Any] = {
                    "json": {
                        "appId": str(credentials["app_id"]),
                        "clientSecret": str(credentials["app_secret"]),
                    }
                }
            elif provider == "feishu":
                endpoint = (
                    self._feishu_api_origin(transport, credentials)
                    + "/open-apis/auth/v3/tenant_access_token/internal"
                )
                method = "POST"
                request_kwargs = {
                    "json": {
                        "app_id": str(credentials["app_id"]),
                        "app_secret": str(credentials["app_secret"]),
                    }
                }
            elif provider == "wechat":
                endpoint = "https://api.weixin.qq.com/cgi-bin/token"
                method = "GET"
                request_kwargs = {
                    "params": {
                        "grant_type": "client_credential",
                        "appid": str(credentials["app_id"]),
                        "secret": str(credentials["app_secret"]),
                    }
                }
            else:
                raise ValueError("unsupported provider token")
            try:
                async with asyncio.timeout(TRANSPORT_TOTAL_TIMEOUT_SECONDS):
                    response = await self._http_client().request(
                        method,
                        endpoint,
                        headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                        follow_redirects=False,
                        **request_kwargs,
                    )
                    validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
                    body = await read_bounded_response(
                        response,
                        max_body_bytes=TRANSPORT_MAX_RESPONSE_BYTES,
                        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        deadline=asyncio.get_running_loop().time()
                        + TRANSPORT_TOTAL_TIMEOUT_SECONDS,
                    )
            except (TimeoutError, httpx.HTTPError, HTTPResponseLimitError) as error:
                raise ValueError(f"{provider} access token request failed") from error
            if not 200 <= response.status_code < 300:
                raise ValueError(f"{provider} access token request failed")
            try:
                payload = strict_json_loads(body.decode("utf-8"))
            except (UnicodeDecodeError, TypeError, ValueError) as error:
                raise ValueError(f"{provider} access token response is invalid") from error
            if not isinstance(payload, dict):
                raise ValueError(f"{provider} access token response is invalid")
            if provider == "feishu":
                token = payload.get("tenant_access_token")
                expires_in = payload.get("expire")
            else:
                token = payload.get("access_token")
                expires_in = payload.get("expires_in")
            if not isinstance(token, str) or not token.strip():
                raise ValueError(f"{provider} access token response is invalid")
            if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float, str)):
                lifetime = 7_200.0
            else:
                try:
                    lifetime = max(60.0, float(expires_in))
                except (TypeError, ValueError):
                    lifetime = 7_200.0
            value = token.strip()
            self._provider_tokens[cache_key] = (value, time.time() + lifetime)
            return value

    async def _invalidate_provider_token(self, transport_id: str, provider: str) -> None:
        async with self._provider_token_lock:
            self._provider_tokens.pop((transport_id, provider), None)

    def _feishu_api_origin(self, transport: TransportRecord, credentials: Mapping[str, Any]) -> str:
        """Resolve a pinned official Feishu/Lark API origin.

        Mainland Feishu and international Lark use different API hosts.  Do
        not accept arbitrary origins from transport settings: this value is
        used for credential exchange and must remain on an official host.
        The configured endpoint is the fallback, which keeps existing
        ``https://open.feishu.cn`` setups working without migration.
        """
        raw_origin = credentials.get("api_origin")
        if not isinstance(raw_origin, str) or not raw_origin.strip():
            raw_origin = transport.endpoint
        parsed = urlsplit(str(raw_origin).strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("feishu api origin is invalid")
        host = parsed.hostname.casefold().rstrip(".")
        if host not in {"open.feishu.cn", "open.larksuite.com"}:
            raise ValueError("feishu api origin must be an official host")
        port = parsed.port
        if port not in (None, 443):
            raise ValueError("feishu api origin port is invalid")
        return f"https://{host}"

    def enqueue_pending(self, subject_id: str, *, limit: int = 50) -> int:
        created = 0
        with self.database.transaction() as connection:
            interactions = connection.execute(
                "SELECT i.* FROM interactions i WHERE i.subject_id = ? "
                "AND i.direction = 'outgoing' "
                "AND (i.channel != 'web' OR i.kind = 'help_request') "
                "AND NOT EXISTS (SELECT 1 FROM interaction_deliveries d "
                "WHERE d.interaction_id = i.interaction_id) ORDER BY i.created_at LIMIT ?",
                (subject_id, max(1, min(limit, 200))),
            ).fetchall()
            for interaction in interactions:
                transport = None
                related_id = interaction["related_interaction_id"]
                if related_id is not None and interaction["channel"] != "web":
                    # A reply must leave through the same bot/app account that
                    # authenticated the incoming message.  Choosing the first
                    # active transport can leak a reply across configured
                    # accounts and gives provider-scoped conversation IDs to
                    # the wrong credential.
                    sources = connection.execute(
                        "SELECT transport_id FROM interaction_inbound_events "
                        "WHERE subject_id = ? AND interaction_id = ? LIMIT 2",
                        (subject_id, related_id),
                    ).fetchall()
                    if len(sources) > 1:
                        raise IntegrityError("incoming interaction has ambiguous transport")
                    if sources:
                        transport = connection.execute(
                            "SELECT * FROM interaction_transports WHERE subject_id = ? "
                            "AND transport_id = ? AND channel = ? AND status = 'active'",
                            (
                                subject_id,
                                sources[0]["transport_id"],
                                interaction["channel"],
                            ),
                        ).fetchone()
                        # Do not fall through to a different account while the
                        # source transport is disabled or revoked.
                        if transport is None:
                            continue
                if (
                    transport is None
                    and interaction["channel"] == "web"
                    and interaction["kind"] == "help_request"
                ):
                    transport = connection.execute(
                        "SELECT * FROM interaction_transports WHERE subject_id = ? "
                        "AND status = 'active' ORDER BY channel, label LIMIT 1",
                        (subject_id,),
                    ).fetchone()
                elif transport is None:
                    transport = connection.execute(
                        "SELECT * FROM interaction_transports WHERE subject_id = ? AND channel = ? "
                        "AND status = 'active' ORDER BY label LIMIT 1",
                        (subject_id, interaction["channel"]),
                    ).fetchone()
                if transport is None:
                    continue
                now = utc_now()
                key = (
                    f"interaction:{interaction['interaction_id']}:"
                    f"transport:{transport['transport_id']}"
                )
                connection.execute(
                    "INSERT OR IGNORE INTO interaction_deliveries(delivery_id, interaction_id, "
                    "subject_id, transport_id, idempotency_key, status, attempts, "
                    "created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, 'queued', 0, ?, ?)",
                    (
                        new_id("delivery"),
                        interaction["interaction_id"],
                        subject_id,
                        transport["transport_id"],
                        key,
                        now,
                        now,
                    ),
                )
                created += 1
        return created

    async def deliver_pending(self, subject_id: str, *, limit: int = 10) -> list[DeliveryRecord]:
        if self._closed:
            raise asyncio.CancelledError
        if subject_id not in self._recovered_subjects:
            self.recover_inflight(subject_id)
            self._recovered_subjects.add(subject_id)
        self.enqueue_pending(subject_id, limit=limit)
        now = utc_now()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM interaction_deliveries WHERE subject_id = ? "
                "AND status IN ('queued', 'failed') "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?) "
                "AND attempts < 3 ORDER BY updated_at LIMIT ?",
                (subject_id, now, max(1, min(limit, 50))),
            ).fetchall()
        results: list[DeliveryRecord] = []
        for row in rows:
            if self._closed:
                raise asyncio.CancelledError
            results.append(await self._deliver(self.get_delivery(str(row["delivery_id"]))))
        return results

    def recover_inflight(self, subject_id: str) -> int:
        now = utc_now()
        with accounting_scope(), self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE interaction_deliveries SET status = 'unknown', "
                "last_error = 'delivery interrupted after dispatch started', "
                "next_retry_at = NULL, updated_at = ? "
                "WHERE subject_id = ? AND status = 'sending'",
                (now, subject_id),
            )
        return max(0, updated.rowcount)

    async def _deliver(self, delivery: DeliveryRecord) -> DeliveryRecord:
        assert_current_lease()
        with self.database.connection() as connection:
            current_delivery = connection.execute(
                "SELECT subject_id FROM interaction_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            ).fetchone()
            interaction = connection.execute(
                "SELECT * FROM interactions WHERE interaction_id = ?", (delivery.interaction_id,)
            ).fetchone()
            transport = connection.execute(
                "SELECT * FROM interaction_transports WHERE transport_id = ?",
                (delivery.transport_id,),
            ).fetchone()

        ownership_error: str | None = None
        if current_delivery is None or current_delivery["subject_id"] != delivery.subject_id:
            ownership_error = "delivery subject mismatch"
        elif interaction is None or transport is None:
            ownership_error = "delivery references missing data"
        elif (
            interaction["subject_id"] != delivery.subject_id
            or transport["subject_id"] != delivery.subject_id
        ):
            # A malformed delivery can otherwise select an interaction from
            # one subject and credentials from another.  Resolve this before
            # the row enters ``sending`` so no external side effect is
            # possible even when the integrity scanner has not run yet.
            ownership_error = "delivery ownership mismatch"
        if ownership_error is not None:
            return self._fail_closed_delivery(delivery, ownership_error)

        now = utc_now()
        provider_reference = (
            self._smtp_message_id(delivery.idempotency_key)
            if transport["channel"] == "email"
            else None
        )
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE interaction_deliveries SET status = 'sending', attempts = attempts + 1, "
                "provider_message_id = COALESCE(provider_message_id, ?), updated_at = ? "
                "WHERE delivery_id = ? AND subject_id = ? AND status IN ('queued', 'failed')",
                (provider_reference, now, delivery.delivery_id, delivery.subject_id),
            )
            if updated.rowcount != 1:
                return self.get_delivery(delivery.delivery_id)
            row = connection.execute(
                "SELECT * FROM interaction_deliveries WHERE delivery_id = ?",
                (delivery.delivery_id,),
            ).fetchone()
            attempts = int(row["attempts"])
        try:
            assert_current_lease()
        except OperationInvalidated:
            with accounting_scope():
                self._finish(
                    delivery.delivery_id,
                    "unknown",
                    "runtime epoch invalidated before external delivery",
                    attempts,
                    provider_message_id=provider_reference,
                )
            raise
        if interaction is None or transport is None:
            return self._finish(
                delivery.delivery_id, "failed", "delivery references missing data", attempts
            )
        try:
            # Re-read all three ownership columns immediately before loading
            # credentials.  This closes the check-then-send window if a
            # damaged row is modified after the initial admission check.
            with self.database.connection() as connection:
                ownership = connection.execute(
                    "SELECT d.subject_id AS delivery_subject_id, "
                    "i.subject_id AS interaction_subject_id, "
                    "t.subject_id AS transport_subject_id "
                    "FROM interaction_deliveries d "
                    "LEFT JOIN interactions i ON i.interaction_id = d.interaction_id "
                    "LEFT JOIN interaction_transports t ON t.transport_id = d.transport_id "
                    "WHERE d.delivery_id = ?",
                    (delivery.delivery_id,),
                ).fetchone()
            if (
                ownership is None
                or ownership["delivery_subject_id"] != delivery.subject_id
                or ownership["interaction_subject_id"] != delivery.subject_id
                or ownership["transport_subject_id"] != delivery.subject_id
            ):
                return self._fail_closed_delivery(delivery, "delivery ownership mismatch")
            record = self.transports._record(transport)
            if record.subject_id != delivery.subject_id:
                return self._fail_closed_delivery(delivery, "delivery ownership mismatch")
            secret = self.transports.secret(record.transport_id, subject_id=delivery.subject_id)
            send_interaction = dict(interaction)
            if interaction["related_interaction_id"] is not None:
                with self.database.connection() as connection:
                    contexts = connection.execute(
                        "SELECT external_thread_id, reply_selector, reply_context_version, "
                        "conversation_id, transport_id, subject_id, external_account_id, "
                        "external_sender_id, channel "
                        "FROM interaction_inbound_events WHERE interaction_id = ? "
                        "AND subject_id = ? LIMIT 2",
                        (
                            interaction["related_interaction_id"],
                            delivery.subject_id,
                        ),
                    ).fetchall()
                    if len(contexts) > 1:
                        raise IntegrityError("incoming interaction has ambiguous reply context")
                    context = contexts[0] if contexts else None
                    legacy_thread = None
                    if context is not None and context["transport_id"] != record.transport_id:
                        raise IntegrityError("reply delivery uses a different source transport")
                    if context is not None:
                        expected_counterparty = (
                            context["external_sender_id"]
                            if context["channel"] == "email"
                            else context["conversation_id"]
                        )
                        if (
                            context["channel"] != record.channel
                            or str(interaction["counterparty"]) != expected_counterparty
                        ):
                            raise IntegrityError(
                                "reply delivery context does not match interaction"
                            )
                    if (
                        context is not None
                        and int(context["reply_context_version"]) == 0
                        and record.channel in {"qq", "feishu"}
                    ):
                        raise IntegrityError("legacy native reply route is unavailable")
                    if context is not None and int(context["reply_context_version"]) == 0:
                        legacy_thread = connection.execute(
                            "SELECT external_thread_id FROM interaction_threads "
                            "WHERE subject_id = ? AND transport_id = ? "
                            "AND external_account_id = ? AND conversation_id = ? "
                            "AND external_thread_id IS NOT NULL "
                            "ORDER BY updated_at DESC LIMIT 1",
                            (
                                context["subject_id"],
                                context["transport_id"],
                                context["external_account_id"],
                                context["conversation_id"],
                            ),
                        ).fetchone()
                if context is not None:
                    thread_id = context["external_thread_id"]
                    if thread_id is None and legacy_thread is not None:
                        thread_id = legacy_thread["external_thread_id"]
                    if thread_id is not None:
                        send_interaction["thread_id"] = thread_id
                    if context["reply_selector"] is not None:
                        send_interaction["reply_selector"] = context["reply_selector"]
            send_interaction["counterparty"] = self.transports.delivery_counterparty(
                record.transport_id, interaction, subject_id=delivery.subject_id
            )
            response_id = await self._send(
                record, secret, send_interaction, delivery.idempotency_key
            )
            try:
                assert_current_lease()
            except OperationInvalidated:
                with accounting_scope():
                    self._finish(
                        delivery.delivery_id,
                        "unknown",
                        "runtime epoch invalidated after delivery",
                        attempts,
                        provider_message_id=provider_reference,
                    )
                raise
        except DeliveryOutcomeUnknown as error:
            return self._finish(
                delivery.delivery_id,
                "unknown",
                str(error),
                attempts,
                provider_message_id=error.provider_message_id,
            )
        except asyncio.CancelledError:
            try:
                self._finish(
                    delivery.delivery_id,
                    "unknown",
                    "delivery cancelled",
                    attempts,
                    provider_message_id=provider_reference,
                )
            finally:
                raise
        except OperationInvalidated:
            with accounting_scope():
                self._finish(
                    delivery.delivery_id,
                    "unknown",
                    "runtime epoch invalidated before delivery",
                    attempts,
                    provider_message_id=provider_reference,
                )
            raise
        except (
            httpx.ReadTimeout,
            httpx.WriteTimeout,
            httpx.ReadError,
            httpx.WriteError,
            httpx.CloseError,
            httpx.RemoteProtocolError,
            HTTPResponseLimitError,
        ):
            return self._finish(
                delivery.delivery_id, "unknown", "transport outcome unknown", attempts
            )
        except Exception as error:
            return self._finish(delivery.delivery_id, "failed", type(error).__name__, attempts)
        return self._finish(
            delivery.delivery_id, "delivered", None, attempts, provider_message_id=response_id
        )

    async def _send(
        self,
        transport: TransportRecord,
        secret: dict[str, Any],
        interaction: Any,
        idempotency_key: str,
    ) -> str | None:
        credentials = secret.get("credentials", {})
        if not isinstance(credentials, Mapping):
            raise ValueError("transport credentials are invalid")
        original_credentials = dict(credentials)
        credentials = await self._provider_credentials(transport, original_credentials)
        # Adapter routing fields are non-secret transport settings kept in
        # SQLite, while provider tokens remain in the private secret file.
        # Pass only the known provider selectors across this boundary so a
        # mutable settings blob can never replace a credential.
        adapter_credentials = dict(credentials)
        # Unit-level delivery tests and a few recovery paths intentionally
        # construct a dispatcher without a TransportStore.  Provider routing
        # selectors are optional in that case; production dispatchers always
        # have the store and therefore still receive the persisted settings.
        if self.transports is not None:
            settings = self.transports.settings(
                transport.transport_id, subject_id=transport.subject_id
            )
            for selector in ("target_type", "receive_id_type", "delivery_mode"):
                if selector in settings:
                    adapter_credentials[selector] = settings[selector]
        self._apply_reply_selector(transport, interaction, adapter_credentials)
        endpoint = str(secret.get("endpoint", transport.endpoint))
        channel = transport.channel
        if channel == "email":
            return cast(
                str | None,
                await self._run_blocking(
                    self._send_email,
                    endpoint,
                    credentials,
                    interaction["counterparty"],
                    interaction["content"],
                    idempotency_key,
                ),
            )
        adapter = OUTBOUND_ADAPTERS.get(channel)
        if adapter is None:
            raise ValueError("unsupported transport channel")
        # The delivery worker historically passed a compact interaction record
        # containing only the counterparty and content.  Adapters for richer
        # envelopes (notably webhooks) also need the channel, so normalize a
        # private copy at this boundary without mutating the persisted record.
        adapter_interaction = dict(interaction)
        adapter_interaction.setdefault("channel", channel)
        provider_name = channel if channel in {"qq", "feishu", "wechat"} else None
        for token_attempt in range(2):
            outbound = adapter.build(
                endpoint, adapter_credentials, adapter_interaction, idempotency_key
            )
            request_headers = {
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "X-Noyra-Idempotency-Key": idempotency_key,
            }
            if outbound.headers:
                request_headers.update(outbound.headers)
            deadline = asyncio.get_running_loop().time() + TRANSPORT_TOTAL_TIMEOUT_SECONDS
            try:
                async with asyncio.timeout(TRANSPORT_TOTAL_TIMEOUT_SECONDS):
                    async with self._http_client().stream(
                        "POST",
                        outbound.endpoint,
                        json=outbound.payload,
                        headers=request_headers,
                        follow_redirects=False,
                    ) as response:
                        validate_response_headers(
                            response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES
                        )
                        if response.status_code >= 500:
                            raise httpx.ReadError("remote transport server error")
                        response.raise_for_status()
                        body_bytes = await read_bounded_response(
                            response,
                            max_body_bytes=TRANSPORT_MAX_RESPONSE_BYTES,
                            max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                            deadline=deadline,
                        )
            except httpx.HTTPStatusError as error:
                if (
                    error.response.status_code == 401
                    and token_attempt == 0
                    and provider_name is not None
                    and original_credentials.get("app_id")
                    and original_credentials.get("app_secret")
                ):
                    await self._invalidate_provider_token(transport.transport_id, provider_name)
                    credentials = await self._provider_credentials(transport, original_credentials)
                    adapter_credentials = dict(credentials)
                    if self.transports is not None:
                        settings = self.transports.settings(
                            transport.transport_id, subject_id=transport.subject_id
                        )
                        for selector in ("target_type", "receive_id_type", "delivery_mode"):
                            if selector in settings:
                                adapter_credentials[selector] = settings[selector]
                    self._apply_reply_selector(transport, interaction, adapter_credentials)
                    continue
                raise
            except TimeoutError as error:
                raise httpx.ReadTimeout("transport response deadline exceeded") from error
            try:
                body = json.loads(body_bytes)
            except (TypeError, ValueError):
                body = {}
            if isinstance(body, dict):
                provider_error = self._provider_error(channel, body)
                if provider_error is not None:
                    if (
                        token_attempt == 0
                        and provider_name is not None
                        and original_credentials.get("app_id")
                        and original_credentials.get("app_secret")
                        and self._provider_error_is_token_expired(channel, body)
                    ):
                        await self._invalidate_provider_token(transport.transport_id, provider_name)
                        credentials = await self._provider_credentials(
                            transport, original_credentials
                        )
                        adapter_credentials = dict(credentials)
                        if self.transports is not None:
                            settings = self.transports.settings(
                                transport.transport_id, subject_id=transport.subject_id
                            )
                            for selector in ("target_type", "receive_id_type", "delivery_mode"):
                                if selector in settings:
                                    adapter_credentials[selector] = settings[selector]
                        self._apply_reply_selector(transport, interaction, adapter_credentials)
                        continue
                    raise ValueError(provider_error)
                for key in ("message_id", "msg_id", "id"):
                    value = body.get(key)
                    if isinstance(value, (str, int)):
                        return str(value)
                data = body.get("data")
                if isinstance(data, Mapping):
                    for key in ("message_id", "msg_id", "id"):
                        value = data.get(key)
                        if isinstance(value, (str, int)):
                            return str(value)
            return None
        return None

    @staticmethod
    def _apply_reply_selector(
        transport: TransportRecord,
        interaction: Mapping[str, Any],
        adapter_credentials: dict[str, Any],
    ) -> None:
        """Apply authenticated inbound routing after operator defaults.

        Transport settings remain the default for proactive messages.  A
        response linked to an inbound provider event instead carries the
        exact provider route that was authenticated with that event.
        """
        reply_selector = interaction.get("reply_selector")
        if reply_selector is None:
            return
        if not isinstance(reply_selector, str) or ":" not in reply_selector:
            raise IntegrityError("inbound reply selector is invalid")
        selector_channel, selector_value = reply_selector.split(":", 1)
        if selector_channel != transport.channel:
            raise IntegrityError("inbound reply selector crosses channel")
        if selector_channel == "qq" and selector_value in {
            "user",
            "group",
            "channel",
            "dm",
        }:
            adapter_credentials["target_type"] = selector_value
            return
        if selector_channel == "feishu" and selector_value == "chat_id":
            adapter_credentials["receive_id_type"] = selector_value
            return
        raise IntegrityError("inbound reply selector is unsupported")

    @staticmethod
    def _provider_error_is_token_expired(channel: str, body: Mapping[str, Any]) -> bool:
        code = body.get("errcode") if channel == "wechat" else body.get("code")
        if channel == "wechat" and isinstance(code, (int, str)) and not isinstance(code, bool):
            try:
                if int(code) in {40001, 40014, 42001}:
                    return True
            except ValueError:
                pass
        detail = " ".join(
            str(body.get(key, "")) for key in ("errmsg", "message", "msg", "StatusMessage")
        ).casefold()
        return any(
            marker in detail
            for marker in ("access_token", "access token", "tenant_access_token", "token expired")
        )

    @staticmethod
    def _provider_error(channel: str, body: Mapping[str, Any]) -> str | None:
        """Interpret successful-HTTP provider error envelopes before delivery commit."""
        if channel == "wechat" and "errcode" in body:
            code = body.get("errcode")
            if isinstance(code, bool) or not isinstance(code, (int, str)):
                return "wechat provider response is invalid"
            try:
                failed = int(code) != 0
            except (TypeError, ValueError):
                failed = True
            if failed:
                return f"wechat provider error {str(body.get('errmsg', code))[:256]}"
        if channel == "feishu":
            code = body.get("code", body.get("StatusCode"))
            if code is not None:
                if isinstance(code, bool) or not isinstance(code, (int, str)):
                    return "feishu provider response is invalid"
                try:
                    failed = int(code) != 0
                except (TypeError, ValueError):
                    failed = True
                if failed:
                    detail = body.get("msg", body.get("StatusMessage", code))
                    return f"feishu provider error {str(detail)[:256]}"
        if channel == "qq" and "code" in body:
            code = body.get("code")
            if isinstance(code, bool) or not isinstance(code, (int, str)):
                return "qq provider response is invalid"
            try:
                failed = int(code) != 0
            except (TypeError, ValueError):
                failed = True
            if failed:
                return f"qq provider error {str(body.get('message', code))[:256]}"
        return None

    @staticmethod
    def _send_email(
        endpoint: str,
        credentials: dict[str, Any],
        recipient: str,
        content: str,
        idempotency_key: str,
    ) -> str:
        parsed = urlsplit(endpoint)
        host = parsed.hostname
        if not host:
            raise ValueError("smtp host is missing")
        port = parsed.port or (465 if parsed.scheme == "smtps" else 587)
        sender = str(credentials.get("from_address", ""))
        if not sender:
            raise ValueError("email from_address is required")
        message = EmailMessage()
        message["From"] = sender
        message["To"] = recipient
        message["Subject"] = "Noyra message"
        message_id = DeliveryDispatcher._smtp_message_id(idempotency_key)
        message["Message-ID"] = message_id
        message.set_content(content)
        addresses = _PublicDNSBackend._public_addresses(host, port)
        address = addresses[0]
        if parsed.scheme == "smtps":
            client: smtplib.SMTP = smtplib.SMTP_SSL(
                timeout=SMTP_TIMEOUT_SECONDS,
                context=ssl.create_default_context(),
            )
        else:
            client = smtplib.SMTP(timeout=SMTP_TIMEOUT_SECONDS)
        cast(Any, client)._host = host
        sent = False
        try:
            code, message_text = client.connect(address, port)
            if code != 220:
                raise smtplib.SMTPConnectError(code, message_text)
            DeliveryDispatcher._verify_smtp_peer(client, address)
            if parsed.scheme != "smtps":
                client.starttls(context=ssl.create_default_context())
            username = credentials.get("username")
            password = credentials.get("password")
            if username and password:
                client.login(str(username), str(password))
            try:
                client.send_message(message)
            except (TimeoutError, OSError, smtplib.SMTPServerDisconnected) as error:
                raise DeliveryOutcomeUnknown(
                    "SMTP send outcome unknown",
                    provider_message_id=message_id,
                ) from error
            sent = True
        finally:
            if sent:
                try:
                    client.quit()
                except (OSError, smtplib.SMTPException):
                    client.close()
            else:
                client.close()
        return message_id

    @staticmethod
    def _smtp_message_id(idempotency_key: str) -> str:
        digest = content_hash({"delivery_idempotency_key": idempotency_key})
        return f"<{digest}@noyra.invalid>"

    @staticmethod
    def _verify_smtp_peer(client: smtplib.SMTP, pinned_address: str) -> None:
        if client.sock is None:
            raise OSError("SMTP connection did not expose a peer socket")
        peer = str(client.sock.getpeername()[0]).split("%", 1)[0]
        expected = str(ipaddress.ip_address(pinned_address.split("%", 1)[0]))
        try:
            actual = str(ipaddress.ip_address(peer))
        except ValueError as error:
            raise OSError("SMTP peer address is invalid") from error
        if actual != expected:
            raise OSError("SMTP peer address changed after DNS pinning")

    def _fail_closed_delivery(self, delivery: DeliveryRecord, error: str) -> DeliveryRecord:
        """Terminally quarantine a delivery that cannot prove ownership.

        A forged or damaged delivery must never remain retryable: retrying it
        would repeatedly re-evaluate an invalid cross-subject reference and
        could turn a later code path into an external side effect.  Exhausting
        the bounded attempt budget keeps the existing delivery schema while
        preserving an auditable ``failed`` outcome.
        """
        now = utc_now()
        with accounting_scope(), self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE interaction_deliveries SET status = 'failed', "
                "attempts = MAX(attempts + 1, 3), last_error = ?, "
                "next_retry_at = NULL, updated_at = ? "
                "WHERE delivery_id = ? AND subject_id = ? "
                "AND status IN ('queued', 'failed', 'sending')",
                (error, now, delivery.delivery_id, delivery.subject_id),
            )
            if updated.rowcount != 1:
                return self.get_delivery(delivery.delivery_id)
        return self.get_delivery(delivery.delivery_id)

    def _finish(
        self,
        delivery_id: str,
        status: str,
        error: str | None,
        attempts: int,
        *,
        provider_message_id: str | None = None,
    ) -> DeliveryRecord:
        now = utc_now()
        next_retry = None
        if status == "failed" and attempts < 3:
            next_retry = (
                datetime.now(UTC) + timedelta(seconds=60 * (2 ** max(0, attempts - 1)))
            ).isoformat(timespec="milliseconds")
        with accounting_scope(), self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE interaction_deliveries SET status = ?, "
                "provider_message_id = COALESCE(?, provider_message_id), "
                "last_error = ?, next_retry_at = ?, updated_at = ? "
                "WHERE delivery_id = ? AND status = 'sending'",
                (status, provider_message_id, error, next_retry, now, delivery_id),
            )
            if updated.rowcount != 1:
                return self.get_delivery(delivery_id)
        return self.get_delivery(delivery_id)

    async def lookup_unknown(
        self,
        delivery_id: str,
        *,
        subject_id: str | None = None,
        actor: str,
        reason: str,
    ) -> DeliveryRecord:
        self._validate_reconciliation_actor(actor, reason)
        delivery = self.get_delivery(delivery_id)
        if subject_id is not None and delivery.subject_id != subject_id:
            raise NotFoundError(f"delivery not found: {delivery_id}")
        if delivery.original_status != "unknown":
            raise InvalidTransitionError("only an unknown delivery supports provider lookup")
        if delivery.reconciliation_id is not None:
            return delivery
        transport = self.transports.get(delivery.transport_id, subject_id=delivery.subject_id)
        secret = self.transports.secret(delivery.transport_id, subject_id=delivery.subject_id)
        settings = self.transports.settings(delivery.transport_id, subject_id=delivery.subject_id)
        try:
            if self._status_lookup is not None:
                evidence = await self._status_lookup(delivery, transport, secret, settings)
            else:
                evidence = await self._lookup_status_endpoint(
                    delivery,
                    transport,
                    secret,
                    settings,
                )
        except Exception as error:
            evidence = ProviderStatusEvidence(
                "error",
                type(error).__name__,
                delivery.provider_message_id,
                {"error_type": type(error).__name__},
            )
        return self._append_reconciliation(
            delivery_id,
            source="provider",
            outcome=evidence.outcome,
            provider_message_id=evidence.provider_message_id,
            provider_status=evidence.provider_status,
            evidence=evidence.details or {},
            actor=actor,
            reason=reason,
            subject_id=subject_id,
        )

    def reconcile_unknown(
        self,
        delivery_id: str,
        outcome: Literal["delivered", "failed", "cancelled"],
        *,
        subject_id: str | None = None,
        actor: str,
        reason: str,
        provider_message_id: str | None = None,
        evidence: Mapping[str, Any] | None = None,
    ) -> DeliveryRecord:
        self._validate_reconciliation_actor(actor, reason)
        return self._append_reconciliation(
            delivery_id,
            source="operator",
            outcome=outcome,
            provider_message_id=provider_message_id,
            provider_status="manual_reconciliation",
            evidence=evidence or {},
            actor=actor,
            reason=reason,
            subject_id=subject_id,
        )

    def reconciliation_history(
        self,
        delivery_id: str,
        *,
        subject_id: str | None = None,
    ) -> list[DeliveryReconciliationRecord]:
        with self.database.connection() as connection:
            delivery = connection.execute(
                "SELECT delivery_id, subject_id FROM interaction_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if delivery is None or (
                subject_id is not None and delivery["subject_id"] != subject_id
            ):
                raise NotFoundError(f"delivery not found: {delivery_id}")
            rows = connection.execute(
                "SELECT * FROM interaction_delivery_reconciliations "
                "WHERE delivery_id = ? ORDER BY sequence",
                (delivery_id,),
            ).fetchall()
        return [self._reconciliation_record(row) for row in rows]

    async def _lookup_status_endpoint(
        self,
        delivery: DeliveryRecord,
        transport: TransportRecord,
        secret: Mapping[str, Any],
        settings: Mapping[str, Any],
    ) -> ProviderStatusEvidence:
        raw_endpoint = settings.get("status_endpoint")
        if not isinstance(raw_endpoint, str) or not raw_endpoint.strip():
            return ProviderStatusEvidence(
                "unsupported",
                "status_lookup_unsupported",
                delivery.provider_message_id,
                {"channel": transport.channel},
            )
        endpoint = raw_endpoint.strip()
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.hostname:
            return ProviderStatusEvidence(
                "error",
                "invalid_status_endpoint",
                delivery.provider_message_id,
                {"endpoint_origin": TransportStore._endpoint_origin(endpoint)},
            )
        try:
            literal_address = ipaddress.ip_address(parsed.hostname)
        except ValueError:
            literal_address = None
        if literal_address is not None and not literal_address.is_global:
            return ProviderStatusEvidence(
                "error",
                "non_public_status_endpoint",
                delivery.provider_message_id,
                {"endpoint_origin": TransportStore._endpoint_origin(endpoint)},
            )
        credentials = secret.get("credentials", {})
        headers = {"Accept": "application/json", "Accept-Encoding": "identity"}
        if isinstance(credentials, Mapping):
            token = credentials.get("status_token")
            if isinstance(token, str) and token:
                headers["Authorization"] = f"Bearer {token}"
        parameters = {"idempotency_key": delivery.idempotency_key}
        if delivery.provider_message_id:
            parameters["provider_message_id"] = delivery.provider_message_id

        async def request(client: httpx.AsyncClient) -> tuple[int, bytes]:
            deadline = asyncio.get_running_loop().time() + TRANSPORT_TOTAL_TIMEOUT_SECONDS
            async with asyncio.timeout(TRANSPORT_TOTAL_TIMEOUT_SECONDS):
                async with client.stream(
                    "GET",
                    endpoint,
                    params=parameters,
                    headers=headers,
                    follow_redirects=False,
                ) as response:
                    validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
                    body = await read_bounded_response(
                        response,
                        max_body_bytes=TRANSPORT_MAX_RESPONSE_BYTES,
                        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        deadline=deadline,
                    )
                    return response.status_code, body

        try:
            if self._status_client is not None:
                status_code, body_bytes = await request(self._status_client)
            else:
                async with httpx.AsyncClient(
                    transport=PublicDNSAsyncHTTPTransport(),
                    timeout=httpx.Timeout(20, connect=8),
                    limits=httpx.Limits(max_connections=4, max_keepalive_connections=0),
                    follow_redirects=False,
                    trust_env=False,
                ) as client:
                    status_code, body_bytes = await request(client)
        except (TimeoutError, httpx.HTTPError, HTTPResponseLimitError) as error:
            return ProviderStatusEvidence(
                "error",
                type(error).__name__,
                delivery.provider_message_id,
                {"error_type": type(error).__name__},
            )
        details: dict[str, Any] = {
            "http_status": status_code,
            "response_hash": hashlib.sha256(body_bytes).hexdigest(),
        }
        if not 200 <= status_code < 300:
            return ProviderStatusEvidence(
                "error",
                f"http_{status_code}",
                delivery.provider_message_id,
                details,
            )
        try:
            body = strict_json_loads(body_bytes.decode("utf-8"))
        except (UnicodeError, TypeError, ValueError):
            return ProviderStatusEvidence(
                "error",
                "invalid_status_response",
                delivery.provider_message_id,
                details,
            )
        if not isinstance(body, dict):
            return ProviderStatusEvidence(
                "error",
                "invalid_status_response",
                delivery.provider_message_id,
                details,
            )
        nested = body.get("data")
        status_value = body.get("delivery_status", body.get("status"))
        if status_value is None and isinstance(nested, dict):
            status_value = nested.get("delivery_status", nested.get("status"))
        provider_status = str(status_value or "unknown").strip().casefold()[:256]
        details["provider_status"] = provider_status
        outcome_map: dict[str, DeliveryReconciliationOutcome] = {
            "delivered": "delivered",
            "sent": "delivered",
            "accepted": "delivered",
            "success": "delivered",
            "failed": "failed",
            "rejected": "failed",
            "undelivered": "failed",
            "cancelled": "failed",
            "not_found": "unknown",
            "pending": "unknown",
            "processing": "unknown",
            "unknown": "unknown",
        }
        outcome = outcome_map.get(provider_status, "error")
        provider_message_id = body.get("provider_message_id", body.get("message_id"))
        if provider_message_id is None and isinstance(nested, dict):
            provider_message_id = nested.get("provider_message_id", nested.get("message_id"))
        if not isinstance(provider_message_id, (str, int)):
            provider_message_id = delivery.provider_message_id
        return ProviderStatusEvidence(
            outcome,
            provider_status,
            None if provider_message_id is None else str(provider_message_id)[:512],
            details,
        )

    def _append_reconciliation(
        self,
        delivery_id: str,
        *,
        source: Literal["provider", "operator"],
        outcome: DeliveryReconciliationOutcome,
        provider_message_id: str | None,
        provider_status: str | None,
        evidence: Mapping[str, Any],
        actor: str,
        reason: str,
        subject_id: str | None,
    ) -> DeliveryRecord:
        if outcome not in {"delivered", "failed", "cancelled", "unknown", "unsupported", "error"}:
            raise ValueError("delivery reconciliation outcome is invalid")
        if source == "operator" and outcome not in {"delivered", "failed", "cancelled"}:
            raise ValueError("operator reconciliation must be terminal")
        if source == "provider" and outcome == "cancelled":
            raise ValueError("provider reconciliation cannot cancel delivery")
        if provider_message_id is not None and not 1 <= len(provider_message_id) <= 512:
            raise ValueError("provider message id is invalid")
        if provider_status is not None and len(provider_status) > 256:
            raise ValueError("provider status is too long")
        evidence_json = canonical_json(dict(evidence))
        if len(evidence_json.encode("utf-8")) > DELIVERY_EVIDENCE_MAX_BYTES:
            raise ValueError("delivery reconciliation evidence is too large")
        evidence_hash = content_hash(dict(evidence))
        now = utc_now()
        reconciliation_id = new_id("delivery-reconciliation")
        with self.database.transaction() as connection:
            delivery = connection.execute(
                "SELECT * FROM interaction_deliveries WHERE delivery_id = ?",
                (delivery_id,),
            ).fetchone()
            if delivery is None:
                raise NotFoundError(f"delivery not found: {delivery_id}")
            if subject_id is not None and delivery["subject_id"] != subject_id:
                raise NotFoundError(f"delivery not found: {delivery_id}")
            if delivery["status"] != "unknown":
                raise InvalidTransitionError("only an unknown delivery can be reconciled")
            terminal = connection.execute(
                "SELECT * FROM interaction_delivery_reconciliations "
                "WHERE delivery_id = ? AND outcome IN ('delivered', 'failed', 'cancelled')",
                (delivery_id,),
            ).fetchone()
            should_insert = terminal is None
            if terminal is not None:
                existing = self._reconciliation_record(terminal)
                if existing.outcome == outcome and (
                    provider_message_id is None
                    or existing.provider_message_id == provider_message_id
                ):
                    should_insert = False
                else:
                    raise InvalidTransitionError("delivery already has a terminal reconciliation")
            if should_insert:
                sequence = int(
                    connection.execute(
                        "SELECT COALESCE(MAX(sequence), 0) + 1 "
                        "FROM interaction_delivery_reconciliations WHERE delivery_id = ?",
                        (delivery_id,),
                    ).fetchone()[0]
                )
                state = {
                    "reconciliation_id": reconciliation_id,
                    "delivery_id": delivery_id,
                    "subject_id": delivery["subject_id"],
                    "sequence": sequence,
                    "source": source,
                    "outcome": outcome,
                    "provider_message_id": provider_message_id,
                    "provider_status": provider_status,
                    "evidence_hash": evidence_hash,
                    "actor": actor,
                    "reason": reason,
                    "created_at": now,
                }
                connection.execute(
                    "INSERT INTO interaction_delivery_reconciliations("
                    "reconciliation_id, delivery_id, subject_id, sequence, source, outcome, "
                    "provider_message_id, provider_status, evidence_json, evidence_hash, actor, "
                    "reason, state_hash, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        reconciliation_id,
                        delivery_id,
                        delivery["subject_id"],
                        sequence,
                        source,
                        outcome,
                        provider_message_id,
                        provider_status,
                        evidence_json,
                        evidence_hash,
                        actor,
                        reason,
                        content_hash(state),
                        now,
                    ),
                )
        return self.get_delivery(delivery_id)

    @staticmethod
    def _validate_reconciliation_actor(actor: str, reason: str) -> None:
        if not actor.strip() or actor.strip().casefold() == "subject" or len(actor) > 256:
            raise PermissionError("delivery reconciliation requires an operator")
        if not reason.strip() or len(reason) > 2_000:
            raise ValueError("delivery reconciliation reason is required and bounded")

    @staticmethod
    def _reconciliation_record(row: Any) -> DeliveryReconciliationRecord:
        reconciliation_id = row["reconciliation_id"]
        raw_evidence = row["evidence_json"]
        if not isinstance(raw_evidence, str):
            raise IntegrityError(
                f"delivery reconciliation evidence is invalid: {reconciliation_id}"
            )
        if len(raw_evidence.encode("utf-8")) > DELIVERY_EVIDENCE_MAX_BYTES:
            raise IntegrityError(
                f"delivery reconciliation evidence is too large: {reconciliation_id}"
            )
        try:
            evidence = strict_json_loads(raw_evidence)
            sequence = strict_int(row["sequence"])
        except (TypeError, ValueError) as error:
            raise IntegrityError(
                f"delivery reconciliation evidence is invalid: {reconciliation_id}"
            ) from error
        if not isinstance(evidence, dict) or sequence <= 0:
            raise IntegrityError(f"delivery reconciliation is invalid: {reconciliation_id}")
        source = row["source"]
        outcome = row["outcome"]
        provider_message_id = row["provider_message_id"]
        provider_status = row["provider_status"]
        if (
            source not in {"provider", "operator"}
            or outcome
            not in {"delivered", "failed", "cancelled", "unknown", "unsupported", "error"}
            or (source == "operator" and outcome not in {"delivered", "failed", "cancelled"})
            or (source == "provider" and outcome == "cancelled")
            or not isinstance(row["actor"], str)
            or not row["actor"].strip()
            or not isinstance(row["reason"], str)
            or not row["reason"].strip()
            or not isinstance(row["created_at"], str)
            or not row["created_at"]
            or (
                provider_message_id is not None
                and (not isinstance(provider_message_id, str) or len(provider_message_id) > 512)
            )
            or (
                provider_status is not None
                and (not isinstance(provider_status, str) or len(provider_status) > 256)
            )
        ):
            raise IntegrityError(f"delivery reconciliation is invalid: {reconciliation_id}")
        evidence_hash = content_hash(evidence)
        state = {
            "reconciliation_id": reconciliation_id,
            "delivery_id": row["delivery_id"],
            "subject_id": row["subject_id"],
            "sequence": sequence,
            "source": source,
            "outcome": outcome,
            "provider_message_id": provider_message_id,
            "provider_status": provider_status,
            "evidence_hash": evidence_hash,
            "actor": row["actor"],
            "reason": row["reason"],
            "created_at": row["created_at"],
        }
        if row["evidence_hash"] != evidence_hash or row["state_hash"] != content_hash(state):
            raise IntegrityError(f"delivery reconciliation hash mismatch: {reconciliation_id}")
        return DeliveryReconciliationRecord(
            str(reconciliation_id),
            str(row["delivery_id"]),
            str(row["subject_id"]),
            sequence,
            str(row["source"]),
            str(row["outcome"]),
            row["provider_message_id"],
            row["provider_status"],
            evidence,
            str(row["evidence_hash"]),
            str(row["actor"]),
            str(row["reason"]),
            str(row["state_hash"]),
            str(row["created_at"]),
        )

    def get_delivery(self, delivery_id: str) -> DeliveryRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM interaction_deliveries WHERE delivery_id = ?", (delivery_id,)
            ).fetchone()
            reconciliation = connection.execute(
                "SELECT * FROM interaction_delivery_reconciliations "
                "WHERE delivery_id = ? AND outcome IN ('delivered', 'failed', 'cancelled')",
                (delivery_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"delivery not found: {delivery_id}")
        resolved = None if reconciliation is None else self._reconciliation_record(reconciliation)
        return DeliveryRecord(
            delivery_id=str(row["delivery_id"]),
            interaction_id=str(row["interaction_id"]),
            subject_id=str(row["subject_id"]),
            transport_id=str(row["transport_id"]),
            idempotency_key=str(row["idempotency_key"]),
            status=str(row["status"] if resolved is None else resolved.outcome),
            provider_message_id=(
                row["provider_message_id"]
                if resolved is None or resolved.provider_message_id is None
                else resolved.provider_message_id
            ),
            attempts=int(row["attempts"]),
            last_error=row["last_error"],
            next_retry_at=row["next_retry_at"],
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            original_status=str(row["status"]),
            reconciliation_id=None if resolved is None else resolved.reconciliation_id,
            reconciliation_source=None if resolved is None else resolved.source,
            reconciliation_reason=None if resolved is None else resolved.reason,
            reconciled_at=None if resolved is None else resolved.created_at,
        )
