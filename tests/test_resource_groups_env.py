"""Focused validation tests for environment-backed model resources."""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
    resource_groups_from_env,
)
from noyra.model.errors import ConfigurationError
from noyra.service import NoyraService


def _payload(api_keys: object) -> str:
    return json.dumps(
        [
            {
                "label": "environment-resource",
                "base_url": "https://models.example/v1",
                "model": "environment-model",
                "api_keys": api_keys,
            }
        ]
    )


def test_resource_groups_from_env_accepts_only_string_api_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOYRA_ECONOMY_MODEL_GROUPS_JSON", _payload(["first", "second"]))

    groups = resource_groups_from_env("economy")

    assert len(groups) == 1
    assert [key.get_secret_value() for key in groups[0].api_keys] == ["first", "second"]


@pytest.mark.parametrize(
    "api_keys",
    [None, True, 7, {"key": "value"}, ["valid", None], ["valid", False], ["valid", 7]],
)
def test_resource_groups_from_env_rejects_non_string_api_keys(
    monkeypatch: pytest.MonkeyPatch, api_keys: object
) -> None:
    monkeypatch.setenv("NOYRA_ECONOMY_MODEL_GROUPS_JSON", _payload(api_keys))

    with pytest.raises(ConfigurationError, match="invalid economy model group configuration"):
        resource_groups_from_env("economy")


def test_resource_groups_from_env_resolves_api_key_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    key_path = tmp_path / "model.key"
    key_path.write_text("file-group-secret\n", encoding="utf-8")
    if os.name == "posix":
        key_path.chmod(0o600)
    monkeypatch.setenv(
        "NOYRA_ECONOMY_MODEL_GROUPS_JSON",
        json.dumps(
            [
                {
                    "label": "file-resource",
                    "base_url": "https://models.example/v1",
                    "model": "file-model",
                    "api_key_files": [str(key_path)],
                }
            ]
        ),
    )

    groups = resource_groups_from_env("economy")

    assert groups[0].api_keys[0].get_secret_value() == "file-group-secret"


def test_environment_reasoning_requires_pinned_capability_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = json.loads(_payload(["key"]))
    payload[0]["model"] = "provider-alias"
    payload[0]["reasoning_effort"] = "high"
    monkeypatch.setenv("NOYRA_ECONOMY_MODEL_GROUPS_JSON", json.dumps(payload))
    with pytest.raises(ConfigurationError, match="capability_revision"):
        resource_groups_from_env("economy")

    payload[0]["capability_revision"] = 3
    payload[0]["capability_digest"] = "a" * 64
    monkeypatch.setenv("NOYRA_ECONOMY_MODEL_GROUPS_JSON", json.dumps(payload))
    groups = resource_groups_from_env("economy")
    assert groups[0].capability_revision == 3
    assert groups[0].capability_digest == "a" * 64


@pytest.mark.parametrize("status", ("active", "disabled", "revoked"))
def test_environment_sync_respects_durable_resource_status(
    tmp_path: Path, status: str, caplog: pytest.LogCaptureFixture
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-environment-resource-sync"
    IdentityStore(database).ensure(subject_id, content_hash(subject_id))
    store = CognitiveResourceStore(database, tmp_path / "secrets")
    proposal = CognitiveResourceGroupInput(
        pool="economy",
        label="environment-resource",
        base_url="https://models.example/v1",
        model="environment-model",
        api_keys=(SecretStr("environment-api-key"),),
    )
    record = store.configure(subject_id, proposal, actor="operator")
    if status == "disabled":
        store.disable(
            record.group_id,
            reason="operator requested disable",
            actor="operator",
            subject_id=subject_id,
        )
    elif status == "revoked":
        store.revoke(
            record.group_id,
            reason="operator revoked resource",
            actor="operator",
            subject_id=subject_id,
        )

    owner = SimpleNamespace(
        kernel=SimpleNamespace(database=database, subject_id=subject_id),
        cognitive_resources=store,
    )
    NoyraService._sync_cognitive_resources(cast(Any, owner), (proposal,))

    persisted = store.get(record.group_id, subject_id=subject_id)
    assert persisted.status == status
    assert [item.group_id for item in store.list(subject_id)] == [record.group_id]
    if status == "active":
        assert f"environment_model_resource_{status}" not in caplog.text
    else:
        assert f"environment_model_resource_{status}" in caplog.text
