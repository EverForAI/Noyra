from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from typing import Any, Self

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.discovery import (
    MigrationDiscovery,
    ResourceObservation,
)
from noyra.migration.policy import MigrationPolicy, MigrationStore
from noyra.migration.targets import TargetRegistry


class StaticObservationProvider:
    def __init__(self: Self, observations: dict[str, ResourceObservation]) -> None:
        self.observations = observations
        self.requested: list[str] = []

    def observe(self: Self, target_id: str) -> ResourceObservation | None:
        self.requested.append(target_id)
        return self.observations.get(target_id)


def _setup(tmp_path: Any) -> Any:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "e" * 64)
    policy_store = MigrationStore(database)
    policy = policy_store.read_policy("Noyra-0001").with_updates(enabled=True)
    policy = policy.with_updates(allowed_target_ids=("target-1",), min_free_bytes=1024)
    registry = TargetRegistry(database, policy_store)
    private = Ed25519PrivateKey.generate()
    public = base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode()
    target = registry.register(
        "Noyra-0001",
        target_id="target-1",
        public_key=public,
        endpoint="https://target.example/migration",
        capabilities={},
        region="test",
        provider="test",
        release_sha="a" * 40,
        os_arch="linux-amd64",
        encrypted_volume=True,
        actor="operator",
    )
    challenge = registry.issue_challenge("target-1", source_epoch="source-1")
    registry.attest(
        "target-1",
        challenge,
        base64.urlsafe_b64encode(private.sign(challenge.signing_bytes())).decode(),
        actor="operator",
    )
    return database, policy, target, private


def _observation(private: Any, *, target_id: Any = "target-1", **changes: Any) -> Any:
    values = {
        "target_id": target_id,
        "observed_at": datetime.now(UTC).isoformat(),
        "capacity": {
            "free_bytes": 8_000_000_000,
            "memory_bytes": 4_000_000_000,
            "cpu_millicores": 2000,
        },
        "latency_ms": 40,
        "cost_microusd_month": 0,
        "region": "test",
        "enrollment_generation": 1,
    }
    values.update(changes)
    unsigned = ResourceObservation.create_signed_payload(**values)
    values["signature"] = base64.urlsafe_b64encode(private.sign(unsigned.signing_bytes())).decode()
    return unsigned.with_signature(values["signature"])


def test_only_registered_targets_are_queried_and_signed_observation_is_returned(
    tmp_path: Any,
) -> None:
    database, policy, _, private = _setup(tmp_path)
    provider = StaticObservationProvider({"target-1": _observation(private)})

    results = MigrationDiscovery(database, provider).discover("Noyra-0001", policy)

    assert provider.requested == ["target-1"]
    assert len(results) == 1
    assert results[0].eligible is True
    observation = results[0].observation
    assert observation is not None
    assert observation.free_bytes == 8_000_000_000


def test_stale_observation_fails_closed(tmp_path: Any) -> None:
    database, policy, _, private = _setup(tmp_path)
    stale = _observation(
        private,
        observed_at=(datetime.now(UTC) - timedelta(minutes=10)).isoformat(),
    )

    result = MigrationDiscovery(database, StaticObservationProvider({"target-1": stale})).discover(
        "Noyra-0001", policy
    )

    assert len(result) == 1
    assert result[0].eligible is False
    assert "resource_observation_stale" in result[0].reasons


def test_unsigned_or_wrong_generation_observation_fails_closed(tmp_path: Any) -> None:
    database, policy, _, private = _setup(tmp_path)
    observation = _observation(private, enrollment_generation=2)

    result = MigrationDiscovery(
        database, StaticObservationProvider({"target-1": observation})
    ).discover("Noyra-0001", policy)

    assert result[0].eligible is False
    assert "resource_observation_invalid" in result[0].reasons


def test_disabled_policy_never_contacts_provider(tmp_path: Any) -> None:
    database, _policy, _, private = _setup(tmp_path)
    disabled = MigrationPolicy.default("Noyra-0001")
    provider = StaticObservationProvider({"target-1": _observation(private)})

    result = MigrationDiscovery(database, provider).discover("Noyra-0001", disabled)

    assert result == []
    assert provider.requested == []


def test_missing_observation_is_explicitly_unavailable(tmp_path: Any) -> None:
    database, policy, _, _ = _setup(tmp_path)

    result = MigrationDiscovery(database, StaticObservationProvider({})).discover(
        "Noyra-0001", policy
    )

    assert result[0].eligible is False
    assert result[0].reasons == ("resource_observation_unavailable",)


def test_observation_payload_is_bounded_and_digest_bound() -> None:
    with pytest.raises(ValueError, match="capacity"):
        ResourceObservation.create_signed_payload(
            target_id="target-1",
            observed_at=datetime.now(UTC).isoformat(),
            capacity={"free_bytes": -1},
            latency_ms=1,
            cost_microusd_month=0,
            region="test",
            enrollment_generation=1,
        )
