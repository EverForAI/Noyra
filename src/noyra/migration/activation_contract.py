"""Shared, strict wire contract for agent and privileged target activation."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime
from typing import Any

ACTIVATION_FORMAT = "noyra-target-activation/v2"
ACTIVATION_REQUEST_KEYS = frozenset(
    {
        "format",
        "task_id",
        "subject_id",
        "target_id",
        "source_epoch",
        "manifest_digest",
        "artifact_id",
        "artifact_sha256",
        "restored_database_sha256",
        "inventory_sha256",
        "health_report_digest",
        "source_fence_digest",
        "recipient_key_fingerprint",
        "target_volume_proof_digest",
        "credential_binding_digest",
        "signer_binding_digest",
        "wallet_mode",
        "wallet_proof_digest",
    }
)
ACTIVATION_RECEIPT_KEYS = ACTIVATION_REQUEST_KEYS | {
    "status",
    "service_unit",
    "active_database_sha256",
    "activated_at",
}
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


def validate_activation_request(request: Mapping[str, Any]) -> dict[str, Any]:
    if set(request) != ACTIVATION_REQUEST_KEYS or request.get("format") != ACTIVATION_FORMAT:
        raise ValueError("migration activation request is invalid")
    patterns = {
        "task_id": r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}",
        "source_epoch": r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,127}",
        "subject_id": r"Noyra-[A-Za-z0-9_-]{1,120}",
        "target_id": r"[A-Za-z0-9_-]{3,128}",
        "artifact_id": r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}",
    }
    for key, pattern in patterns.items():
        if not isinstance(request[key], str) or not re.fullmatch(pattern, request[key]):
            raise ValueError("migration activation identity is invalid")
    for key in ACTIVATION_REQUEST_KEYS - {*patterns, "format", "wallet_mode"}:
        value = request[key]
        if value is None and key in {
            "signer_binding_digest",
            "wallet_proof_digest",
            "inventory_sha256",
        }:
            continue
        if not isinstance(value, str) or not _DIGEST.fullmatch(value):
            raise ValueError("migration activation digest is invalid")
    mode = request["wallet_mode"]
    signer, wallet = request["signer_binding_digest"], request["wallet_proof_digest"]
    if not isinstance(mode, str) or mode not in {
        "disabled",
        "external_signer_rebind",
        "local_wallet_transfer",
    }:
        raise ValueError("migration activation wallet mode is invalid")
    if (
        (mode == "disabled" and (signer is not None or wallet is not None))
        or (mode == "external_signer_rebind" and (wallet is None or signer != wallet))
        or (mode == "local_wallet_transfer" and (wallet is None or signer is not None))
    ):
        raise ValueError("migration activation wallet binding is invalid")
    return dict(request)


def validate_activation_receipt(request: Mapping[str, Any], receipt: Mapping[str, Any]) -> None:
    validate_activation_request(request)
    if set(receipt) != ACTIVATION_RECEIPT_KEYS or any(
        receipt.get(k) != v for k, v in request.items()
    ):
        raise ValueError("migration activation receipt binding is invalid")
    if receipt["status"] != "active" or receipt["service_unit"] != "noyra.service":
        raise ValueError("migration activation receipt status is invalid")
    digest, timestamp = receipt["active_database_sha256"], receipt["activated_at"]
    if (
        not isinstance(digest, str)
        or not _DIGEST.fullmatch(digest)
        or not isinstance(timestamp, str)
    ):
        raise ValueError("migration activation receipt is invalid")
    if datetime.fromisoformat(timestamp.replace("Z", "+00:00")).tzinfo is None:
        raise ValueError("migration activation receipt timestamp is invalid")
