"""Persistent target enrollment and one-time challenge consumption."""

# SQL and compact record projections remain easier to audit in this module.
# ruff: noqa: E501

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from noyra.core.database import Database
from noyra.core.errors import NotFoundError
from noyra.core.identity import validate_subject_id
from noyra.core.types import canonical_json, content_hash, utc_now

from .policy import MigrationStore
from .trust import TargetAttestation, TargetChallenge, TrustEvidence


@dataclass(frozen=True)
class TargetRegistration:
    target_id: str
    subject_id: str
    public_key: str
    key_fingerprint: str
    recipient_public_key: str | None
    recipient_key_fingerprint: str | None
    enrollment_generation: int
    endpoint: str
    capabilities: dict[str, Any]
    region: str | None
    provider: str | None
    release_sha: str
    os_arch: str
    encrypted_volume: bool
    status: str
    attested_at: str | None
    attestation_epoch: str | None
    created_at: str
    updated_at: str


class TargetRegistry:
    def __init__(self, database: Database, policy_store: MigrationStore):
        self.database = database
        self.policy_store = policy_store

    def register(
        self,
        subject_id: str,
        *,
        target_id: str,
        public_key: str,
        recipient_public_key: str | None = None,
        endpoint: str,
        capabilities: Mapping[str, Any],
        region: str | None,
        provider: str | None,
        release_sha: str,
        os_arch: str,
        encrypted_volume: bool,
        actor: str,
    ) -> TargetRegistration:
        validate_subject_id(subject_id)
        self._validate_id(target_id)
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ValueError("target actor is invalid")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError("target endpoint must be HTTPS")
        if len(parsed.path) > 256 or parsed.query or parsed.fragment:
            raise ValueError("target endpoint must be an HTTPS origin or fixed agent path")
        public_bytes = self._decode_public_key(public_key)
        recipient_bytes = self._decode_recipient_public_key(recipient_public_key)
        if type(encrypted_volume) is not bool or not encrypted_volume:
            raise ValueError("target volume must be encrypted")
        if (
            not isinstance(release_sha, str)
            or len(release_sha) not in {40, 64}
            or any(character not in "0123456789abcdefABCDEF" for character in release_sha)
        ):
            raise ValueError("target release SHA is invalid")
        if not isinstance(os_arch, str) or not 1 <= len(os_arch) <= 64:
            raise ValueError("target OS architecture is invalid")
        if not isinstance(capabilities, Mapping) or len(capabilities) > 64:
            raise ValueError("target capabilities are invalid")
        capabilities_json = canonical_json(dict(capabilities))
        if len(capabilities_json.encode("utf-8")) > 16_384:
            raise ValueError("target capabilities are too large")
        fingerprint = hashlib.sha256(public_bytes).hexdigest()
        recipient_fingerprint = (
            hashlib.sha256(recipient_bytes).hexdigest() if recipient_bytes is not None else None
        )
        now = utc_now()
        with self.database.transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM subject_identity WHERE subject_id=?", (subject_id,)
                ).fetchone()
                is None
            ):
                raise NotFoundError(f"subject not found: {subject_id}")
            if (
                connection.execute(
                    "SELECT 1 FROM migration_targets WHERE target_id=?", (target_id,)
                ).fetchone()
                is not None
            ):
                raise ValueError("target id is already registered")
            connection.execute(
                """INSERT INTO migration_targets(
                   target_id, subject_id, public_key, key_fingerprint,
                   recipient_public_key, recipient_key_fingerprint, enrollment_generation,
                   endpoint, capabilities_json, region, provider, release_sha, os_arch,
                   encrypted_volume, status, created_at, updated_at, state_hash
                ) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,1,'pending',?,?,?)""",
                (
                    target_id,
                    subject_id,
                    public_key,
                    fingerprint,
                    recipient_public_key,
                    recipient_fingerprint,
                    endpoint,
                    capabilities_json,
                    region,
                    provider,
                    release_sha.lower(),
                    os_arch,
                    now,
                    now,
                    self._state_hash(
                        target_id=target_id,
                        subject_id=subject_id,
                        public_key=public_key,
                        key_fingerprint=fingerprint,
                        recipient_public_key=recipient_public_key,
                        recipient_key_fingerprint=recipient_fingerprint,
                        enrollment_generation=1,
                        endpoint=endpoint,
                        capabilities_json=capabilities_json,
                        region=region,
                        provider=provider,
                        release_sha=release_sha.lower(),
                        os_arch=os_arch,
                        encrypted_volume=True,
                        status="pending",
                        created_at=now,
                        updated_at=now,
                        attested_at=None,
                        attestation_epoch=None,
                        revoked_at=None,
                        revoke_reason=None,
                    ),
                ),
            )
            MigrationStore._append_audit(
                connection,
                subject_id,
                "migration_target_registered",
                actor.strip(),
                {
                    "target_id": target_id,
                    "key_fingerprint": fingerprint,
                    "recipient_key_fingerprint": recipient_fingerprint,
                    "endpoint": endpoint,
                },
            )
            return self._load(connection, target_id)

    def issue_challenge(self, target_id: str, *, source_epoch: str) -> TargetChallenge:
        if not source_epoch or len(source_epoch) > 128:
            raise ValueError("source epoch is invalid")
        expires = (datetime.now(UTC) + timedelta(minutes=5)).isoformat(timespec="milliseconds")
        nonce = secrets.token_urlsafe(32)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=?", (target_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration target not found: {target_id}")
            self._assert_row_integrity(row)
            if row["status"] not in {"pending", "active"}:
                raise ValueError("target is revoked or quarantined")
            connection.execute(
                "INSERT INTO migration_target_challenges(nonce,target_id,source_epoch,expires_at,issued_at) VALUES (?,?,?,?,?)",
                (nonce, target_id, source_epoch, expires, utc_now()),
            )
            target_subject = connection.execute(
                "SELECT subject_id FROM migration_targets WHERE target_id=?", (target_id,)
            ).fetchone()["subject_id"]
            MigrationStore._append_audit(
                connection,
                target_subject,
                "migration_target_challenge_issued",
                "system",
                {"target_id": target_id, "source_epoch": source_epoch, "expires_at": expires},
            )
        return TargetChallenge(nonce=nonce, expires_at=expires, source_epoch=source_epoch)

    def attest(
        self, target_id: str, challenge: TargetChallenge, signature: str, *, actor: str
    ) -> TrustEvidence:
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ValueError("target attestation actor is invalid")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=?", (target_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration target not found: {target_id}")
            self._assert_row_integrity(row)
            if row["status"] not in {"pending", "active"}:
                raise ValueError("target is revoked or quarantined")
            stored = connection.execute(
                "SELECT * FROM migration_target_challenges WHERE nonce=? AND target_id=?",
                (challenge.nonce, target_id),
            ).fetchone()
            if stored is None or stored["consumed_at"] is not None:
                raise ValueError("challenge nonce was already consumed or is unknown")
            if (
                stored["expires_at"] != challenge.expires_at
                or stored["source_epoch"] != challenge.source_epoch
            ):
                raise ValueError("challenge does not match issued nonce")
            evidence = TargetAttestation(
                target_id=target_id,
                public_key=row["public_key"],
                challenge=challenge,
                signature=signature,
            ).verify()
            connection.execute(
                "UPDATE migration_target_challenges SET consumed_at=? WHERE nonce=? AND consumed_at IS NULL",
                (utc_now(), challenge.nonce),
            )
            attested_at = evidence.verified_at
            connection.execute(
                "UPDATE migration_targets SET status='active',attested_at=?,attestation_epoch=?,updated_at=?,state_hash=? WHERE target_id=? AND status IN ('pending','active')",
                (
                    attested_at,
                    challenge.source_epoch,
                    attested_at,
                    self._state_hash(
                        target_id=row["target_id"],
                        subject_id=row["subject_id"],
                        public_key=row["public_key"],
                        key_fingerprint=row["key_fingerprint"],
                        recipient_public_key=row["recipient_public_key"],
                        recipient_key_fingerprint=row["recipient_key_fingerprint"],
                        enrollment_generation=int(row["enrollment_generation"]),
                        endpoint=row["endpoint"],
                        capabilities_json=row["capabilities_json"],
                        region=row["region"],
                        provider=row["provider"],
                        release_sha=row["release_sha"],
                        os_arch=row["os_arch"],
                        encrypted_volume=bool(row["encrypted_volume"]),
                        status="active",
                        created_at=row["created_at"],
                        updated_at=attested_at,
                        attested_at=attested_at,
                        attestation_epoch=challenge.source_epoch,
                        revoked_at=row["revoked_at"],
                        revoke_reason=row["revoke_reason"],
                    ),
                    target_id,
                ),
            )
            MigrationStore._append_audit(
                connection,
                row["subject_id"],
                "migration_target_attested",
                actor.strip(),
                {
                    "target_id": target_id,
                    "key_fingerprint": evidence.key_fingerprint,
                    "source_epoch": challenge.source_epoch,
                },
            )
            return evidence

    def revoke(self, target_id: str, *, reason: str, actor: str) -> None:
        if not reason.strip() or len(reason) > 512:
            raise ValueError("target revoke reason is invalid")
        if not isinstance(actor, str) or not actor.strip() or len(actor) > 128:
            raise ValueError("target revoke actor is invalid")
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=?", (target_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError(f"migration target not found: {target_id}")
            self._assert_row_integrity(row)
            if row["status"] == "revoked":
                return
            now = utc_now()
            connection.execute(
                "UPDATE migration_targets SET status='revoked', revoked_at=?, revoke_reason=?, updated_at=?,state_hash=? WHERE target_id=?",
                (
                    now,
                    reason.strip(),
                    now,
                    self._state_hash(
                        target_id=row["target_id"],
                        subject_id=row["subject_id"],
                        public_key=row["public_key"],
                        key_fingerprint=row["key_fingerprint"],
                        recipient_public_key=row["recipient_public_key"],
                        recipient_key_fingerprint=row["recipient_key_fingerprint"],
                        enrollment_generation=int(row["enrollment_generation"]),
                        endpoint=row["endpoint"],
                        capabilities_json=row["capabilities_json"],
                        region=row["region"],
                        provider=row["provider"],
                        release_sha=row["release_sha"],
                        os_arch=row["os_arch"],
                        encrypted_volume=bool(row["encrypted_volume"]),
                        status="revoked",
                        created_at=row["created_at"],
                        updated_at=now,
                        attested_at=row["attested_at"],
                        attestation_epoch=row["attestation_epoch"],
                        revoked_at=now,
                        revoke_reason=reason.strip(),
                    ),
                    target_id,
                ),
            )
            MigrationStore._append_audit(
                connection,
                row["subject_id"],
                "migration_target_revoked",
                actor.strip(),
                {"target_id": target_id, "reason": reason.strip()},
            )

    @staticmethod
    def _validate_id(target_id: str) -> None:
        if (
            not isinstance(target_id, str)
            or not 3 <= len(target_id) <= 128
            or any(
                character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
                for character in target_id
            )
        ):
            raise ValueError("target id is invalid")

    def assert_integrity(self, target_id: str) -> None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=?", (target_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError(f"migration target not found: {target_id}")
        self._assert_row_integrity(row)

    @staticmethod
    def _decode_public_key(public_key: str) -> bytes:
        try:
            raw = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
        except (ValueError, TypeError) as error:
            raise ValueError("target public key is invalid") from error
        if len(raw) != 32:
            raise ValueError("target public key must contain a 32-byte Ed25519 key")
        return raw

    @staticmethod
    def _decode_recipient_public_key(public_key: str | None) -> bytes | None:
        # Pre-v79 rows may be read, but they cannot pass migration execution.
        if public_key is None:
            return None
        if not isinstance(public_key, str) or not public_key:
            raise ValueError("recipient public key is invalid")
        try:
            encoded = public_key.encode("ascii")
            raw = base64.b64decode(
                encoded + b"=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
        except (ValueError, TypeError, UnicodeEncodeError, binascii.Error) as error:
            raise ValueError("recipient public key is invalid") from error
        canonical = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
        if len(raw) != 32 or canonical != public_key:
            raise ValueError("recipient public key must contain a canonical 32-byte X25519 key")
        return raw

    @staticmethod
    def _load(connection: Any, target_id: str) -> TargetRegistration:
        row = connection.execute(
            "SELECT * FROM migration_targets WHERE target_id=?", (target_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError(f"migration target not found: {target_id}")
        return TargetRegistration(
            target_id=row["target_id"],
            subject_id=row["subject_id"],
            public_key=row["public_key"],
            key_fingerprint=row["key_fingerprint"],
            recipient_public_key=row["recipient_public_key"],
            recipient_key_fingerprint=row["recipient_key_fingerprint"],
            enrollment_generation=int(row["enrollment_generation"]),
            endpoint=row["endpoint"],
            capabilities=json.loads(row["capabilities_json"]),
            region=row["region"],
            provider=row["provider"],
            release_sha=row["release_sha"],
            os_arch=row["os_arch"],
            encrypted_volume=bool(row["encrypted_volume"]),
            status=row["status"],
            attested_at=row["attested_at"],
            attestation_epoch=row["attestation_epoch"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _state_hash(**values: Any) -> str:
        return content_hash(
            {
                "target_id": values["target_id"],
                "subject_id": values["subject_id"],
                "public_key": values["public_key"],
                "key_fingerprint": values["key_fingerprint"],
                "recipient_public_key": values["recipient_public_key"],
                "recipient_key_fingerprint": values["recipient_key_fingerprint"],
                "enrollment_generation": int(values["enrollment_generation"]),
                "endpoint": values["endpoint"],
                "capabilities_json": values["capabilities_json"],
                "region": values["region"],
                "provider": values["provider"],
                "release_sha": values["release_sha"],
                "os_arch": values["os_arch"],
                "encrypted_volume": bool(values["encrypted_volume"]),
                "status": values["status"],
                "created_at": values["created_at"],
                "updated_at": values["updated_at"],
                "attested_at": values["attested_at"],
                "attestation_epoch": values["attestation_epoch"],
                "revoked_at": values["revoked_at"],
                "revoke_reason": values["revoke_reason"],
            }
        )

    @classmethod
    def _state_hash_from_row(cls, row: Any) -> str:
        return cls._state_hash(
            target_id=row["target_id"],
            subject_id=row["subject_id"],
            public_key=row["public_key"],
            key_fingerprint=row["key_fingerprint"],
            recipient_public_key=row["recipient_public_key"],
            recipient_key_fingerprint=row["recipient_key_fingerprint"],
            enrollment_generation=int(row["enrollment_generation"]),
            endpoint=row["endpoint"],
            capabilities_json=row["capabilities_json"],
            region=row["region"],
            provider=row["provider"],
            release_sha=row["release_sha"],
            os_arch=row["os_arch"],
            encrypted_volume=bool(row["encrypted_volume"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            attested_at=row["attested_at"],
            attestation_epoch=row["attestation_epoch"],
            revoked_at=row["revoked_at"],
            revoke_reason=row["revoke_reason"],
        )

    @classmethod
    def _legacy_state_hash_from_row(cls, row: Any) -> str:
        return content_hash(
            {
                "target_id": row["target_id"],
                "subject_id": row["subject_id"],
                "key_fingerprint": row["key_fingerprint"],
                "enrollment_generation": int(row["enrollment_generation"]),
                "endpoint": row["endpoint"],
                "capabilities_json": row["capabilities_json"],
                "region": row["region"],
                "provider": row["provider"],
                "release_sha": row["release_sha"],
                "os_arch": row["os_arch"],
                "encrypted_volume": bool(row["encrypted_volume"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "attested_at": row["attested_at"],
                "attestation_epoch": row["attestation_epoch"],
            }
        )

    @classmethod
    def _assert_row_integrity(cls, row: Any) -> None:
        public_key = str(row["public_key"])
        try:
            decoded = base64.urlsafe_b64decode(public_key + "=" * (-len(public_key) % 4))
        except (ValueError, TypeError) as error:
            raise ValueError("migration target integrity check failed") from error
        if hashlib.sha256(decoded).hexdigest() != row["key_fingerprint"]:
            raise ValueError("migration target integrity check failed")
        recipient_public = row["recipient_public_key"]
        recipient_fingerprint = row["recipient_key_fingerprint"]
        if recipient_public is None or recipient_fingerprint is None:
            if recipient_public is not None or recipient_fingerprint is not None:
                raise ValueError("migration target integrity check failed")
        else:
            recipient_decoded = cls._decode_recipient_public_key(str(recipient_public))
            if (
                recipient_decoded is None
                or hashlib.sha256(recipient_decoded).hexdigest() != recipient_fingerprint
            ):
                raise ValueError("migration target integrity check failed")
        expected = cls._state_hash_from_row(row)
        if row["state_hash"] == expected:
            return
        # Schema 74 target rows predate the expanded hash envelope. Accept a
        # legacy row only when its public-key fingerprint still matches; the
        # next durable target mutation writes the expanded envelope.
        if row["state_hash"] == cls._legacy_state_hash_from_row(row):
            return
        raise ValueError("migration target integrity check failed")
