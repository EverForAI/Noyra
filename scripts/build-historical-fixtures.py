from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "historical"
TEMP_ROOT = ROOT / ".runtime" / "m41"


@dataclass(frozen=True)
class FixtureSource:
    name: str
    version: int
    commit: str
    upgrade_from: tuple[str, ...] = ()


SOURCES = (
    FixtureSource("schema-v6", 6, "0ce18d42da917124a459dedfa1fb4aa6b097ff17"),
    FixtureSource("schema-v16", 16, "27dbe17e24bfd8a158e485d8f34430a34694c270"),
    FixtureSource("schema-v21", 21, "66e2181f183543422de6c1fdd0cf40ddfa231dae"),
    FixtureSource("schema-v27", 27, "731265924aba1cd79ef8c850a3574a31a22ba69f"),
    FixtureSource("schema-v31", 31, "b67be86f0d247a7e30e8a461de3458972526a5f0"),
    FixtureSource("schema-v71", 71, "4d988c5af19f72e3751755ca7331d5e1a0e1b4cd"),
    FixtureSource(
        "schema-v71-from-v63",
        71,
        "4d988c5af19f72e3751755ca7331d5e1a0e1b4cd",
        ("f564c5b6a0b62d9a2112592f55fe907d612d7a89",),
    ),
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_source(source: FixtureSource, destination: Path) -> None:
    archive = subprocess.run(
        ["git", "archive", "--format=tar", source.commit, "src/noyra"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as tar:
        tar.extractall(destination, filter="data")


def create_database(source: FixtureSource, source_root: Path, target: Path) -> None:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(source_root / "src")
    subprocess.run(
        [
            sys.executable,
            "-c",
            "from pathlib import Path; from noyra.core.database import Database; "
            "Database(Path(__import__('sys').argv[1]))",
            str(target),
        ],
        cwd=source_root,
        env=environment,
        check=True,
    )

    subject_id = f"Noyra-m41-historical-v{source.version}"
    if source.version == 71:
        populate_schema71(source, source_root, target, subject_id, environment)
        return
    anchor_event_id = f"evt_m41_historical_v{source.version}"
    timestamp = "2020-01-01T00:00:00.000+00:00"
    payload = {"fixture": source.name, "schema_version": source.version}
    payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    payload_hash = hashlib.sha256(payload_json.encode()).hexdigest()
    with closing(sqlite3.connect(target)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO subject_identity("
            "subject_id, project_name, genesis_hash, identity_status, created_at, updated_at, "
            "state_version"
            ") VALUES (?, 'Noyra', ?, 'active', ?, ?, 0)",
            (subject_id, hashlib.sha256(subject_id.encode()).hexdigest(), timestamp, timestamp),
        )
        connection.execute(
            "INSERT INTO events("
            "event_id, subject_id, event_type, source, occurred_at, observed_at, payload_json, "
            "payload_hash, privacy_level, causal_parent_ids_json, processing_status"
            ") VALUES (?, ?, 'm41_historical_anchor', 'm41-fixture', ?, ?, ?, ?, 'private', "
            "'[]', 'recorded')",
            (anchor_event_id, subject_id, timestamp, timestamp, payload_json, payload_hash),
        )
        connection.commit()


def populate_schema71(
    source: FixtureSource,
    source_root: Path,
    target: Path,
    subject_id: str,
    environment: dict[str, str],
) -> None:
    subprocess.run(
        [
            sys.executable,
            "-c",
            """from pathlib import Path
from unittest.mock import patch
import sys
import hashlib
from noyra.core.database import Database
from noyra.core.events import EventStore
from noyra.core.identity import IdentityStore
from noyra.core.lifecycle import LifecycleManager
from noyra.core.provider_health import ProviderHealthStore
from noyra.model.ledger import ModelLedger
from noyra.wallet.economy import WalletEconomyStore
from noyra.wallet.economy_types import PaymentPolicyInput

database = Database(Path(sys.argv[1]))
subject = sys.argv[2]
IdentityStore(database).ensure(subject, hashlib.sha256(subject.encode()).hexdigest())
events = EventStore(database)
events.append(subject, 'm41_historical_anchor', 'm41-fixture',
              {'fixture': sys.argv[3], 'schema_version': 71}, event_id='evt_m41_historical_v71')
LifecycleManager(database, events, subject).ensure_initial()
health = ProviderHealthStore(database)
with patch('noyra.core.provider_health.utc_now', return_value='2020-01-01T00:00:00.000+00:00'):
    health.record_attempt(subject, 'model', 'historical-provider', 'success', True, 100, None)
    health.record_attempt(subject, 'model', 'historical-provider', 'failure', False, 900, 'timeout')
ledger = ModelLedger(database)
with patch(
    'noyra.model.ledger.new_id',
    side_effect=['mcall_historical_71_1', 'mcall_historical_71_2'],
):
    for number in range(2):
        call, _ = ledger.prepare_call(subject, 'historical-provider', 'historical-model',
                                     'fixture', 'a' * 64, f'historical-{number}')
        if number == 0:
            ledger.finish_call(call.call_id, 'failed')
wallet = WalletEconomyStore(database)
wallet.update_policy(subject, PaymentPolicyInput(
    mode='conditional_confirmation', per_order_limit='15', daily_limit='50',
    recipient_allowlist_enabled=True,
    allowed_recipient_addresses=['0x' + '1' * 40], automation_enabled=False,
    emergency_paused=True,
), expected_version=1, actor='operator')
""",
            str(target),
            subject_id,
            source.name,
        ],
        cwd=source_root,
        env=environment,
        check=True,
    )


def compress_database(source: Path, target: Path) -> None:
    with (
        source.open("rb") as input_stream,
        target.open("wb") as output_stream,
        gzip.GzipFile(filename="", mode="wb", fileobj=output_stream, mtime=0) as archive,
    ):
        while chunk := input_stream.read(1024 * 1024):
            archive.write(chunk)


def main() -> None:
    parser = argparse.ArgumentParser(description="Build fixtures from frozen historical sources")
    parser.add_argument(
        "--schema-version", type=int, choices=[source.version for source in SOURCES]
    )
    selected_version = parser.parse_args().schema_version
    OUTPUT.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    fixtures: list[dict[str, Any]] = []
    manifest_path = OUTPUT / "manifest.json"
    if selected_version is not None and manifest_path.exists():
        fixtures = [
            item
            for item in json.loads(manifest_path.read_text(encoding="utf-8"))["fixtures"]
            if item["schema_version"] != selected_version
        ]
    with tempfile.TemporaryDirectory(prefix="fixtures-", dir=TEMP_ROOT) as directory:
        working = Path(directory)
        for source in SOURCES:
            if selected_version is not None and source.version != selected_version:
                continue
            source_root = working / f"source-{source.name}"
            source_root.mkdir()
            extract_source(source, source_root)
            database = working / f"{source.name}.sqlite3"
            for index, commit in enumerate(source.upgrade_from):
                ancestor = FixtureSource(f"{source.name}-ancestor-{index}", 0, commit)
                ancestor_root = working / ancestor.name
                ancestor_root.mkdir()
                extract_source(ancestor, ancestor_root)
                subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        "from pathlib import Path; from noyra.core.database import Database; "
                        "Database(Path(__import__('sys').argv[1]))",
                        str(database),
                    ],
                    cwd=ancestor_root,
                    env=dict(os.environ, PYTHONPATH=str(ancestor_root / "src")),
                    check=True,
                )
            create_database(source, source_root, database)
            archive = OUTPUT / f"{source.name}.sqlite3.gz"
            compress_database(database, archive)
            fixtures.append(
                {
                    "name": source.name,
                    "schema_version": source.version,
                    "source_commit": source.commit,
                    **({"upgrade_from": list(source.upgrade_from)} if source.upgrade_from else {}),
                    "archive": archive.name,
                    "archive_sha256": sha256(archive),
                    "database_sha256": sha256(database),
                    "subject_id": f"Noyra-m41-historical-v{source.version}",
                    "anchor_event_id": f"evt_m41_historical_v{source.version}",
                    "expected_migration_error": (
                        "missing required features: secret_cleanup_queue"
                        if source.version == 31
                        else None
                    ),
                }
            )

    fixtures.sort(key=lambda item: int(item["schema_version"]))
    manifest = {"format_version": 1, "fixtures": fixtures}
    manifest_path.write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
