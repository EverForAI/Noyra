"""Token verification and session records, shared by the HTTP facade."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import time
from dataclasses import dataclass

from pydantic import SecretStr


def _ascii_token_equal(supplied: object, expected: str) -> bool:
    """Reject malformed credentials without changing valid token equivalence."""
    return (
        isinstance(supplied, str)
        and supplied.isascii()
        and expected.isascii()
        and hmac.compare_digest(supplied, expected)
    )


def _verify_trusted_proxy_principal(
    principal: object,
    timestamp: object,
    signature: object,
    peer_address: object,
    *,
    trusted_proxy_cidrs: tuple[str, ...],
    hmac_key: SecretStr | None,
    method: str,
    path: str,
    now: int | None = None,
) -> str | None:
    if (
        hmac_key is None
        or not isinstance(principal, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}", principal)
        or not isinstance(timestamp, str)
        or re.fullmatch(r"[0-9]{1,12}", timestamp) is None
        or not isinstance(signature, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", signature) is None
        or not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 4_096
    ):
        return None
    try:
        peer = ipaddress.ip_address(str(peer_address))
        timestamp_value = int(timestamp)
    except ValueError:
        return None
    if not any(peer in ipaddress.ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs):
        return None
    if abs((int(time.time()) if now is None else now) - timestamp_value) > 60:
        return None
    message = "\n".join(
        (
            "noyra-authenticated-principal/v1",
            principal,
            timestamp,
            method.upper(),
            path,
        )
    ).encode("utf-8")
    expected = hmac.new(
        hmac_key.get_secret_value().encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    return principal if hmac.compare_digest(signature.casefold(), expected) else None


@dataclass(frozen=True)
class _AdminSession:
    role: str
    actor: str
    csrf_token: str
    expires_at: float
