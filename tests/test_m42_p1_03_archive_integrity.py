from __future__ import annotations

import base64
import json
import zlib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from noyra.core import (
    Database,
    EventStore,
    IdentityStore,
    LongRunResilience,
    ResilienceReport,
    SnapshotStore,
)
from noyra.core.archive import LocalArchiveProvider
from noyra.core.errors import PayloadLimitError
from noyra.core.event_archive import (
    EventArchiveVerificationLimits,
    EventPayloadArchive,
)
from noyra.core.types import canonical_json, content_hash

_SUBJECT_ID = "Noyra-p103-archive"
_EVENT_ID = "evt-p103-archived"
_KEY_BYTES = bytes(range(32))
_KEY_ID = "p103-key-v1"


@dataclass(frozen=True)
class _ArchivedFixture:
    database: Database
    root: Path
    archive_root: Path
    object_key: str
    archive_path: Path


def _create_archived_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _ArchivedFixture:
    key = base64.urlsafe_b64encode(_KEY_BYTES).decode("ascii")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", key)
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", _KEY_ID)
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, "a" * 64)
    SnapshotStore(database).save(
        _SUBJECT_ID,
        {"stable": True},
        state_version=1,
        reason="p103 fixture",
    )
    EventStore(database).append(
        _SUBJECT_ID,
        "archived_experience",
        "p103-test",
        {"value": "original evidence"},
        occurred_at="2020-01-01T00:00:00+00:00",
        event_id=_EVENT_ID,
    )
    archive_root = tmp_path / "subject" / "cold"
    archive = EventPayloadArchive(database, archive_root)
    assert archive.archive_cold(_SUBJECT_ID, older_than_days=30) == 1
    with database.connection() as connection:
        row = connection.execute(
            "SELECT object_key FROM event_payload_segments WHERE subject_id = ?",
            (_SUBJECT_ID,),
        ).fetchone()
    assert row is not None
    object_key = str(row["object_key"])
    return _ArchivedFixture(
        database=database,
        root=tmp_path,
        archive_root=archive_root,
        object_key=object_key,
        archive_path=archive_root / object_key,
    )


def _archive_detail(report: ResilienceReport) -> dict[str, object]:
    raw = report.checks["event_archive_integrity"]
    detail = json.loads(raw)
    assert isinstance(detail, dict)
    assert raw == canonical_json(detail)
    return detail


def _rewrite_archive_body(
    fixture: _ArchivedFixture,
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    provider = LocalArchiveProvider(fixture.archive_root)
    compressed = provider.get(fixture.object_key)
    body: dict[str, Any] = json.loads(zlib.decompress(compressed))
    mutate(body)
    rewritten = zlib.compress(canonical_json(body).encode("utf-8"), level=9)
    digest = provider.put(fixture.object_key, rewritten)
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE event_payload_segments SET compressed_hash = ? "
            "WHERE subject_id = ? AND object_key = ?",
            (digest, _SUBJECT_ID, fixture.object_key),
        )


def test_valid_archived_event_passes_all_integrity_layers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    read_limits: list[int | None] = []
    original_get = LocalArchiveProvider.get

    def bounded_get(
        provider: LocalArchiveProvider,
        object_key: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        read_limits.append(max_bytes)
        return original_get(provider, object_key, max_bytes=max_bytes)

    monkeypatch.setattr(LocalArchiveProvider, "get", bounded_get)

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "ok"
    assert report.p0 == ()
    detail = _archive_detail(report)
    assert detail == {
        "events": 1,
        "reason": None,
        "segments": 1,
        "status": "ok",
        "verified_events": 1,
        "verified_segments": 1,
    }
    assert read_limits and all(limit is not None for limit in read_limits)


@pytest.mark.parametrize("key_state", ["missing", "wrong-key"])
def test_unavailable_archive_key_is_degraded_not_p0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    key_state: str,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    if key_state == "missing":
        monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY")
        monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID")
    else:
        monkeypatch.setenv(
            "NOYRA_ARCHIVE_ENCRYPTION_KEY",
            base64.urlsafe_b64encode(b"z" * 32).decode("ascii"),
        )

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "degraded:key_unavailable"
    assert report.p0 == ()
    assert report.p1.count("event_archive:key_unavailable") == 1
    detail = _archive_detail(report)
    assert detail["status"] == "degraded"
    assert detail["reason"] == "key_unavailable"
    assert detail["events"] == 1
    assert detail["segments"] == 1
    assert detail["verified_events"] == 0
    assert detail["verified_segments"] == 0


def test_legacy_archive_metadata_remains_readable_and_wrong_key_is_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE event_payload_segments SET archive_format = NULL, "
            "encryption_key_id = NULL, encryption_key_fingerprint = NULL "
            "WHERE subject_id = ? AND object_key = ?",
            (_SUBJECT_ID, fixture.object_key),
        )

    valid = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()
    assert valid.checks["event_hashes"] == "ok"
    assert valid.p0 == ()
    assert _archive_detail(valid)["status"] == "ok"

    monkeypatch.setenv(
        "NOYRA_ARCHIVE_ENCRYPTION_KEY",
        base64.urlsafe_b64encode(b"z" * 32).decode("ascii"),
    )
    degraded = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()
    assert degraded.checks["sqlite_integrity"] == "ok"
    assert degraded.checks["event_hashes"] == "degraded:key_unavailable"
    assert degraded.p0 == ()
    assert "event_archive:key_unavailable" in degraded.p1


@pytest.mark.parametrize(
    "corruption",
    [
        "ciphertext",
        "manifest-count",
        "manifest-count-real",
        "inner-payload",
        "body-format",
        "manifest-key-id",
        "manifest-fingerprint",
        "manifest-time",
        "manifest-created-at",
        "partial-metadata",
        "segment-id",
        "duplicate-event",
        "nonfinite-payload",
        "deep-payload",
        "missing-object",
        "missing-segment",
    ],
)
def test_archive_corruption_is_p0(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    if corruption == "ciphertext":
        stored = fixture.archive_path.read_bytes()
        fixture.archive_path.write_bytes(stored[:-1] + bytes([stored[-1] ^ 0x01]))
    elif corruption == "manifest-count":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET event_count = event_count + 1 "
                "WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "manifest-count-real":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET event_count = 1.5 "
                "WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "manifest-key-id":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET encryption_key_id = 'tampered-key-id' "
                "WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "manifest-fingerprint":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET encryption_key_fingerprint = ? "
                "WHERE subject_id = ? AND object_key = ?",
                ("f" * 64, _SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "manifest-time":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET first_occurred_at = ? "
                "WHERE subject_id = ? AND object_key = ?",
                ("2099-01-01T00:00:00+00:00", _SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "manifest-created-at":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET created_at = ? "
                "WHERE subject_id = ? AND object_key = ?",
                ("2099-01-01T00:00:00+00:00", _SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "partial-metadata":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET encryption_key_id = NULL "
                "WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "segment-id":
        with fixture.database.transaction() as connection:
            connection.execute(
                "UPDATE event_payload_segments SET segment_id = 'tampered-segment' "
                "WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "duplicate-event":

        def duplicate_event(body: dict[str, object]) -> None:
            events = body["events"]
            assert isinstance(events, list) and len(events) == 1
            item = events[0]
            assert isinstance(item, dict)
            events.append(dict(item))

        _rewrite_archive_body(fixture, duplicate_event)
    elif corruption in {"nonfinite-payload", "deep-payload"}:

        def invalid_payload(body: dict[str, object]) -> None:
            events = body["events"]
            assert isinstance(events, list) and len(events) == 1
            item = events[0]
            assert isinstance(item, dict)
            item["payload_json"] = (
                '{"value":NaN}'
                if corruption == "nonfinite-payload"
                else '{"value":' + "[" * 1_100 + "0" + "]" * 1_100 + "}"
            )
            item["payload_hash"] = "0" * 64

        _rewrite_archive_body(fixture, invalid_payload)
    elif corruption == "missing-object":
        fixture.archive_path.unlink()
    elif corruption == "missing-segment":
        with fixture.database.transaction() as connection:
            connection.execute(
                "DELETE FROM event_payload_segments WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    elif corruption == "body-format":
        _rewrite_archive_body(
            fixture,
            lambda body: body.__setitem__("format", "tampered-format"),
        )
    else:

        def alter_payload(body: dict[str, object]) -> None:
            events = body["events"]
            assert isinstance(events, list) and len(events) == 1
            item = events[0]
            assert isinstance(item, dict)
            payload = {"value": "tampered evidence"}
            item["payload_json"] = canonical_json(payload)
            item["payload_hash"] = content_hash(payload)

        _rewrite_archive_body(fixture, alter_payload)

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "failed"
    assert "event_archive:corrupt" in report.p0
    assert "event_archive:key_unavailable" not in report.p1
    detail = _archive_detail(report)
    assert detail["status"] == "corrupt"
    assert detail["reason"]
    assert detail["events"] == 1
    assert detail["segments"] == (0 if corruption == "missing-segment" else 1)


def test_combined_key_id_and_ciphertext_tamper_is_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    stored = fixture.archive_path.read_bytes()
    fixture.archive_path.write_bytes(stored[:-1] + bytes([stored[-1] ^ 0x01]))
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE event_payload_segments SET encryption_key_id = 'tampered-key-id' "
            "WHERE subject_id = ? AND object_key = ?",
            (_SUBJECT_ID, fixture.object_key),
        )

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "failed"
    assert "event_archive:corrupt" in report.p0
    assert "event_archive:key_unavailable" not in report.p1


def test_fingerprint_and_ciphertext_tamper_fails_closed_as_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    stored = fixture.archive_path.read_bytes()
    fixture.archive_path.write_bytes(stored[:-1] + bytes([stored[-1] ^ 0x01]))
    with fixture.database.transaction() as connection:
        connection.execute(
            "UPDATE event_payload_segments SET encryption_key_fingerprint = ? "
            "WHERE subject_id = ? AND object_key = ?",
            ("f" * 64, _SUBJECT_ID, fixture.object_key),
        )

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "failed"
    assert "event_archive:corrupt" in report.p0
    assert "event_archive:key_unavailable" not in report.p1


@pytest.mark.parametrize("database_corruption", ["missing-segment", "manifest-count"])
def test_database_archive_corruption_precedes_missing_key_classification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    database_corruption: str,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    with fixture.database.transaction() as connection:
        if database_corruption == "missing-segment":
            connection.execute(
                "DELETE FROM event_payload_segments WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
        else:
            connection.execute(
                "UPDATE event_payload_segments SET event_count = event_count + 1 "
                "WHERE subject_id = ? AND object_key = ?",
                (_SUBJECT_ID, fixture.object_key),
            )
    monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY")
    monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID")

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "failed"
    assert "event_archive:corrupt" in report.p0
    assert "event_archive:key_unavailable" not in report.p1


def test_archive_verification_limit_is_degraded_without_sqlite_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    limits = EventArchiveVerificationLimits(
        max_segment_decompressed_bytes=64,
        max_total_decompressed_bytes=128,
    )

    report = LongRunResilience(
        fixture.database,
        _SUBJECT_ID,
        fixture.root,
        archive_verification_limits=limits,
    ).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "degraded:verification_limit"
    assert report.p0 == ()
    assert "event_archive:verification_limit" in report.p1
    detail = _archive_detail(report)
    assert detail["status"] == "degraded"
    assert detail["reason"] == "verification_limit"


def test_valid_segment_above_configured_event_budget_is_degraded_not_corrupt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = base64.urlsafe_b64encode(_KEY_BYTES).decode("ascii")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", key)
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", _KEY_ID)
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, "a" * 64)
    SnapshotStore(database).save(
        _SUBJECT_ID,
        {"stable": True},
        state_version=1,
        reason="p103 fixture",
    )
    store = EventStore(database)
    for index in range(2):
        store.append(
            _SUBJECT_ID,
            "experience",
            "p103-test",
            {"index": index},
            occurred_at="2020-01-01T00:00:00+00:00",
            event_id=f"evt-p103-budget-{index}",
        )
    archive = EventPayloadArchive(database, tmp_path / "subject" / "cold")
    assert archive.archive_cold(_SUBJECT_ID, older_than_days=30, limit=2) == 2
    limits = EventArchiveVerificationLimits(max_events_per_segment=1)

    report = LongRunResilience(
        database,
        _SUBJECT_ID,
        tmp_path,
        archive_verification_limits=limits,
    ).audit()

    assert report.event_count == 2
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "degraded:verification_limit"
    assert report.p0 == ()
    assert "event_archive:verification_limit" in report.p1


def test_oversize_unauthenticated_object_is_conservatively_verification_limited(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    with fixture.archive_path.open("ab") as stream:
        stream.write(b"x" * 2_048)
    limits = EventArchiveVerificationLimits(
        max_segment_decompressed_bytes=64,
        max_total_decompressed_bytes=128,
    )

    report = LongRunResilience(
        fixture.database,
        _SUBJECT_ID,
        fixture.root,
        archive_verification_limits=limits,
    ).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "degraded:verification_limit"
    assert report.p0 == ()
    assert "event_archive:verification_limit" in report.p1


def test_hot_event_verification_limit_is_degraded_without_materializing_payload(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, "a" * 64)
    SnapshotStore(database).save(
        _SUBJECT_ID,
        {"stable": True},
        state_version=1,
        reason="p103 fixture",
    )
    EventStore(database).append(
        _SUBJECT_ID,
        "experience",
        "p103-test",
        {"value": "x" * 128},
        event_id="evt-p103-large-hot",
    )
    limits = EventArchiveVerificationLimits(
        max_hot_event_payload_bytes=64,
        max_hot_event_bytes=128,
    )

    report = LongRunResilience(
        database,
        _SUBJECT_ID,
        tmp_path,
        archive_verification_limits=limits,
    ).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "degraded:verification_limit"
    assert report.p0 == ()
    assert "event_archive:verification_limit" in report.p1


def test_archive_provider_unavailable_is_degraded(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)

    def unavailable(
        provider: LocalArchiveProvider,
        object_key: str,
        *,
        max_bytes: int | None = None,
    ) -> bytes:
        del provider, object_key, max_bytes
        raise PermissionError("archive temporarily unavailable")

    monkeypatch.setattr(LocalArchiveProvider, "get", unavailable)

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "degraded:unavailable"
    assert report.p0 == ()
    assert "event_archive:unavailable" in report.p1


def test_bounded_local_archive_read_never_uses_unbounded_read_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = base64.urlsafe_b64encode(_KEY_BYTES).decode("ascii")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", key)
    provider = LocalArchiveProvider(tmp_path / "cold")
    provider.put("events/bounded.enc", b"bounded payload")

    def reject_unbounded_read(path: Path) -> bytes:
        raise AssertionError(f"unexpected unbounded read: {path}")

    monkeypatch.setattr(Path, "read_bytes", reject_unbounded_read)

    assert provider.get("events/bounded.enc", max_bytes=64) == b"bounded payload"
    with pytest.raises(PayloadLimitError, match="read limit"):
        provider.get("events/bounded.enc", max_bytes=4)


def test_invalid_archived_tombstone_is_reported_as_event_corruption(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    with fixture.database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_json = '{\"tampered\":true}' WHERE event_id = ?",
            (_EVENT_ID,),
        )

    report = LongRunResilience(fixture.database, _SUBJECT_ID, fixture.root).audit()

    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "failed"
    assert f"event_hash:{_EVENT_ID}" in report.p0


@pytest.mark.parametrize(
    "payload_json",
    [
        '{"value":NaN}',
        '{"value":' + "[" * 1_100 + "0" + "]" * 1_100 + "}",
    ],
)
def test_noncanonical_hot_payload_is_event_corruption_not_sqlite_failure(
    tmp_path: Path,
    payload_json: str,
) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, "a" * 64)
    SnapshotStore(database).save(
        _SUBJECT_ID,
        {"stable": True},
        state_version=1,
        reason="p103 fixture",
    )
    EventStore(database).append(
        _SUBJECT_ID,
        "experience",
        "p103-test",
        {"value": "original"},
        event_id="evt-p103-invalid-hot",
    )
    with database.transaction() as connection:
        connection.execute("DROP TRIGGER prevent_event_immutable_update")
        connection.execute(
            "UPDATE events SET payload_json = ? WHERE event_id = ?",
            (payload_json, "evt-p103-invalid-hot"),
        )

    report = LongRunResilience(database, _SUBJECT_ID, tmp_path).audit()

    assert report.event_count == 1
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "failed"
    assert "audit_runtime" not in report.checks
    assert "event_hash:evt-p103-invalid-hot" in report.p0


def test_multiple_segments_restore_payloads_and_preserve_event_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = base64.urlsafe_b64encode(_KEY_BYTES).decode("ascii")
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", key)
    monkeypatch.setenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", _KEY_ID)
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, "a" * 64)
    SnapshotStore(database).save(
        _SUBJECT_ID,
        {"stable": True},
        state_version=1,
        reason="p103 fixture",
    )
    store = EventStore(database)
    payloads = {
        f"evt-p103-{index}": {"index": index, "evidence": f"payload-{index}"} for index in range(3)
    }
    for event_id, payload in payloads.items():
        store.append(
            _SUBJECT_ID,
            "archived_experience",
            "p103-test",
            payload,
            occurred_at="2020-01-01T00:00:00+00:00",
            event_id=event_id,
        )
    with database.connection() as connection:
        original_root = str(
            connection.execute(
                "SELECT root_hash FROM event_chain_roots WHERE subject_id = ? "
                "ORDER BY sequence_number DESC LIMIT 1",
                (_SUBJECT_ID,),
            ).fetchone()[0]
        )
    archive = EventPayloadArchive(database, tmp_path / "subject" / "cold")
    for _ in range(3):
        assert archive.archive_cold(_SUBJECT_ID, older_than_days=30, limit=1) == 1

    report = LongRunResilience(database, _SUBJECT_ID, tmp_path).audit()

    assert report.event_count == 3
    assert report.checks["event_hashes"] == "ok"
    assert report.p0 == ()
    assert _archive_detail(report) == {
        "events": 3,
        "reason": None,
        "segments": 3,
        "status": "ok",
        "verified_events": 3,
        "verified_segments": 3,
    }
    with database.connection() as connection:
        rows = connection.execute(
            "SELECT * FROM events WHERE subject_id = ? ORDER BY event_id",
            (_SUBJECT_ID,),
        ).fetchall()
        restored = {
            str(row["event_id"]): store.payload_from_row(
                row,
                connection=connection,
                max_archive_bytes=64_000_000,
            )
            for row in rows
        }
        current_root = str(
            connection.execute(
                "SELECT root_hash FROM event_chain_roots WHERE subject_id = ? "
                "ORDER BY sequence_number DESC LIMIT 1",
                (_SUBJECT_ID,),
            ).fetchone()[0]
        )
    assert restored == payloads
    assert current_root == original_root


def test_concurrent_archive_commit_cannot_mix_integrity_snapshots(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _create_archived_fixture(tmp_path, monkeypatch)
    EventStore(fixture.database).append(
        _SUBJECT_ID,
        "archived_experience",
        "p103-test",
        {"value": "second evidence"},
        occurred_at="2020-01-02T00:00:00+00:00",
        event_id="evt-p103-second",
    )
    archive = EventPayloadArchive(fixture.database, fixture.archive_root)
    archived = False

    def concurrent_commit() -> None:
        nonlocal archived
        if not archived:
            archived = True
            assert archive.archive_cold(_SUBJECT_ID, older_than_days=30, limit=1) == 1

    report = LongRunResilience(
        fixture.database,
        _SUBJECT_ID,
        fixture.root,
        archive_verification_checkpoint=concurrent_commit,
    ).audit()

    assert archived
    assert report.event_count == 2
    assert report.checks["sqlite_integrity"] == "ok"
    assert report.checks["event_hashes"] == "ok"
    assert report.p0 == ()
    detail = _archive_detail(report)
    assert detail["events"] == 1
    assert detail["segments"] == 1
