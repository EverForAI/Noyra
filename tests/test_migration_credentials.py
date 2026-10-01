from __future__ import annotations

# ruff: noqa: E501
import pytest

from noyra.migration.credentials import CredentialRebinder


def test_credential_plan_only_contains_references() -> None:
    plan = CredentialRebinder.plan({"model": "/etc/noyra/model.key", "token": "secret"}, {"model": "systemd:model"})
    assert plan.references == {"model": "systemd:model"}
    assert "secret" not in repr(plan)


def test_credential_plan_rejects_raw_secret_values() -> None:
    with pytest.raises(ValueError, match="reference"):
        CredentialRebinder.plan({}, {"model": "sk-live-secret-value"})
