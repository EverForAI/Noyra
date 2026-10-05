"""Private operator-owned migration connection configuration."""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from noyra.core.at_rest import validate_private_file, validate_private_root


def write_target_token(root: Path, target_id: str, token: str) -> None:
    if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", target_id):
        raise ValueError("migration target identity invalid")
    if (
        not isinstance(token, str)
        or not 32 <= len(token) <= 4096
        or any(char.isspace() for char in token)
    ):
        raise ValueError("migration channel token invalid")
    directory = validate_private_root(root, create=True)
    destination = validate_private_file(directory, f"{target_id}.token")
    temporary = validate_private_file(directory, f".{secrets.token_hex(16)}.tmp")
    try:
        fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(token.encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
