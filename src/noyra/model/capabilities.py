"""Durable, subject-scoped model capability observations."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .reasoning import REASONING_EFFORTS, ReasoningEffort, reasoning_profile

CAPABILITY_PROFILE_VERSION = 1
CAPABILITY_PROBE_TTL_DAYS = 30


@dataclass(frozen=True)
class StoredCapability:
    supported: bool
    efforts: tuple[ReasoningEffort, ...]
    tested_efforts: tuple[ReasoningEffort, ...]
    parameter: str | None
    version: int
    revision: int
    source: str
    status: str
    observed_at: str
    expires_at: str | None

    def public(self, *, selected: ReasoningEffort | None = None) -> dict[str, Any]:
        return {
            "supported": self.supported,
            "efforts": list(self.efforts),
            "tested_efforts": list(self.tested_efforts),
            "parameter": self.parameter,
            "version": self.version,
            "revision": self.revision,
            "source": self.source,
            "status": self.status,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "selected": selected if selected in self.efforts else None,
        }


def _endpoint_hash(base_url: str) -> str:
    return content_hash({"endpoint": base_url.strip().rstrip("/")})


def _parse_efforts(value: Any) -> tuple[ReasoningEffort, ...]:
    if not isinstance(value, list) or any(item not in REASONING_EFFORTS for item in value):
        raise IntegrityError("model capability efforts are invalid")
    return tuple(item for item in REASONING_EFFORTS if item in value)


def _is_expired(expires_at: str | None, *, now: datetime | None = None) -> bool:
    if not expires_at:
        return False
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError as error:
        raise IntegrityError("model capability expiry is invalid") from error
    if expires.tzinfo is None:
        raise IntegrityError("model capability expiry is invalid")
    current = now or datetime.now(UTC)
    return expires <= current


class CapabilityProfileStore:
    """Read and write bounded capability observations without storing secrets."""

    def __init__(self, database: Database):
        self.database = database

    @staticmethod
    def _table_exists(connection: Any) -> bool:
        return (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                ("model_capability_profiles",),
            ).fetchone()
            is not None
        )

    @staticmethod
    def _from_row(row: Any) -> StoredCapability:
        state = dict(row)
        state_hash = state.pop("state_hash")
        if content_hash(state) != state_hash:
            raise IntegrityError("model capability profile hash mismatch")
        try:
            payload = json.loads(row["capabilities_json"])
        except (TypeError, ValueError) as error:
            raise IntegrityError("model capability profile is invalid") from error
        if not isinstance(payload, dict):
            raise IntegrityError("model capability profile is invalid")
        efforts = _parse_efforts(payload.get("efforts"))
        tested_efforts = _parse_efforts(payload.get("tested_efforts", []))
        supported = payload.get("supported")
        parameter = payload.get("parameter")
        version = payload.get("version")
        if type(supported) is not bool or (
            parameter is not None and not isinstance(parameter, str)
        ):
            raise IntegrityError("model capability profile is invalid")
        if type(version) is not int or version < 1:
            raise IntegrityError("model capability profile version is invalid")
        if any(item not in efforts for item in tested_efforts):
            raise IntegrityError("model capability tested efforts exceed the profile")
        if supported != bool(efforts) or (supported and parameter != "reasoning_effort"):
            raise IntegrityError("model capability profile is inconsistent")
        return StoredCapability(
            supported,
            efforts,
            tested_efforts,
            parameter,
            version,
            int(row["revision"]),
            str(row["source"]),
            str(row["status"]),
            str(row["observed_at"]),
            row["expires_at"],
        )

    def get(
        self,
        subject_id: str,
        provider_type: str,
        base_url: str,
        model: str,
    ) -> StoredCapability | None:
        with self.database.connection() as connection:
            if not self._table_exists(connection):
                return None
            row = connection.execute(
                "SELECT * FROM model_capability_profiles "
                "WHERE subject_id=? AND provider_type=? AND endpoint_hash=? AND model=?",
                (subject_id, provider_type, _endpoint_hash(base_url), model.strip()),
            ).fetchone()
        if row is None:
            return None
        capability = self._from_row(row)
        if _is_expired(capability.expires_at) and capability.status == "known":
            return StoredCapability(
                capability.supported,
                capability.efforts,
                capability.tested_efforts,
                capability.parameter,
                capability.version,
                capability.revision,
                capability.source,
                "stale",
                capability.observed_at,
                capability.expires_at,
            )
        return capability

    def record_probe_success(
        self,
        subject_id: str,
        provider_type: str,
        base_url: str,
        model: str,
        effort: ReasoningEffort,
    ) -> StoredCapability | None:
        if effort not in REASONING_EFFORTS:
            raise ValueError("invalid reasoning capability effort")
        candidate_profile = reasoning_profile(model, provider_type)
        if effort not in candidate_profile.candidate_efforts:
            raise ValueError("reasoning effort is not a candidate for this model")
        now = utc_now()
        expires = (datetime.now(UTC) + timedelta(days=CAPABILITY_PROBE_TTL_DAYS)).isoformat()
        endpoint_hash = _endpoint_hash(base_url)
        with self.database.transaction() as connection:
            if not self._table_exists(connection):
                return None
            row = connection.execute(
                "SELECT * FROM model_capability_profiles "
                "WHERE subject_id=? AND provider_type=? AND endpoint_hash=? AND model=?",
                (subject_id, provider_type, endpoint_hash, model.strip()),
            ).fetchone()
            if row is None:
                efforts: tuple[ReasoningEffort, ...] = candidate_profile.efforts or (effort,)
                tested_efforts: tuple[ReasoningEffort, ...] = (effort,)
                revision = 1
                profile_id = new_id("mcap")
            else:
                current = self._from_row(row)
                efforts = tuple(
                    item for item in REASONING_EFFORTS if item in (*current.efforts, effort)
                )
                if effort not in candidate_profile.candidate_efforts:
                    raise ValueError("reasoning effort is not a candidate for this model")
                if current.status == "known" and not _is_expired(current.expires_at):
                    tested_efforts = tuple(
                        item
                        for item in REASONING_EFFORTS
                        if item in (*current.tested_efforts, effort)
                    )
                else:
                    tested_efforts = (effort,)
                revision = current.revision + 1
                profile_id = str(row["profile_id"])
            payload = {
                "supported": True,
                "efforts": list(efforts),
                "tested_efforts": list(tested_efforts),
                "parameter": "reasoning_effort",
                "version": CAPABILITY_PROFILE_VERSION,
            }
            state = {
                "profile_id": profile_id,
                "subject_id": subject_id,
                "provider_type": provider_type,
                "endpoint_hash": endpoint_hash,
                "model": model.strip(),
                "capabilities_json": canonical_json(payload),
                "revision": revision,
                "source": "probe",
                "status": "known",
                "observed_at": now,
                "expires_at": expires,
            }
            connection.execute(
                "INSERT INTO model_capability_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(subject_id,provider_type,endpoint_hash,model) DO UPDATE SET "
                "capabilities_json=excluded.capabilities_json, revision=excluded.revision, "
                "source=excluded.source, status=excluded.status, observed_at=excluded.observed_at, "
                "expires_at=excluded.expires_at, state_hash=excluded.state_hash",
                (*state.values(), content_hash(state)),
            )
        return StoredCapability(
            True,
            efforts,
            tested_efforts,
            "reasoning_effort",
            CAPABILITY_PROFILE_VERSION,
            revision,
            "probe",
            "known",
            now,
            expires,
        )
