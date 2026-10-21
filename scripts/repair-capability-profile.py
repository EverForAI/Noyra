#!/usr/bin/env python3
"""Offline, explicitly pinned recovery for a corrupted imported capability profile.

Run as the database owner after stopping Noyra. The protected input must be
an independently trusted single profile export, not reconstructed DB contents.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from noyra.core.at_rest import validate_private_root
from noyra.core.credentials import read_secret_file
from noyra.core.database import Database
from noyra.core.locking import ProcessLock
from noyra.model.capabilities import CapabilityProfileStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--profile-file", type=Path, required=True)
    parser.add_argument("--expected-profile-id", required=True)
    parser.add_argument("--expected-state-hash", required=True)
    parser.add_argument("--evidence-path", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    database = args.database.absolute()
    if database.is_symlink() or not database.is_file():
        parser.error("database must be an existing regular file")
    if os.name == "posix" and database.stat().st_uid != os.geteuid():
        parser.error("run as the database owner to preserve private file ownership")
    data_root = validate_private_root(database.parent, expected_device=database.stat().st_dev)
    database = data_root / database.name
    if database.stat().st_nlink != 1:
        parser.error("database must not be hardlinked")
    configured_evidence = args.evidence_path.absolute()
    evidence_root = validate_private_root(
        configured_evidence.parent, expected_device=configured_evidence.parent.stat().st_dev
    )
    evidence = evidence_root / configured_evidence.name
    if evidence_root == data_root or data_root in evidence_root.parents:
        parser.error("evidence must be outside the runtime data directory")
    if os.name == "posix" and (
        evidence.parent.stat().st_uid != os.geteuid()
        or evidence.parent.stat().st_mode & 0o777 != 0o700
    ):
        parser.error("evidence directory must belong to this user and use mode 0700")
    payload = json.loads(
        read_secret_file(
            args.profile_file,
            label="trusted capability profile",
            single_line=False,
            max_bytes=1_000_000,
        )
    )
    if isinstance(payload, dict) and "profiles" in payload:
        profiles = payload["profiles"]
        if not isinstance(profiles, list) or len(profiles) != 1:
            parser.error("recovery requires exactly one trusted profile")
        payload = profiles[0]

    def record_evidence(record: dict[str, object]) -> None:
        descriptor = os.open(
            evidence, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600
        )
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(record, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name == "posix":
            directory = os.open(evidence.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory)
            finally:
                os.close(directory)

    lock = ProcessLock(database.with_name(database.name + ".lock"))
    lock.acquire()
    try:
        CapabilityProfileStore(
            Database(database, initialize=False, strict_schema_contract=True)
        ).repair_imported_profile(
            args.subject,
            payload,
            expected_profile_id=args.expected_profile_id,
            expected_state_hash=args.expected_state_hash,
            record_evidence=record_evidence,
        )
    finally:
        lock.release()
    print(json.dumps({"status": "repaired", "evidence_path": str(evidence)}))


if __name__ == "__main__":
    main()
