from __future__ import annotations

import os
from pathlib import Path

import pytest

from noyra.core.credentials import CredentialError, read_env_secret, read_secret_file


def _private_file(path: Path, value: str) -> Path:
    path.write_text(value, encoding="utf-8")
    if os.name == "posix":
        path.chmod(0o600)
    return path


def test_secret_file_is_trimmed_and_bounded(tmp_path: Path) -> None:
    path = _private_file(tmp_path / "provider.key", "  provider-secret\n")

    assert read_secret_file(path, label="provider API key") == "provider-secret"


def test_secret_file_rejects_embedded_control_data(tmp_path: Path) -> None:
    path = _private_file(tmp_path / "provider.key", "provider-secret\nsecond-line")

    with pytest.raises(CredentialError, match="content is invalid"):
        read_secret_file(path, label="provider API key")


def test_file_source_overrides_legacy_inline_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _private_file(tmp_path / "provider.key", "file-secret\n")
    monkeypatch.setenv("NOYRA_TEST_SECRET", "inline-secret")
    monkeypatch.setenv("NOYRA_TEST_SECRET_FILE", str(path))
    monkeypatch.delenv("NOYRA_TEST_SECRET_CREDENTIAL", raising=False)

    assert (
        read_env_secret(
            value_var="NOYRA_TEST_SECRET",
            file_var="NOYRA_TEST_SECRET_FILE",
            credential_var="NOYRA_TEST_SECRET_CREDENTIAL",
            label="provider API key",
        )
        == "file-secret"
    )


def test_systemd_credential_source_is_supported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _private_file(tmp_path / "model-key", "credential-secret\n")
    monkeypatch.delenv("NOYRA_TEST_SECRET", raising=False)
    monkeypatch.delenv("NOYRA_TEST_SECRET_FILE", raising=False)
    monkeypatch.setenv("NOYRA_TEST_SECRET_CREDENTIAL", "model-key")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))

    assert (
        read_env_secret(
            value_var="NOYRA_TEST_SECRET",
            file_var="NOYRA_TEST_SECRET_FILE",
            credential_var="NOYRA_TEST_SECRET_CREDENTIAL",
            label="provider API key",
        )
        == "credential-secret"
    )


def test_secret_sources_cannot_be_combined(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = _private_file(tmp_path / "provider.key", "file-secret")
    monkeypatch.setenv("NOYRA_TEST_SECRET_FILE", str(path))
    monkeypatch.setenv("NOYRA_TEST_SECRET_CREDENTIAL", "model-key")

    with pytest.raises(CredentialError, match="choose one secret source"):
        read_env_secret(
            value_var="NOYRA_TEST_SECRET",
            file_var="NOYRA_TEST_SECRET_FILE",
            credential_var="NOYRA_TEST_SECRET_CREDENTIAL",
            label="provider API key",
        )


def test_missing_configured_file_fails_closed_instead_of_using_inline_value(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOYRA_TEST_SECRET", "inline-secret")
    monkeypatch.setenv("NOYRA_TEST_SECRET_FILE", str(tmp_path / "missing.key"))
    monkeypatch.delenv("NOYRA_TEST_SECRET_CREDENTIAL", raising=False)

    with pytest.raises(CredentialError, match="credential is unavailable"):
        read_env_secret(
            value_var="NOYRA_TEST_SECRET",
            file_var="NOYRA_TEST_SECRET_FILE",
            credential_var="NOYRA_TEST_SECRET_CREDENTIAL",
            label="credential",
        )


def test_secret_file_rejects_group_or_world_access_on_posix(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permission contract")
    path = _private_file(tmp_path / "provider.key", "provider-secret")
    path.chmod(0o644)

    with pytest.raises(CredentialError, match="must not be readable"):
        read_secret_file(path, label="provider API key")
