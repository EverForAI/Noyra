from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from noyra.wallet.config import configured_wallet_signer_from_env


def test_wallet_mode_defaults_to_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "NOYRA_WALLET_MODE",
        "NOYRA_WALLET_SIGNER_ENDPOINT",
        "NOYRA_WALLET_SIGNER_ID",
        "NOYRA_WALLET_SIGNER_BEARER_TOKEN",
    ):
        monkeypatch.delenv(name, raising=False)
    assert configured_wallet_signer_from_env() is None


def test_explicit_disabled_overrides_legacy_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "disabled")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "legacy")
    assert configured_wallet_signer_from_env() is None


def test_external_legacy_endpoint_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NOYRA_WALLET_MODE", raising=False)
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ID", "legacy")
    signer = configured_wallet_signer_from_env()
    assert signer is not None
    assert signer.signer_id == "legacy"
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


def test_local_rejects_external_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("NOYRA_WALLET_MODE", "local")
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(tmp_path / "wallet.json"))
    monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", str(tmp_path / "password"))
    monkeypatch.setenv("NOYRA_WALLET_RPC_URLS_JSON", json.dumps({"1": "https://rpc.example"}))
    monkeypatch.setenv("NOYRA_WALLET_SIGNER_ENDPOINT", "https://signer.example")
    with pytest.raises(ValueError, match="local wallet configuration is invalid"):
        configured_wallet_signer_from_env()


def test_external_rejects_local_fields(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
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
    monkeypatch.setenv("NOYRA_WALLET_KEYSTORE_PATH", str(tmp_path / "wallet.json"))
    monkeypatch.setenv("NOYRA_WALLET_PASSWORD_FILE", str(tmp_path / "password"))
    monkeypatch.setenv("NOYRA_WALLET_RPC_URLS_JSON", '{"1":"https://rpc.example","01":"https://rpc2.example"}')
    with pytest.raises(ValueError, match="local wallet configuration is invalid"):
        configured_wallet_signer_from_env()
