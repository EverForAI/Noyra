"""Shared v1 manifest descriptor validation; no catalog or file access."""

from __future__ import annotations

import re
from typing import Any

from .errors import IntegrityError


def validate_manifest_descriptor(
    manifest: Any, *, maximum: int, reason: str = "manifest descriptor is invalid"
) -> None:
    """Validate the descriptor shape, not the file or its durability.

    ``maximum`` is the caller's plaintext manifest byte budget. The existing v1
    encrypted-object envelope adds 37 bytes. Keep this independent of bundle and
    catalog imports so both publication formats can use the same contract.
    """
    if not (
        isinstance(manifest, dict)
        and set(manifest) == {"name", "stored_bytes", "stored_hash", "plaintext_hash"}
        and isinstance(manifest["name"], str)
        and manifest["name"] == "manifest.enc"
        and type(manifest["stored_bytes"]) is int
        and 37 <= manifest["stored_bytes"] <= maximum + 37
        and all(
            isinstance(manifest[field], str)
            and re.fullmatch(r"[a-f0-9]{64}", manifest[field]) is not None
            for field in ("stored_hash", "plaintext_hash")
        )
    ):
        raise IntegrityError("workflow export " + reason)
