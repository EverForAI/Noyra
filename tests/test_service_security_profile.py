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
    if changes.get("profile") == "production":
        values["at_rest_mode"] = "required"
        values["backup_keyring_path"] = Path(".runtime/test-security-profile/backup.keys")
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


def test_container_internal_production_profile_allows_container_listener() -> None:
    settings = _settings(
        profile="production",
        deployment_profile="container_internal",
        host="0.0.0.0",
        allow_insecure_non_loopback=True,
    )
    assert settings.deployment_profile == "container_internal"


def test_container_internal_profile_requires_explicit_insecure_opt_in() -> None:
    with pytest.raises(ValueError, match="container_internal"):
        _settings(
            profile="production",
            deployment_profile="container_internal",
            host="0.0.0.0",
        )


def test_from_env_reads_profile_and_production_cookie_default() -> None:
    environment = {
        "NOYRA_PROFILE": "production",
        "NOYRA_DATA_DIR": ".runtime/test-security-profile-env",
        "NOYRA_SUBJECT_ID": "Noyra-security-profile-env",
        "NOYRA_GENESIS_HASH": content_hash({"test": "security-profile-env"}),
        "NOYRA_ADMIN_TOKEN": "admin-token-with-sufficient-entropy-123456",
        "NOYRA_HOST": "127.0.0.1",
        "NOYRA_AT_REST_MODE": "required",
        "NOYRA_BACKUP_KEYRING_PATH": ".runtime/test-security-profile-env/backup.keys",
    }
    with patch.dict(os.environ, environment, clear=False):
        settings = ServiceSettings.from_env()
    assert settings.profile == "production"
    assert settings.admin_session_cookie_secure is True


def test_production_settings_require_at_rest_enforcement() -> None:
    with pytest.raises(ValueError, match="production profile requires at-rest protection"):
        _settings(
            profile="production",
            at_rest_mode="development",
            backup_keyring_path=None,
        )


def test_production_from_env_requires_explicit_genesis_hash() -> None:
    environment = {
        "NOYRA_PROFILE": "production",
        "NOYRA_DATA_DIR": ".runtime/test-security-profile-genesis",
        "NOYRA_SUBJECT_ID": "Noyra-security-profile-genesis",
        "NOYRA_ADMIN_TOKEN": "admin-token-with-sufficient-entropy-123456",
        "NOYRA_HOST": "127.0.0.1",
        "NOYRA_AT_REST_MODE": "required",
        "NOYRA_BACKUP_KEYRING_PATH": ".runtime/test-security-profile-genesis/backup.keys",
    }
    with (
        patch.dict(os.environ, environment, clear=True),
        pytest.raises(ValueError, match="explicit NOYRA_GENESIS_HASH"),
    ):
        ServiceSettings.from_env()


def test_production_from_env_defaults_runtime_export_off() -> None:
    environment = {
        "NOYRA_PROFILE": "production",
        "NOYRA_DATA_DIR": ".runtime/test-security-profile-export",
        "NOYRA_SUBJECT_ID": "Noyra-security-profile-export",
        "NOYRA_GENESIS_HASH": content_hash({"test": "export"}),
        "NOYRA_ADMIN_TOKEN": "admin-token-with-sufficient-entropy-123456",
        "NOYRA_HOST": "127.0.0.1",
        "NOYRA_AT_REST_MODE": "required",
        "NOYRA_BACKUP_KEYRING_PATH": ".runtime/test-security-profile-export/backup.keys",
    }
    with patch.dict(os.environ, environment, clear=True):
        settings = ServiceSettings.from_env()
    assert settings.developer_log_export_enabled is False


def test_operator_token_file_rejects_symlink(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlink is unavailable")
    target = tmp_path / "target.token"
    target.write_text("operator-token-with-sufficient-entropy-123456", encoding="ascii")
    link = tmp_path / "operator.token"
    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable")
    with pytest.raises(ValueError, match="operator token file"):
        ServiceSettings._read_operator_token_file(link)


def test_preflight_env_file_does_not_inherit_unlisted_noyra_variables(tmp_path: Path) -> None:
    hash_key = tmp_path / "public-hash.key"
    hash_key.write_text("stable-public-ip-hash-key-which-is-long-enough", encoding="utf-8")
    if os.name == "posix":
        hash_key.chmod(0o600)
    env_file = tmp_path / "noyra.env"
    env_file.write_text(
        "\n".join(
            [
                "NOYRA_PROFILE=production",
                "NOYRA_DATA_DIR=" + str(tmp_path / "data"),
                "NOYRA_SUBJECT_ID=Noyra-security-profile-env-file",
                "NOYRA_GENESIS_HASH=" + content_hash({"test": "env-file"}),
                "NOYRA_ADMIN_TOKEN=admin-token-with-sufficient-entropy-123456",
                "NOYRA_HOST=127.0.0.1",
                "NOYRA_PUBLIC_SITE_URL=https://admin.example",
                "NOYRA_PUBLIC_HASH_KEY_FILE=" + str(hash_key),
                "NOYRA_AT_REST_MODE=required",
                "NOYRA_BACKUP_KEYRING_PATH=" + str(tmp_path / "backup.keys"),
            ]
        ),
        encoding="utf-8",
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PYTHONPATH": "src",
            "NOYRA_TRUSTED_PROXY_CIDRS": "192.0.2.0/24",
        }
    )
    result = subprocess.run(
        [sys.executable, "scripts/preflight-production.py", "--env-file", str(env_file)],
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
            "NOYRA_AT_REST_MODE": "required",
            "NOYRA_BACKUP_KEYRING_PATH": ".runtime/test-security-profile-preflight/backup.keys",
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
            "NOYRA_AT_REST_MODE": "required",
            "NOYRA_BACKUP_KEYRING_PATH": str(tmp_path / "backup.keys"),
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
