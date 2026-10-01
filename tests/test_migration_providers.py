from __future__ import annotations

import pytest

from noyra.migration.policy import MigrationPolicy
from noyra.migration.providers import RegisteredTargetProvider, TargetCandidate


def test_provider_is_disabled_until_policy_is_enabled() -> None:
    candidate = TargetCandidate("target-1", "us", "provider", "a" * 40, True, 10, 5)
    policy = MigrationPolicy.default("Noyra-0001")
    assert RegisteredTargetProvider([candidate]).list_candidates(policy) == []


def test_provider_never_provisions_implicitly() -> None:
    policy = MigrationPolicy.default("Noyra-0001").with_updates(enabled=True)
    candidate = TargetCandidate("target-1", "us", "provider", "a" * 40, True, 10, 5)
    with pytest.raises(RuntimeError, match="register"):
        RegisteredTargetProvider([candidate]).provision(candidate, policy)
