from __future__ import annotations

import base64
import io
import json
import os
import urllib.request
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.core import release_assurance as assurance
from noyra.core.external_gates import EXPECTED_GATE_IDS, _canonical_payload
from noyra.migration.policy import MigrationStore
from noyra.wallet import PaymentPolicyInput, WalletEconomyStore


def signed_evidence(age_days: int = 0) -> tuple[dict[str, Any], str]:
    key = Ed25519PrivateKey.generate()
    reviewed = datetime.now(UTC) - timedelta(days=age_days, minutes=2)
    record: dict[str, Any] = {
        "format": "noyra-external-gates/v1",
        "schema_version": 1,
        "commit_sha": "a" * 40,
        "status": "passed",
        "reviewed_at": reviewed.isoformat(),
        "reviewer": {"id": "reviewer", "role": "release-reviewer"},
        "gates": [
            {
                "id": name,
                "status": "passed",
                "executed_by": "operator",
                "reviewed_by": "reviewer",
                "started_at": (reviewed - timedelta(hours=1)).isoformat(),
                "finished_at": reviewed.isoformat(),
                "evidence_refs": ["isolated-host/report.json"],
            }
            for name in EXPECTED_GATE_IDS
        ],
    }
    record["signature"] = {
        "algorithm": "ed25519",
        "value": base64.b64encode(key.sign(_canonical_payload(record))).decode(),
    }
    public_key = base64.b64encode(
        key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode()
    return record, public_key


def test_runtime_does_not_expire_published_acceptance_every_72_hours() -> None:
    record, key = signed_evidence(age_days=30)
    assurance.verify_evidence(record, "a" * 40, key)
    reviewed = datetime.fromisoformat(record["reviewed_at"])
    assurance.verify_evidence(record, "a" * 40, key, publication=reviewed + timedelta(hours=1))
    with pytest.raises(ValueError, match="reviewed_at"):
        assurance.verify_evidence(record, "a" * 40, key, publication=datetime.now(UTC))


@pytest.mark.parametrize("change", ["sha", "signature", "reviewer", "secret", "gate", "window"])
def test_evidence_rejects_untrusted_or_incomplete_contract(change: str) -> None:
    record, key = signed_evidence()
    if change == "sha":
        record["commit_sha"] = "b" * 40
    elif change == "signature":
        _, key = signed_evidence()
    elif change == "reviewer":
        record["gates"][0]["executed_by"] = "reviewer"
    elif change == "secret":
        record["password"] = "example"
    elif change == "gate":
        record["gates"][0]["id"] = "unknown"
    else:
        record["gates"][0]["started_at"] = record["reviewed_at"]
    with pytest.raises(ValueError, match="release_evidence_invalid"):
        assurance.verify_evidence(record, "a" * 40, key)


def test_channel_defaults_stable_and_development_requires_explicit_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(assurance, "controlled_text", lambda path, **_: path.read_text())
    path = tmp_path / "channel"
    assert assurance.upgrade_channel(path) == "stable"
    path.write_text("development-main\n", encoding="utf-8")
    assert assurance.upgrade_channel(path) == "development-main"
    path.write_text("anything\n", encoding="utf-8")
    with pytest.raises(ValueError, match="upgrade_channel_invalid"):
        assurance.upgrade_channel(path)


def test_latest_draft_release_cannot_be_used_as_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(assurance, "github_json", lambda _: {"tag_name": "v0.1.0", "draft": True})
    with pytest.raises(ValueError, match="stable_release_unavailable"):
        assurance.stable_metadata("EverForAI", "Noyra")


def test_stable_verifier_requires_latest_same_sha_quality_before_download(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(assurance, "stable_metadata", lambda *_: {"sha": "a" * 40, "tag": "v0.1.0"})
    monkeypatch.setattr(
        assurance,
        "github_json",
        lambda _: {
            "workflow_runs": [
                {"id": 1, "head_sha": "a" * 40, "status": "completed", "conclusion": "success"},
                {"id": 2, "head_sha": "a" * 40, "status": "in_progress", "conclusion": None},
            ]
        },
    )
    with pytest.raises(ValueError, match="upgrade_quality_required"):
        assurance.verified_stable_target("a" * 40)
    with pytest.raises(ValueError, match="upgrade_target_stale"):
        assurance.verified_stable_target("b" * 40)


def test_stable_artifact_signature_verified_before_target_is_returned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    record, key = signed_evidence(age_days=5)
    publication = datetime.fromisoformat(record["reviewed_at"]) + timedelta(hours=1)
    monkeypatch.setattr(
        assurance,
        "stable_metadata",
        lambda *_: {"sha": "a" * 40, "tag": "v0.1.0", "published_at": publication.isoformat()},
    )
    monkeypatch.setattr(
        assurance,
        "github_json",
        lambda _: {
            "workflow_runs": [
                {"id": 1, "head_sha": "a" * 40, "status": "completed", "conclusion": "success"}
            ]
        },
    )
    monkeypatch.setattr(assurance, "controlled_text", lambda *_, **__: key)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *_, **__: io.BytesIO(json.dumps(record).encode()),
    )
    assert assurance.verified_stable_target("a" * 40) == ("v0.1.0", record)
    record["gates"][0]["evidence_refs"] = ["tampered.json"]
    with pytest.raises(ValueError, match="signature"):
        assurance.verified_stable_target("a" * 40)


def test_current_release_evidence_is_sha_bound_and_pointer_controlled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, key = signed_evidence()
    release = tmp_path / "releases" / "release"
    release.mkdir(parents=True)
    (release / ".noyra-source-sha").write_text("a" * 40)
    (release / assurance.EVIDENCE_NAME).write_text(json.dumps(record))
    public_key = tmp_path / "public-key"
    public_key.write_text(key)
    pointer = tmp_path / "current"
    try:
        pointer.symlink_to(release, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlink permission unavailable")
    monkeypatch.setattr(assurance, "CURRENT_PATH", pointer)
    monkeypatch.setattr(assurance, "PUBLIC_KEY_PATH", public_key)
    monkeypatch.setattr(assurance, "controlled_text", lambda path, **_: path.read_text())
    actual = pointer.lstat()
    root_pointer = os.stat_result(
        (
            actual.st_mode,
            actual.st_ino,
            actual.st_dev,
            actual.st_nlink,
            0,
            actual.st_gid,
            actual.st_size,
            actual.st_atime,
            actual.st_mtime,
            actual.st_ctime,
        )
    )
    original = Path.lstat

    def metadata(path: Path) -> os.stat_result:
        return root_pointer if path == pointer else original(path)

    with patch.object(Path, "lstat", metadata):
        assert assurance.current_evidence()["commit_sha"] == "a" * 40
        (release / ".noyra-source-sha").write_text("b" * 40)
        with pytest.raises(ValueError, match="commit_sha"):
            assurance.current_evidence()


def test_production_payment_gate_allows_pause_and_disable_without_evidence(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-release", "a" * 64)
    store = WalletEconomyStore(database, production=False)
    automatic = PaymentPolicyInput(
        mode="automatic", automation_enabled=True, per_order_limit="5", daily_limit="10"
    )
    store.update_policy("Noyra-release", automatic, expected_version=1, actor="operator")
    store.production = True
    with pytest.raises(ValueError, match="production_release_evidence_required"):
        store.update_policy("Noyra-release", automatic, expected_version=2, actor="operator")
    assert store.get_policy("Noyra-release").policy_version == 2
    store.update_policy(
        "Noyra-release",
        automatic.model_copy(update={"emergency_paused": True}),
        expected_version=2,
        actor="operator",
    )
    store.update_policy(
        "Noyra-release",
        automatic.model_copy(update={"automation_enabled": False}),
        expected_version=3,
        actor="operator",
    )
    # Persisted automatic policies cannot authorize new signing after restart.
    assert not store._policy_allows(
        None,
        {"payment_mode": "automatic"},
        {"mode": "automatic", "automation_enabled": True, "emergency_paused": False},
    )


def test_container_acceptance_binds_signed_evidence_to_image_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    record, key = signed_evidence()
    source = tmp_path / "image-source-sha"
    bundle = tmp_path / "acceptance"
    bundle.mkdir()
    source.write_text("a" * 40, encoding="utf-8")
    (bundle / "external-gates.json").write_text(json.dumps(record), encoding="utf-8")
    (bundle / "public-key").write_text(key, encoding="utf-8")
    monkeypatch.setenv("NOYRA_DEPLOYMENT_PROFILE", "container_internal")
    monkeypatch.setattr(assurance, "CONTAINER_SOURCE_PATH", source)
    monkeypatch.setattr(assurance, "CONTAINER_EVIDENCE_PATH", bundle)
    monkeypatch.setattr(assurance, "controlled_text", lambda path, **_: path.read_text())
    assert assurance.current_evidence()["commit_sha"] == "a" * 40
    source.write_text("b" * 40)
    with pytest.raises(ValueError, match="commit_sha"):
        assurance.current_evidence()
    with pytest.raises(ValueError, match="production_release_evidence_required"):
        assurance.require_activation_evidence(production=True)
    source.write_text("a" * 40)
    record["gates"][0]["evidence_refs"] = ["tampered.json"]
    (bundle / "external-gates.json").write_text(json.dumps(record))
    assert assurance.activation_status()["status"] == "unverified"
    source.unlink()
    monkeypatch.setenv("NOYRA_SOURCE_SHA", "a" * 40)
    assert assurance.activation_status()["status"] == "unverified"


def test_container_automation_evidence_mount_is_optional_and_read_only() -> None:
    root = Path(__file__).resolve().parents[1]
    compose = (root / "docker-compose.yml").read_text(encoding="utf-8")
    overlay = (root / "docker-compose.assurance.yml").read_text(encoding="utf-8")
    dockerfile = (root / "Dockerfile").read_text(encoding="utf-8")
    assert "NOYRA_SOURCE_SHA: ${NOYRA_SOURCE_SHA:-}" in compose
    assert "release-assurance" not in compose
    assert "read_only: true" in overlay
    assert "create_host_path: false" in overlay
    assert "target: /run/noyra/release-assurance" in overlay
    assert 'pathlib.Path("/opt/noyra/.noyra-source-sha")' in dockerfile
    assert "path.chmod(0o444)" in dockerfile


def test_production_migration_gate_preserves_manual_and_local_wallet_choice(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure("Noyra-release", "a" * 64)
    store = MigrationStore(database, production=True)
    manual = store.update_policy(
        "Noyra-release",
        1,
        {
            "enabled": True,
            "wallet_mode": "local_wallet_transfer",
            "local_wallet_transfer_enabled": True,
        },
        "operator",
    )
    assert manual.approval_mode == "manual"
    with pytest.raises(ValueError, match="production_release_evidence_required"):
        store.update_policy(
            "Noyra-release",
            2,
            {"approval_mode": "policy_auto", "allowed_target_ids": ["trusted-target"]},
            "operator",
        )
    assert store.read_policy("Noyra-release").revision == 2
    assert not store.update_policy("Noyra-release", 2, {"enabled": False}, "operator").enabled
