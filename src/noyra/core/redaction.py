from __future__ import annotations

import json
import re
from typing import Any

_REDACTED = "[REDACTED]"
_DEPTH_LIMIT = "[REDACTED_DEPTH_LIMIT]"

# Keep the key contract exact enough that telemetry such as ``token_count`` and
# ``max_output_tokens`` remains useful.  Suffix matching still covers namespaced
# secrets such as ``provider_refresh_token`` and ``smtp_password``.
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "admin_token",
        "api_key",
        "api_keys",
        "apikey",
        "authorization",
        "auth_token",
        "bearer_token",
        "client_secret",
        "cookie",
        "cookies",
        "credential",
        "credentials",
        "csrf_token",
        "id_token",
        "password",
        "passwd",
        "private_key",
        "private_keys",
        "private_key_path",
        "private_key_reference",
        "proxy_authorization",
        "refresh_token",
        "secret",
        "secrets",
        "secret_path",
        "secret_reference",
        "session",
        "session_id",
        "session_key",
        "session_token",
        "set_cookie",
        "token",
        "token_path",
        "token_reference",
        "xsrf_token",
    }
)
_SENSITIVE_KEY_SUFFIXES = tuple(f"_{name}" for name in _SENSITIVE_KEY_NAMES)
_SECRET_VALUE_SUFFIXES = (
    "_ciphertext",
    "_encrypted",
    "_header",
    "_json",
    "_material",
    "_pem",
    "_raw",
    "_text",
    "_value",
)
_SAFE_METADATA_KEYS = frozenset(
    {
        "encryption_key_fingerprint",
        "key_fingerprint",
    }
)

_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?"
    r"(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
    re.IGNORECASE | re.DOTALL,
)
_SECRET_HEADER = re.compile(
    r"(?im)(?P<name>authorization|proxy-authorization|cookie|set-cookie|"
    r"x-api-key|api-key|x-auth-token)\s*:\s*[^\r\n]*"
)
_BEARER = re.compile(r"(?i)\b(?P<scheme>bearer\s+)[A-Za-z0-9._~+/=-]{8,}")
_KNOWN_TOKEN = re.compile(
    r"\b(?:sk-|AIza|ghp_|github_pat_)[A-Za-z0-9_-]{12,}\b",
    re.IGNORECASE,
)
_SECRET_BASE_LABEL = (
    r"(?:api[_-]?keys?|admin[_-]?token|access[_-]?token|refresh[_-]?token|"
    r"session(?:[_-]?(?:id|key|token))?|csrf[_-]?token|xsrf[_-]?token|"
    r"auth[_-]?token|id[_-]?token|bearer[_-]?token|token|cookies?|set[_-]?cookie|"
    r"password|passwd|secrets?|credentials?|private[_-]?keys?)"
)
_SECRET_LABEL = rf"(?:(?:[a-z0-9]+[_-])*{_SECRET_BASE_LABEL})"
_QUOTED_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?<![a-z0-9_])(?P<key_quote>[\"']?)(?P<label>{_SECRET_LABEL})"
    r"(?P=key_quote)"
    r"(?P<separator>\s*[:=]\s*)(?P<value_quote>[\"'])"
    r"(?P<value>[^\r\n]*?)(?P=value_quote)"
)
_UNQUOTED_SECRET_ASSIGNMENT = re.compile(
    rf"(?i)(?P<label>\b{_SECRET_LABEL}\b)(?P<separator>\s*[:=]\s*)"
    r"(?P<value>[^\s,;?&#\"'<>\[\]\}]+)"
)
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def _normalize_key(key: str) -> str:
    # Accept JSON/HTTP camelCase spellings as well as snake/kebab case.
    separated = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key)
    return re.sub(r"[^a-z0-9]+", "_", separated.casefold()).strip("_")


def is_sensitive_key(key: str) -> bool:
    """Return whether a structured field contains credential material.

    Metadata explicitly known to be non-secret is retained.  The remaining
    contract supports both exact keys and namespaced suffixes without matching
    unrelated metrics merely because they contain the word ``token``.
    """

    normalized = _normalize_key(key)
    if normalized in _SAFE_METADATA_KEYS:
        return False
    candidate = normalized
    for suffix in _SECRET_VALUE_SUFFIXES:
        if candidate.endswith(suffix):
            candidate = candidate.removesuffix(suffix)
            break
    return candidate in _SENSITIVE_KEY_NAMES or candidate.endswith(_SENSITIVE_KEY_SUFFIXES)


def _json_container(value: str) -> dict[str, Any] | list[Any] | None:
    candidate = value.strip()
    if not candidate or candidate[0] not in "[{" or candidate[-1] not in "]}":
        return None
    try:
        parsed = json.loads(candidate)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, (dict, list)) else None


def _replace_secret_header(match: re.Match[str]) -> str:
    return f"{match.group('name')}: {_REDACTED}"


def _replace_quoted_assignment(match: re.Match[str]) -> str:
    return (
        f"{match.group('key_quote')}{match.group('label')}{match.group('key_quote')}"
        f"{match.group('separator')}{match.group('value_quote')}{_REDACTED}"
        f"{match.group('value_quote')}"
    )


def _replace_unquoted_assignment(match: re.Match[str]) -> str:
    return f"{match.group('label')}{match.group('separator')}{_REDACTED}"


def redact_secret_text(value: str, *, depth: int = 0) -> str:
    """Redact credentials embedded in free text, headers, or JSON strings."""

    if depth > 32:
        return _DEPTH_LIMIT
    parsed = _json_container(value)
    if parsed is not None:
        return json.dumps(
            redact_secrets(parsed, depth=depth + 1),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    redacted = _PRIVATE_KEY.sub(_REDACTED, value)
    redacted = _SECRET_HEADER.sub(_replace_secret_header, redacted)
    redacted = _BEARER.sub(lambda match: f"{match.group('scheme')}{_REDACTED}", redacted)
    redacted = _KNOWN_TOKEN.sub(_REDACTED, redacted)
    redacted = _QUOTED_SECRET_ASSIGNMENT.sub(_replace_quoted_assignment, redacted)
    return _UNQUOTED_SECRET_ASSIGNMENT.sub(_replace_unquoted_assignment, redacted)


def redact_secrets(value: Any, *, depth: int = 0) -> Any:
    """Redact secrets from nested content without truncating diagnostic data."""

    if depth > 32:
        return _DEPTH_LIMIT
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = str(key)
            if is_sensitive_key(rendered_key):
                result[rendered_key] = _REDACTED
            else:
                result[rendered_key] = redact_secrets(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [redact_secrets(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        return redact_secret_text(value, depth=depth)
    return value


def redact_text(value: str) -> str:
    """Redact credentials and basic PII from training-safe free text."""

    return _EMAIL.sub(_REDACTED, redact_secret_text(value))


def redact_payload(value: Any, *, depth: int = 0) -> Any:
    """Redact secrets and basic PII from nested JSON-compatible content."""

    if depth > 32:
        return _DEPTH_LIMIT
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            rendered_key = str(key)
            if is_sensitive_key(rendered_key):
                result[rendered_key] = _REDACTED
            else:
                result[rendered_key] = redact_payload(item, depth=depth + 1)
        return result
    if isinstance(value, list):
        return [redact_payload(item, depth=depth + 1) for item in value[:10_000]]
    if isinstance(value, tuple):
        return [redact_payload(item, depth=depth + 1) for item in value[:10_000]]
    if isinstance(value, str):
        bounded = value if len(value) <= 100_000 else value[:100_000] + "\n[TRUNCATED]"
        parsed = _json_container(bounded)
        if parsed is not None:
            cleaned = redact_payload(parsed, depth=depth + 1)
            return json.dumps(
                cleaned,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        return redact_text(bounded)
    return value
