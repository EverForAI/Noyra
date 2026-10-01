from __future__ import annotations

# ruff: noqa: E501
import base64

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore
from noyra.migration.fencing import EpochLease
from noyra.migration.policy import MigrationStore
from noyra.migration.targets import TargetRegistry


def test_only_one_active_epoch_and_old_epoch_is_stale(tmp_path) -> None:
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure("Noyra-0001", "1" * 64)
    private = Ed25519PrivateKey.generate()
    TargetRegistry(db, MigrationStore(db)).register("Noyra-0001", target_id="target-1", public_key=base64.urlsafe_b64encode(private.public_key().public_bytes_raw()).decode(), endpoint="https://target.example", capabilities={}, region=None, provider=None, release_sha="a" * 40, os_arch="linux-amd64", encrypted_volume=True, actor="operator")
    TargetRegistry(db, MigrationStore(db)).register("Noyra-0001", target_id="target-2", public_key=base64.urlsafe_b64encode(Ed25519PrivateKey.generate().public_key().public_bytes_raw()).decode(), endpoint="https://target2.example", capabilities={}, region=None, provider=None, release_sha="a" * 40, os_arch="linux-amd64", encrypted_volume=True, actor="operator")
    first = EpochLease.acquire(db, "Noyra-0001", "target-1", expected_source_epoch=None)
    first.assert_current()
    with pytest.raises(ValueError, match="active"):
        EpochLease.acquire(db, "Noyra-0001", "target-2", expected_source_epoch=first.epoch_id)
    first.revoke("cutover", "operator")
    with pytest.raises(ValueError, match="stale"):
        first.assert_current()
