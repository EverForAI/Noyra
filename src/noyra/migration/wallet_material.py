"""Explicit local-keystore transfer inside the recipient-encrypted payload."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from noyra.wallet.keystore import load_account, read_password_file


def verify_local_wallet(root: Path, address: str) -> None:
    account = load_account(root / "wallet.json", read_password_file(root / "password"))
    if account.address.casefold() != address.casefold():
        raise ValueError("migration wallet address mismatch")


def source_wallet_files(
    task: Any, binding: Mapping[str, Any], rpc_file: Path
) -> tuple[Path, Path, Path]:
    approval = binding.get("approval")
    address = binding.get("address")
    if (
        not isinstance(approval, Mapping)
        or approval.get("task_id") != task.task_id
        or approval.get("address") != address
        or not isinstance(address, str)
        or not approval.get("approval_id")
        or not approval.get("channel_id")
    ):
        raise ValueError("local wallet task approval required")
    expiry = datetime.fromisoformat(str(approval.get("expires_at", "")))
    if expiry.tzinfo is None or expiry <= datetime.now(UTC):
        raise ValueError("local wallet task approval expired")
    if os.environ.get("NOYRA_WALLET_MODE") != "local":
        raise ValueError("local wallet migration requires configured local signer")
    keystore = Path(os.environ["NOYRA_WALLET_KEYSTORE_PATH"])
    password = Path(os.environ["NOYRA_WALLET_PASSWORD_FILE"])
    if not keystore.is_absolute() or not password.is_absolute():
        raise ValueError("local wallet paths must be absolute")
    account = load_account(keystore, read_password_file(password))
    if account.address.casefold() != address.casefold():
        raise ValueError("local wallet source identity mismatch")
    from noyra.wallet.config import _parse_rpc_urls

    rpc_urls = _parse_rpc_urls(os.environ["NOYRA_WALLET_RPC_URLS_JSON"])
    descriptor = os.open(rpc_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
        json.dump({str(key): value for key, value in rpc_urls.items()}, stream)
    return keystore, password, rpc_file
