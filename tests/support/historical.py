from __future__ import annotations

import gzip
import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HistoricalFixture:
    name: str
    schema_version: int
    source_commit: str
    archive: Path
    archive_sha256: str
    database_sha256: str
    subject_id: str
    anchor_event_id: str
    expected_migration_error: str | None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_historical_fixtures(manifest_path: Path) -> tuple[HistoricalFixture, ...]:
    raw: Any = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format_version") != 1:
        raise ValueError("unsupported historical fixture manifest")
    entries = raw.get("fixtures")
    if not isinstance(entries, list) or not entries:
        raise ValueError("historical fixture manifest must contain fixtures")

    fixtures: list[HistoricalFixture] = []
    for item in entries:
        if not isinstance(item, dict):
            raise ValueError("historical fixture entry must be an object")
        archive = manifest_path.parent / str(item["archive"])
        fixture = HistoricalFixture(
            name=str(item["name"]),
            schema_version=int(item["schema_version"]),
            source_commit=str(item["source_commit"]),
            archive=archive,
            archive_sha256=str(item["archive_sha256"]),
            database_sha256=str(item["database_sha256"]),
            subject_id=str(item["subject_id"]),
            anchor_event_id=str(item["anchor_event_id"]),
            expected_migration_error=(
                None
                if item.get("expected_migration_error") is None
                else str(item["expected_migration_error"])
            ),
        )
        if fixture.schema_version < 1:
            raise ValueError("historical schema version must be positive")
        if len(fixture.source_commit) != 40:
            raise ValueError("historical fixture source commit must be a full Git hash")
        fixtures.append(fixture)
    return tuple(fixtures)


def materialize_historical_database(fixture: HistoricalFixture, destination_dir: Path) -> Path:
    """Expand a checked fixture only into a caller-owned temporary directory."""
    destination_dir = destination_dir.resolve()
    if not destination_dir.is_dir():
        raise ValueError("historical fixture destination must be an existing directory")
    if not fixture.archive.is_file():
        raise FileNotFoundError(fixture.archive)
    if _sha256(fixture.archive) != fixture.archive_sha256:
        raise ValueError(f"historical fixture archive checksum mismatch: {fixture.name}")

    target = destination_dir / f"{fixture.name}.sqlite3"
    with gzip.open(fixture.archive, "rb") as source, target.open("xb") as output:
        shutil.copyfileobj(source, output, length=1024 * 1024)
    if _sha256(target) != fixture.database_sha256:
        target.unlink(missing_ok=True)
        raise ValueError(f"historical fixture database checksum mismatch: {fixture.name}")
    return target
