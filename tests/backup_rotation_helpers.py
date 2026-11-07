"""Explicit request fixtures; each call deliberately starts a new test operation."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from noyra.core.at_rest import BackupKeyring
from noyra.core.backup_rotation import BackupRotationJournal


def rotate_new_request(path: Path) -> BackupKeyring:
    request = uuid4().hex
    api = BackupRotationJournal(path)
    observed = api.status(request_id=request)
    assert observed.observed_keyring_hash is not None
    result = BackupKeyring.rotate(
        path,
        request_id=request,
        expected_generation=observed.generation,
        expected_source_hash=observed.observed_keyring_hash,
    )
    assert result.state == "accepted"
    return BackupKeyring.from_path(path)
