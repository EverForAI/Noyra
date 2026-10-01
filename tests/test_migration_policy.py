from __future__ import annotations

import pytest

from noyra.core import Database, IdentityStore
from noyra.migration.policy import (
    MigrationPolicy,
    MigrationPolicyConflictError,
    MigrationStore,
)


def _store(tmp_path):
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-0001", "a" * 64)
    return database, MigrationStore(database)


def test_fresh_policy_is_disabled_and_safe(tmp_path) -> None:
    _, store = _store(tmp_path)

    policy = store.read_policy("Noyra-0001")

    assert policy.enabled is False
    assert policy.approval_mode == "disabled"
    assert policy.revision == 1
    assert policy.emergency_recovery_enabled is False
    assert policy.local_wallet_transfer_enabled is False
    assert policy.wallet_mode == "external_signer_rebind"
    assert policy.allowed_target_ids == ()


def test_enabling_without_mode_selects_manual(tmp_path) -> None:
    _, store = _store(tmp_path)

    policy = store.update_policy(
        "Noyra-0001",
        expected_revision=1,
        patch={"enabled": True},
        actor="operator",
    )

    assert policy.enabled is True
    assert policy.approval_mode == "manual"
    assert policy.revision == 2


def test_policy_auto_requires_non_empty_allowlist(tmp_path) -> None:
    _, store = _store(tmp_path)

    with pytest.raises(ValueError, match="target"):
        store.update_policy(
            "Noyra-0001",
            expected_revision=1,
            patch={"enabled": True, "approval_mode": "policy_auto"},
            actor="operator",
        )


def test_local_wallet_requires_explicit_opt_in(tmp_path) -> None:
    _, store = _store(tmp_path)

    with pytest.raises(ValueError, match="local wallet"):
        store.update_policy(
            "Noyra-0001",
            expected_revision=1,
            patch={"enabled": True, "wallet_mode": "local_wallet_transfer"},
            actor="operator",
        )


def test_stale_revision_is_rejected_without_mutation(tmp_path) -> None:
    _, store = _store(tmp_path)
    store.update_policy(
        "Noyra-0001",
        expected_revision=1,
        patch={"enabled": True},
        actor="operator",
    )

    with pytest.raises(MigrationPolicyConflictError):
        store.update_policy(
            "Noyra-0001",
            expected_revision=1,
            patch={"approval_mode": "manual"},
            actor="operator",
        )

    assert store.read_policy("Noyra-0001").revision == 2


def test_policy_model_rejects_unbounded_values() -> None:
    policy = MigrationPolicy.default("Noyra-0001")
    with pytest.raises(ValueError, match="cooldown"):
        policy.with_updates(rejection_cooldown_seconds=31_536_001)
