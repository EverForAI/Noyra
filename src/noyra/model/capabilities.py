"""Durable, subject-scoped model capability observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .reasoning import REASONING_EFFORTS, ReasoningEffort, reasoning_profile

CAPABILITY_PROFILE_VERSION = 1
CAPABILITY_PROBE_TTL_DAYS = 30


@dataclass(frozen=True)
class StoredCapability:
    provider_type: str
    endpoint_hash: str
    model: str
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

    @property
    def profile_digest(self) -> str:
        """Stable, non-secret identifier that an environment can pin."""

        return content_hash(
            {
                "supported": self.supported,
                "provider_type": self.provider_type,
                "endpoint_hash": self.endpoint_hash,
                "model": self.model,
                "efforts": list(self.efforts),
                "tested_efforts": list(self.tested_efforts),
                "parameter": self.parameter,
                "version": self.version,
                "revision": self.revision,
                "observed_at": self.observed_at,
                "expires_at": self.expires_at,
            }
        )

    def public(self, *, selected: ReasoningEffort | None = None) -> dict[str, Any]:
        configured_selected = selected if selected in self.efforts else None
        effective_selected = (
            configured_selected
            if self.status == "known" and configured_selected in self.tested_efforts
            else None
        )
        effective_status = (
            "ready"
            if effective_selected is not None
            else "probe_required"
            if configured_selected is not None
            else self.status
        )
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
            "profile_digest": self.profile_digest,
            # ``selected`` is the effective runtime value.  A stale or
            # partially-tested profile must never look active in a public
            # management payload; operators still need the configured value
            # to understand why a call is blocked.
            "selected": effective_selected,
            "configured_selected": configured_selected,
            "effective_status": effective_status,
        }

    def export(self, *, provider_type: str, base_url: str, model: str) -> dict[str, Any]:
        """Return the non-secret profile reference used by env provisioning."""

        if (
            provider_type != self.provider_type
            or _endpoint_hash(base_url) != self.endpoint_hash
            or model.strip() != self.model
        ):
            raise ValueError("model capability export identity mismatch")

        return {
            "provider_type": provider_type,
            "base_url": base_url,
            "model": model,
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
            "profile_digest": self.profile_digest,
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
            str(row["provider_type"]),
            str(row["endpoint_hash"]),
            str(row["model"]),
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
                capability.provider_type,
                capability.endpoint_hash,
                capability.model,
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
            provider_type,
            endpoint_hash,
            model.strip(),
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

    @staticmethod
    def _import_candidate(item: object) -> StoredCapability:
        if not isinstance(item, dict):
            raise ValueError("model capability profile entry is invalid")
        provider_type = item.get("provider_type")
        base_url = item.get("base_url")
        model = item.get("model")
        if (
            provider_type != "openai_compatible"
            or not isinstance(base_url, str)
            or not isinstance(model, str)
            or not model.strip()
        ):
            raise ValueError("model capability profile identity is invalid")
        parsed = urlsplit(base_url.strip())
        if (
            parsed.scheme.lower() != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("model capability profile endpoint is invalid")
        efforts = _parse_efforts(item.get("efforts"))
        tested_efforts = _parse_efforts(item.get("tested_efforts", []))
        supported = item.get("supported")
        parameter = item.get("parameter")
        version = item.get("version")
        revision = item.get("revision")
        observed_at = item.get("observed_at")
        expires_at = item.get("expires_at")
        if (
            type(supported) is not bool
            or (supported != bool(efforts))
            or (supported and parameter != "reasoning_effort")
            or (not supported and parameter is not None)
            or type(version) is not int
            or version < 1
            or type(revision) is not int
            or revision < 1
            or any(value not in efforts for value in tested_efforts)
            or not isinstance(observed_at, str)
            or not isinstance(expires_at, str)
        ):
            raise ValueError("model capability profile is invalid")
        try:
            observed_time = datetime.fromisoformat(observed_at)
            expires_time = datetime.fromisoformat(expires_at)
        except ValueError as error:
            raise ValueError("model capability profile timestamps are invalid") from error
        if observed_time.tzinfo is None or expires_time.tzinfo is None:
            raise ValueError("model capability profile timestamps must include a timezone")
        endpoint_hash = _endpoint_hash(base_url)
        candidate = StoredCapability(
            provider_type,
            endpoint_hash,
            model.strip(),
            supported,
            efforts,
            tested_efforts,
            parameter,
            version,
            revision,
            "operator",
            "known",
            observed_at,
            expires_at,
        )
        if item.get("profile_digest") != candidate.profile_digest:
            raise ValueError("model capability profile digest mismatch")
        return candidate

    @staticmethod
    def _import_state(
        subject_id: str, candidate: StoredCapability, profile_id: str
    ) -> dict[str, Any]:
        return {
            "profile_id": profile_id,
            "subject_id": subject_id,
            "provider_type": candidate.provider_type,
            "endpoint_hash": candidate.endpoint_hash,
            "model": candidate.model,
            "capabilities_json": canonical_json(
                {
                    "supported": candidate.supported,
                    "efforts": list(candidate.efforts),
                    "tested_efforts": list(candidate.tested_efforts),
                    "parameter": candidate.parameter,
                    "version": candidate.version,
                }
            ),
            "revision": candidate.revision,
            "source": "operator",
            "status": "known",
            "observed_at": candidate.observed_at,
            "expires_at": candidate.expires_at,
        }

    def import_profiles(self, subject_id: str, payload: object, *, bootstrap: bool = False) -> int:
        """Import profiles; bootstrap preserves newer verified durable observations."""
        if isinstance(payload, dict):
            payload = payload.get("profiles")
        if not isinstance(payload, list) or len(payload) > 256:
            raise ValueError("model capability profile file is invalid")
        candidates = [self._import_candidate(item) for item in payload]
        imported = 0
        with self.database.transaction() as connection:
            if not self._table_exists(connection):
                raise ValueError("model capability profile table is unavailable")
            for candidate in candidates:
                row = connection.execute(
                    "SELECT * FROM model_capability_profiles WHERE subject_id=? "
                    "AND provider_type=? AND endpoint_hash=? AND model=?",
                    (subject_id, candidate.provider_type, candidate.endpoint_hash, candidate.model),
                ).fetchone()
                if row is not None:
                    existing = self._from_row(row)
                    if existing.revision > candidate.revision:
                        if bootstrap:
                            continue
                        raise ValueError("model capability profile revision rollback")
                    if existing.revision == candidate.revision:
                        if existing.profile_digest != candidate.profile_digest:
                            raise ValueError("model capability profile revision conflict")
                        continue
                state = self._import_state(
                    subject_id, candidate, row["profile_id"] if row is not None else new_id("mcap")
                )
                connection.execute(
                    "INSERT INTO model_capability_profiles VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(subject_id,provider_type,endpoint_hash,model) DO UPDATE SET "
                    "capabilities_json=excluded.capabilities_json, revision=excluded.revision, "
                    "source=excluded.source, status=excluded.status, "
                    "observed_at=excluded.observed_at, "
                    "expires_at=excluded.expires_at, state_hash=excluded.state_hash",
                    (*state.values(), content_hash(state)),
                )
                imported += 1
        return imported

    def repair_imported_profile(
        self,
        subject_id: str,
        profile: object,
        *,
        expected_profile_id: str,
        expected_state_hash: str,
        record_evidence: Callable[[dict[str, Any]], None],
    ) -> None:
        """Explicit offline recovery for the historical import-ID bug only.

        Every stored field must match an independently trusted export. Ordinary
        reads/import/bootstrap keep failing closed; they never invoke recovery.
        The caller must durably record the authorization before this transaction
        changes the single pinned record.
        """
        candidate = self._import_candidate(profile)
        expected = self._import_state(subject_id, candidate, expected_profile_id)
        with self.database.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM model_capability_profiles WHERE profile_id=? AND subject_id=?",
                (expected_profile_id, subject_id),
            ).fetchone()
            if row is None:
                raise ValueError("profile repair identity mismatch")
            before = dict(row)
            old_hash = before.pop("state_hash")
            if old_hash != expected_state_hash or before != expected:
                raise ValueError("profile repair requires an exact trusted export and state pin")
            new_hash = content_hash(expected)
            if new_hash == old_hash:
                raise ValueError("profile repair refuses a healthy record")
            record_evidence(
                {
                    "operation": "repair_imported_profile",
                    "authorized_at": utc_now(),
                    "before": dict(row),
                    "intended_state_hash": new_hash,
                    "trusted_profile_digest": candidate.profile_digest,
                }
            )
            connection.execute(
                "UPDATE model_capability_profiles SET state_hash=? "
                "WHERE profile_id=? AND state_hash=?",
                (new_hash, expected_profile_id, expected_state_hash),
            )
            self._from_row(
                connection.execute(
                    "SELECT * FROM model_capability_profiles WHERE profile_id=?",
                    (expected_profile_id,),
                ).fetchone()
            )
