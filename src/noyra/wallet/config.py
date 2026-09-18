"""Fail-closed wallet signer selection from process environment.

The parser keeps credentials out of error messages and supports the legacy
endpoint-only configuration for existing deployments.  Local wallet secrets
are read from protected files and only the resulting account object is passed
to :class:`LocalWalletSigner`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import urlsplit

from .execution import HTTPSWalletSigner, WalletSigner


def _env(name: str) -> str:
    return os.getenv(name, "").strip()


def _invalid() -> ValueError:
    # Never include values read from the environment in diagnostics.
    return ValueError("local wallet configuration is invalid")


def _parse_rpc_urls(raw: str) -> dict[int, str]:
    if not raw:
        raise _invalid()

    def pairs(items: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in items:
            if key in result:
                raise _invalid()
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (TypeError, ValueError, json.JSONDecodeError):
        raise _invalid() from None
    if not isinstance(value, Mapping) or not value:
        raise _invalid()
    urls: dict[int, str] = {}
    for key, endpoint in value.items():
        if not isinstance(key, str) or not key.isascii() or not key.isdigit() or key == "0":
            raise _invalid()
        # Require canonical decimal keys so 1 and 01 cannot silently alias.
        if str(int(key)) != key:
            raise _invalid()
        chain_id = int(key)
        if chain_id < 1 or chain_id > 2**63 - 1 or chain_id in urls:
            raise _invalid()
        if not isinstance(endpoint, str) or len(endpoint) > 2048:
            raise _invalid()
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme.casefold() != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise _invalid()
        urls[chain_id] = endpoint.rstrip("/")
    return urls


def _local_setting(name: str, *aliases: str) -> str:
    for key in (name, *aliases):
        value = _env(key)
        if value:
            return value
    return ""


def configured_wallet_signer_from_env() -> WalletSigner | None:
    """Build the configured signer, or ``None`` when signing is disabled.

    Explicit ``disabled`` always wins over stale endpoint/local settings.  If
    no mode is set, an endpoint plus identity retains the legacy HTTPS signer.
    """

    mode = _env("NOYRA_WALLET_MODE").casefold()
    endpoint = _env("NOYRA_WALLET_SIGNER_ENDPOINT")
    signer_id = _env("NOYRA_WALLET_SIGNER_ID")
    bearer = _env("NOYRA_WALLET_SIGNER_BEARER_TOKEN")
    if mode and mode not in {"disabled", "external", "local"}:
        raise ValueError("NOYRA_WALLET_MODE is invalid")
    if mode == "disabled":
        return None

    if mode == "local":
        if endpoint or signer_id or bearer:
            raise _invalid()
        keystore = _local_setting("NOYRA_WALLET_KEYSTORE_PATH", "NOYRA_WALLET_LOCAL_KEYSTORE_PATH")
        password_file = _local_setting(
            "NOYRA_WALLET_PASSWORD_FILE", "NOYRA_WALLET_LOCAL_PASSWORD_FILE"
        )
        rpc_json = _local_setting("NOYRA_WALLET_RPC_URLS_JSON", "NOYRA_WALLET_LOCAL_RPC_URLS_JSON")
        if not keystore or not password_file or not rpc_json:
            raise _invalid()
        try:
            rpc_urls = _parse_rpc_urls(rpc_json)
            from .keystore import load_account, read_password_file
            from .local import LocalWalletSigner

            account = load_account(Path(keystore), read_password_file(Path(password_file)))
            return LocalWalletSigner(account, rpc_urls)
        except ValueError:
            raise _invalid() from None
        except Exception as error:
            # Do not expose paths, passwords, or dependency exception text.
            raise _invalid() from error

    # No explicit mode: endpoint-only is the compatibility behavior.  An
    # explicit external mode has the same strict endpoint and identity rules.
    if mode == "external" and any(
        _env(name)
        for name in (
            "NOYRA_WALLET_KEYSTORE_PATH",
            "NOYRA_WALLET_LOCAL_KEYSTORE_PATH",
            "NOYRA_WALLET_PASSWORD_FILE",
            "NOYRA_WALLET_LOCAL_PASSWORD_FILE",
            "NOYRA_WALLET_RPC_URLS_JSON",
            "NOYRA_WALLET_LOCAL_RPC_URLS_JSON",
        )
    ):
        raise ValueError("external wallet configuration is invalid")
    if mode == "external" or endpoint or signer_id or bearer:
        if not endpoint or not signer_id:
            raise ValueError("external wallet signer requires endpoint and identity")
        try:
            timeout = float(_env("NOYRA_WALLET_SIGNER_TIMEOUT_SECONDS") or "15")
            return HTTPSWalletSigner(
                endpoint,
                signer_id=signer_id,
                timeout_seconds=timeout,
                bearer_token=bearer or None,
            )
        except (TypeError, ValueError):
            raise ValueError("external wallet signer configuration is invalid") from None
    return None


__all__ = ["configured_wallet_signer_from_env"]
