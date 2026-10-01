from __future__ import annotations

import pytest

from noyra.migration.credentials import CredentialBindingReceipt, CredentialRebinder


def test_credential_plan_only_contains_references() -> None:
    plan = CredentialRebinder.plan(
        {"model": "/etc/noyra/model.key", "token": "secret"}, {"model": "systemd:model"}
    )
    assert plan.references == {"model": "systemd:model"}
    assert "secret" not in repr(plan)


def test_credential_plan_rejects_raw_secret_values() -> None:
    with pytest.raises(ValueError, match="reference"):
        CredentialRebinder.plan({}, {"model": "sk-live-secret-value"})


def test_credential_apply_returns_only_references_and_fingerprints() -> None:
    plan = CredentialRebinder.plan(
        {
            "model": "model-secret",
            "search": "/etc/noyra/search.key",
            "operator": "operator-token",
        },
        {"model": "systemd:model", "search": "file:/etc/noyra/search.key"},
    )
    receipt = CredentialRebinder.apply(plan)
    assert isinstance(receipt, CredentialBindingReceipt)
    assert receipt.references == plan.references
    assert receipt.fingerprints["model"] != "model-secret"
    assert "model-secret" not in repr(receipt)
    assert receipt.to_dict()["status"] == "rebind_required"


def test_credential_reference_rejects_traversal_and_unknown_scheme() -> None:
    with pytest.raises(ValueError, match="reference"):
        CredentialRebinder.plan({}, {"model": "file:../secret"})
    with pytest.raises(ValueError, match="reference"):
        CredentialRebinder.plan({}, {"model": "https://example.test/key"})
