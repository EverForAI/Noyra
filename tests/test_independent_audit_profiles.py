"""Cross-operation regressions for the independent October audit."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.resources import CognitiveResourceGroupInput, CognitiveResourceStore
from noyra.service import NoyraService


def stores(tmp_path: Path) -> tuple[str, CapabilityProfileStore, CapabilityProfileStore]:
    subject = "Noyra-profile-regression"
    databases = [Database(tmp_path / name) for name in ("source.sqlite3", "target.sqlite3")]
    for database in databases:
        IdentityStore(database).ensure(subject, content_hash(subject))
    return subject, CapabilityProfileStore(databases[0]), CapabilityProfileStore(databases[1])


def exported(store: CapabilityProfileStore, subject: str, effort: Any) -> dict[str, Any]:
    profile = store.record_probe_success(
        subject, "openai_compatible", "https://models.example/v1", "provider-alias", effort
    )
    assert profile is not None
    return profile.export(
        provider_type="openai_compatible",
        base_url="https://models.example/v1",
        model="provider-alias",
    )


def test_profile_upgrade_preserves_identity_hash_and_transaction(tmp_path: Path) -> None:
    subject, source, target = stores(tmp_path)
    first = exported(source, subject, "low")
    target.import_profiles(subject, [first])
    with target.database.connection() as connection:
        identity = connection.execute(
            "SELECT profile_id FROM model_capability_profiles"
        ).fetchone()[0]
    second = exported(source, subject, "high")
    assert target.import_profiles(subject, [second]) == 1
    with target.database.connection() as connection:
        row = dict(connection.execute("SELECT * FROM model_capability_profiles").fetchone())
    assert row["profile_id"] == identity
    state_hash = row.pop("state_hash")
    assert content_hash(row) == state_hash
    stored = target.get(subject, "openai_compatible", "https://models.example/v1", "provider-alias")
    assert stored is not None and stored.revision == 2
    assert target.import_profiles(subject, [second]) == 0
    with pytest.raises(ValueError, match="rollback"):
        target.import_profiles(subject, [first])
    third = exported(source, subject, "medium")
    with pytest.raises(ValueError):
        target.import_profiles(subject, [third, {"model": "invalid"}])
    stored = target.get(subject, "openai_compatible", "https://models.example/v1", "provider-alias")
    assert stored is not None and stored.revision == 2
    latest = source.get(subject, "openai_compatible", "https://models.example/v1", "provider-alias")
    assert latest is not None
    conflict = replace(latest, revision=2)
    with pytest.raises(ValueError, match="revision conflict"):
        target.import_profiles(
            subject,
            [
                conflict.export(
                    provider_type="openai_compatible",
                    base_url="https://models.example/v1",
                    model="provider-alias",
                )
            ],
        )


def test_bootstrap_online_probe_and_restart_preserve_newer_state(tmp_path: Path) -> None:
    subject, source, target = stores(tmp_path)
    first = exported(source, subject, "high")
    target.import_profiles(subject, [first], bootstrap=True)
    resources = CognitiveResourceStore(target.database, tmp_path / "secrets")
    proposal = CognitiveResourceGroupInput(
        pool="economy",
        label="configured",
        base_url="https://models.example/v1",
        model="provider-alias",
        reasoning_effort="high",
        capability_revision=first["revision"],
        capability_digest=first["profile_digest"],
        api_keys=(SecretStr("test-key"),),
    )
    owner = SimpleNamespace(
        kernel=SimpleNamespace(database=target.database, subject_id=subject),
        cognitive_resources=resources,
    )
    NoyraService._sync_cognitive_resources(owner, (proposal,))  # type: ignore[arg-type]
    newer = exported(target, subject, "high")
    assert newer["revision"] > first["revision"]
    assert target.import_profiles(subject, [first], bootstrap=True) == 0
    NoyraService._sync_cognitive_resources(owner, (proposal,))  # type: ignore[arg-type]
    # A stale reference may not provision a new group, even after bootstrap.
    new_proposal = proposal.model_copy(update={"label": "new-group"})
    with pytest.raises(RuntimeError, match="capability_profile_required"):
        NoyraService._sync_cognitive_resources(owner, (new_proposal,))  # type: ignore[arg-type]
    with target.database.transaction() as connection:
        connection.execute("UPDATE model_capability_profiles SET state_hash=?", ("0" * 64,))
    with pytest.raises(IntegrityError, match="hash mismatch"):
        target.import_profiles(subject, [first], bootstrap=True)


def test_active_and_environment_identity_survive_management_page_boundary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("noyra.model.resources.MAX_RESOURCE_PAGE_SIZE", 1)
    subject, _, target = stores(tmp_path)
    resources = CognitiveResourceStore(target.database, tmp_path / "secrets")
    proposal = CognitiveResourceGroupInput(
        pool="economy",
        label="a-disabled",
        base_url="https://models.example/v1",
        model="alias",
        api_keys=(SecretStr("test-key"),),
    )
    disabled = resources.configure(subject, proposal, actor="operator")
    resources.disable(disabled.group_id, reason="disabled", actor="operator", subject_id=subject)
    active_proposal = proposal.model_copy(update={"label": "z-active"})
    active = resources.configure(subject, active_proposal, actor="operator")
    assert [item.group_id for item in resources.list(subject)] == [disabled.group_id]
    assert [item.group_id for item in resources.active(subject, "economy")] == [active.group_id]
    owner = SimpleNamespace(
        kernel=SimpleNamespace(database=target.database, subject_id=subject),
        cognitive_resources=resources,
    )
    NoyraService._sync_cognitive_resources(owner, (active_proposal,))  # type: ignore[arg-type]
    key = resources.keys(active.group_id, subject_id=subject)[0]
    resources.revoke_key(key.key_id, reason="revoked", actor="operator", subject_id=subject)
    with pytest.raises(RuntimeError, match="key_revoked"):
        NoyraService._sync_cognitive_resources(owner, (active_proposal,))  # type: ignore[arg-type]
    assert resources.select_key(active.group_id, subject_id=subject) is None


@pytest.mark.parametrize(
    "mismatch", [None, "profile-id", "state-hash", "trusted-export", "evidence-failure"]
)
def test_profile_recovery_requires_exact_export_pin_and_durable_authorization(
    tmp_path: Path, mismatch: str | None
) -> None:
    subject, source, target = stores(tmp_path)
    first = exported(source, subject, "low")
    target.import_profiles(subject, [first])
    second = exported(source, subject, "high")
    target.import_profiles(subject, [second])
    with target.database.transaction() as connection:
        row = dict(connection.execute("SELECT * FROM model_capability_profiles").fetchone())
        # Reproduce the old writer's hash over a different generated profile id.
        old_state = {key: value for key, value in row.items() if key != "state_hash"}
        old_state["profile_id"] = "mcap_previous_bug_generated_id"
        bad_hash = content_hash(old_state)
        connection.execute("UPDATE model_capability_profiles SET state_hash=?", (bad_hash,))
    with pytest.raises(IntegrityError):
        target.import_profiles(subject, [second])
    evidence: list[dict[str, Any]] = []

    def record(item: dict[str, Any]) -> None:
        if mismatch == "evidence-failure":
            raise OSError("evidence disk unavailable")
        evidence.append(item)

    arguments: dict[str, Any] = dict(
        expected_profile_id="wrong" if mismatch == "profile-id" else row["profile_id"],
        expected_state_hash="f" * 64 if mismatch == "state-hash" else bad_hash,
        record_evidence=record,
    )
    if mismatch:
        with pytest.raises((ValueError, OSError)):
            target.repair_imported_profile(
                subject, first if mismatch == "trusted-export" else second, **arguments
            )
        with pytest.raises(IntegrityError):
            target.get(subject, "openai_compatible", "https://models.example/v1", "provider-alias")
    else:
        target.repair_imported_profile(subject, second, **arguments)
        repaired = target.get(
            subject, "openai_compatible", "https://models.example/v1", "provider-alias"
        )
        assert repaired is not None and repaired.revision == 2
        assert len(evidence) == 1 and evidence[0]["before"]["state_hash"] == bad_hash
        with pytest.raises(ValueError, match="healthy"):
            target.repair_imported_profile(
                subject,
                second,
                expected_profile_id=row["profile_id"],
                expected_state_hash=evidence[0]["intended_state_hash"],
                record_evidence=record,
            )


@pytest.mark.parametrize("evidence_location", ["external", "inside-alias"])
def test_offline_profile_repair_command_records_authorization(
    tmp_path: Path, evidence_location: str
) -> None:
    import json
    import os
    import subprocess
    import sys

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    subject, source, target = stores(data)
    profile = exported(source, subject, "high")
    target.import_profiles(subject, [profile])
    with target.database.transaction() as connection:
        row = dict(connection.execute("SELECT * FROM model_capability_profiles").fetchone())
        connection.execute("UPDATE model_capability_profiles SET state_hash=?", ("0" * 64,))
    trusted_file = tmp_path / "trusted.json"
    trusted_file.write_text(json.dumps({"profiles": [profile]}), encoding="utf-8")
    trusted_file.chmod(0o600)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    evidence = (
        evidence_root / ".." / "data" / "authorization.json"
        if evidence_location == "inside-alias"
        else evidence_root / "authorization.json"
    )
    project = Path(__file__).resolve().parents[1]
    command = [
        sys.executable,
        str(project / "scripts" / "repair-capability-profile.py"),
        "--database",
        str(target.database.path),
        "--subject",
        subject,
        "--profile-file",
        str(trusted_file),
        "--expected-profile-id",
        row["profile_id"],
        "--expected-state-hash",
        "0" * 64,
        "--evidence-path",
        str(evidence),
    ]
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(project / "src")},
    )
    if evidence_location == "inside-alias":
        assert completed.returncode != 0 and not evidence.exists()
        with target.database.connection() as connection:
            assert (
                connection.execute("SELECT state_hash FROM model_capability_profiles").fetchone()[0]
                == "0" * 64
            )
        return
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert json.loads(completed.stdout)["status"] == "repaired"
    assert json.loads(evidence.read_text(encoding="utf-8"))["before"]["state_hash"] == "0" * 64
    assert (
        target.get(subject, "openai_compatible", "https://models.example/v1", "provider-alias")
        is not None
    )


def test_offline_repair_refuses_foreign_schema_without_initializing_it(tmp_path: Path) -> None:
    import json
    import os
    import sqlite3
    import subprocess
    import sys
    from contextlib import closing

    data = tmp_path / "data"
    data.mkdir(mode=0o700)
    subject, source, _ = stores(data)
    trusted = tmp_path / "trusted.json"
    trusted.write_text(json.dumps(exported(source, subject, "high")), encoding="utf-8")
    trusted.chmod(0o600)
    database = data / "foreign.sqlite3"
    with closing(sqlite3.connect(database)) as connection, connection:
        connection.execute("CREATE TABLE sentinel (value TEXT)")
        connection.execute("INSERT INTO sentinel VALUES('untouched')")
    database.chmod(0o600)
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir(mode=0o700)
    evidence = evidence_root / "authorization.json"
    project = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            str(project / "scripts" / "repair-capability-profile.py"),
            "--database",
            str(database),
            "--subject",
            subject,
            "--profile-file",
            str(trusted),
            "--expected-profile-id",
            "mcap_expected",
            "--expected-state-hash",
            "0" * 64,
            "--evidence-path",
            str(evidence),
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(project / "src")},
    )
    assert completed.returncode != 0
    assert not evidence.exists()
    with closing(sqlite3.connect(database)) as connection:
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [("sentinel",)]
        assert connection.execute("SELECT value FROM sentinel").fetchall() == [("untouched",)]
