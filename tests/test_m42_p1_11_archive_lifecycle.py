from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import pytest

from noyra.core import (
    ArchiveKeyMaterial,
    ArchiveKeyring,
    ArchiveReplicaLedger,
    ArchiveTransferQueue,
    CloudArchiveCoordinator,
    Database,
    EventStore,
    IdentityStore,
    IntegrityRegistry,
    StorageQuota,
    StorageUsageHistory,
    StorageUsageScanner,
)
from noyra.core.archive import LocalArchiveProvider, S3ArchiveProvider
from noyra.core.at_rest import _windows_harden
from noyra.core.errors import (
    ArchiveKeyUnavailableError,
    ArchiveUnavailableError,
    IntegrityError,
    PayloadLimitError,
)
from noyra.core.event_archive import EventPayloadArchive
from noyra.core.resilience import LongRunResilience
from noyra.core.storage import ArchiveStore
from noyra.core.types import content_hash
from noyra.world import FetchedDocument, ObservationStore, SourceRegistry, WorldIntegrity
from noyra.world.observation_archive import ObservationContentArchive

_SUBJECT_ID = "Noyra-p111-archive-lifecycle"
_KEY_1 = b"1" * 32
_KEY_2 = b"2" * 32


@dataclass
class _MemoryCloud:
    provider_id: str = "memory-cloud-p111"
    read_mode: str = "good"

    def __post_init__(self) -> None:
        self.name = "memory-cloud"
        self.objects: dict[str, bytes] = {}

    def put(self, object_key: str, payload: bytes) -> str:
        self.objects[object_key] = payload
        return hashlib.sha256(payload).hexdigest()

    def get(self, object_key: str, *, max_bytes: int | None = None) -> bytes:
        if self.read_mode == "timeout":
            raise TimeoutError("cloud read timed out")
        if self.read_mode == "missing" or object_key not in self.objects:
            raise FileNotFoundError(object_key)
        payload = self.objects[object_key]
        if self.read_mode == "corrupt":
            payload = payload[:-1] + bytes([payload[-1] ^ 0x01])
        if max_bytes is not None and len(payload) > max_bytes:
            raise PayloadLimitError("cloud object exceeds its read limit")
        return payload

    def exists(self, object_key: str) -> bool:
        return object_key in self.objects


@dataclass(frozen=True)
class _ArchivedPair:
    database: Database
    root: Path
    archive_root: Path
    subject_id: str
    source_id: str
    source_url: str
    event_id: str
    event_key: str
    observation_id: str
    observation_key: str
    observation_content: str


def _encoded(key: bytes) -> str:
    return base64.urlsafe_b64encode(key).decode("ascii")


def _material(key_id: str, key: bytes, status: str) -> ArchiveKeyMaterial:
    return ArchiveKeyMaterial(key_id, key, hashlib.sha256(key).hexdigest(), status)


def _write_keyring(
    path: Path,
    *,
    generation: int,
    active_key_id: str,
    legacy_key_id: str | None,
    keys: tuple[tuple[str, bytes, str], ...],
) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "noyra-archive-keyring/v1",
                "generation": generation,
                "active_key_id": active_key_id,
                "legacy_key_id": legacy_key_id,
                "keys": [
                    {"key_id": key_id, "status": status, "key_b64": _encoded(key)}
                    for key_id, key, status in keys
                ],
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    if os.name == "nt":
        _windows_harden(path)


def _configure_keyring(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("NOYRA_ARCHIVE_KEYRING_PATH", str(path))
    monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("NOYRA_ARCHIVE_ENCRYPTION_KEY_ID", raising=False)


def _create_archived_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> _ArchivedPair:
    keyring_path = tmp_path / "archive-keyring.json"
    _write_keyring(
        keyring_path,
        generation=1,
        active_key_id="key-1",
        legacy_key_id="key-1",
        keys=(("key-1", _KEY_1, "active"),),
    )
    _configure_keyring(monkeypatch, keyring_path)
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, content_hash({"seed": "p111"}))
    source = SourceRegistry(database).register(
        _SUBJECT_ID,
        "P1-11 source",
        "https://example.com/p111",
        "news",
        trust_score=0.8,
        status="active",
        reason="P1-11 archive lifecycle fixture",
    )
    event = EventStore(database).append(
        _SUBJECT_ID,
        "p111_event",
        "p111-test",
        {"evidence": "event-one"},
        occurred_at="2020-01-01T00:00:00+00:00",
    )
    observation_content = "P1-11 archived observation content."
    observation = ObservationStore(database).record(
        _SUBJECT_ID,
        source.source_id,
        FetchedDocument(
            url=source.url,
            title="P1-11 observation",
            content=observation_content,
            content_hash=content_hash(observation_content),
            media_type="text/html",
            injection_signals=(),
            etag=None,
            last_modified=None,
            fetched_at="2020-01-01T00:00:00+00:00",
        ),
    )[0]
    archive_root = tmp_path / "subject" / "cold"
    assert (
        EventPayloadArchive(database, archive_root).archive_cold(_SUBJECT_ID, older_than_days=30)
        >= 1
    )
    assert (
        ObservationContentArchive(database, archive_root).archive_cold(
            _SUBJECT_ID, older_than_days=30
        )
        == 1
    )
    with database.connection() as connection:
        event_key = str(
            connection.execute(
                "SELECT payload_archive_key FROM events WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()[0]
        )
        observation_key = str(
            connection.execute(
                "SELECT object_key FROM observation_content_segments WHERE subject_id = ?",
                (_SUBJECT_ID,),
            ).fetchone()[0]
        )
    return _ArchivedPair(
        database=database,
        root=tmp_path,
        archive_root=archive_root,
        subject_id=_SUBJECT_ID,
        source_id=source.source_id,
        source_url=source.url,
        event_id=event.event_id,
        event_key=event_key,
        observation_id=observation.observation_id,
        observation_key=observation_key,
        observation_content=observation_content,
    )


def _upload_and_collect(pair: _ArchivedPair, cloud: _MemoryCloud) -> dict[str, int]:
    coordinator = CloudArchiveCoordinator(
        pair.database,
        pair.subject_id,
        pair.root / "training_raw",
        cloud,
        local_archive_root=pair.archive_root,
    )
    return coordinator.tick(garbage_collect_local=True)


def test_key_rotation_keeps_mixed_event_and_observation_segments_readable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = _create_archived_pair(tmp_path, monkeypatch)
    keyring_path = tmp_path / "archive-keyring.json"
    _write_keyring(
        keyring_path,
        generation=2,
        active_key_id="key-2",
        legacy_key_id="key-1",
        keys=(("key-1", _KEY_1, "retired"), ("key-2", _KEY_2, "active")),
    )
    second_event = EventStore(pair.database).append(
        pair.subject_id,
        "p111_event",
        "p111-test",
        {"evidence": "event-two"},
        occurred_at="2020-01-02T00:00:00+00:00",
    )
    second_content = "P1-11 second archived observation."
    second_observation = ObservationStore(pair.database).record(
        pair.subject_id,
        pair.source_id,
        FetchedDocument(
            url=pair.source_url,
            title="P1-11 observation two",
            content=second_content,
            content_hash=content_hash(second_content),
            media_type="text/html",
            injection_signals=(),
            etag=None,
            last_modified=None,
            fetched_at="2020-01-02T00:00:00+00:00",
        ),
    )[0]
    assert (
        EventPayloadArchive(pair.database, pair.archive_root).archive_cold(
            pair.subject_id, older_than_days=30
        )
        >= 1
    )
    assert (
        ObservationContentArchive(pair.database, pair.archive_root).archive_cold(
            pair.subject_id, older_than_days=30
        )
        == 1
    )

    events = EventStore(pair.database)
    observations = ObservationStore(pair.database)
    assert events.get(pair.event_id).payload == {"evidence": "event-one"}
    assert events.get(second_event.event_id).payload == {"evidence": "event-two"}
    assert (
        observations.get(pair.observation_id, subject_id=pair.subject_id).content
        == pair.observation_content
    )
    assert (
        observations.get(second_observation.observation_id, subject_id=pair.subject_id).content
        == second_content
    )
    with pair.database.connection() as connection:
        event_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT encryption_key_id FROM event_payload_segments WHERE subject_id = ?",
                (pair.subject_id,),
            )
        }
        observation_keys = {
            str(row[0])
            for row in connection.execute(
                "SELECT encryption_key_id FROM observation_content_segments WHERE subject_id = ?",
                (pair.subject_id,),
            )
        }
        generations = [
            int(row[0])
            for row in connection.execute(
                "SELECT generation FROM archive_keyring_revisions WHERE subject_id = ? "
                "ORDER BY generation",
                (pair.subject_id,),
            )
        ]
    assert event_keys == {"key-1", "key-2"}
    assert observation_keys == {"key-1", "key-2"}
    assert generations == [1, 2]

    _write_keyring(
        keyring_path,
        generation=3,
        active_key_id="key-2",
        legacy_key_id=None,
        keys=(("key-2", _KEY_2, "active"),),
    )
    report = LongRunResilience(pair.database, pair.subject_id, pair.root).audit()
    assert "event_archive:key_unavailable" in report.p1
    assert "event_archive:corrupt" not in report.p0
    with pytest.raises(ArchiveKeyUnavailableError):
        ObservationStore(pair.database).get(pair.observation_id, subject_id=pair.subject_id)


def test_keyring_rejects_key_id_reuse_and_generation_rollback(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, content_hash({"seed": "keyring"}))
    first = ArchiveKeyring(
        generation=1,
        active_key_id="stable-id",
        legacy_key_id="stable-id",
        keys=(_material("stable-id", _KEY_1, "active"),),
    )
    assert first.record_revision(database, _SUBJECT_ID)
    reused = ArchiveKeyring(
        generation=2,
        active_key_id="stable-id",
        legacy_key_id="stable-id",
        keys=(_material("stable-id", _KEY_2, "active"),),
    )
    with pytest.raises(ArchiveKeyUnavailableError, match="cannot be reused"):
        reused.record_revision(database, _SUBJECT_ID)

    second = ArchiveKeyring(
        generation=2,
        active_key_id="key-2",
        legacy_key_id="stable-id",
        keys=(
            _material("stable-id", _KEY_1, "retired"),
            _material("key-2", _KEY_2, "active"),
        ),
    )
    assert second.record_revision(database, _SUBJECT_ID)
    with pytest.raises(ArchiveKeyUnavailableError, match="rolled back"):
        first.validate_revision(database, _SUBJECT_ID)


def test_cloud_ack_without_matching_readback_never_becomes_verified(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, content_hash({"seed": "readback"}))
    object_key = "events/event-segment_readback.json.zlib.enc"
    payload = b"encrypted-archive-payload"
    ArchiveStore(database).register(
        _SUBJECT_ID,
        storage_class="cloud",
        object_key=object_key,
        byte_size=len(payload),
        payload=payload,
    )
    queue = ArchiveTransferQueue(database, _SUBJECT_ID, tmp_path / "training_raw")
    queue.enqueue(object_key, payload)
    cloud = _MemoryCloud(read_mode="corrupt")

    assert queue.drain(cloud) == {"uploaded": 0, "failed": 1, "dead": 0}
    with database.connection() as connection:
        transfer = connection.execute(
            "SELECT status, last_error FROM archive_transfer_queue WHERE subject_id = ?",
            (_SUBJECT_ID,),
        ).fetchone()
        archive = connection.execute(
            "SELECT status, verified_at FROM storage_archives WHERE subject_id = ?",
            (_SUBJECT_ID,),
        ).fetchone()
        replica = connection.execute(
            "SELECT state, verified_at FROM archive_object_replicas "
            "WHERE subject_id = ? AND replica_type = 'cloud'",
            (_SUBJECT_ID,),
        ).fetchone()
    assert transfer["status"] == "failed"
    assert transfer["last_error"] == "IntegrityError"
    assert archive["status"] == "pending"
    assert archive["verified_at"] is None
    assert replica["state"] == "corrupt"
    assert replica["verified_at"] is None


def test_cloud_only_event_and_observation_reads_restore_local_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = _create_archived_pair(tmp_path, monkeypatch)
    cloud = _MemoryCloud()
    result = _upload_and_collect(pair, cloud)
    assert result["uploaded"] == 2
    assert result["garbage_collected"] == 2
    assert not (pair.archive_root / pair.event_key).exists()
    assert not (pair.archive_root / pair.observation_key).exists()

    event_payloads = EventPayloadArchive(
        pair.database,
        pair.archive_root,
        cloud_provider=cloud,
    ).load_segment(pair.subject_id, pair.event_key)
    observation_content = ObservationContentArchive(
        pair.database,
        pair.archive_root,
        cloud_provider=cloud,
    ).load_content(pair.subject_id, pair.observation_key, pair.observation_id)
    assert event_payloads[pair.event_id] == {"evidence": "event-one"}
    assert observation_content == pair.observation_content
    assert (pair.archive_root / pair.event_key).is_file()
    assert (pair.archive_root / pair.observation_key).is_file()
    with pair.database.connection() as connection:
        states = {
            (str(row["object_key"]), str(row["state"]))
            for row in connection.execute(
                "SELECT object_key, state FROM archive_object_replicas "
                "WHERE subject_id = ? AND replica_type = 'local'",
                (pair.subject_id,),
            )
        }
    assert (pair.event_key, "present") in states
    assert (pair.observation_key, "present") in states


def test_integrity_cloud_read_through_is_read_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = _create_archived_pair(tmp_path, monkeypatch)
    cloud = _MemoryCloud()
    assert _upload_and_collect(pair, cloud)["garbage_collected"] == 2
    monkeypatch.setenv("NOYRA_ARCHIVE_S3_BUCKET", "p111")
    monkeypatch.setattr(S3ArchiveProvider, "from_env", classmethod(lambda cls: cloud))
    with pair.database.connection() as connection:
        before = connection.execute(
            "SELECT COUNT(*) FROM archive_object_replica_revisions WHERE subject_id = ?",
            (pair.subject_id,),
        ).fetchone()[0]

    event = EventPayloadArchive.verify_subject_integrity(
        pair.database,
        pair.archive_root,
        pair.subject_id,
    )
    world = WorldIntegrity(pair.database).verify(pair.subject_id)

    with pair.database.connection() as connection:
        after = connection.execute(
            "SELECT COUNT(*) FROM archive_object_replica_revisions WHERE subject_id = ?",
            (pair.subject_id,),
        ).fetchone()[0]
    assert event.status == "ok"
    assert world["observation_content_segments"] == 1
    assert after == before
    assert not (pair.archive_root / pair.event_key).exists()
    assert not (pair.archive_root / pair.observation_key).exists()


@pytest.mark.parametrize(
    ("mode", "expected_error", "expected_state", "error_code"),
    [
        ("timeout", ArchiveUnavailableError, "unavailable", "TimeoutError"),
        ("missing", IntegrityError, "missing", "FileNotFoundError"),
        ("corrupt", IntegrityError, "corrupt", "IntegrityError"),
    ],
)
def test_cloud_restore_failure_classification_is_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    expected_error: type[Exception],
    expected_state: str,
    error_code: str,
) -> None:
    pair = _create_archived_pair(tmp_path, monkeypatch)
    cloud = _MemoryCloud()
    assert _upload_and_collect(pair, cloud)["garbage_collected"] == 2
    cloud.read_mode = mode

    with pytest.raises(expected_error):
        EventPayloadArchive(
            pair.database,
            pair.archive_root,
            cloud_provider=cloud,
        ).load_segment(pair.subject_id, pair.event_key)
    with pair.database.connection() as connection:
        rows = connection.execute(
            "SELECT replica_type, state, last_error_code FROM archive_object_replicas "
            "WHERE subject_id = ? AND object_key = ? ORDER BY replica_type",
            (pair.subject_id, pair.event_key),
        ).fetchall()
    states = {str(row["replica_type"]): str(row["state"]) for row in rows}
    errors = {str(row["replica_type"]): row["last_error_code"] for row in rows}
    assert states == {"cloud": expected_state, "local": expected_state}
    assert errors == {"cloud": error_code, "local": error_code}


def test_interrupted_local_gc_and_restore_transitions_reconcile_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = _create_archived_pair(tmp_path, monkeypatch)
    ledger = ArchiveReplicaLedger(pair.database)
    with pair.database.connection() as connection:
        row = connection.execute(
            "SELECT * FROM archive_object_replicas WHERE subject_id = ? "
            "AND object_key = ? AND replica_type = 'local'",
            (pair.subject_id, pair.event_key),
        ).fetchone()
    replica_id = str(row["replica_id"])
    archive_path = pair.archive_root / pair.event_key
    stored = archive_path.read_bytes()
    ledger.transition(replica_id, "gc_pending", reason="simulate interrupted GC")
    archive_path.unlink()
    coordinator = CloudArchiveCoordinator(
        pair.database,
        pair.subject_id,
        pair.root / "training_raw",
        _MemoryCloud(),
        local_archive_root=pair.archive_root,
    )
    coordinator._recover_local_transitions()
    absent = ledger.find(pair.subject_id, pair.event_key, replica_type="local")
    assert absent is not None
    assert absent["state"] == "absent"

    ledger.transition(replica_id, "restoring", reason="simulate interrupted restore")
    provider = LocalArchiveProvider(
        pair.archive_root,
        encryption_key=_KEY_1,
        key_id="key-1",
        create_root=False,
    )
    provider.put_stored(pair.event_key, stored)
    coordinator._recover_local_transitions()
    present = ledger.find(pair.subject_id, pair.event_key, replica_type="local")
    assert present is not None
    assert present["state"] == "present"

    ledger.transition(replica_id, "restoring", reason="simulate corrupt interrupted restore")
    archive_path.write_bytes(stored[:-1] + bytes([stored[-1] ^ 0x01]))
    coordinator._recover_local_transitions()
    reconciled = ledger.find(
        pair.subject_id,
        pair.event_key,
        replica_type="local",
    )
    assert reconciled is not None
    assert reconciled["state"] == "corrupt"
    assert reconciled["last_error_code"] == "ciphertext_mismatch"


def test_interrupted_uploading_replica_is_retried_and_verified(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, content_hash({"seed": "upload-recovery"}))
    object_key = "events/event-segment_interrupted.json.zlib.enc"
    payload = b"interrupted-upload"
    queue = ArchiveTransferQueue(database, _SUBJECT_ID, tmp_path / "training_raw")
    transfer_id = queue.enqueue(object_key, payload)
    cloud = _MemoryCloud()
    replica = ArchiveReplicaLedger(database).ensure(
        _SUBJECT_ID,
        object_key,
        replica_type="cloud",
        provider_id=cloud.provider_id,
        ciphertext_hash=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        state="pending",
        reason="fixture",
    )
    ArchiveReplicaLedger(database).transition(
        str(replica["replica_id"]),
        "uploading",
        reason="simulate interrupted upload",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE archive_transfer_queue SET status = 'uploading', claim_token = ?, "
            "lease_owner = ?, lease_expires_at = ? WHERE transfer_id = ?",
            (
                "archive-claim_interrupted",
                "worker-interrupted",
                "2020-01-01T00:00:00.000+00:00",
                transfer_id,
            ),
        )

    assert queue.drain(cloud) == {"uploaded": 1, "failed": 0, "dead": 0}
    with database.connection() as connection:
        transfer_state = connection.execute(
            "SELECT status FROM archive_transfer_queue WHERE transfer_id = ?",
            (transfer_id,),
        ).fetchone()[0]
        replica_state = connection.execute(
            "SELECT state FROM archive_object_replicas WHERE replica_id = ?",
            (replica["replica_id"],),
        ).fetchone()[0]
    assert transfer_state == "uploaded"
    assert replica_state == "verified"


def test_expired_upload_recovery_only_updates_current_provider_replica(tmp_path: Path) -> None:
    database = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(database).ensure(_SUBJECT_ID, content_hash({"seed": "provider-fence"}))
    object_key = "events/event-segment_provider-fence.json.zlib.enc"
    payload = b"provider-fenced-upload"
    queue = ArchiveTransferQueue(database, _SUBJECT_ID, tmp_path / "training_raw")
    transfer_id = queue.enqueue(object_key, payload)
    current = _MemoryCloud(provider_id="memory-cloud-current")
    other = _MemoryCloud(provider_id="memory-cloud-other")
    ledger = ArchiveReplicaLedger(database)

    # Insert the other provider last so the pre-fix, object-only recovery
    # query would select and mutate the wrong replica.
    current_replica = ledger.ensure(
        _SUBJECT_ID,
        object_key,
        replica_type="cloud",
        provider_id=current.provider_id,
        ciphertext_hash=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        state="pending",
        reason="current provider fixture",
    )
    other_replica = ledger.ensure(
        _SUBJECT_ID,
        object_key,
        replica_type="cloud",
        provider_id=other.provider_id,
        ciphertext_hash=hashlib.sha256(payload).hexdigest(),
        byte_size=len(payload),
        state="pending",
        reason="other provider fixture",
    )
    ledger.transition(
        str(current_replica["replica_id"]),
        "uploading",
        reason="current provider interrupted upload",
    )
    ledger.transition(
        str(other_replica["replica_id"]),
        "uploading",
        reason="other provider interrupted upload",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE archive_transfer_queue SET status = 'uploading', claim_token = ?, "
            "lease_owner = ?, lease_expires_at = ? WHERE transfer_id = ?",
            (
                "archive-claim_provider-fence",
                "worker-provider-fence",
                "2020-01-01T00:00:00.000+00:00",
                transfer_id,
            ),
        )

    assert queue.drain(current) == {"uploaded": 1, "failed": 0, "dead": 0}
    with database.connection() as connection:
        states = {
            str(row["provider_id"]): str(row["state"])
            for row in connection.execute(
                "SELECT provider_id, state FROM archive_object_replicas "
                "WHERE subject_id = ? AND object_key = ? AND replica_type = 'cloud'",
                (_SUBJECT_ID, object_key),
            ).fetchall()
        }
    assert states == {
        current.provider_id: "verified",
        other.provider_id: "uploading",
    }


def test_existing_archive_integrity_check_verifies_p111_ledgers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pair = _create_archived_pair(tmp_path, monkeypatch)
    usage = StorageUsageScanner(pair.root, pair.database).scan()
    StorageUsageHistory(pair.database, pair.subject_id).record(
        usage,
        StorageQuota(),
        10_000_000,
    )

    report = IntegrityRegistry().run(
        pair.database,
        pair.subject_id,
        pair.root,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("core.archive_dead_letter",),
    )
    result = report.checks[0]
    assert result.status == "ok"
    assert result.details["keyring_revisions"] == 1
    assert result.details["archive_replicas"] == 2
    assert result.details["replica_revisions"] == 2
    assert result.details["storage_usage_samples"] == 1

    with pair.database.transaction() as connection:
        connection.execute(
            "UPDATE archive_object_replicas SET state_hash = ? "
            "WHERE subject_id = ? AND object_key = ? AND replica_type = 'local'",
            ("0" * 64, pair.subject_id, pair.event_key),
        )
    corrupted = IntegrityRegistry().run(
        pair.database,
        pair.subject_id,
        pair.root,
        profile="manual",
        policy_mode="alert",
        deadline_seconds=15,
        check_ids=("core.archive_dead_letter",),
    )
    assert corrupted.status == "corrupt"
    assert corrupted.p0 == ("core.archive_dead_letter:integrity_error",)
