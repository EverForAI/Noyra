from __future__ import annotations

from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.interaction.participants import ParticipantStore


def test_sources_are_stable_scoped_and_do_not_merge_nicknames(tmp_path: Path) -> None:
    db = Database(tmp_path / "test.db", strict_schema_contract=True)
    for subject in ("subject-a", "subject-b"):
        IdentityStore(db).ensure(subject, "a" * 64)
    with db.transaction() as c:
        a = ParticipantStore.source(c, "subject-a", "telegram", "bot-1", "123", label="Same")
        assert (
            ParticipantStore.source(
                c, "subject-a", "telegram", "bot-1", "123", label="Renamed"
            ).entity_id
            == a.entity_id
        )
        b = ParticipantStore.source(c, "subject-a", "telegram", "bot-1", "456", label="Same")
        d = ParticipantStore.source(c, "subject-b", "telegram", "bot-1", "123", label="Same")
        assert len({a.entity_id, b.entity_id, d.entity_id}) == 3
        with pytest.raises(ValueError, match="another"):
            ParticipantStore.source(
                c, "subject-a", "telegram", "bot-1", "123", entity_id=b.entity_id
            )
        with pytest.raises(Exception, match="not found"):
            ParticipantStore.source(
                c, "subject-b", "web", "local", "foreign", entity_id=a.entity_id
            )


def test_schema104_strict_reopen_and_foreign_key_check(tmp_path: Path) -> None:
    db = Database(tmp_path / "db", strict_schema_contract=True)
    with db.connection() as c:
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            c.execute("SELECT value FROM schema_meta WHERE key='schema_version'").fetchone()[0]
            == "104"
        )
    Database(db.path, strict_schema_contract=True)


def test_schema103_upgrade_preserves_sources_and_export_omits_credentials(tmp_path: Path) -> None:
    import json
    import zipfile
    from io import BytesIO

    from noyra.core.database import _SCHEMA_DDL_FINGERPRINTS
    from noyra.core.runtime_export import RuntimeLogExporter

    db = Database(tmp_path / "db")
    with db.transaction() as c:
        for name in (
            "participant_notification_reads",
            "participant_applications",
            "participant_submissions",
            "participant_post_links",
            "participant_auth_limits",
            "participant_api_credentials",
            "participant_sessions",
            "participant_credentials",
            "participant_accounts",
            "participant_message_links",
            "participant_sources",
            "participant_entities",
        ):
            c.execute(f"DROP TABLE {name}")
        c.execute("UPDATE schema_meta SET value='103' WHERE key='schema_version'")
        c.execute(
            "UPDATE schema_meta SET value=? WHERE key='schema_ddl_contract'",
            ("103:" + _SCHEMA_DDL_FINGERPRINTS[103],),
        )
    db = Database(db.path, strict_schema_contract=True)
    IdentityStore(db).ensure("subject-a", "a" * 64)
    with db.transaction() as c:
        who = ParticipantStore.source(
            c, "subject-a", "account", "local", "account-a", entity_type="human"
        )
        c.execute(
            "INSERT INTO participant_accounts VALUES(?,?,?,?,?)",
            ("account-a", "subject-a", who.entity_id, "alice", "2026-10-23T00:00:00Z"),
        )
        c.execute(
            "INSERT INTO participant_credentials VALUES(?,?,?,?,?)",
            (
                "account-a",
                "subject-a",
                "PASSWORD-HASH-SECRET",
                "RECOVERY-HASH-SECRET",
                "2026-10-23T00:00:00Z",
            ),
        )
    artifact = RuntimeLogExporter(db).export("subject-a", actor="test")
    with zipfile.ZipFile(BytesIO(artifact.content)) as archive:
        assert "tables/participant_entities.jsonl" in archive.namelist()
        assert "tables/participant_credentials.jsonl" not in archive.namelist()
        assert b"PASSWORD-HASH-SECRET" not in b"".join(archive.read(n) for n in archive.namelist())
        manifest = json.loads(archive.read("manifest.json"))
        assert manifest["schema_version"] == 104
