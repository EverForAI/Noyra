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


def test_credential_verification_returns_target_bound_receipt() -> None:
    plan = CredentialRebinder.plan({"model": "source-secret"}, {"model": "systemd:model"})
    from noyra.core.types import content_hash

    task_id = "task-credential"
    manifest_digest = "a" * 64
    identity = "target-host-1"
    proof = content_hash(
        {
            "task_id": task_id,
            "manifest_digest": manifest_digest,
            "target_id": "target-1",
            "target_identity": identity,
            "references": plan.references,
            "fingerprints": plan.fingerprints,
        }
    )
    receipt = CredentialRebinder.verify(
        plan,
        {
            "status": "verified",
            "target_id": "target-1",
            "target_identity": identity,
            "availability_proof": proof,
        },
        task_id=task_id,
        manifest_digest=manifest_digest,
    )
    assert receipt.status == "verified"
    assert receipt.availability_proof == proof
