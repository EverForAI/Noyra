from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core.credentials import CredentialError, read_env_secret
from noyra.core.types import content_hash
from noyra.model.errors import ConfigurationError
from noyra.model.resources import resource_groups_from_env
from noyra.service import ServiceSettings


def test_production_operator_token_rejects_inline_source() -> None:
    environment = {
        "NOYRA_PROFILE": "production",
        "NOYRA_DATA_DIR": ".runtime/test-production-secret-source",
        "NOYRA_SUBJECT_ID": "Noyra-production-secret-source",
        "NOYRA_GENESIS_HASH": content_hash({"test": "production-secret-source"}),
        "NOYRA_OPERATOR_TOKEN": "inline-operator-token-with-sufficient-entropy",
        "NOYRA_HOST": "127.0.0.1",
        "NOYRA_AT_REST_MODE": "required",
        "NOYRA_BACKUP_KEYRING_PATH": ".runtime/test-production-secret-source/backup.keys",
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        pytest.raises(ValueError, match="inline secret is disabled for operator token"),
    ):
        ServiceSettings.from_env()


def test_production_model_group_rejects_inline_api_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOYRA_PROFILE", "production")
    monkeypatch.setenv(
        "NOYRA_ECONOMY_MODEL_GROUPS_JSON",
        json.dumps(
            [
                {
                    "group_id": "economy-inline",
                    "provider": "openai-compatible",
                    "base_url": "https://model.example/v1",
                    "model": "model-v1",
                    "api_keys": ["inline-provider-secret"],
                }
            ]
        ),
    )

    with pytest.raises(ConfigurationError, match="managed secret source"):
        resource_groups_from_env("economy")


def test_production_managed_secret_rejects_stale_inline_value(tmp_path: Path) -> None:
    secret_path = tmp_path / "provider.key"
    secret_path.write_text("managed-provider-secret", encoding="utf-8")
    with (
        patch.dict(
            os.environ,
            {
                "NOYRA_PROFILE": "production",
                "NOYRA_PROVIDER_API_KEY_FILE": str(secret_path),
                "NOYRA_PROVIDER_API_KEY": "stale-inline-provider-secret",
            },
            clear=True,
        ),
        pytest.raises(CredentialError, match="inline secret is disabled"),
    ):
        read_env_secret(
            value_var="NOYRA_PROVIDER_API_KEY",
            file_var="NOYRA_PROVIDER_API_KEY_FILE",
            credential_var="NOYRA_PROVIDER_API_KEY_CREDENTIAL",
            label="provider API key",
            allow_inline=False,
        )


def test_preflight_reports_inline_model_group_api_keys(tmp_path: Path) -> None:
    hash_key = tmp_path / "public-hash.key"
    hash_key.write_text("stable-public-ip-hash-key-which-is-long-enough", encoding="utf-8")
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": "src",
            "NOYRA_PROFILE": "production",
            "NOYRA_DATA_DIR": str(tmp_path / "data"),
            "NOYRA_SUBJECT_ID": "Noyra-production-group-preflight",
            "NOYRA_GENESIS_HASH": content_hash({"test": "group-preflight"}),
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PUBLIC_SITE_URL": "https://admin.example",
            "NOYRA_PUBLIC_HASH_KEY_FILE": str(hash_key),
            "NOYRA_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
            "NOYRA_AT_REST_MODE": "required",
            "NOYRA_BACKUP_KEYRING_PATH": str(tmp_path / "backup.keys"),
            "NOYRA_ECONOMY_MODEL_GROUPS_JSON": json.dumps(
                [
                    {
                        "group_id": "economy-inline",
                        "provider": "openai-compatible",
                        "base_url": "https://model.example/v1",
                        "model": "model-v1",
                        "api_keys": ["inline-provider-secret"],
                    }
                ]
            ),
        }
    )
    result = subprocess.run(
        [sys.executable, "scripts/preflight-production.py"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    payload = json.loads(result.stdout)
    assert result.returncode != 0
    assert any(
        item["id"] == "economy_model_group_api_key_source" and item["status"] == "fail"
        for item in payload["checks"]
    )
