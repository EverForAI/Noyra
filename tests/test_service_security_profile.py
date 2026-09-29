from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import SecretStr

from noyra.core.types import content_hash
from noyra.service import ServiceSettings, _load_public_post_hash_key


def _settings(**changes: object) -> ServiceSettings:
    values: dict[str, object] = {
        "data_dir": Path(".runtime/test-security-profile"),
        "subject_id": "Noyra-security-profile",
        "genesis_hash": content_hash({"test": "security-profile"}),
        "admin_token": SecretStr("admin-token-with-sufficient-entropy-123456"),
    }
    values.update(changes)
    return ServiceSettings(**values)


def test_production_profile_defaults_admin_session_cookie_to_secure() -> None:
    settings = _settings(profile="production")
    assert settings.profile == "production"
    assert settings.admin_session_cookie_secure is True


def test_production_profile_rejects_insecure_non_loopback_listener() -> None:
    with pytest.raises(ValueError, match=r"production.*loopback"):
        _settings(
            profile="production",
            host="0.0.0.0",
            allow_insecure_non_loopback=True,
        )


def test_from_env_reads_profile_and_production_cookie_default() -> None:
    environment = {
        "NOYRA_PROFILE": "production",
        "NOYRA_DATA_DIR": ".runtime/test-security-profile-env",
        "NOYRA_SUBJECT_ID": "Noyra-security-profile-env",
        "NOYRA_GENESIS_HASH": content_hash({"test": "security-profile-env"}),
        "NOYRA_ADMIN_TOKEN": "admin-token-with-sufficient-entropy-123456",
        "NOYRA_HOST": "127.0.0.1",
    }
    with patch.dict(os.environ, environment, clear=False):
        settings = ServiceSettings.from_env()
    assert settings.profile == "production"
    assert settings.admin_session_cookie_secure is True


def test_production_preflight_reports_missing_trusted_proxy_cidr() -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": "src",
            "NOYRA_PROFILE": "production",
            "NOYRA_DATA_DIR": ".runtime/test-security-profile-preflight",
            "NOYRA_SUBJECT_ID": "Noyra-security-profile-preflight",
            "NOYRA_GENESIS_HASH": content_hash({"test": "security-profile-preflight"}),
            "NOYRA_ADMIN_TOKEN": "admin-token-with-sufficient-entropy-123456",
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PUBLIC_SITE_URL": "https://admin.example",
            "NOYRA_TRUSTED_PROXY_CIDRS": "",
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
        item["id"] == "trusted_proxy_cidrs" and item["status"] == "fail"
        for item in payload["checks"]
    )


def test_production_preflight_rejects_missing_persistent_public_hash_key(
    tmp_path: Path,
) -> None:
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": "src",
            "NOYRA_PROFILE": "production",
            "NOYRA_DATA_DIR": str(tmp_path / "data"),
            "NOYRA_SUBJECT_ID": "Noyra-security-profile-hash-key",
            "NOYRA_GENESIS_HASH": content_hash({"test": "security-profile-hash-key"}),
            "NOYRA_ADMIN_TOKEN": "admin-token-with-sufficient-entropy-123456",
            "NOYRA_HOST": "127.0.0.1",
            "NOYRA_PUBLIC_SITE_URL": "https://admin.example",
            "NOYRA_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
            "NOYRA_PUBLIC_HASH_KEY_FILE": str(tmp_path / "missing.key"),
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
        item["id"] == "public_hash_key" and item["status"] == "fail" for item in payload["checks"]
    )


def test_public_post_hash_key_is_stable_when_reloaded_after_restart(tmp_path: Path) -> None:
    key_path = tmp_path / "public-hash.key"
    key_path.write_text("stable-public-ip-hash-key-which-is-long-enough", encoding="utf-8")
    if os.name == "posix":
        key_path.chmod(0o600)
    settings = _settings(profile="production", public_hash_key_file=key_path)

    first_process_key = _load_public_post_hash_key(settings)
    restarted_process_key = _load_public_post_hash_key(settings)

    assert first_process_key == restarted_process_key
    assert first_process_key is not None and len(first_process_key) >= 32


def test_production_service_rejects_missing_public_post_hash_key(tmp_path: Path) -> None:
    settings = _settings(
        profile="production",
        public_hash_key_file=tmp_path / "missing-public-hash.key",
    )

    with pytest.raises(ValueError, match="persistent public anti-abuse hash key"):
        _load_public_post_hash_key(settings)
