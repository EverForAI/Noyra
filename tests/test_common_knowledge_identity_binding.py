from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import Database, IdentityStore, IntegrityRegistry, SubjectKernel
from noyra.core import database as database_module
from noyra.core.schema.contracts import _SCHEMA_DDL_FINGERPRINTS
from noyra.core.types import content_hash, utc_now
from noyra.knowledge import CommonKnowledgeProposal, CommonKnowledgeStore


@pytest.mark.parametrize("revoked", [False, True])
def test_schema_118_backfills_only_existing_explicit_peer_bindings(
    tmp_path: Path, revoked: bool
) -> None:
    path = tmp_path / "schema117.sqlite3"
    subject_id = "Noyra-binding-migration-subject"
    publisher_id = "Noyra-binding-migration-publisher"
    private_key = Ed25519PrivateKey.generate()
    public_bytes = private_key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    public_key = base64.urlsafe_b64encode(public_bytes).decode("ascii")
    key_id = content_hash(public_key)[:32]
    unbound_private_key = Ed25519PrivateKey.generate()
    unbound_public_key = base64.urlsafe_b64encode(
        unbound_private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    unbound_key_id = content_hash(unbound_public_key)[:32]
    now = utc_now()

    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 117):
        database = Database(path, strict_schema_contract=True)
        IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
        with database.transaction() as connection:
            connection.executemany(
                "INSERT INTO common_knowledge_trusted_keys("
                "key_id, public_key, label, status, created_at, revoked_at) "
                "VALUES (?, ?, ?, 'active', ?, NULL)",
                [
                    (key_id, public_key, "peer publisher", now),
                    (unbound_key_id, unbound_public_key, "legacy direct trust", now),
                ],
            )
            peer_values = {
                "peer_id": "knowledge-peer-migration-fixture",
                "subject_id": subject_id,
                "label": "explicit peer",
                "endpoint": "https://publisher.example/knowledge",
                "publisher_subject_id": publisher_id,
                "key_id": key_id,
                "public_key": public_key,
                "status": "active",
                "cursor": 0,
                "remote_etag": None,
                "sync_interval_seconds": 300,
                "next_sync_at": now,
                "last_sync_at": None,
                "last_error_code": None,
                "created_at": now,
                "updated_at": now,
            }
            connection.execute(
                "INSERT INTO common_knowledge_peers("
                "peer_id, subject_id, label, endpoint, publisher_subject_id, key_id, "
                "public_key, status, cursor, remote_etag, sync_interval_seconds, next_sync_at, "
                "last_sync_at, last_error_code, state_hash, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    peer_values["peer_id"],
                    peer_values["subject_id"],
                    peer_values["label"],
                    peer_values["endpoint"],
                    peer_values["publisher_subject_id"],
                    peer_values["key_id"],
                    peer_values["public_key"],
                    peer_values["status"],
                    peer_values["cursor"],
                    peer_values["remote_etag"],
                    peer_values["sync_interval_seconds"],
                    peer_values["next_sync_at"],
                    peer_values["last_sync_at"],
                    peer_values["last_error_code"],
                    content_hash(CommonKnowledgeStore._peer_state_values(peer_values)),
                    peer_values["created_at"],
                    peer_values["updated_at"],
                ),
            )
            if revoked:
                connection.execute(
                    "UPDATE common_knowledge_trusted_keys SET status='revoked', revoked_at=? "
                    "WHERE key_id=?",
                    (now, key_id),
                )

    upgraded = Database(path, strict_schema_contract=True)
    with upgraded.read_transaction() as connection:
        version = connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()[0]
        bindings = {
            (row["key_id"], row["publisher_subject_id"], row["status"])
            for row in connection.execute(
                "SELECT key_id, publisher_subject_id, status "
                "FROM common_knowledge_trusted_publishers WHERE subject_id=?",
                (subject_id,),
            )
        }
    assert version == "118"
    assert bindings == {(key_id, publisher_id, "revoked" if revoked else "active")}
    Database(path, strict_schema_contract=True)


def test_interrupted_binding_migration_restores_schema117_then_retries(tmp_path: Path) -> None:
    path = tmp_path / "interrupted.sqlite3"
    with patch.object(database_module, "CURRENT_SCHEMA_VERSION", 117):
        database = Database(path, strict_schema_contract=True)
    subject = "Noyra-migration-rollback"
    IdentityStore(database).ensure(subject, content_hash("migration-rollback"))
    execute = Database._execute_sql_script

    def interrupted(connection: sqlite3.Connection, script: str) -> None:
        execute(connection, script)
        if script == database_module.MIGRATIONS[118]:
            raise OSError("synthetic binding migration interruption")

    with (
        patch.object(Database, "_execute_sql_script", staticmethod(interrupted)),
        pytest.raises(OSError, match="binding migration interruption"),
    ):
        Database(path, strict_schema_contract=True)
    with database.read_transaction() as connection:
        assert Database.schema_ddl_fingerprint(connection) == _SCHEMA_DDL_FINGERPRINTS[117]
        assert (
            connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()[0]
            == "117"
        )
        assert (
            connection.execute("SELECT subject_id FROM subject_identity").fetchone()[0] == subject
        )
    Database(path, strict_schema_contract=True)


def test_trust_key_rejects_binding_an_external_key_to_the_local_subject(
    tmp_path: Path,
) -> None:
    subject_id = "Noyra-local-binding-subject"
    kernel = SubjectKernel(
        tmp_path / "noyra.sqlite3",
        subject_id,
        content_hash({"subject": subject_id}),
    )
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.urlsafe_b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw,
            serialization.PublicFormat.Raw,
        )
    ).decode("ascii")
    store = CommonKnowledgeStore(kernel.database, subject_id, tmp_path / "secrets")
    try:
        with pytest.raises(PermissionError, match="cannot trust an external key"):
            store.trust_key(
                public_key,
                publisher_subject_id=subject_id,
                label="self impersonation",
                actor="operator",
            )
        with kernel.database.read_transaction() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM common_knowledge_trusted_publishers"
                ).fetchone()[0]
                == 0
            )
    finally:
        kernel.close()


def test_publisher_binding_identity_and_revocation_state_are_guarded(tmp_path: Path) -> None:
    database = Database(tmp_path / "binding-guards.sqlite3")
    subject = "Reader-Guards"
    IdentityStore(database).ensure(subject, content_hash(subject))
    store = CommonKnowledgeStore(database, subject, tmp_path / "secrets")
    public_key = base64.urlsafe_b64encode(
        Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    ).decode("ascii")
    key_id = store.trust_key(
        public_key, publisher_subject_id="Publisher-Guards", label="guards", actor="operator"
    )
    with database.transaction() as connection:
        with pytest.raises(sqlite3.IntegrityError, match="transition is invalid"):
            connection.execute(
                "UPDATE common_knowledge_trusted_publishers SET publisher_subject_id=?",
                (subject,),
            )
        with pytest.raises(sqlite3.IntegrityError, match="cannot trust an external key"):
            connection.execute(
                "INSERT INTO common_knowledge_trusted_publishers(subject_id, "
                "publisher_subject_id, key_id, status, created_at) VALUES(?,?,?,'active',?)",
                (subject, subject, key_id, utc_now()),
            )
        with pytest.raises(sqlite3.IntegrityError, match="transition is invalid"):
            connection.execute("UPDATE common_knowledge_trusted_publishers SET status='revoked'")
    # Repeated trust is idempotent; revoke and explicit re-trust are valid transitions.
    store.trust_key(
        public_key, publisher_subject_id="Publisher-Guards", label="guards", actor="operator"
    )
    store.revoke_trusted_key(key_id, reason="withdraw", actor="operator")
    store.trust_key(
        public_key, publisher_subject_id="Publisher-Guards", label="guards", actor="operator"
    )


def test_legacy_accepted_import_without_binding_is_preserved_but_not_usable(
    tmp_path: Path,
) -> None:
    publisher_root = tmp_path / "publisher"
    reader_root = tmp_path / "reader"
    publisher_root.mkdir()
    reader_root.mkdir()
    publisher_kernel = SubjectKernel(
        publisher_root / "noyra.sqlite3",
        "Noyra-legacy-binding-publisher",
        content_hash("legacy-binding-publisher"),
    )
    reader_id = "Noyra-legacy-binding-reader"
    reader_kernel = SubjectKernel(
        reader_root / "noyra.sqlite3",
        reader_id,
        content_hash("legacy-binding-reader"),
    )
    publisher = CommonKnowledgeStore(
        publisher_kernel.database, publisher_kernel.subject_id, publisher_root / "secrets"
    )
    reader = CommonKnowledgeStore(reader_kernel.database, reader_id, reader_root / "secrets")
    try:
        package = publisher.publish(
            CommonKnowledgeProposal(
                scope="protocol",
                title="Legacy accepted advisory",
                summary="An old key-only trust record must not confer publisher authority.",
                payload={
                    "procedure": ["retain the original signed evidence"],
                    "compatibility": ["noyra/v1"],
                    "validation": ["require explicit publisher binding"],
                    "tags": ["migration"],
                },
            )
        )
        envelope = publisher.export_package(package.package_id)
        reader.trust_key(
            publisher.public_key,
            publisher_subject_id=publisher.subject_id,
            label="migration fixture",
            actor="operator",
        )
        reader.import_package(envelope)
        reader.accept(package.package_id, reason="accepted before the binding was removed")

        with reader_kernel.database.transaction() as connection:
            connection.execute(
                "DELETE FROM common_knowledge_trusted_publishers "
                "WHERE subject_id=? AND publisher_subject_id=? AND key_id=?",
                (reader_id, publisher.subject_id, envelope["key_id"]),
            )

        assert reader.usable() == []
        pending = reader.review_queue()
        assert len(pending) == 1
        assert pending[0]["publisher_binding_required"] is True
        assert "payload" not in pending[0]
        assert pending[0]["publisher_subject_id"] == publisher.subject_id
        report = IntegrityRegistry().run(
            reader_kernel.database,
            reader_id,
            reader_root,
            profile="manual",
            policy_mode="alert",
            deadline_seconds=10,
            check_ids=("knowledge.common",),
        )
        check = next(item for item in report.checks if item.id == "knowledge.common")
        assert report.p0 == () and report.p1 == ()
        assert check.details["unbound_accepted_imports"] == 1
        reader.trust_key(
            publisher.public_key,
            publisher_subject_id=publisher.subject_id,
            label="explicit legacy reauthorization",
            actor="operator",
        )
        assert [record.package_id for record in reader.usable()] == [package.package_id]
    finally:
        reader_kernel.close()
        publisher_kernel.close()


def test_retrust_cannot_restore_other_publishers_or_other_subject_bindings(
    tmp_path: Path,
) -> None:
    database = Database(tmp_path / "shared.sqlite3")
    subjects = ("Reader-One", "Reader-Two", "Publisher-One")
    for subject in subjects:
        IdentityStore(database).ensure(subject, content_hash(subject))
    first, second, publisher = (
        CommonKnowledgeStore(database, subject, tmp_path / subject / "secrets")
        for subject in subjects
    )
    package = publisher.publish(
        CommonKnowledgeProposal(
            scope="protocol",
            title="Scoped revocation example",
            summary="Reauthorizing one source preserves other revocations.",
            payload={"procedure": ["check the configured source"], "tags": ["trust"]},
        )
    )
    envelope = publisher.export_package(package.package_id)
    for reader in (first, second):
        reader.trust_key(
            publisher.public_key,
            publisher_subject_id=publisher.subject_id,
            label="original publisher",
            actor="operator",
        )
        reader.import_package(envelope)
    first.revoke_trusted_key(publisher.key_id, reason="withdraw trust", actor="operator")
    first.trust_key(
        publisher.public_key,
        publisher_subject_id="Publisher-Two",
        label="different publisher",
        actor="operator",
    )
    # The public key row is active again, but neither original binding is active.
    for reader in (first, second):
        with pytest.raises(PermissionError, match="not trusted"):
            reader.import_package(envelope)
    first.trust_key(
        publisher.public_key,
        publisher_subject_id=publisher.subject_id,
        label="original publisher",
        actor="operator",
    )
    first.import_package(envelope)
    with pytest.raises(PermissionError, match="not trusted"):
        second.import_package(envelope)


def test_local_feed_rejects_historical_external_key_claim_before_backfill(
    tmp_path: Path,
) -> None:
    from noyra.core.errors import IntegrityError
    from noyra.core.types import canonical_json

    database = Database(tmp_path / "feed.sqlite3")
    subject = "Reader-Feed"
    IdentityStore(database).ensure(subject, content_hash(subject))
    reader = CommonKnowledgeStore(database, subject, tmp_path / "secrets")
    private_key = Ed25519PrivateKey.generate()
    public_key = base64.urlsafe_b64encode(
        private_key.public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw
        )
    ).decode("ascii")
    payload = {"procedure": ["retain evidence"], "tags": ["trust"]}
    body = {
        "package_id": "knowledge-historical-forgery",
        "publisher_subject_id": subject,
        "scope": "protocol",
        "title": "Historical forged source",
        "summary": "This is synthetic legacy evidence.",
        "payload": payload,
        "payload_hash": content_hash(payload),
        "key_id": content_hash(public_key)[:32],
        "version": 1,
        "created_at": utc_now(),
    }
    signature = base64.urlsafe_b64encode(
        private_key.sign(canonical_json(body).encode("utf-8"))
    ).decode("ascii")
    with database.transaction() as connection:
        connection.execute(
            "INSERT INTO common_knowledge_packages(package_id, publisher_subject_id, scope, "
            "title, summary, payload_json, payload_hash, signature, key_id, version, status, "
            "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'published', ?)",
            (
                body["package_id"],
                subject,
                body["scope"],
                body["title"],
                body["summary"],
                canonical_json(payload),
                body["payload_hash"],
                signature,
                body["key_id"],
                1,
                body["created_at"],
            ),
        )
    with pytest.raises(IntegrityError, match="publisher identity"):
        reader.discovery_document()
    with database.read_transaction() as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM common_knowledge_versions").fetchone()[0] == 0
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM common_knowledge_sync_events").fetchone()[0]
            == 0
        )
