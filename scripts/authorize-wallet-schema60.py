from __future__ import annotations

import argparse
import json
import sqlite3
from contextlib import closing
from pathlib import Path

from noyra.core.database import CURRENT_SCHEMA_VERSION, Database
from noyra.core.locking import ProcessLock
from noyra.core.wallet_schema import WalletLegacyApproval, wallet_upgrade_fingerprint


def main() -> None:
    parser = argparse.ArgumentParser(description="Offline, explicit legacy wallet authorization")
    parser.add_argument("database", type=Path)
    parser.add_argument("--expected-fingerprint")
    parser.add_argument("--actor")
    parser.add_argument("--reason")
    args = parser.parse_args()
    path = args.database.resolve(strict=True)
    if args.expected_fingerprint is None and (args.actor or args.reason):
        parser.error("actor/reason require an explicitly reviewed fingerprint")
    approval = None
    if args.expected_fingerprint is not None:
        if not args.actor or not args.reason:
            parser.error("authorization requires --actor and --reason")
        approval = WalletLegacyApproval(args.expected_fingerprint, args.actor, args.reason)
    lock = ProcessLock(f"{path}.lock")
    lock.acquire()
    try:
        if approval is None:
            with closing(sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)) as connection:
                connection.row_factory = sqlite3.Row
                connection.execute("BEGIN")
                marker = connection.execute(
                    "SELECT value FROM schema_meta WHERE key='schema_version'"
                ).fetchone()
                if marker is None or marker[0] != "59":
                    raise RuntimeError("offline wallet approval requires schema 59")
                print(
                    json.dumps(
                        {
                            "database": str(path),
                            "fingerprint": wallet_upgrade_fingerprint(connection),
                            "approved": False,
                            "historical_authenticity_proven": False,
                            "required_review": (
                                "Independently confirm every policy field and ledger timestamp "
                                "before authorization."
                            ),
                        },
                        indent=2,
                    )
                )
        else:
            Database(path, initialize=False).initialize(wallet_legacy_approval=approval)
            print(
                f"Schema {CURRENT_SCHEMA_VERSION} installed; explicit authorization recorded "
                "in audit_records."
            )
    finally:
        lock.release()


if __name__ == "__main__":
    main()
