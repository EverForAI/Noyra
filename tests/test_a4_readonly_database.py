from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from noyra.core.database import Database
from noyra.core.errors import RuntimeOwnershipError
from noyra.core.runtime import SubjectKernel
from noyra.core.types import content_hash


def _probe_database(path: Path, *, journal_mode: str = "WAL") -> None:
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute(f"PRAGMA journal_mode={journal_mode}").fetchone()[0] == (
            journal_mode.lower()
        )
        connection.execute("CREATE TABLE live_wal_probe(value TEXT)")
        connection.execute("INSERT INTO live_wal_probe VALUES ('baseline')")
        connection.commit()


def _files(root: Path) -> dict[str, bytes]:
    return {item.name: item.read_bytes() for item in root.iterdir() if item.is_file()}


def test_live_readonly_database_reads_committed_uncheckpointed_wal(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite3"
    _probe_database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE live_wal_probe SET value='latest'")
        writer.commit()
        assert Path(str(path) + "-wal").stat().st_size > 0
        readonly = Database(path, initialize=False, read_only=True)
        with readonly.read_transaction() as reader:
            assert reader.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "latest"
        # An unfinished write is not part of the committed live view.
        writer.execute("UPDATE live_wal_probe SET value='uncommitted'")
        with readonly.read_transaction() as reader:
            assert reader.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "latest"
        writer.rollback()


def test_live_read_transaction_is_consistent_and_next_transaction_refreshes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "live.sqlite3"
    _probe_database(path)
    readonly = Database(path, initialize=False, read_only=True)
    with closing(sqlite3.connect(path)) as writer, readonly.read_transaction() as reader:
        assert reader.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "baseline"
        writer.execute("UPDATE live_wal_probe SET value='next'")
        writer.commit()
        assert reader.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "baseline"
        with readonly.read_transaction() as fresh:
            assert fresh.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "next"


def test_live_reads_do_not_rewrite_main_database_or_checkpoint_wal(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite3"
    _probe_database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        writer.execute("UPDATE live_wal_probe SET value='committed'")
        writer.commit()
        wal_path = Path(str(path) + "-wal")
        before = (path.read_bytes(), wal_path.read_bytes())
        readonly = Database(path, initialize=False, read_only=True, strict_schema_contract=False)
        for _ in range(3):
            with readonly.read_transaction() as reader:
                assert reader.execute("SELECT value FROM live_wal_probe").fetchone()[0] == (
                    "committed"
                )
        # SQLite may update SHM read marks. Do not claim its coordination
        # files are byte-for-byte unchanged when reading a live WAL database.
        assert (path.read_bytes(), wal_path.read_bytes()) == before


@pytest.mark.parametrize("immutable_snapshot", [False, True])
def test_readonly_missing_path_does_not_create_database_or_directories(
    tmp_path: Path, immutable_snapshot: bool
) -> None:
    path = tmp_path / "missing" / "database.sqlite3"
    with pytest.raises(FileNotFoundError):
        Database(path, initialize=False, read_only=True, immutable_snapshot=immutable_snapshot)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("immutable_snapshot", [False, True])
def test_readonly_initialization_is_rejected_before_creating_anything(
    tmp_path: Path, immutable_snapshot: bool
) -> None:
    with pytest.raises(ValueError, match="initialize=False"):
        Database(
            tmp_path / "absent" / "database.sqlite3",
            read_only=True,
            immutable_snapshot=immutable_snapshot,
        )
    assert list(tmp_path.iterdir()) == []


def test_immutable_requires_readonly_before_initialization(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="read_only=True"):
        Database(tmp_path / "absent" / "database.sqlite3", immutable_snapshot=True)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("immutable_snapshot", [False, True])
def test_readonly_blocks_writes_even_with_query_only_disabled(
    tmp_path: Path, immutable_snapshot: bool
) -> None:
    path = tmp_path / "sealed.sqlite3"
    _probe_database(path, journal_mode="DELETE")
    readonly = Database(
        path, initialize=False, read_only=True, immutable_snapshot=immutable_snapshot
    )
    before = _files(tmp_path)
    with readonly.connection() as connection:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("INSERT INTO live_wal_probe VALUES ('forbidden')")
        connection.execute("PRAGMA query_only=OFF")
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("CREATE TABLE forbidden(value TEXT)")
    assert _files(tmp_path) == before


@pytest.mark.parametrize("journal_mode", ["DELETE", "WAL"])
def test_sealed_snapshot_preserves_files_bytes_schema_and_cannot_be_promoted(
    tmp_path: Path, journal_mode: str
) -> None:
    path = tmp_path / "sealed.sqlite3"
    _probe_database(path, journal_mode=journal_mode)
    before = _files(tmp_path)
    assert set(before) == {path.name}
    readonly = Database(path, initialize=False, read_only=True, immutable_snapshot=True)
    with readonly.read_transaction() as connection:
        assert connection.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "baseline"
        assert connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        assert connection.execute("SELECT name FROM sqlite_schema WHERE type='table'").fetchall()
    with pytest.raises(RuntimeOwnershipError, match="cannot become writable"):
        readonly.promote_to_writable()
    assert readonly.read_only is True
    assert _files(tmp_path) == before


@pytest.mark.parametrize("suffix", ["-wal", "-shm", "-journal"])
@pytest.mark.parametrize("contents", [b"", b"untrusted journal state"])
def test_sealed_snapshot_rejects_visible_sidecars_without_changing_them(
    tmp_path: Path, suffix: str, contents: bytes
) -> None:
    path = tmp_path / "sealed.sqlite3"
    _probe_database(path, journal_mode="DELETE")
    readonly = Database(path, initialize=False, read_only=True, immutable_snapshot=True)
    # Check on every connection, including sidecars appearing after facade creation.
    path.with_name(path.name + suffix).write_bytes(contents)
    before = _files(tmp_path)
    with pytest.raises(RuntimeOwnershipError, match="SQLite journal state"), readonly.connection():
        pytest.fail("journal state must never be silently ignored")
    assert _files(tmp_path) == before


def test_sealed_snapshot_rejects_live_uncheckpointed_wal(tmp_path: Path) -> None:
    path = tmp_path / "live.sqlite3"
    _probe_database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("UPDATE live_wal_probe SET value='latest'")
        writer.commit()
        readonly = Database(path, initialize=False, read_only=True, immutable_snapshot=True)
        with (
            pytest.raises(RuntimeOwnershipError, match="SQLite journal state"),
            readonly.read_transaction(),
        ):
            pytest.fail("must refuse stale immutable interpretation of a live database")
        # Refusal does not disrupt the real owner or discard committed changes.
        assert writer.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "latest"


def test_sealed_snapshot_rejects_dangling_journal_symlink(tmp_path: Path) -> None:
    path = tmp_path / "sealed.sqlite3"
    _probe_database(path, journal_mode="DELETE")
    sidecar = path.with_name(path.name + "-wal")
    try:
        sidecar.symlink_to(tmp_path / "missing-target")
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable for this test account")
    try:
        readonly = Database(path, initialize=False, read_only=True, immutable_snapshot=True)
        with (
            pytest.raises(RuntimeOwnershipError, match="SQLite journal state"),
            readonly.connection(),
        ):
            pytest.fail("a dangling sidecar link is still visible journal state")
        assert sidecar.is_symlink()
        assert not (tmp_path / "missing-target").exists()
    finally:
        sidecar.unlink()


@pytest.mark.parametrize("immutable_snapshot", [False, True])
def test_readonly_uri_handles_filename_metacharacters(
    tmp_path: Path, immutable_snapshot: bool
) -> None:
    path = tmp_path / "数据库 # readonly %3f & immutable=1.sqlite3"
    _probe_database(path, journal_mode="DELETE")
    readonly = Database(
        path, initialize=False, read_only=True, immutable_snapshot=immutable_snapshot
    )
    before = _files(tmp_path)
    with readonly.read_transaction() as connection:
        assert connection.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "baseline"
    assert _files(tmp_path) == before


def test_kernel_preflight_reads_latest_wal_and_promotes_only_after_ownership(
    tmp_path: Path,
) -> None:
    path = tmp_path / "noyra.sqlite3"
    subject_id = "Noyra-readonly-test"
    genesis = content_hash({"project": "Noyra", "seed": "readonly-boundary"})
    owner = SubjectKernel(path, subject_id, genesis)
    observer: SubjectKernel | None = None
    try:
        owner.boot()
        with closing(sqlite3.connect(path)) as anchor:
            assert anchor.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0] == 0
            anchor.execute("PRAGMA wal_autocheckpoint=0")
            snapshot = owner.checkpoint({"state": "committed in WAL"}, reason="readonly test")
            assert owner.identity_store.load(subject_id).state_version == 1
            # Demonstrate the exact stale view that the previous facade exposed.
            with closing(
                sqlite3.connect(f"{path.as_uri()}?mode=ro&immutable=1", uri=True)
            ) as stale:
                assert (
                    stale.execute("SELECT state_version FROM subject_identity").fetchone()[0] == 0
                )
            before = (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes())
            observer = SubjectKernel(path, subject_id, genesis, allow_subject_creation=False)
            assert observer.identity is not None
            assert observer.identity.state_version == 1
            assert observer.identity.last_checkpoint == snapshot.snapshot_id
            assert observer.snapshot_store.latest(subject_id).snapshot_id == snapshot.snapshot_id
            observer.verify_continuity()
            with pytest.raises(RuntimeOwnershipError):
                observer.boot()
            assert not observer.process_lock.held
            assert observer.database.read_only is True
            assert owner.process_lock.held
            assert (path.read_bytes(), path.with_name(path.name + "-wal").read_bytes()) == before
        owner.close()
        observer.boot()
        assert observer.process_lock.held
        assert observer.database.read_only is False
        observer.checkpoint({"state": "new owner"}, reason="ownership acquired")
        assert observer.identity_store.load(subject_id).state_version == 2
    finally:
        if observer is not None:
            observer.close()
        owner.close()


def test_readonly_directory_requires_explicit_snapshot_role_without_live_fallback(
    tmp_path: Path,
) -> None:
    if os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0:
        pytest.skip("requires POSIX permissions enforced for a non-root test account")
    path = tmp_path / "sealed.sqlite3"
    _probe_database(path)
    before = _files(tmp_path)
    path.chmod(0o400)
    tmp_path.chmod(0o500)
    try:
        readonly = Database(path, initialize=False, read_only=True, immutable_snapshot=True)
        with readonly.read_transaction() as connection:
            assert (
                connection.execute("SELECT value FROM live_wal_probe").fetchone()[0] == "baseline"
            )
        assert _files(tmp_path) == before
        live = Database(path, initialize=False, read_only=True)
        with pytest.raises(sqlite3.OperationalError), live.read_transaction() as connection:
            connection.execute("SELECT value FROM live_wal_probe").fetchone()
        assert _files(tmp_path) == before
    finally:
        tmp_path.chmod(0o700)
        path.chmod(0o600)


def test_live_readonly_directory_can_use_existing_readable_wal_sidecars(tmp_path: Path) -> None:
    if os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0:
        pytest.skip("requires POSIX permissions enforced for a non-root test account")
    path = tmp_path / "live.sqlite3"
    _probe_database(path)
    with closing(sqlite3.connect(path)) as writer:
        writer.execute("UPDATE live_wal_probe SET value='latest'")
        writer.commit()
        wal = path.with_name(path.name + "-wal")
        shm = path.with_name(path.name + "-shm")
        before = (path.read_bytes(), wal.read_bytes())
        for item in (path, wal, shm):
            item.chmod(0o400)
        tmp_path.chmod(0o500)
        try:
            readonly = Database(path, initialize=False, read_only=True)
            with readonly.read_transaction() as connection:
                assert connection.execute("SELECT value FROM live_wal_probe").fetchone()[0] == (
                    "latest"
                )
            assert (path.read_bytes(), wal.read_bytes()) == before
        finally:
            tmp_path.chmod(0o700)
            for item in (path, wal, shm):
                item.chmod(0o600)
