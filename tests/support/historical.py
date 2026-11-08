from __future__ import annotations

import gzip
import hashlib
import json
import shutil
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from noyra.core.database import SCHEMA_SQL, Database


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


def materialize_migration_database(path: Path, version: int) -> Database:
    """Build an early schema by forward migration, never by relabeling a current DB.

    Use checksum-verified historical anchors when one precedes the target; v1
    is the base SQL itself. Stop the real migration runner before any later
    feature pass. Wallet approval migrations (v59+) are deliberately excluded.
    The returned facade does not initialize or install current optional features.
    """
    if not 1 <= version < 59:
        raise ValueError("historical test target must be between schema 1 and 58")
    if path.exists():
        raise FileExistsError(path)
    fixtures = load_historical_fixtures(
        Path(__file__).parents[1] / "fixtures" / "historical" / "manifest.json"
    )
    anchors = [
        item
        for item in fixtures
        if item.schema_version <= version and item.expected_migration_error is None
    ]
    with tempfile.TemporaryDirectory(prefix="legacy-schema-", dir=path.parent) as folder:
        if anchors:
            anchor = max(anchors, key=lambda item: item.schema_version)
            source = materialize_historical_database(anchor, Path(folder))
        else:
            source = Path(folder) / "schema-v1.sqlite3"
            with closing(sqlite3.connect(source)) as connection:
                Database._execute_sql_script(connection, SCHEMA_SQL)
                connection.commit()
        database = Database(source, initialize=False)
        with patch("noyra.core.database.CURRENT_SCHEMA_VERSION", version):
            database._migrate()
        with database.connection() as connection:
            marker = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()[0]
            if marker != str(version):
                raise ValueError("historical test target schema was not reached")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("historical test schema has invalid foreign keys")
            if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("historical test schema is corrupt")
        source.rename(path)
    return Database(path, initialize=False)
