from __future__ import annotations

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

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "tests" / "fixtures" / "historical"
TEMP_ROOT = ROOT / ".runtime" / "m41"


@dataclass(frozen=True)
class FixtureSource:
    name: str
    version: int
    commit: str


SOURCES = (
    FixtureSource("schema-v6", 6, "0ce18d42da917124a459dedfa1fb4aa6b097ff17"),
    FixtureSource("schema-v16", 16, "27dbe17e24bfd8a158e485d8f34430a34694c270"),
    FixtureSource("schema-v21", 21, "66e2181f183543422de6c1fdd0cf40ddfa231dae"),
    FixtureSource("schema-v27", 27, "731265924aba1cd79ef8c850a3574a31a22ba69f"),
    FixtureSource("schema-v31", 31, "b67be86f0d247a7e30e8a461de3458972526a5f0"),
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


def compress_database(source: Path, target: Path) -> None:
    with (
        source.open("rb") as input_stream,
        target.open("wb") as output_stream,
        gzip.GzipFile(filename="", mode="wb", fileobj=output_stream, mtime=0) as archive,
    ):
        while chunk := input_stream.read(1024 * 1024):
            archive.write(chunk)


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    TEMP_ROOT.mkdir(parents=True, exist_ok=True)
    fixtures: list[dict[str, str | int]] = []
    with tempfile.TemporaryDirectory(prefix="fixtures-", dir=TEMP_ROOT) as directory:
        working = Path(directory)
        for source in SOURCES:
            source_root = working / f"source-{source.version}"
            source_root.mkdir()
            extract_source(source, source_root)
            database = working / f"{source.name}.sqlite3"
            create_database(source, source_root, database)
            archive = OUTPUT / f"{source.name}.sqlite3.gz"
            compress_database(database, archive)
            fixtures.append(
                {
                    "name": source.name,
                    "schema_version": source.version,
                    "source_commit": source.commit,
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

    manifest = {"format_version": 1, "fixtures": fixtures}
    (OUTPUT / "manifest.json").write_text(
        json.dumps(manifest, indent=2, ensure_ascii=True) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
