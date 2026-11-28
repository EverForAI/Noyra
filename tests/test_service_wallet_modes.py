from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

from noyra.core.at_rest import _windows_harden
from noyra.wallet.config import configured_wallet_signer_from_env


def test_wallet_mode_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NOYRA_WALLET_MODE",
        "NOYRA_WALLET_SIGNER_ENDPOINT",
        "NOYRA_WALLET_SIGNER_ID",
        "NOYRA_WALLET_SIGNER_BEARER_TOKEN",
        "NOYRA_WALLET_SIGNER_ADDRESS",
    ):
        monkeypatch.delenv(name, raising=False)
    assert configured_wallet_signer_from_env() is None


def test_explicit_disabled_overrides_legacy_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "disabled")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "legacy")
    assert configured_wallet_signer_from_env() is None


def test_legacy_endpoint_cannot_open_signer_or_resolve_credentials(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("NOYRA_WALLET_MODE", raising=False)
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "legacy")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_BEARER_TOKEN_FILE", "synthetic-private-path")
    read_secret = Mock(side_effect=AssertionError("credential must not be read"))
    factory = Mock(side_effect=AssertionError("signer must not be constructed"))
    monkeypatch.setattr("noyra.wallet.config.read_env_secret", read_secret)
    monkeypatch.setattr("noyra.wallet.config.HTTPSWalletSigner", factory)
    assert configured_wallet_signer_from_env() is None
    assert "set NOYRA_WALLET_MODE explicitly" in caplog.text
    assert "synthetic-private-path" not in caplog.text and "signer.example" not in caplog.text
    read_secret.assert_not_called()
    factory.assert_not_called()


def test_external_signer_can_bind_a_wallet_address(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "external")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "external")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ADDRESS", "0xA111111111111111111111111111111111111111")
    signer = configured_wallet_signer_from_env()
    assert signer is not None
    assert cast(Any, signer).address == "0xa111111111111111111111111111111111111111"
    cast(Any, signer).close()


def test_external_signer_bearer_token_can_use_a_private_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    token_path = tmp_path / "signer.token"
    token_path.write_text("file-signer-token\n", encoding="utf-8")
    if os.name == "posix":
        token_path.chmod(0o600)
    elif os.name == "nt":
        _windows_harden(tmp_path, entire_tree=True)
    monkeypatch.setenv("NOYRA_WALLET_MODE", "external")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "external")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_BEARER_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_BEARER_TOKEN", "inline-secret-must-not-win")

    signer = configured_wallet_signer_from_env()
    assert signer is not None
    assert cast(Any, signer).bearer_token == "file-signer-token"
    cast(Any, signer).close()


def test_local_requires_private_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
    for name in (
        "NOYRA_WALLET_KEYSTORE_PATH",
        "NOYRA_WALLET_PASSWORD_FILE",
        "NOYRA_WALLET_RPC_URLS_JSON",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(ValueError, match="local wallet configuration is invalid"):
        configured_wallet_signer_from_env()


def test_local_rejects_external_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(tmp_path / "wallet.json"))
    monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", str(tmp_path / "password"))
    monkeypatch.setenv("NOYRA_WALLET_RPC_URLS_JSON", json.dumps({"1": "https://rpc.example"}))
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    with pytest.raises(ValueError, match="local wallet configuration is invalid"):
        configured_wallet_signer_from_env()


def test_external_rejects_local_fields(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "external")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "external")
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(tmp_path / "wallet.json"))
    with pytest.raises(ValueError, match="external wallet configuration is invalid"):
        configured_wallet_signer_from_env()


def test_rpc_urls_reject_duplicate_or_non_https_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
    monkeypatch.setenv("NOYRA_PROFILE", "test")
    monkeypatch.setenv("NOYRA_WALLET_TESTNET_ENABLED", "true")
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(tmp_path / "wallet.json"))
    monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", str(tmp_path / "password"))
    monkeypatch.setenv(
        "NOYRA_WALLET_RPC_URLS_JSON", '{"1":"https://rpc.example","01":"https://rpc2.example"}'
    )
    with pytest.raises(ValueError, match="local wallet configuration is invalid"):
        configured_wallet_signer_from_env()


@pytest.mark.parametrize(
    ("profile", "opt_in", "rpc", "error"),
    [
        ("development", "false", '{"11155111":"https://rpc.example"}', "wallet testnet"),
        ("production", "true", '{"11155111":"https://rpc.example"}', "wallet testnet"),
        ("development", "true", '{"1":"https://rpc.example"}', "isolated development testnet"),
        (
            "test",
            "true",
            '{"11155111":"https://rpc.example","1":"https://rpc.example"}',
            "isolated development testnet",
        ),
    ],
)
def test_local_policy_rejects_before_opening_wallet_material(
    monkeypatch: pytest.MonkeyPatch,
    profile: str,
    opt_in: str,
    rpc: str,
    error: str,
) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
    monkeypatch.setenv("NOYRA_PROFILE", profile)
    monkeypatch.setenv("NOYRA_WALLET_TESTNET_ENABLED", opt_in)
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", "synthetic-keystore")
    monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", "synthetic-password-file")
    monkeypatch.setenv("NOYRA_WALLET_RPC_URLS_JSON", rpc)
    read_password = Mock(side_effect=AssertionError("password must not be read"))
    monkeypatch.setattr("noyra.wallet.keystore.read_password_file", read_password)
    with pytest.raises(ValueError, match=error):
        configured_wallet_signer_from_env()
    read_password.assert_not_called()
