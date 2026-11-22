"""Durable, subject-scoped model capability observations."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from noyra.core.database import Database
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, new_id, utc_now

from .reasoning import REASONING_EFFORTS, ReasoningEffort, reasoning_profile

CAPABILITY_PROFILE_VERSION = 1
CAPABILITY_PROBE_TTL_DAYS = 60
CAPABILITY_REFRESH_LEAD = timedelta(days=7)
CAPABILITY_REFRESH_GRACE = timedelta(days=7)
CAPABILITY_READY_STATES = frozenset({"ready", "refresh_due"})
CAPABILITY_RUNTIME_STATES = CAPABILITY_READY_STATES | {"grace"}
CAPABILITY_REVALIDATION_STATES = frozenset({"refresh_due", "grace"})


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
    effort_expiries: tuple[tuple[ReasoningEffort, str | None], ...] = ()
    rejected_efforts: tuple[ReasoningEffort, ...] = ()

    def effort_expiry(self, effort: ReasoningEffort) -> str | None:
        return dict(self.effort_expiries).get(effort, self.expires_at)

    def effort_status(self, effort: ReasoningEffort, *, now: datetime | None = None) -> str:
        if effort in self.rejected_efforts:
            return "rejected"
        if (
            self.status not in {"known", "stale"}
            or self.parameter != "reasoning_effort"
            or effort not in self.tested_efforts
        ):
            return "probe_required"
        deadline = _parse_expiry(self.effort_expiry(effort))
        if deadline is None:
            return "ready"
        remaining = deadline - (now or datetime.now(UTC))
        if remaining <= -CAPABILITY_REFRESH_GRACE:
            return "probe_required"
        if remaining <= timedelta(0):
            return "grace"
        if remaining <= CAPABILITY_REFRESH_LEAD:
            return "refresh_due"
        return "ready"

    def _effort_metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        if self.effort_expiries:
            result["effort_expiries"] = dict(self.effort_expiries)
        if self.rejected_efforts:
            result["rejected_efforts"] = list(self.rejected_efforts)
        return result

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
                **self._effort_metadata(),
            }
        )

    def public(
        self, *, selected: ReasoningEffort | None = None, existing_resource: bool = False
    ) -> dict[str, Any]:
        effective_status = self.effort_status(selected) if selected is not None else self.status
        if not existing_resource and effective_status == "grace":
            effective_status = "probe_required"
        effective_selected = selected if effective_status in CAPABILITY_RUNTIME_STATES else None
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
            "fresh_efforts": [
                effort
                for effort in self.tested_efforts
                if self.effort_status(effort) in CAPABILITY_READY_STATES
            ],
            "revalidate_at": self.effort_expiry(selected) if selected is not None else None,
            "selected": effective_selected,
            "configured_selected": selected,
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
            **self._effort_metadata(),
        }


def _endpoint_hash(base_url: str) -> str:
    return content_hash({"endpoint": base_url.strip().rstrip("/")})


def _parse_efforts(value: Any) -> tuple[ReasoningEffort, ...]:
    if not isinstance(value, list) or any(item not in REASONING_EFFORTS for item in value):
        raise IntegrityError("model capability efforts are invalid")
    return tuple(item for item in REASONING_EFFORTS if item in value)


def _parse_expiry(expires_at: object) -> datetime | None:
    if expires_at is None:
        return None
    if not isinstance(expires_at, str):
        raise IntegrityError("model capability expiry is invalid")
    try:
        expires = datetime.fromisoformat(expires_at)
    except ValueError as error:
        raise IntegrityError("model capability expiry is invalid") from error
    if expires.utcoffset() is None:
        raise IntegrityError("model capability expiry is invalid")
    return expires


def _effort_metadata(
    payload: dict[str, Any],
    efforts: tuple[ReasoningEffort, ...],
    tested: tuple[ReasoningEffort, ...],
) -> tuple[tuple[tuple[ReasoningEffort, str | None], ...], tuple[ReasoningEffort, ...]]:
    expiries = payload.get("effort_expiries", {})
    rejected = _parse_efforts(payload.get("rejected_efforts", []))
    if (
        not isinstance(expiries, dict)
        or (expiries and set(expiries) != set(tested))
        or any(effort in tested or effort not in efforts for effort in rejected)
    ):
        raise IntegrityError("model capability effort metadata is invalid")
    for expiry in expiries.values():
        _parse_expiry(expiry)
    return tuple((effort, expiries[effort]) for effort in tested if effort in expiries), rejected


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
        deadlines, rejected = _effort_metadata(payload, efforts, tested_efforts)
        _parse_expiry(row["expires_at"])
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
            deadlines,
            rejected,
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
        if (
            capability.status == "known"
            and capability.tested_efforts
            and not any(
                capability.effort_status(effort) in CAPABILITY_READY_STATES
                for effort in capability.tested_efforts
            )
        ):
            return replace(capability, status="stale")
        return capability

    def record_probe_success(
        self,
        subject_id: str,
        provider_type: str,
        base_url: str,
        model: str,
        effort: ReasoningEffort,
    ) -> StoredCapability | None:
        return self._record_observation(subject_id, provider_type, base_url, model, effort, "probe")

    def record_runtime_observation(
        self,
        subject_id: str,
        provider_type: str,
        base_url: str,
        model: str,
        effort: ReasoningEffort,
        *,
        rejected: bool = False,
        expected: StoredCapability | None = None,
    ) -> StoredCapability | None:
        return self._record_observation(
            subject_id,
            provider_type,
            base_url,
            model,
            effort,
            "rejected" if rejected else "refresh",
            expected=expected,
        )

    def _record_observation(
        self,
        subject_id: str,
        provider_type: str,
        base_url: str,
        model: str,
        effort: ReasoningEffort,
        observation: Literal["probe", "refresh", "rejected"],
        *,
        expected: StoredCapability | None = None,
    ) -> StoredCapability | None:
        if effort not in REASONING_EFFORTS:
            raise ValueError("invalid reasoning capability effort")
        candidate_profile = reasoning_profile(model, provider_type)
        if effort not in candidate_profile.candidate_efforts:
            raise ValueError("reasoning effort is not a candidate for this model")
        endpoint_hash = _endpoint_hash(base_url)
        with self.database.transaction() as connection:
            if not self._table_exists(connection):
                return None
            row = connection.execute(
                "SELECT * FROM model_capability_profiles "
                "WHERE subject_id=? AND provider_type=? AND endpoint_hash=? AND model=?",
                (subject_id, provider_type, endpoint_hash, model.strip()),
            ).fetchone()
            current = self._from_row(row) if row is not None else None
            now = datetime.now(UTC)
            # An in-flight result must not overwrite a newer probe of this effort.
            if observation == "rejected" and (
                (current is None) != (expected is None)
                or (
                    current is not None
                    and expected is not None
                    and (
                        current.effort_expiry(effort) != expected.effort_expiry(effort)
                        or (effort in current.tested_efforts) != (effort in expected.tested_efforts)
                    )
                )
            ):
                return current
            if observation == "refresh" and (
                current is None
                or current.effort_status(effort, now=now) not in CAPABILITY_REVALIDATION_STATES
            ):
                return current
            if (
                observation == "rejected"
                and current is not None
                and effort in current.rejected_efforts
            ):
                return current
            previous_tested = (
                current.tested_efforts if current and current.status in {"known", "stale"} else ()
            )
            deadlines = (
                {item: current.effort_expiry(item) for item in previous_tested} if current else {}
            )
            rejected = set(current.rejected_efforts if current else ())
            if observation == "rejected":
                deadlines.pop(effort, None)
                rejected.add(effort)
            else:
                deadlines[effort] = (now + timedelta(days=CAPABILITY_PROBE_TTL_DAYS)).isoformat()
                rejected.discard(effort)
            available = set(current.efforts if current else candidate_profile.efforts) | {effort}
            efforts = tuple(item for item in REASONING_EFFORTS if item in available)
            tested = tuple(item for item in REASONING_EFFORTS if item in deadlines)
            # The legacy deadline remains conservative for readers without per-effort metadata.
            finite = [value for value in deadlines.values() if value is not None]
            expires = min(finite, key=datetime.fromisoformat) if finite else None
            payload = {
                "supported": True,
                "efforts": list(efforts),
                "tested_efforts": list(tested),
                "parameter": "reasoning_effort",
                "version": CAPABILITY_PROFILE_VERSION,
                "effort_expiries": deadlines,
                "rejected_efforts": [item for item in REASONING_EFFORTS if item in rejected],
            }
            state = {
                "profile_id": row["profile_id"] if row is not None else new_id("mcap"),
                "subject_id": subject_id,
                "provider_type": provider_type,
                "endpoint_hash": endpoint_hash,
                "model": model.strip(),
                "capabilities_json": canonical_json(payload),
                "revision": current.revision + 1 if current else 1,
                "source": "probe",
                "status": "known" if tested else "failed",
                "observed_at": now.isoformat(timespec="milliseconds"),
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
        return self._from_row({**state, "state_hash": content_hash(state)})

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
            or (expires_at is not None and not isinstance(expires_at, str))
        ):
            raise ValueError("model capability profile is invalid")
        try:
            observed_time = datetime.fromisoformat(observed_at)
            expires_time = _parse_expiry(expires_at)
            deadlines, rejected = _effort_metadata(item, efforts, tested_efforts)
        except (ValueError, IntegrityError) as error:
            raise ValueError("model capability profile timestamps are invalid") from error
        if observed_time.utcoffset() is None or (
            expires_time is None and (tested_efforts or not rejected)
        ):
            raise ValueError("model capability profile timestamps must include a timezone")
        if any(deadline is None for _, deadline in deadlines):
            raise ValueError("model capability tested efforts require finite expiry dates")
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
            "failed" if rejected and not tested_efforts else "known",
            observed_at,
            expires_at,
            deadlines,
            rejected,
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
                    **candidate._effort_metadata(),
                }
            ),
            "revision": candidate.revision,
            "source": "operator",
            "status": candidate.status,
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
