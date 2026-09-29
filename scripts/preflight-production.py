#!/usr/bin/env python3
"""Validate the non-secret production safety contract without mutating state."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from noyra.core.credentials import CredentialError, read_secret_file  # noqa: E402
from noyra.service import ServiceSettings  # noqa: E402


def _check(check_id: str, passed: bool, reason: str) -> dict[str, str]:
    return {"id": check_id, "status": "pass" if passed else "fail", "reason": reason}


def evaluate(settings: ServiceSettings | None, error: Exception | None = None) -> dict[str, Any]:
    checks: list[dict[str, str]] = []
    if settings is None:
        checks.append(_check("settings", False, f"settings could not be loaded: {error}"))
    else:
        checks.extend(
            [
                _check("profile", settings.profile == "production", "profile must be production"),
                _check(
                    "listener",
                    settings.host.casefold() in {"127.0.0.1", "::1", "localhost"},
                    "production runtime must listen on loopback behind HTTPS",
                ),
                _check(
                    "admin_session_cookie_secure",
                    settings.admin_session_cookie_secure is True,
                    "admin session cookie must be Secure",
                ),
                _check(
                    "public_site_url",
                    settings.public_site_url is not None,
                    "production needs an HTTPS public/admin origin",
                ),
                _check(
                    "trusted_proxy_cidrs",
                    bool(settings.trusted_proxy_cidrs),
                    "production proxy source ranges must be explicit",
                ),
                _check(
                    "at_rest_mode",
                    settings.at_rest_mode == "required",
                    "production requires at-rest protection",
                ),
                _check(
                    "backup_keyring",
                    settings.backup_keyring_path is not None,
                    "production requires an external backup keyring",
                ),
                _check(
                    "developer_log_export",
                    settings.developer_log_export_enabled is False,
                    "production must disable developer runtime export",
                ),
            ]
        )
        hash_key_path = settings.public_hash_key_file or (
            settings.data_dir / "secrets" / "public-post-ip-hash.key"
        )
        try:
            hash_key = read_secret_file(hash_key_path, label="public anti-abuse hash key")
            hash_key_ready = len(hash_key.encode("utf-8")) >= 32
        except (CredentialError, OSError, ValueError):
            hash_key_ready = False
        checks.append(
            _check(
                "public_hash_key",
                hash_key_ready,
                "persistent private public anti-abuse hash key of at least 32 bytes is required",
            )
        )
        for kind in ("MODEL", "EMBEDDING"):
            configured = bool(
                os.getenv(f"NOYRA_{kind}_BASE_URL", "").strip()
                or os.getenv(f"NOYRA_{kind}_MODEL", "").strip()
            )
            inline = bool(os.getenv(f"NOYRA_{kind}_API_KEY", "").strip())
            managed = bool(
                os.getenv(f"NOYRA_{kind}_API_KEY_FILE", "").strip()
                or os.getenv(f"NOYRA_{kind}_API_KEY_CREDENTIAL", "").strip()
            )
            checks.append(
                _check(
                    f"{kind.lower()}_api_key_source",
                    not configured or (managed and not inline),
                    "managed file/systemd credential required when the provider is configured",
                )
            )
    return {
        "status": "pass" if checks and all(item["status"] == "pass" for item in checks) else "fail",
        "checks": checks,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    arguments = parser.parse_args()
    if arguments.env_file is not None:
        values: dict[str, str] = {}
        for raw_line in arguments.env_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, raw_value = line.split("=", 1)
            key = key.strip()
            if not key or not key.replace("_", "").isalnum():
                raise SystemExit(f"invalid environment key in {arguments.env_file}: {key!r}")
            try:
                parsed = shlex.split(raw_value, posix=True)
            except ValueError as error:
                raise SystemExit(f"invalid environment value for {key}") from error
            values[key] = parsed[0] if parsed else ""
        # The file is the complete production configuration contract.  Do not
        # let a parent shell, CI runner, or sudo environment silently supply
        # omitted NOYRA_* values and change what this preflight validates.
        for key in tuple(os.environ):
            if key.startswith("NOYRA_"):
                os.environ.pop(key, None)
        os.environ.update(values)
    settings: ServiceSettings | None = None
    error: Exception | None = None
    try:
        settings = ServiceSettings.from_env()
    except Exception as caught:  # pragma: no cover - exercised through CLI contract
        error = caught
    payload = evaluate(settings, error)
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
