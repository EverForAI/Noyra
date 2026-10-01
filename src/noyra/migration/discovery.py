"""Fail-closed resource observations and source-need assessment.

This module deliberately has no networking or provisioning code.  A caller must
provide an observation adapter for an already enrolled target.  The adapter is
given only the registered target id and the returned observation is verified
against the enrollment public key before it can be used for migration planning.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from noyra.core.database import Database
from noyra.core.types import canonical_json

from .policy import MigrationPolicy


def _bounded_ratio(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not 0.0 <= float(value) <= 1.0:
        raise ValueError(f"{name} must be between 0 and 1")
    return float(value)


def _decode_key(value: str) -> bytes:
    try:
        raw = base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4))
    except (ValueError, TypeError) as error:
        raise ValueError("target public key is invalid") from error
    if len(raw) != 32:
        raise ValueError("target public key is invalid")
    return raw


@dataclass(frozen=True)
class ResourceObservation:
    target_id: str
    observed_at: str
    capacity: dict[str, int]
    latency_ms: int
    cost_microusd_month: int
    region: str | None
    enrollment_generation: int
    evidence_hash: str
    signature: str | None = None

    @classmethod
    def create_signed_payload(
        cls,
        *,
        target_id: str,
        observed_at: str,
        capacity: Mapping[str, Any],
        latency_ms: int,
        cost_microusd_month: int,
        region: str | None,
        enrollment_generation: int,
    ) -> ResourceObservation:
        values = cls._validated_values(
            target_id,
            observed_at,
            capacity,
            latency_ms,
            cost_microusd_month,
            region,
            enrollment_generation,
        )
        digest = hashlib.sha256(canonical_json(values).encode("utf-8")).hexdigest()
        return cls(**values, evidence_hash=digest)

    def with_signature(self, signature: str) -> ResourceObservation:
        if not isinstance(signature, str) or not signature.strip() or len(signature) > 512:
            raise ValueError("resource observation signature is invalid")
        return replace(self, signature=signature.strip())

    @property
    def free_bytes(self) -> int:
        return int(self.capacity.get("free_bytes", 0))

    def signing_bytes(self) -> bytes:
        values = self._unsigned_values()
        return (
            f"noyra-resource-observation-v1\n{canonical_json(values)}\n{self.evidence_hash}"
        ).encode()

    def verify(self, *, public_key: str, expected_target_id: str, expected_generation: int) -> None:
        if self.target_id != expected_target_id:
            raise ValueError("resource observation target mismatch")
        if self.enrollment_generation != expected_generation:
            raise ValueError("resource observation enrollment generation mismatch")
        expected_hash = hashlib.sha256(
            canonical_json(self._unsigned_values()).encode("utf-8")
        ).hexdigest()
        if self.evidence_hash != expected_hash:
            raise ValueError("resource observation evidence hash mismatch")
        if not self.signature:
            raise ValueError("resource observation signature is missing")
        try:
            signature = base64.urlsafe_b64decode(self.signature + "=" * (-len(self.signature) % 4))
            Ed25519PublicKey.from_public_bytes(_decode_key(public_key)).verify(
                signature, self.signing_bytes()
            )
        except (ValueError, TypeError, InvalidSignature) as error:
            raise ValueError("resource observation signature is invalid") from error

    def is_fresh(self, *, max_age_seconds: int = 300, now: datetime | None = None) -> bool:
        if type(max_age_seconds) is not int or not 1 <= max_age_seconds <= 86_400:
            raise ValueError("resource observation age bound is invalid")
        try:
            observed = datetime.fromisoformat(self.observed_at.replace("Z", "+00:00"))
        except (TypeError, ValueError) as error:
            raise ValueError("resource observation timestamp is invalid") from error
        if observed.tzinfo is None:
            raise ValueError("resource observation timestamp must include timezone")
        age = (now or datetime.now(UTC)).astimezone(UTC) - observed.astimezone(UTC)
        return timedelta(seconds=0) <= age <= timedelta(seconds=max_age_seconds)

    def _unsigned_values(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "observed_at": self.observed_at,
            "capacity": self.capacity,
            "latency_ms": self.latency_ms,
            "cost_microusd_month": self.cost_microusd_month,
            "region": self.region,
            "enrollment_generation": self.enrollment_generation,
        }

    @staticmethod
    def _validated_values(
        target_id: str,
        observed_at: str,
        capacity: Mapping[str, Any],
        latency_ms: int,
        cost_microusd_month: int,
        region: str | None,
        enrollment_generation: int,
    ) -> dict[str, Any]:
        if not isinstance(target_id, str) or not target_id.strip() or len(target_id) > 128:
            raise ValueError("resource observation target id is invalid")
        if not isinstance(observed_at, str) or not observed_at.strip():
            raise ValueError("resource observation timestamp is invalid")
        try:
            timestamp = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("resource observation timestamp is invalid") from error
        if timestamp.tzinfo is None:
            raise ValueError("resource observation timestamp must include timezone")
        if not isinstance(capacity, Mapping) or not capacity or len(capacity) > 16:
            raise ValueError("resource observation capacity is invalid")
        bounded_capacity: dict[str, int] = {}
        for key, value in capacity.items():
            if not isinstance(key, str) or not key or len(key) > 64 or type(value) is not int:
                raise ValueError("resource observation capacity is invalid")
            if not 0 <= value <= 10**18:
                raise ValueError("resource observation capacity is invalid")
            bounded_capacity[key] = value
        if "free_bytes" not in bounded_capacity:
            raise ValueError("resource observation capacity must include free_bytes")
        if type(latency_ms) is not int or not 0 <= latency_ms <= 3_600_000:
            raise ValueError("resource observation latency is invalid")
        if type(cost_microusd_month) is not int or not 0 <= cost_microusd_month <= 10**12:
            raise ValueError("resource observation cost is invalid")
        if region is not None and (not isinstance(region, str) or not 1 <= len(region) <= 128):
            raise ValueError("resource observation region is invalid")
        if type(enrollment_generation) is not int or not 1 <= enrollment_generation <= 2**63 - 1:
            raise ValueError("resource observation enrollment generation is invalid")
        return {
            "target_id": target_id.strip(),
            "observed_at": timestamp.astimezone(UTC).isoformat(timespec="milliseconds"),
            "capacity": bounded_capacity,
            "latency_ms": latency_ms,
            "cost_microusd_month": cost_microusd_month,
            "region": None if region is None else region.strip(),
            "enrollment_generation": enrollment_generation,
        }


@dataclass(frozen=True)
class NeedAssessment:
    source_health: float
    workload: float
    storage_pressure: float
    provider_health: float
    benefit_score: float
    reason_code: str
    reason: str
    evidence: dict[str, Any]
    payment_in_flight: bool = False
    maintenance_conflict: bool = False

    @property
    def actionable(self) -> bool:
        return (
            self.storage_pressure >= 0.6
            or self.provider_health <= 0.5
            or self.source_health <= 0.4
            or self.workload >= 0.85
        )


class MigrationNeed:
    """Bounded deterministic source-need assessor used by the proposal builder."""

    @staticmethod
    def assess(
        *,
        source_health: float,
        workload: float = 0.0,
        storage_pressure: float = 0.0,
        provider_health: float = 1.0,
        evidence: Mapping[str, Any] | None = None,
        payment_in_flight: bool = False,
        maintenance_conflict: bool = False,
        reason_code: str | None = None,
        reason: str | None = None,
    ) -> NeedAssessment:
        values = {
            "source_health": _bounded_ratio(source_health, "source health"),
            "workload": _bounded_ratio(workload, "workload"),
            "storage_pressure": _bounded_ratio(storage_pressure, "storage pressure"),
            "provider_health": _bounded_ratio(provider_health, "provider health"),
        }
        if type(payment_in_flight) is not bool or type(maintenance_conflict) is not bool:
            raise TypeError("migration conflict flags must be boolean")
        if evidence is None:
            evidence = {}
        if not isinstance(evidence, Mapping) or len(evidence) > 32:
            raise ValueError("migration need evidence is invalid")
        evidence_copy = json.loads(canonical_json(dict(evidence)))
        if len(canonical_json(evidence_copy).encode("utf-8")) > 16_384:
            raise ValueError("migration need evidence is too large")
        benefit = min(
            1.0,
            max(
                0.0,
                values["storage_pressure"] * 0.45
                + (1.0 - values["provider_health"]) * 0.25
                + (1.0 - values["source_health"]) * 0.2
                + values["workload"] * 0.1,
            ),
        )
        selected_code = reason_code or (
            "storage_pressure"
            if values["storage_pressure"] >= 0.6
            else "provider_degradation"
            if values["provider_health"] <= 0.5
            else "source_health"
        )
        if not isinstance(selected_code, str) or not re.fullmatch(
            r"[a-z][a-z0-9_]{0,63}", selected_code
        ):
            raise ValueError("migration need reason code is invalid")
        selected_reason = (reason or "source conditions indicate a bounded migration need").strip()
        if not selected_reason or len(selected_reason) > 2048:
            raise ValueError("migration need reason is invalid")
        return NeedAssessment(
            **values,
            benefit_score=benefit,
            reason_code=selected_code,
            reason=selected_reason,
            evidence=evidence_copy,
            payment_in_flight=payment_in_flight,
            maintenance_conflict=maintenance_conflict,
        )


class ResourceObservationProvider(Protocol):
    def observe(self, target_id: str) -> ResourceObservation | None: ...


@dataclass(frozen=True)
class DiscoveryResult:
    target_id: str
    status: str
    trusted: bool
    resources_verified: bool
    eligible: bool
    reasons: tuple[str, ...]
    observation: ResourceObservation | None = None

    @property
    def free_bytes(self) -> int:
        return 0 if self.observation is None else self.observation.free_bytes

    @property
    def region(self) -> str | None:
        return None if self.observation is None else self.observation.region


class MigrationDiscovery:
    def __init__(
        self,
        database: Database,
        observation_provider: ResourceObservationProvider,
        *,
        max_age_seconds: int = 300,
    ) -> None:
        self.database = database
        self.observation_provider = observation_provider
        self.max_age_seconds = max_age_seconds

    def discover(self, subject_id: str, policy: MigrationPolicy) -> list[DiscoveryResult]:
        if not policy.enabled or policy.approval_mode == "disabled":
            return []
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM migration_targets WHERE subject_id=? AND status='active' "
                "AND attested_at IS NOT NULL ORDER BY updated_at DESC LIMIT 256",
                (subject_id,),
            ).fetchall()
        results: list[DiscoveryResult] = []
        for row in rows:
            target_id = str(row["target_id"])
            if policy.allowed_target_ids and target_id not in policy.allowed_target_ids:
                results.append(
                    DiscoveryResult(
                        target_id,
                        str(row["status"]),
                        True,
                        False,
                        False,
                        ("target_not_allowlisted",),
                    )
                )
                continue
            try:
                from .targets import TargetRegistry

                TargetRegistry._assert_row_integrity(row)
            except (KeyError, TypeError, ValueError):
                results.append(
                    DiscoveryResult(
                        target_id,
                        str(row["status"]),
                        False,
                        False,
                        False,
                        ("target_integrity_unavailable",),
                    )
                )
                continue
            try:
                observation = self.observation_provider.observe(target_id)
            except Exception:
                observation = None
            if observation is None:
                results.append(
                    DiscoveryResult(
                        target_id,
                        str(row["status"]),
                        True,
                        False,
                        False,
                        ("resource_observation_unavailable",),
                    )
                )
                continue
            reasons: list[str] = []
            try:
                observation.verify(
                    public_key=str(row["public_key"]),
                    expected_target_id=target_id,
                    expected_generation=int(row["enrollment_generation"]),
                )
                if not observation.is_fresh(max_age_seconds=self.max_age_seconds):
                    reasons.append("resource_observation_stale")
                if row["region"] and observation.region != row["region"]:
                    reasons.append("resource_observation_region_mismatch")
                if observation.free_bytes < policy.min_free_bytes:
                    reasons.append("target_storage_insufficient")
            except ValueError:
                reasons.append("resource_observation_invalid")
            results.append(
                DiscoveryResult(
                    target_id,
                    str(row["status"]),
                    True,
                    not reasons,
                    not reasons,
                    tuple(reasons),
                    observation,
                )
            )
        return results
