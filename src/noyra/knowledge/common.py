from __future__ import annotations

import base64
import ipaddress
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from noyra.core.database import Database
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import (
    canonical_json,
    content_hash,
    new_id,
    strict_int,
    strict_json_loads,
    utc_now,
)

from .sync import (
    COMMON_KNOWLEDGE_SYNC_PROTOCOL,
    CommonKnowledgeClientProtocol,
    CommonKnowledgeHTTPClient,
    CommonKnowledgeSyncError,
)

KnowledgeScope = Literal["skill", "pitfall", "protocol", "reference"]

_FORBIDDEN_TOKENS = {
    "identity",
    "personality",
    "emotion",
    "mood",
    "private_memory",
    "autobiographical",
    "relationship",
    "goal",
    "mission",
    "wallet",
    "credential",
    "password",
    "secret",
    "token",
    "conversation",
    "subject_id",
}

_ALLOWED_FIELDS = {
    "skill": {"steps", "prerequisites", "validation", "tags", "compatibility"},
    "pitfall": {"symptom", "cause", "avoidance", "environment", "tags"},
    "protocol": {"procedure", "compatibility", "validation", "tags"},
    "reference": {"facts", "sources", "tags", "valid_until"},
}

_SIGNED_FIELDS = (
    "package_id",
    "publisher_subject_id",
    "scope",
    "title",
    "summary",
    "payload",
    "payload_hash",
    "key_id",
    "version",
    "created_at",
)

_SYNC_EVENT_FIELDS = (
    "protocol",
    "event_id",
    "publisher_subject_id",
    "sequence",
    "event_type",
    "package_id",
    "series_id",
    "package_version",
    "predecessor_package_id",
    "envelope",
    "envelope_hash",
    "key_id",
    "occurred_at",
)

_DEFAULT_COMPATIBILITY = frozenset(
    {
        "generic",
        "noyra",
        "noyra/v1",
        "http",
        "https",
        "json",
        "linux",
        "python",
        "sqlite",
        "ubuntu",
        "windows",
    }
)


class CommonKnowledgeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: KnowledgeScope
    title: str = Field(min_length=1, max_length=256)
    summary: str = Field(min_length=1, max_length=2_000)
    payload: dict[str, Any]
    version: int = Field(default=1, ge=1, le=1_000_000)

    @model_validator(mode="after")
    def validate_isolation(self) -> CommonKnowledgeProposal:
        if not set(self.payload).issubset(_ALLOWED_FIELDS[self.scope]):
            raise ValueError("common knowledge payload contains fields outside its scope")
        encoded = canonical_json(
            {"title": self.title.strip(), "summary": self.summary.strip(), "payload": self.payload}
        )
        if len(encoded.encode("utf-8")) > 100_000:
            raise ValueError("common knowledge package exceeds 100 KB")
        lowered = encoded.casefold()
        if any(token in lowered for token in _FORBIDDEN_TOKENS):
            raise ValueError("common knowledge cannot contain subject-private state")
        if not self.title.strip() or not self.summary.strip():
            raise ValueError("common knowledge text cannot be blank")
        compatibility = self.payload.get("compatibility")
        if compatibility is not None and (
            not isinstance(compatibility, list)
            or len(compatibility) > 64
            or any(
                not isinstance(item, str) or not item.strip() or len(item.strip()) > 128
                for item in compatibility
            )
        ):
            raise ValueError("common knowledge compatibility metadata is invalid")
        valid_until = self.payload.get("valid_until")
        if valid_until is not None:
            if not isinstance(valid_until, str):
                raise ValueError("common knowledge validity metadata is invalid")
            try:
                parsed = datetime.fromisoformat(valid_until)
            except ValueError as error:
                raise ValueError("common knowledge validity metadata is invalid") from error
            if parsed.tzinfo is None:
                raise ValueError("common knowledge validity metadata requires a timezone")
        return self


class CommonKnowledgePeerInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str = Field(min_length=1, max_length=128)
    endpoint: str = Field(min_length=1, max_length=2_000)
    publisher_subject_id: str = Field(min_length=3, max_length=128)
    public_key: str = Field(min_length=1, max_length=1_000)
    sync_interval_seconds: int = Field(default=3_600, ge=60, le=86_400)

    @field_validator("label", "publisher_subject_id")
    @classmethod
    def validate_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("common knowledge peer text cannot be blank")
        return normalized

    @field_validator("publisher_subject_id")
    @classmethod
    def validate_publisher_subject_id(cls, value: str) -> str:
        normalized = value.strip()
        try:
            validate_subject_id(normalized)
        except ValueError as error:
            raise ValueError("common knowledge peer publisher subject id is invalid") from error
        return normalized

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        normalized = value.strip().rstrip("/")
        parsed = urlsplit(normalized)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("common knowledge peer must be a HTTPS origin")
        if parsed.hostname.casefold() == "localhost":
            raise ValueError("common knowledge peer cannot target localhost")
        try:
            address = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
        except ValueError:
            return normalized
        if not address.is_global:
            raise ValueError("common knowledge peer must use a public endpoint")
        return normalized


@dataclass(frozen=True)
class CommonKnowledgePackage:
    package_id: str
    publisher_subject_id: str
    scope: str
    title: str
    summary: str
    payload: dict[str, Any]
    payload_hash: str
    signature: str
    key_id: str
    version: int
    status: str
    created_at: str
    revoked_at: str | None


@dataclass(frozen=True)
class CommonKnowledgeAdvisory:
    package_id: str
    scope: str
    title: str
    summary: str
    payload: dict[str, Any]
    payload_hash: str
    signer_key_id: str
    envelope_hash: str

    def as_context(self) -> dict[str, Any]:
        return {
            "package_id": self.package_id,
            "scope": self.scope,
            "title": self.title,
            "summary": self.summary,
            "payload": self.payload,
            "payload_hash": self.payload_hash,
            "signer_key_id": self.signer_key_id,
            "envelope_hash": self.envelope_hash,
            "boundary": "advisory_non_private",
        }


@dataclass(frozen=True)
class CommonKnowledgeVersion:
    package_id: str
    publisher_subject_id: str
    series_id: str
    version: int
    predecessor_package_id: str | None
    created_at: str


@dataclass(frozen=True)
class CommonKnowledgePeerRecord:
    peer_id: str
    subject_id: str
    label: str
    endpoint: str
    publisher_subject_id: str
    key_id: str
    status: str
    cursor: int
    remote_etag: str | None
    sync_interval_seconds: int
    next_sync_at: str
    last_sync_at: str | None
    last_error_code: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class CommonKnowledgeSyncResult:
    peer_id: str
    outcome: str
    cursor_before: int
    cursor_after: int
    discovered: int
    imported: int
    revoked: int
    remote_etag: str | None


@dataclass(frozen=True)
class CommonKnowledgeEvaluation:
    evaluation_id: str
    package_id: str
    status: str
    reason: str
    requested_at: str
    completed_at: str | None


class CommonKnowledgeStore:
    """Signed advisory knowledge with trust, quarantine and revocation boundaries."""

    def __init__(
        self,
        database: Database,
        subject_id: str,
        key_dir: Path | str,
        *,
        allow_key_creation: bool = True,
        compatibility: frozenset[str] | set[str] | None = None,
        clock: Callable[[], str] = utc_now,
    ):
        self.database = database
        self.subject_id = validate_subject_id(subject_id)
        self.key_dir = Path(key_dir).resolve()
        self.key_dir.mkdir(parents=True, exist_ok=True)
        self.compatibility = frozenset(
            item.strip().casefold()
            for item in (compatibility or _DEFAULT_COMPATIBILITY)
            if item.strip()
        )
        self.clock = clock
        self._private_key = self._load_or_create_key(allow_create=allow_key_creation)
        self.public_key = (
            ""
            if self._private_key is None
            else self._public_key_text(self._private_key.public_key())
        )
        self.key_id = content_hash(self.public_key)[:32] if self.public_key else ""

    def ensure_signing_key(self) -> None:
        if self._private_key is not None:
            return
        self._private_key = self._load_or_create_key(allow_create=True)
        assert self._private_key is not None
        self.public_key = self._public_key_text(self._private_key.public_key())
        self.key_id = content_hash(self.public_key)[:32]

    def publish(
        self,
        proposal: CommonKnowledgeProposal,
        *,
        actor: str = "subject",
        series_id: str | None = None,
        supersedes_package_id: str | None = None,
    ) -> CommonKnowledgePackage:
        if actor != "subject":
            raise PermissionError("only the subject can publish common knowledge")
        if series_id is not None and (not series_id.strip() or len(series_id.strip()) > 256):
            raise ValueError("common knowledge series identifier is invalid")
        if supersedes_package_id is not None and (
            not supersedes_package_id.strip() or len(supersedes_package_id.strip()) > 256
        ):
            raise ValueError("common knowledge predecessor identifier is invalid")
        package_id = new_id("knowledge")
        created_at = self.clock()
        body = {
            "package_id": package_id,
            "publisher_subject_id": self.subject_id,
            "scope": proposal.scope,
            "title": proposal.title.strip(),
            "summary": proposal.summary.strip(),
            "payload": proposal.payload,
            "payload_hash": content_hash(proposal.payload),
            "key_id": self.key_id,
            "version": proposal.version,
            "created_at": created_at,
        }
        signature = self._sign(body)
        envelope = {**body, "signature": signature}
        with self.database.transaction() as connection:
            resolved_series_id = series_id.strip() if series_id is not None else package_id
            if supersedes_package_id is not None:
                predecessor = connection.execute(
                    "SELECT * FROM common_knowledge_versions WHERE subject_id = ? "
                    "AND package_id = ?",
                    (self.subject_id, supersedes_package_id.strip()),
                ).fetchone()
                if predecessor is None:
                    raise ValueError("common knowledge predecessor version is unavailable")
                if predecessor["publisher_subject_id"] != self.subject_id:
                    raise PermissionError("common knowledge predecessor has a different publisher")
                if series_id is not None and predecessor["series_id"] != resolved_series_id:
                    raise ValueError("common knowledge predecessor has a different series")
                if proposal.version <= int(predecessor["package_version"]):
                    raise ValueError("common knowledge version must advance its predecessor")
                resolved_series_id = str(predecessor["series_id"])
            elif (
                connection.execute(
                    "SELECT 1 FROM common_knowledge_versions WHERE subject_id = ? "
                    "AND publisher_subject_id = ? AND series_id = ? LIMIT 1",
                    (self.subject_id, self.subject_id, resolved_series_id),
                ).fetchone()
                is not None
            ):
                raise ValueError("common knowledge series continuation requires a predecessor")
            connection.execute(
                "INSERT INTO common_knowledge_packages(package_id, publisher_subject_id, scope, "
                "title, summary, payload_json, payload_hash, signature, key_id, version, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?)",
                (
                    package_id,
                    self.subject_id,
                    proposal.scope,
                    proposal.title.strip(),
                    proposal.summary.strip(),
                    canonical_json(proposal.payload),
                    body["payload_hash"],
                    signature,
                    self.key_id,
                    proposal.version,
                    created_at,
                ),
            )
            self._insert_version(
                connection,
                package_id=package_id,
                publisher_subject_id=self.subject_id,
                series_id=resolved_series_id,
                version=proposal.version,
                predecessor_package_id=(
                    None if supersedes_package_id is None else supersedes_package_id.strip()
                ),
                created_at=created_at,
            )
            self._append_sync_event(
                connection,
                event_type="published",
                package_id=package_id,
                series_id=resolved_series_id,
                package_version=proposal.version,
                predecessor_package_id=(
                    None if supersedes_package_id is None else supersedes_package_id.strip()
                ),
                envelope=envelope,
                occurred_at=created_at,
            )
        return self.get(package_id)

    def trust_key(self, public_key: str, *, label: str, actor: str) -> str:
        if not actor.strip() or actor == "subject" or not label.strip():
            raise PermissionError("trusted publisher keys are operator-provided resources")
        self._parse_public_key(public_key)
        key_id = content_hash(public_key)[:32]
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO common_knowledge_trusted_keys(key_id, public_key, label, status, "
                "created_at) VALUES (?, ?, ?, 'active', ?) ON CONFLICT(key_id) DO UPDATE SET "
                "public_key = excluded.public_key, label = excluded.label, status = 'active', "
                "revoked_at = NULL",
                (key_id, public_key, label.strip(), utc_now()),
            )
        return key_id

    def export_package(self, package_id: str) -> dict[str, Any]:
        """Return the exact signed envelope accepted by :meth:`import_package`.

        The envelope is deliberately kept free of local presentation metadata.  In
        particular, package status and a node's public key are not signed package
        fields and must not be mixed into the provenance-bound document.
        """
        return self._verified_envelope(self.get(package_id))

    def package_metadata(self, package_id: str) -> dict[str, Any]:
        """Return a local display view without changing the import envelope."""
        record = self.get(package_id)
        return {
            **self._verified_envelope(record),
            "status": record.status,
            "public_key": self.public_key if record.key_id == self.key_id else None,
        }

    def import_package(self, envelope: dict[str, Any]) -> CommonKnowledgePackage:
        proposal, body, verified_envelope, envelope_json, envelope_hash = self._normalize_envelope(
            envelope
        )
        signature = str(verified_envelope["signature"])
        now = self.clock()
        with self.database.transaction() as connection:
            key = connection.execute(
                "SELECT public_key FROM common_knowledge_trusted_keys "
                "WHERE key_id = ? AND status = 'active'",
                (body["key_id"],),
            ).fetchone()
            if key is None:
                raise PermissionError("common knowledge publisher key is not trusted")
            public_key = str(key["public_key"])
            if content_hash(public_key)[:32] != body["key_id"]:
                raise IntegrityError("common knowledge trusted key id mismatch")
            self._verify(public_key, body, signature)

            existing = connection.execute(
                "SELECT * FROM common_knowledge_packages WHERE package_id = ?",
                (body["package_id"],),
            ).fetchone()
            if existing is not None:
                existing_record = self._record_from_row(existing)
                existing_envelope = self._verified_envelope(existing_record)
                if canonical_json(existing_envelope) != envelope_json:
                    raise IntegrityError("common knowledge package id collision")
                if existing_record.status != "published":
                    raise PermissionError("common knowledge package is not publishable")
            else:
                connection.execute(
                    "INSERT INTO common_knowledge_packages(package_id, publisher_subject_id, "
                    "scope, title, summary, payload_json, payload_hash, signature, key_id, "
                    "version, "
                    "status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?)",
                    (
                        body["package_id"],
                        body["publisher_subject_id"],
                        proposal.scope,
                        proposal.title,
                        proposal.summary,
                        canonical_json(proposal.payload),
                        body["payload_hash"],
                        signature,
                        body["key_id"],
                        proposal.version,
                        body["created_at"],
                    ),
                )

            imported = connection.execute(
                "SELECT * FROM common_knowledge_imports WHERE package_id = ? AND subject_id = ?",
                (body["package_id"], self.subject_id),
            ).fetchone()
            if imported is None:
                import_id = new_id("knowledge-import")
                connection.execute(
                    "INSERT INTO common_knowledge_imports(import_id, package_id, subject_id, "
                    "status, reason, imported_at) VALUES (?, ?, ?, 'quarantined', ?, ?)",
                    (
                        import_id,
                        body["package_id"],
                        self.subject_id,
                        "signature verified; awaiting subject evaluation",
                        now,
                    ),
                )
            else:
                import_id = str(imported["import_id"])

            provenance = connection.execute(
                "SELECT * FROM common_knowledge_import_provenance WHERE import_id = ?",
                (import_id,),
            ).fetchone()
            if provenance is None:
                connection.execute(
                    "INSERT INTO common_knowledge_import_provenance(provenance_id, import_id, "
                    "package_id, subject_id, envelope_json, envelope_hash, payload_hash, "
                    "signer_key_id, signature, verified_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        new_id("knowledge-provenance"),
                        import_id,
                        body["package_id"],
                        self.subject_id,
                        envelope_json,
                        envelope_hash,
                        body["payload_hash"],
                        body["key_id"],
                        signature,
                        now,
                    ),
                )
            elif (
                provenance["envelope_json"] != envelope_json
                or provenance["envelope_hash"] != envelope_hash
                or provenance["payload_hash"] != body["payload_hash"]
                or provenance["signer_key_id"] != body["key_id"]
                or provenance["signature"] != signature
            ):
                raise IntegrityError("common knowledge import provenance mismatch")
        return self.get(str(body["package_id"]))

    def accept(self, package_id: str, *, reason: str, actor: str = "subject") -> None:
        evaluation = self.begin_evaluation(package_id, reason=reason, actor=actor)
        self.complete_evaluation(
            evaluation.evaluation_id,
            decision="accepted",
            reason=reason,
            actor=actor,
        )

    def begin_evaluation(
        self,
        package_id: str,
        *,
        reason: str,
        actor: str = "subject",
    ) -> CommonKnowledgeEvaluation:
        if actor != "subject" or not reason.strip():
            raise PermissionError("only the subject can request common knowledge evaluation")
        now = self.clock()
        evaluation_id = new_id("knowledge-evaluation")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM common_knowledge_packages WHERE package_id = ?", (package_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"common knowledge package not found: {package_id}")
            record = self._record_from_row(row)
            if not self._verify_record(
                connection, record, require_compatible=True, allow_untrusted=False
            ):
                raise PermissionError("common knowledge package is incompatible")
            imported = connection.execute(
                "SELECT import_id, status FROM common_knowledge_imports "
                "WHERE package_id = ? AND subject_id = ?",
                (package_id, self.subject_id),
            ).fetchone()
            if imported is None or imported["status"] != "quarantined":
                raise ValueError("common knowledge package is not quarantined")
            existing = connection.execute(
                "SELECT e.evaluation_id FROM common_knowledge_evaluation_events e "
                "WHERE e.subject_id = ? AND e.package_id = ? AND e.sequence = 1 "
                "AND NOT EXISTS (SELECT 1 FROM common_knowledge_evaluation_events terminal "
                "WHERE terminal.evaluation_id = e.evaluation_id AND terminal.sequence = 2) "
                "LIMIT 1",
                (self.subject_id, package_id),
            ).fetchone()
            if existing is not None:
                raise ValueError("common knowledge evaluation is already pending")
            provenance = connection.execute(
                "SELECT envelope_hash FROM common_knowledge_import_provenance "
                "WHERE package_id = ? AND subject_id = ?",
                (package_id, self.subject_id),
            ).fetchone()
            if provenance is None:
                raise IntegrityError("common knowledge import provenance is missing")
            self._insert_evaluation_event(
                connection,
                evaluation_id=evaluation_id,
                package_id=package_id,
                sequence=1,
                status="requested",
                envelope_hash=str(provenance["envelope_hash"]),
                reason=reason.strip(),
                occurred_at=now,
            )
        return CommonKnowledgeEvaluation(
            evaluation_id,
            package_id,
            "requested",
            reason.strip(),
            now,
            None,
        )

    def complete_evaluation(
        self,
        evaluation_id: str,
        *,
        decision: Literal["accepted", "rejected"],
        reason: str,
        actor: str = "subject",
    ) -> CommonKnowledgeEvaluation:
        if actor != "subject" or not reason.strip():
            raise PermissionError("only the subject can complete common knowledge evaluation")
        now = self.clock()
        with self.database.transaction() as connection:
            requested = connection.execute(
                "SELECT * FROM common_knowledge_evaluation_events "
                "WHERE evaluation_id = ? AND subject_id = ? AND sequence = 1 "
                "AND status = 'requested'",
                (evaluation_id, self.subject_id),
            ).fetchone()
            if requested is None:
                raise NotFoundError(f"common knowledge evaluation not found: {evaluation_id}")
            terminal = connection.execute(
                "SELECT 1 FROM common_knowledge_evaluation_events "
                "WHERE evaluation_id = ? AND sequence = 2",
                (evaluation_id,),
            ).fetchone()
            if terminal is not None:
                raise ValueError("common knowledge evaluation is already complete")
            package_id = str(requested["package_id"])
            package_row = connection.execute(
                "SELECT * FROM common_knowledge_packages WHERE package_id = ?",
                (package_id,),
            ).fetchone()
            if package_row is None:
                raise IntegrityError("common knowledge evaluation package is missing")
            record = self._record_from_row(package_row)
            if decision == "accepted" and not self._verify_record(
                connection,
                record,
                require_compatible=True,
                allow_untrusted=False,
            ):
                raise PermissionError("common knowledge package is incompatible")
            updated = connection.execute(
                "UPDATE common_knowledge_imports SET status = ?, reason = ? "
                "WHERE package_id = ? AND subject_id = ? AND status = 'quarantined'",
                (decision, reason.strip(), package_id, self.subject_id),
            )
            if updated.rowcount != 1:
                raise ValueError("common knowledge package is not quarantined")
            if decision == "accepted":
                version = connection.execute(
                    "SELECT series_id, package_version FROM common_knowledge_versions "
                    "WHERE subject_id = ? AND package_id = ?",
                    (self.subject_id, package_id),
                ).fetchone()
                if version is not None:
                    connection.execute(
                        "UPDATE common_knowledge_imports SET status = 'revoked', reason = ? "
                        "WHERE subject_id = ? AND status = 'accepted' AND package_id IN ("
                        "SELECT package_id FROM common_knowledge_versions "
                        "WHERE subject_id = ? AND series_id = ? AND package_version < ?)",
                        (
                            f"superseded by explicitly evaluated package {package_id}",
                            self.subject_id,
                            self.subject_id,
                            version["series_id"],
                            version["package_version"],
                        ),
                    )
            self._insert_evaluation_event(
                connection,
                evaluation_id=evaluation_id,
                package_id=package_id,
                sequence=2,
                status=decision,
                envelope_hash=str(requested["envelope_hash"]),
                reason=reason.strip(),
                occurred_at=now,
            )
        return CommonKnowledgeEvaluation(
            evaluation_id,
            package_id,
            decision,
            reason.strip(),
            str(requested["occurred_at"]),
            now,
        )

    def review_queue(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """Return imported packages awaiting or undergoing subject review."""
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT p.package_id, i.status AS import_status, i.reason AS import_reason, "
                "i.imported_at FROM common_knowledge_packages p "
                "JOIN common_knowledge_imports i ON i.package_id = p.package_id "
                "WHERE i.subject_id = ? AND i.status != 'revoked' "
                "ORDER BY i.imported_at DESC LIMIT ?",
                (self.subject_id, max(1, min(limit, 1_000))),
            ).fetchall()
        return [
            {
                **self.package_metadata(str(row["package_id"])),
                "import_status": str(row["import_status"]),
                "import_reason": str(row["import_reason"]),
                "imported_at": str(row["imported_at"]),
            }
            for row in rows
        ]

    def revoke(self, package_id: str, *, reason: str, actor: str = "subject") -> None:
        """Revoke a package published by this subject without touching private state."""
        if actor not in {"subject", "web-operator"} or not reason.strip():
            raise PermissionError("common knowledge revocation requires an authorized actor")
        package = self.get(package_id)
        if package.publisher_subject_id != self.subject_id:
            raise PermissionError("only the publisher can revoke common knowledge")
        now = self.clock()
        with self.database.transaction() as connection:
            version = connection.execute(
                "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
                (self.subject_id, package_id),
            ).fetchone()
            if version is None:
                self._backfill_local_package(connection, package)
                version = connection.execute(
                    "SELECT * FROM common_knowledge_versions "
                    "WHERE subject_id = ? AND package_id = ?",
                    (self.subject_id, package_id),
                ).fetchone()
            assert version is not None
            updated = connection.execute(
                "UPDATE common_knowledge_packages SET status = 'revoked', revoked_at = ? "
                "WHERE package_id = ? AND publisher_subject_id = ? AND status = 'published'",
                (now, package_id, self.subject_id),
            )
            if updated.rowcount != 1:
                raise ValueError("common knowledge package is already revoked")
            connection.execute(
                "UPDATE common_knowledge_imports SET status = 'revoked', reason = ? "
                "WHERE package_id = ? AND status IN ('quarantined', 'accepted')",
                (reason.strip(), package_id),
            )
            self._append_sync_event(
                connection,
                event_type="revoked",
                package_id=package_id,
                series_id=str(version["series_id"]),
                package_version=int(version["package_version"]),
                predecessor_package_id=version["predecessor_package_id"],
                envelope=self._verified_envelope(package),
                occurred_at=now,
            )

    def revoke_trusted_key(self, key_id: str, *, reason: str, actor: str) -> None:
        if not actor.strip() or actor == "subject" or not reason.strip():
            raise PermissionError("trusted key revocation requires an authorized operator")
        now = self.clock()
        with self.database.transaction() as connection:
            updated = connection.execute(
                "UPDATE common_knowledge_trusted_keys SET status = 'revoked', revoked_at = ? "
                "WHERE key_id = ? AND status = 'active'",
                (now, key_id),
            )
            if updated.rowcount != 1:
                raise ValueError("common knowledge trusted key is not active")
            connection.execute(
                "UPDATE common_knowledge_imports SET status = 'revoked', reason = ? "
                "WHERE subject_id = ? AND status IN ('quarantined', 'accepted') AND package_id IN "
                "(SELECT package_id FROM common_knowledge_packages WHERE key_id = ?)",
                (reason.strip(), self.subject_id, key_id),
            )

    def usable(self, *, scope: str | None = None) -> list[CommonKnowledgePackage]:
        with self.database.connection() as connection:
            params: list[Any] = [self.subject_id]
            clause = ""
            if scope is not None:
                clause = " AND p.scope = ?"
                params.append(scope)
            rows = connection.execute(
                "SELECT p.package_id FROM common_knowledge_packages p "
                "JOIN common_knowledge_imports i ON i.package_id = p.package_id "
                "WHERE i.subject_id = ? AND i.status = 'accepted' "
                "AND p.status = 'published'" + clause + " ORDER BY p.created_at DESC",
                tuple(params),
            ).fetchall()
        usable: list[CommonKnowledgePackage] = []
        for row in rows:
            with self.database.connection() as connection:
                package_row = connection.execute(
                    "SELECT * FROM common_knowledge_packages WHERE package_id = ?",
                    (row["package_id"],),
                ).fetchone()
                if package_row is None:
                    raise IntegrityError("accepted common knowledge package is missing")
                record = self._record_from_row(package_row)
                if self._verify_record(
                    connection,
                    record,
                    require_compatible=True,
                    allow_untrusted=True,
                ):
                    usable.append(record)
        return usable

    def advisories(
        self, *, scope: str | None = None, limit: int = 20
    ) -> list[CommonKnowledgeAdvisory]:
        bounded = max(1, min(limit, 100))
        advisories: list[CommonKnowledgeAdvisory] = []
        for record in self.usable(scope=scope)[:bounded]:
            with self.database.connection() as connection:
                provenance = connection.execute(
                    "SELECT envelope_hash FROM common_knowledge_import_provenance "
                    "WHERE package_id = ? AND subject_id = ?",
                    (record.package_id, self.subject_id),
                ).fetchone()
            if provenance is None:
                raise IntegrityError("common knowledge import provenance is missing")
            payload = strict_json_loads(canonical_json(record.payload))
            assert isinstance(payload, dict)
            advisories.append(
                CommonKnowledgeAdvisory(
                    package_id=record.package_id,
                    scope=record.scope,
                    title=record.title,
                    summary=record.summary,
                    payload=payload,
                    payload_hash=record.payload_hash,
                    signer_key_id=record.key_id,
                    envelope_hash=str(provenance["envelope_hash"]),
                )
            )
        return advisories

    def advisory_context(self, limit: int = 20) -> list[dict[str, Any]]:
        return [advisory.as_context() for advisory in self.advisories(limit=limit)]

    def version(self, package_id: str) -> CommonKnowledgeVersion:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
                (self.subject_id, package_id),
            ).fetchone()
        if row is None:
            package = self.get(package_id)
            if package.publisher_subject_id != self.subject_id:
                raise NotFoundError(f"common knowledge version not found: {package_id}")
            with self.database.transaction() as connection:
                self._backfill_local_package(connection, package)
            return self.version(package_id)
        return self._version_from_row(row)

    def series_versions(self, series_id: str) -> list[CommonKnowledgeVersion]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND series_id = ? "
                "ORDER BY package_version, created_at, package_id",
                (self.subject_id, series_id),
            ).fetchall()
        return [self._version_from_row(row) for row in rows]

    def register_peer(
        self,
        proposal: CommonKnowledgePeerInput,
        *,
        actor: str,
    ) -> CommonKnowledgePeerRecord:
        if not actor.strip() or actor == "subject":
            raise PermissionError("common knowledge peers are operator-provided resources")
        self._parse_public_key(proposal.public_key)
        key_id = content_hash(proposal.public_key)[:32]
        if proposal.publisher_subject_id == self.subject_id or key_id == self.key_id:
            raise ValueError("common knowledge peer cannot reference this node")
        peer_id = new_id("knowledge-peer")
        now = self.clock()
        values = {
            "peer_id": peer_id,
            "subject_id": self.subject_id,
            "label": proposal.label,
            "endpoint": proposal.endpoint,
            "publisher_subject_id": proposal.publisher_subject_id,
            "key_id": key_id,
            "public_key": proposal.public_key,
            "status": "active",
            "cursor": 0,
            "remote_etag": None,
            "sync_interval_seconds": proposal.sync_interval_seconds,
            "next_sync_at": now,
            "last_sync_at": None,
            "last_error_code": None,
            "created_at": now,
            "updated_at": now,
        }
        with self.database.transaction() as connection:
            connection.execute(
                "INSERT INTO common_knowledge_trusted_keys(key_id, public_key, label, status, "
                "created_at) VALUES (?, ?, ?, 'active', ?) ON CONFLICT(key_id) DO UPDATE SET "
                "public_key = excluded.public_key, label = excluded.label, status = 'active', "
                "revoked_at = NULL",
                (key_id, proposal.public_key, proposal.label, now),
            )
            connection.execute(
                "INSERT INTO common_knowledge_peers(peer_id, subject_id, label, endpoint, "
                "publisher_subject_id, key_id, public_key, status, cursor, remote_etag, "
                "sync_interval_seconds, next_sync_at, last_sync_at, last_error_code, state_hash, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, 'active', 0, NULL, ?, ?, "
                "NULL, NULL, ?, ?, ?)",
                (
                    peer_id,
                    self.subject_id,
                    proposal.label,
                    proposal.endpoint,
                    proposal.publisher_subject_id,
                    key_id,
                    proposal.public_key,
                    proposal.sync_interval_seconds,
                    now,
                    content_hash(values),
                    now,
                    now,
                ),
            )
        return self.peer(peer_id)

    def peer(self, peer_id: str) -> CommonKnowledgePeerRecord:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM common_knowledge_peers WHERE peer_id = ? AND subject_id = ?",
                (peer_id, self.subject_id),
            ).fetchone()
        if row is None:
            raise NotFoundError(f"common knowledge peer not found: {peer_id}")
        return self._peer_from_row(row)

    def peers(self) -> list[CommonKnowledgePeerRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM common_knowledge_peers WHERE subject_id = ? ORDER BY label, peer_id",
                (self.subject_id,),
            ).fetchall()
        return [self._peer_from_row(row) for row in rows]

    def discovery_document(self) -> dict[str, Any]:
        self._ensure_local_sync_state()
        with self.database.connection() as connection:
            latest = connection.execute(
                "SELECT sequence, event_hash FROM common_knowledge_sync_events "
                "WHERE subject_id = ? ORDER BY sequence DESC LIMIT 1",
                (self.subject_id,),
            ).fetchone()
        latest_sequence = 0 if latest is None else strict_int(latest["sequence"])
        latest_event_hash = "" if latest is None else str(latest["event_hash"])
        etag = content_hash(
            {
                "protocol": COMMON_KNOWLEDGE_SYNC_PROTOCOL,
                "publisher_subject_id": self.subject_id,
                "key_id": self.key_id,
                "latest_sequence": latest_sequence,
                "latest_event_hash": latest_event_hash,
            }
        )
        body = {
            "protocol": COMMON_KNOWLEDGE_SYNC_PROTOCOL,
            "publisher_subject_id": self.subject_id,
            "key_id": self.key_id,
            "public_key": self.public_key,
            "latest_sequence": latest_sequence,
            "latest_event_hash": latest_event_hash,
            "etag": etag,
        }
        return {**body, "signature": self._sign(body)}

    def feed_document(self, *, cursor: int, limit: int = 50) -> dict[str, Any]:
        if cursor < 0:
            raise ValueError("common knowledge feed cursor is invalid")
        bounded = max(1, min(limit, 100))
        discovery = self.discovery_document()
        if cursor > strict_int(discovery["latest_sequence"]):
            raise ValueError("common knowledge feed cursor is ahead of the publisher")
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM common_knowledge_sync_events WHERE subject_id = ? "
                "AND sequence > ? ORDER BY sequence LIMIT ?",
                (self.subject_id, cursor, bounded),
            ).fetchall()
        events = [self._sync_event_from_row(row) for row in rows]
        next_cursor = cursor if not rows else strict_int(rows[-1]["sequence"])
        body = {
            "protocol": COMMON_KNOWLEDGE_SYNC_PROTOCOL,
            "publisher_subject_id": self.subject_id,
            "key_id": self.key_id,
            "cursor": cursor,
            "next_cursor": next_cursor,
            "has_more": next_cursor < strict_int(discovery["latest_sequence"]),
            "etag": discovery["etag"],
            "events": events,
        }
        return {**body, "signature": self._sign(body)}

    def sync_peer(
        self,
        peer_id: str,
        *,
        client: CommonKnowledgeClientProtocol | None = None,
        max_events: int = 100,
    ) -> CommonKnowledgeSyncResult:
        if not 1 <= max_events <= 1_000:
            raise ValueError("common knowledge sync event budget is invalid")
        with self.database.connection() as connection:
            peer_row = connection.execute(
                "SELECT * FROM common_knowledge_peers WHERE peer_id = ? AND subject_id = ?",
                (peer_id, self.subject_id),
            ).fetchone()
        if peer_row is None:
            raise NotFoundError(f"common knowledge peer not found: {peer_id}")
        peer = self._peer_from_row(peer_row)
        if peer.status != "active":
            raise PermissionError("common knowledge peer is disabled")
        cursor_before = peer.cursor
        cursor = cursor_before
        discovered = imported = revoked = 0
        remote_etag: str | None = None
        owned_client: CommonKnowledgeHTTPClient | None = None
        active_client: CommonKnowledgeClientProtocol
        if client is None:
            owned_client = CommonKnowledgeHTTPClient(str(peer_row["endpoint"]))
            active_client = owned_client
        else:
            active_client = client
        try:
            discovery = active_client.discovery(etag=peer.remote_etag)
            if discovery is None:
                result = CommonKnowledgeSyncResult(
                    peer_id,
                    "unchanged",
                    cursor_before,
                    cursor,
                    0,
                    0,
                    0,
                    peer.remote_etag,
                )
                self._finish_peer_sync(result, error_code=None)
                return result
            latest_sequence, remote_etag = self._verify_discovery(peer_row, discovery)
            if latest_sequence < cursor:
                raise IntegrityError("common knowledge peer cursor moved backwards")
            while cursor < latest_sequence and discovered < max_events:
                page = active_client.feed(
                    cursor=cursor,
                    limit=min(50, max_events - discovered),
                )
                events = self._verify_feed_page(
                    peer_row,
                    page,
                    cursor=cursor,
                    expected_etag=remote_etag,
                )
                if not events:
                    raise IntegrityError("common knowledge peer feed ended before its cursor")
                for event in events:
                    if discovered >= max_events:
                        break
                    sequence = self._apply_remote_event(peer_row, event, expected=cursor + 1)
                    cursor = sequence
                    discovered += 1
                    if event["event_type"] == "published":
                        imported += 1
                    else:
                        revoked += 1
            outcome = "succeeded" if cursor == latest_sequence else "partial"
            result = CommonKnowledgeSyncResult(
                peer_id,
                outcome,
                cursor_before,
                cursor,
                discovered,
                imported,
                revoked,
                remote_etag if outcome == "succeeded" else None,
            )
            self._finish_peer_sync(result, error_code=None)
            return result
        except (CommonKnowledgeSyncError, IntegrityError, PermissionError, ValueError) as error:
            code = (
                error.code
                if isinstance(error, CommonKnowledgeSyncError)
                else "common_knowledge_peer_invalid_evidence"
            )
            failed = CommonKnowledgeSyncResult(
                peer_id,
                "failed",
                cursor_before,
                self.peer(peer_id).cursor,
                discovered,
                imported,
                revoked,
                None,
            )
            self._finish_peer_sync(failed, error_code=code)
            if isinstance(error, CommonKnowledgeSyncError):
                raise
            raise CommonKnowledgeSyncError(code) from error
        finally:
            if owned_client is not None:
                owned_client.close()

    def sync_due(
        self,
        *,
        max_peers: int = 2,
        max_events_per_peer: int = 100,
        checkpoint: Callable[[], None] | None = None,
    ) -> int:
        now = self.clock()
        if checkpoint is not None:
            checkpoint()
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT peer_id FROM common_knowledge_peers WHERE subject_id = ? "
                "AND status = 'active' AND next_sync_at <= ? "
                "ORDER BY next_sync_at, peer_id LIMIT ?",
                (self.subject_id, now, max(1, min(max_peers, 8))),
            ).fetchall()
        completed = 0
        for row in rows:
            if checkpoint is not None:
                checkpoint()
            try:
                self.sync_peer(
                    str(row["peer_id"]),
                    max_events=max_events_per_peer,
                )
            except CommonKnowledgeSyncError:
                continue
            completed += 1
            if checkpoint is not None:
                checkpoint()
        return completed

    def evaluation(self, evaluation_id: str) -> CommonKnowledgeEvaluation:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM common_knowledge_evaluation_events "
                "WHERE evaluation_id = ? AND subject_id = ? ORDER BY sequence",
                (evaluation_id, self.subject_id),
            ).fetchall()
        if not rows:
            raise NotFoundError(f"common knowledge evaluation not found: {evaluation_id}")
        latest = rows[-1]
        return CommonKnowledgeEvaluation(
            evaluation_id,
            str(latest["package_id"]),
            str(latest["status"]),
            str(latest["reason"]),
            str(rows[0]["occurred_at"]),
            None if len(rows) == 1 else str(latest["occurred_at"]),
        )

    def published(self, *, limit: int = 100) -> list[CommonKnowledgePackage]:
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT package_id FROM common_knowledge_packages "
                "WHERE publisher_subject_id = ? AND status = 'published' "
                "ORDER BY created_at DESC LIMIT ?",
                (self.subject_id, max(1, min(limit, 1_000))),
            ).fetchall()
        return [self.get(str(row["package_id"])) for row in rows]

    def get(self, package_id: str) -> CommonKnowledgePackage:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM common_knowledge_packages WHERE package_id = ?", (package_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"common knowledge package not found: {package_id}")
            record = self._record_from_row(row)
            self._verify_record(connection, record, require_compatible=False, allow_untrusted=False)
            return record

    @staticmethod
    def _version_from_row(row: Any) -> CommonKnowledgeVersion:
        version = strict_int(row["package_version"])
        if version < 1:
            raise IntegrityError("common knowledge version is invalid")
        return CommonKnowledgeVersion(
            str(row["package_id"]),
            str(row["publisher_subject_id"]),
            str(row["series_id"]),
            version,
            row["predecessor_package_id"],
            str(row["created_at"]),
        )

    def _insert_version(
        self,
        connection: Any,
        *,
        package_id: str,
        publisher_subject_id: str,
        series_id: str,
        version: int,
        predecessor_package_id: str | None,
        created_at: str,
    ) -> None:
        existing = connection.execute(
            "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
            (self.subject_id, package_id),
        ).fetchone()
        values = {
            "package_id": package_id,
            "subject_id": self.subject_id,
            "publisher_subject_id": publisher_subject_id,
            "series_id": series_id,
            "package_version": version,
            "predecessor_package_id": predecessor_package_id,
            "created_at": created_at,
        }
        if existing is not None:
            durable = {name: existing[name] for name in values}
            if durable != values or existing["state_hash"] != content_hash(values):
                raise IntegrityError("common knowledge version metadata mismatch")
            return
        if predecessor_package_id is not None:
            predecessor = connection.execute(
                "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
                (self.subject_id, predecessor_package_id),
            ).fetchone()
            if (
                predecessor is None
                or predecessor["publisher_subject_id"] != publisher_subject_id
                or predecessor["series_id"] != series_id
                or strict_int(predecessor["package_version"]) >= version
            ):
                raise IntegrityError("common knowledge version predecessor is invalid")
        elif (
            connection.execute(
                "SELECT 1 FROM common_knowledge_versions WHERE subject_id = ? "
                "AND publisher_subject_id = ? AND series_id = ? LIMIT 1",
                (self.subject_id, publisher_subject_id, series_id),
            ).fetchone()
            is not None
        ):
            raise IntegrityError("common knowledge version series has multiple roots")
        connection.execute(
            "INSERT INTO common_knowledge_versions(package_id, subject_id, "
            "publisher_subject_id, series_id, package_version, predecessor_package_id, "
            "state_hash, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                package_id,
                self.subject_id,
                publisher_subject_id,
                series_id,
                version,
                predecessor_package_id,
                content_hash(values),
                created_at,
            ),
        )

    def _append_sync_event(
        self,
        connection: Any,
        *,
        event_type: Literal["published", "revoked"],
        package_id: str,
        series_id: str,
        package_version: int,
        predecessor_package_id: str | None,
        envelope: dict[str, Any],
        occurred_at: str,
    ) -> dict[str, Any]:
        sequence = strict_int(
            connection.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 FROM common_knowledge_sync_events "
                "WHERE subject_id = ?",
                (self.subject_id,),
            ).fetchone()[0]
        )
        body = {
            "protocol": COMMON_KNOWLEDGE_SYNC_PROTOCOL,
            "event_id": new_id("knowledge-event"),
            "publisher_subject_id": self.subject_id,
            "sequence": sequence,
            "event_type": event_type,
            "package_id": package_id,
            "series_id": series_id,
            "package_version": package_version,
            "predecessor_package_id": predecessor_package_id,
            "envelope": envelope,
            "envelope_hash": content_hash(envelope),
            "key_id": self.key_id,
            "occurred_at": occurred_at,
        }
        event_hash = content_hash(body)
        signature = self._sign(body)
        connection.execute(
            "INSERT INTO common_knowledge_sync_events(event_id, subject_id, "
            "publisher_subject_id, sequence, event_type, package_id, key_id, event_json, "
            "event_hash, signature, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                body["event_id"],
                self.subject_id,
                self.subject_id,
                sequence,
                event_type,
                package_id,
                self.key_id,
                canonical_json(body),
                event_hash,
                signature,
                occurred_at,
            ),
        )
        return {**body, "event_hash": event_hash, "signature": signature}

    def _sync_event_from_row(self, row: Any) -> dict[str, Any]:
        try:
            body = strict_json_loads(row["event_json"])
        except (TypeError, ValueError) as error:
            raise IntegrityError("common knowledge sync event is invalid") from error
        if not isinstance(body, dict):
            raise IntegrityError("common knowledge sync event is invalid")
        if (
            body.get("event_id") != row["event_id"]
            or body.get("publisher_subject_id") != row["publisher_subject_id"]
            or body.get("sequence") != row["sequence"]
            or body.get("event_type") != row["event_type"]
            or body.get("package_id") != row["package_id"]
            or body.get("key_id") != row["key_id"]
            or body.get("occurred_at") != row["occurred_at"]
            or content_hash(body) != row["event_hash"]
        ):
            raise IntegrityError("common knowledge sync event metadata mismatch")
        self._verify(self.public_key, body, str(row["signature"]))
        return {**body, "event_hash": str(row["event_hash"]), "signature": str(row["signature"])}

    def _ensure_local_sync_state(self) -> None:
        with self.database.transaction() as connection:
            rows = connection.execute(
                "SELECT * FROM common_knowledge_packages WHERE publisher_subject_id = ? "
                "ORDER BY created_at, package_id",
                (self.subject_id,),
            ).fetchall()
            for row in rows:
                self._backfill_local_package(connection, self._record_from_row(row))

    def _backfill_local_package(
        self,
        connection: Any,
        package: CommonKnowledgePackage,
    ) -> None:
        version = connection.execute(
            "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
            (self.subject_id, package.package_id),
        ).fetchone()
        if version is None:
            self._insert_version(
                connection,
                package_id=package.package_id,
                publisher_subject_id=self.subject_id,
                series_id=package.package_id,
                version=package.version,
                predecessor_package_id=None,
                created_at=package.created_at,
            )
            version = connection.execute(
                "SELECT * FROM common_knowledge_versions WHERE subject_id = ? AND package_id = ?",
                (self.subject_id, package.package_id),
            ).fetchone()
        assert version is not None
        published = connection.execute(
            "SELECT 1 FROM common_knowledge_sync_events WHERE subject_id = ? "
            "AND package_id = ? AND event_type = 'published'",
            (self.subject_id, package.package_id),
        ).fetchone()
        if published is None:
            self._append_sync_event(
                connection,
                event_type="published",
                package_id=package.package_id,
                series_id=str(version["series_id"]),
                package_version=strict_int(version["package_version"]),
                predecessor_package_id=version["predecessor_package_id"],
                envelope=self._verified_envelope(package),
                occurred_at=package.created_at,
            )
        if package.status == "revoked":
            revoked = connection.execute(
                "SELECT 1 FROM common_knowledge_sync_events WHERE subject_id = ? "
                "AND package_id = ? AND event_type = 'revoked'",
                (self.subject_id, package.package_id),
            ).fetchone()
            if revoked is None:
                self._append_sync_event(
                    connection,
                    event_type="revoked",
                    package_id=package.package_id,
                    series_id=str(version["series_id"]),
                    package_version=strict_int(version["package_version"]),
                    predecessor_package_id=version["predecessor_package_id"],
                    envelope=self._verified_envelope(package),
                    occurred_at=package.revoked_at or package.created_at,
                )

    @staticmethod
    def _peer_state_values(row: Mapping[str, Any]) -> dict[str, Any]:
        fields = (
            "peer_id",
            "subject_id",
            "label",
            "endpoint",
            "publisher_subject_id",
            "key_id",
            "public_key",
            "status",
            "cursor",
            "remote_etag",
            "sync_interval_seconds",
            "next_sync_at",
            "last_sync_at",
            "last_error_code",
            "created_at",
            "updated_at",
        )
        return {field: row[field] for field in fields}

    def _peer_from_row(self, row: Any) -> CommonKnowledgePeerRecord:
        proposal = CommonKnowledgePeerInput(
            label=row["label"],
            endpoint=row["endpoint"],
            publisher_subject_id=row["publisher_subject_id"],
            public_key=row["public_key"],
            sync_interval_seconds=row["sync_interval_seconds"],
        )
        cursor = strict_int(row["cursor"])
        if (
            row["subject_id"] != self.subject_id
            or row["status"] not in {"active", "disabled"}
            or cursor < 0
            or content_hash(proposal.public_key)[:32] != row["key_id"]
            or row["state_hash"] != content_hash(self._peer_state_values(row))
        ):
            raise IntegrityError("common knowledge peer state is invalid")
        return CommonKnowledgePeerRecord(
            str(row["peer_id"]),
            self.subject_id,
            proposal.label,
            proposal.endpoint,
            proposal.publisher_subject_id,
            str(row["key_id"]),
            str(row["status"]),
            cursor,
            row["remote_etag"],
            proposal.sync_interval_seconds,
            str(row["next_sync_at"]),
            row["last_sync_at"],
            row["last_error_code"],
            str(row["created_at"]),
            str(row["updated_at"]),
        )

    def _verify_discovery(self, peer: Any, document: Mapping[str, Any]) -> tuple[int, str]:
        fields = (
            "protocol",
            "publisher_subject_id",
            "key_id",
            "public_key",
            "latest_sequence",
            "latest_event_hash",
            "etag",
        )
        if not all(field in document for field in (*fields, "signature")):
            raise IntegrityError("common knowledge discovery document is incomplete")
        body = {field: document[field] for field in fields}
        latest_sequence = strict_int(body["latest_sequence"])
        if (
            body["protocol"] != COMMON_KNOWLEDGE_SYNC_PROTOCOL
            or body["publisher_subject_id"] != peer["publisher_subject_id"]
            or body["key_id"] != peer["key_id"]
            or body["public_key"] != peer["public_key"]
            or latest_sequence < 0
            or not isinstance(body["latest_event_hash"], str)
        ):
            raise IntegrityError("common knowledge discovery identity mismatch")
        expected_etag = content_hash(
            {
                "protocol": COMMON_KNOWLEDGE_SYNC_PROTOCOL,
                "publisher_subject_id": peer["publisher_subject_id"],
                "key_id": peer["key_id"],
                "latest_sequence": latest_sequence,
                "latest_event_hash": body["latest_event_hash"],
            }
        )
        if body["etag"] != expected_etag:
            raise IntegrityError("common knowledge discovery etag mismatch")
        self._verify(str(peer["public_key"]), body, str(document["signature"]))
        return latest_sequence, expected_etag

    def _verify_feed_page(
        self,
        peer: Any,
        document: Mapping[str, Any],
        *,
        cursor: int,
        expected_etag: str,
    ) -> list[dict[str, Any]]:
        fields = (
            "protocol",
            "publisher_subject_id",
            "key_id",
            "cursor",
            "next_cursor",
            "has_more",
            "etag",
            "events",
        )
        if not all(field in document for field in (*fields, "signature")):
            raise IntegrityError("common knowledge feed document is incomplete")
        body = {field: document[field] for field in fields}
        events = body["events"]
        if (
            body["protocol"] != COMMON_KNOWLEDGE_SYNC_PROTOCOL
            or body["publisher_subject_id"] != peer["publisher_subject_id"]
            or body["key_id"] != peer["key_id"]
            or strict_int(body["cursor"]) != cursor
            or body["etag"] != expected_etag
            or not isinstance(body["has_more"], bool)
            or not isinstance(events, list)
            or len(events) > 100
            or any(not isinstance(event, dict) for event in events)
        ):
            raise IntegrityError("common knowledge feed metadata mismatch")
        next_cursor = strict_int(body["next_cursor"])
        expected_next = cursor if not events else strict_int(events[-1].get("sequence"))
        if next_cursor != expected_next or next_cursor < cursor:
            raise IntegrityError("common knowledge feed cursor mismatch")
        self._verify(str(peer["public_key"]), body, str(document["signature"]))
        return events

    def _verify_sync_event(
        self,
        peer: Any,
        event: Mapping[str, Any],
        *,
        expected: int,
    ) -> dict[str, Any]:
        if not all(field in event for field in (*_SYNC_EVENT_FIELDS, "event_hash", "signature")):
            raise IntegrityError("common knowledge sync event is incomplete")
        body = {field: event[field] for field in _SYNC_EVENT_FIELDS}
        sequence = strict_int(body["sequence"])
        envelope = body["envelope"]
        if (
            body["protocol"] != COMMON_KNOWLEDGE_SYNC_PROTOCOL
            or body["publisher_subject_id"] != peer["publisher_subject_id"]
            or body["key_id"] != peer["key_id"]
            or sequence != expected
            or body["event_type"] not in {"published", "revoked"}
            or not isinstance(body["event_id"], str)
            or not isinstance(body["package_id"], str)
            or not isinstance(body["series_id"], str)
            or not isinstance(envelope, dict)
            or content_hash(envelope) != body["envelope_hash"]
            or content_hash(body) != event["event_hash"]
        ):
            raise IntegrityError("common knowledge sync event metadata mismatch")
        _, package_body, _, _, _ = self._normalize_envelope(envelope)
        if (
            package_body["publisher_subject_id"] != peer["publisher_subject_id"]
            or package_body["key_id"] != peer["key_id"]
            or package_body["package_id"] != body["package_id"]
            or package_body["version"] != strict_int(body["package_version"])
        ):
            raise IntegrityError("common knowledge sync event package mismatch")
        predecessor = body["predecessor_package_id"]
        if predecessor is not None and not isinstance(predecessor, str):
            raise IntegrityError("common knowledge sync event predecessor is invalid")
        self._verify(
            str(peer["public_key"]),
            package_body,
            str(envelope["signature"]),
        )
        self._verify(str(peer["public_key"]), body, str(event["signature"]))
        return {
            **body,
            "event_hash": str(event["event_hash"]),
            "signature": str(event["signature"]),
        }

    def _apply_remote_event(self, peer: Any, event: Mapping[str, Any], *, expected: int) -> int:
        verified = self._verify_sync_event(peer, event, expected=expected)
        envelope = dict(verified["envelope"])
        package_id = str(verified["package_id"])
        if verified["event_type"] == "published":
            self.import_package(envelope)
        else:
            try:
                existing = self.get(package_id)
            except NotFoundError:
                self.import_package(envelope)
            else:
                if self._verified_envelope(existing) != envelope:
                    raise IntegrityError("common knowledge revocation envelope mismatch")
        now = self.clock()
        with self.database.transaction() as connection:
            self._insert_version(
                connection,
                package_id=package_id,
                publisher_subject_id=str(verified["publisher_subject_id"]),
                series_id=str(verified["series_id"]),
                version=strict_int(verified["package_version"]),
                predecessor_package_id=verified["predecessor_package_id"],
                created_at=str(envelope["created_at"]),
            )
            if verified["event_type"] == "published":
                # A signed successor supersedes accepted guidance as soon as
                # it reaches the reader.  The successor itself remains
                # quarantined until the subject explicitly evaluates it.
                connection.execute(
                    "UPDATE common_knowledge_imports SET status = 'revoked', reason = ? "
                    "WHERE subject_id = ? AND status = 'accepted' AND package_id IN ("
                    "SELECT package_id FROM common_knowledge_versions "
                    "WHERE subject_id = ? AND series_id = ? AND package_version < ?)",
                    (
                        f"superseded by synchronized package {package_id}",
                        self.subject_id,
                        self.subject_id,
                        verified["series_id"],
                        verified["package_version"],
                    ),
                )
            if verified["event_type"] == "revoked":
                updated = connection.execute(
                    "UPDATE common_knowledge_packages SET status = 'revoked', revoked_at = ? "
                    "WHERE package_id = ? AND publisher_subject_id = ? "
                    "AND key_id = ? AND status IN ('published', 'superseded')",
                    (
                        verified["occurred_at"],
                        package_id,
                        peer["publisher_subject_id"],
                        peer["key_id"],
                    ),
                )
                if updated.rowcount not in {0, 1}:
                    raise IntegrityError("common knowledge revocation update is invalid")
                connection.execute(
                    "UPDATE common_knowledge_imports SET status = 'revoked', reason = ? "
                    "WHERE package_id = ? AND subject_id = ? "
                    "AND status IN ('quarantined', 'accepted')",
                    ("publisher revocation synchronized", package_id, self.subject_id),
                )
            raw_event = {**verified}
            existing_event = connection.execute(
                "SELECT event_hash FROM common_knowledge_remote_events "
                "WHERE peer_id = ? AND event_id = ?",
                (peer["peer_id"], verified["event_id"]),
            ).fetchone()
            if existing_event is None:
                connection.execute(
                    "INSERT INTO common_knowledge_remote_events(peer_id, event_id, subject_id, "
                    "publisher_subject_id, sequence, event_type, package_id, event_json, "
                    "event_hash, signature, received_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        peer["peer_id"],
                        verified["event_id"],
                        self.subject_id,
                        verified["publisher_subject_id"],
                        verified["sequence"],
                        verified["event_type"],
                        package_id,
                        canonical_json(raw_event),
                        verified["event_hash"],
                        verified["signature"],
                        now,
                    ),
                )
            elif existing_event["event_hash"] != verified["event_hash"]:
                raise IntegrityError("common knowledge remote event replay mismatch")
            self._update_peer_connection(
                connection,
                str(peer["peer_id"]),
                cursor=strict_int(verified["sequence"]),
                updated_at=now,
            )
        return strict_int(verified["sequence"])

    def _update_peer_connection(
        self,
        connection: Any,
        peer_id: str,
        **changes: Any,
    ) -> None:
        row = connection.execute(
            "SELECT * FROM common_knowledge_peers WHERE peer_id = ? AND subject_id = ?",
            (peer_id, self.subject_id),
        ).fetchone()
        if row is None:
            raise NotFoundError(f"common knowledge peer not found: {peer_id}")
        values = self._peer_state_values(row)
        values.update(changes)
        assignments = (
            "cursor = ?, remote_etag = ?, next_sync_at = ?, last_sync_at = ?, "
            "last_error_code = ?, updated_at = ?, state_hash = ?"
        )
        connection.execute(
            f"UPDATE common_knowledge_peers SET {assignments} WHERE peer_id = ?",
            (
                values["cursor"],
                values["remote_etag"],
                values["next_sync_at"],
                values["last_sync_at"],
                values["last_error_code"],
                values["updated_at"],
                content_hash(values),
                peer_id,
            ),
        )

    def _finish_peer_sync(
        self,
        result: CommonKnowledgeSyncResult,
        *,
        error_code: str | None,
    ) -> None:
        now = self.clock()
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM common_knowledge_peers WHERE peer_id = ? AND subject_id = ?",
                (result.peer_id, self.subject_id),
            ).fetchone()
            if row is None:
                raise NotFoundError(f"common knowledge peer not found: {result.peer_id}")
            interval = strict_int(row["sync_interval_seconds"])
            delay = interval if error_code is None else min(300, interval)
            next_sync = (datetime.fromisoformat(now) + timedelta(seconds=delay)).isoformat(
                timespec="milliseconds"
            )
            self._update_peer_connection(
                connection,
                result.peer_id,
                cursor=result.cursor_after,
                remote_etag=result.remote_etag,
                next_sync_at=next_sync,
                last_sync_at=now,
                last_error_code=error_code,
                updated_at=now,
            )
            values = {
                "sync_id": new_id("knowledge-sync"),
                "peer_id": result.peer_id,
                "subject_id": self.subject_id,
                "outcome": result.outcome,
                "cursor_before": result.cursor_before,
                "cursor_after": result.cursor_after,
                "discovered": result.discovered,
                "imported": result.imported,
                "revoked": result.revoked,
                "error_code": error_code,
                "occurred_at": now,
            }
            connection.execute(
                "INSERT INTO common_knowledge_sync_runs(sync_id, peer_id, subject_id, outcome, "
                "cursor_before, cursor_after, discovered, imported, revoked, error_code, "
                "state_hash, occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    values["sync_id"],
                    result.peer_id,
                    self.subject_id,
                    result.outcome,
                    result.cursor_before,
                    result.cursor_after,
                    result.discovered,
                    result.imported,
                    result.revoked,
                    error_code,
                    content_hash(values),
                    now,
                ),
            )

    def _insert_evaluation_event(
        self,
        connection: Any,
        *,
        evaluation_id: str,
        package_id: str,
        sequence: int,
        status: str,
        envelope_hash: str,
        reason: str,
        occurred_at: str,
    ) -> None:
        values = {
            "evaluation_event_id": new_id("knowledge-evaluation-event"),
            "evaluation_id": evaluation_id,
            "package_id": package_id,
            "subject_id": self.subject_id,
            "sequence": sequence,
            "status": status,
            "envelope_hash": envelope_hash,
            "reason": reason,
            "occurred_at": occurred_at,
        }
        connection.execute(
            "INSERT INTO common_knowledge_evaluation_events(evaluation_event_id, evaluation_id, "
            "package_id, subject_id, sequence, status, envelope_hash, reason, state_hash, "
            "occurred_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                values["evaluation_event_id"],
                evaluation_id,
                package_id,
                self.subject_id,
                sequence,
                status,
                envelope_hash,
                reason,
                content_hash(values),
                occurred_at,
            ),
        )

    @classmethod
    def _normalize_envelope(
        cls, envelope: Mapping[str, Any]
    ) -> tuple[
        CommonKnowledgeProposal,
        dict[str, Any],
        dict[str, Any],
        str,
        str,
    ]:
        required = {*_SIGNED_FIELDS, "signature"}
        if set(envelope) != required:
            missing = sorted(required - set(envelope))
            extra = sorted(set(envelope) - required)
            raise ValueError(
                "common knowledge envelope fields are not exact"
                + (f"; missing={missing}" if missing else "")
                + (f"; extra={extra}" if extra else "")
            )
        for name in (
            "package_id",
            "title",
            "summary",
            "payload_hash",
            "signature",
            "key_id",
            "created_at",
        ):
            if not isinstance(envelope[name], str) or not str(envelope[name]).strip():
                raise ValueError(f"common knowledge envelope {name} is invalid")
        if not isinstance(envelope["publisher_subject_id"], str):
            raise ValueError("common knowledge envelope publisher_subject_id is invalid")
        try:
            validate_subject_id(str(envelope["publisher_subject_id"]))
        except ValueError as error:
            raise ValueError("common knowledge envelope publisher_subject_id is invalid") from error
        proposal = CommonKnowledgeProposal(
            scope=envelope["scope"],
            title=envelope["title"],
            summary=envelope["summary"],
            payload=envelope["payload"],
            version=envelope["version"],
        )
        payload_hash = content_hash(proposal.payload)
        if payload_hash != envelope["payload_hash"]:
            raise IntegrityError("common knowledge payload hash mismatch")
        body = {
            "package_id": str(envelope["package_id"]),
            "publisher_subject_id": str(envelope["publisher_subject_id"]),
            "scope": proposal.scope,
            "title": proposal.title,
            "summary": proposal.summary,
            "payload": proposal.payload,
            "payload_hash": payload_hash,
            "key_id": str(envelope["key_id"]),
            "version": proposal.version,
            "created_at": str(envelope["created_at"]),
        }
        verified_envelope = {**body, "signature": str(envelope["signature"])}
        envelope_json = canonical_json(verified_envelope)
        return proposal, body, verified_envelope, envelope_json, content_hash(verified_envelope)

    @staticmethod
    def _signed_body(record: CommonKnowledgePackage) -> dict[str, Any]:
        return {
            "package_id": record.package_id,
            "publisher_subject_id": record.publisher_subject_id,
            "scope": record.scope,
            "title": record.title,
            "summary": record.summary,
            "payload": record.payload,
            "payload_hash": record.payload_hash,
            "key_id": record.key_id,
            "version": record.version,
            "created_at": record.created_at,
        }

    @classmethod
    def _verified_envelope(cls, record: CommonKnowledgePackage) -> dict[str, Any]:
        return {**cls._signed_body(record), "signature": record.signature}

    @staticmethod
    def _record_from_row(row: Any) -> CommonKnowledgePackage:
        if not isinstance(row["payload_json"], str):
            raise IntegrityError("common knowledge package payload is invalid")
        try:
            payload = strict_json_loads(row["payload_json"])
            proposal = CommonKnowledgeProposal(
                scope=row["scope"],
                title=row["title"],
                summary=row["summary"],
                payload=payload,
                version=row["version"],
            )
        except (TypeError, ValueError) as error:
            raise IntegrityError("common knowledge package payload is invalid") from error
        if not isinstance(payload, dict) or content_hash(payload) != row["payload_hash"]:
            raise IntegrityError("common knowledge package payload is invalid")
        return CommonKnowledgePackage(
            str(row["package_id"]),
            str(row["publisher_subject_id"]),
            proposal.scope,
            proposal.title,
            proposal.summary,
            payload,
            str(row["payload_hash"]),
            str(row["signature"]),
            str(row["key_id"]),
            proposal.version,
            str(row["status"]),
            str(row["created_at"]),
            row["revoked_at"],
        )

    def _verify_record(
        self,
        connection: Any,
        record: CommonKnowledgePackage,
        *,
        require_compatible: bool,
        allow_untrusted: bool,
    ) -> bool:
        if record.publisher_subject_id == self.subject_id and record.key_id == self.key_id:
            public_key = self.public_key
            if not public_key:
                raise IntegrityError("common knowledge signing key is unavailable")
        else:
            key = connection.execute(
                "SELECT public_key, status FROM common_knowledge_trusted_keys WHERE key_id = ?",
                (record.key_id,),
            ).fetchone()
            if key is None or key["status"] != "active":
                if allow_untrusted:
                    return False
                raise PermissionError("common knowledge publisher key is not trusted")
            public_key = str(key["public_key"])
        if content_hash(public_key)[:32] != record.key_id:
            raise IntegrityError("common knowledge trusted key id mismatch")
        self._verify(public_key, self._signed_body(record), record.signature)

        imported = connection.execute(
            "SELECT * FROM common_knowledge_imports WHERE package_id = ? AND subject_id = ?",
            (record.package_id, self.subject_id),
        ).fetchone()
        if imported is not None:
            provenance = connection.execute(
                "SELECT * FROM common_knowledge_import_provenance WHERE import_id = ?",
                (imported["import_id"],),
            ).fetchone()
            self._verify_provenance(record, imported, provenance)
        return not require_compatible or self._is_compatible(record)

    def _verify_provenance(
        self, record: CommonKnowledgePackage, imported: Any, provenance: Any
    ) -> None:
        if provenance is None:
            raise IntegrityError("common knowledge import provenance is missing")
        expected = self._verified_envelope(record)
        expected_json = canonical_json(expected)
        try:
            stored = strict_json_loads(provenance["envelope_json"])
        except (TypeError, ValueError) as error:
            raise IntegrityError("common knowledge import envelope is invalid") from error
        if (
            imported["package_id"] != record.package_id
            or imported["subject_id"] != self.subject_id
            or provenance["package_id"] != record.package_id
            or provenance["subject_id"] != self.subject_id
            or stored != expected
            or provenance["envelope_json"] != expected_json
            or provenance["envelope_hash"] != content_hash(expected)
            or provenance["payload_hash"] != record.payload_hash
            or provenance["signer_key_id"] != record.key_id
            or provenance["signature"] != record.signature
        ):
            raise IntegrityError("common knowledge import provenance mismatch")

    def _is_compatible(self, record: CommonKnowledgePackage) -> bool:
        requirements = record.payload.get("compatibility")
        if requirements and not any(
            str(requirement).strip().casefold() in self.compatibility
            for requirement in requirements
        ):
            return False
        valid_until = record.payload.get("valid_until")
        if valid_until is not None:
            expires = datetime.fromisoformat(str(valid_until)).astimezone(UTC)
            now = datetime.fromisoformat(self.clock())
            if now.tzinfo is None:
                raise IntegrityError("common knowledge clock requires a timezone")
            if expires <= now.astimezone(UTC):
                return False
        return True

    def _load_or_create_key(self, *, allow_create: bool) -> Ed25519PrivateKey | None:
        path = self.key_dir / "common-knowledge-ed25519.key"
        if path.exists():
            raw = path.read_bytes()
            if len(raw) != 32:
                raise IntegrityError("common knowledge signing key is invalid")
            return Ed25519PrivateKey.from_private_bytes(raw)
        if not allow_create:
            return None
        key = Ed25519PrivateKey.generate()
        raw = key.private_bytes(
            serialization.Encoding.Raw,
            serialization.PrivateFormat.Raw,
            serialization.NoEncryption(),
        )
        temporary = path.with_name(f".{path.name}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        descriptor = os.open(temporary, flags, 0o600)
        try:
            try:
                view = memoryview(raw)
                while view:
                    written = os.write(descriptor, view)
                    if written <= 0:
                        raise OSError("common knowledge key write made no progress")
                    view = view[written:]
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            temporary.replace(path)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
        return key

    def _sign(self, body: dict[str, Any]) -> str:
        if self._private_key is None:
            raise IntegrityError("common knowledge signing key is unavailable")
        signature = self._private_key.sign(canonical_json(body).encode("utf-8"))
        return base64.urlsafe_b64encode(signature).decode("ascii")

    @classmethod
    def _verify(cls, public_key: str, body: dict[str, Any], signature: str) -> None:
        try:
            cls._parse_public_key(public_key).verify(
                base64.urlsafe_b64decode(signature), canonical_json(body).encode("utf-8")
            )
        except Exception as error:
            raise IntegrityError("common knowledge signature is invalid") from error

    @staticmethod
    def _public_key_text(key: Ed25519PublicKey) -> str:
        raw = key.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        return base64.urlsafe_b64encode(raw).decode("ascii")

    @staticmethod
    def _parse_public_key(value: str) -> Ed25519PublicKey:
        try:
            raw = base64.urlsafe_b64decode(value)
            if len(raw) != 32:
                raise ValueError("wrong key length")
            return Ed25519PublicKey.from_public_bytes(raw)
        except Exception as error:
            raise ValueError("common knowledge public key is invalid") from error
