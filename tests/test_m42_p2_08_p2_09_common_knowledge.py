from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path
from typing import Any, cast

import pytest

from noyra.cognition import CognitionSettings
from noyra.cognition.thought import IntrinsicThought, ThoughtAgendaRecord
from noyra.core import RuntimeLogExporter, SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import canonical_json, content_hash, strict_json_loads
from noyra.knowledge import CommonKnowledgeProposal, CommonKnowledgeStore

SIGNED_FIELDS = (
    "package_id",
    "publisher_subject_id",
    "scope",
    "title",
    "summary",
    "payload",
    "payload_hash",
    "key_id",
    "version",
    "created_at",
)


def make_store(root: Path, subject_id: str) -> tuple[SubjectKernel, CommonKnowledgeStore]:
    kernel = SubjectKernel(
        root / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    return kernel, CommonKnowledgeStore(kernel.database, subject_id, root / f"secrets-{subject_id}")


def publish_protocol(
    store: CommonKnowledgeStore, *, title: str, compatibility: str
) -> dict[str, Any]:
    package = store.publish(
        CommonKnowledgeProposal(
            scope="protocol",
            title=title,
            summary="A bounded provider-neutral procedure.",
            payload={
                "procedure": ["classify outcome", "verify durable evidence"],
                "compatibility": [compatibility],
                "validation": ["one durable record"],
                "tags": ["reliability"],
            },
        )
    )
    return store.export_package(package.package_id)


def test_package_id_collision_is_rejected_before_second_subject_import_link() -> None:
    with (
        tempfile.TemporaryDirectory() as publisher_one_dir,
        tempfile.TemporaryDirectory() as publisher_two_dir,
        tempfile.TemporaryDirectory() as reader_dir,
    ):
        publisher_one_kernel, publisher_one = make_store(
            Path(publisher_one_dir), "Noyra-collision-publisher-one"
        )
        publisher_two_kernel, publisher_two = make_store(
            Path(publisher_two_dir), "Noyra-collision-publisher-two"
        )
        reader_one_kernel, reader_one = make_store(Path(reader_dir), "Noyra-collision-reader-one")
        reader_two_kernel, reader_two = make_store(Path(reader_dir), "Noyra-collision-reader-two")
        try:
            original = publish_protocol(
                publisher_one, title="Original bounded protocol", compatibility="noyra/v1"
            )
            reader_one.trust_key(publisher_one.public_key, label="publisher one", actor="operator")
            reader_one.import_package(original)

            collision = publish_protocol(
                publisher_two, title="Different signed content", compatibility="noyra/v1"
            )
            collision["package_id"] = original["package_id"]
            collision_body = {field: collision[field] for field in SIGNED_FIELDS}
            collision["signature"] = publisher_two._sign(collision_body)
            reader_two.trust_key(publisher_two.public_key, label="publisher two", actor="operator")

            with pytest.raises(IntegrityError, match="package id collision"):
                reader_two.import_package(collision)
            with reader_two_kernel.database.connection() as connection:
                imports = connection.execute(
                    "SELECT COUNT(*) FROM common_knowledge_imports WHERE subject_id = ?",
                    (reader_two_kernel.subject_id,),
                ).fetchone()[0]
                provenance = connection.execute(
                    "SELECT COUNT(*) FROM common_knowledge_import_provenance WHERE subject_id = ?",
                    (reader_two_kernel.subject_id,),
                ).fetchone()[0]
            assert imports == 0
            assert provenance == 0
        finally:
            reader_two_kernel.close()
            reader_one_kernel.close()
            publisher_two_kernel.close()
            publisher_one_kernel.close()


def test_import_provenance_binds_exact_envelope_and_is_runtime_exported() -> None:
    with (
        tempfile.TemporaryDirectory() as publisher_dir,
        tempfile.TemporaryDirectory() as reader_dir,
    ):
        publisher_kernel, publisher = make_store(Path(publisher_dir), "Noyra-provenance-publisher")
        reader_kernel, reader = make_store(Path(reader_dir), "Noyra-provenance-reader")
        try:
            envelope = publish_protocol(
                publisher, title="Exact provenance protocol", compatibility="noyra/v1"
            )
            assert set(envelope) == {*SIGNED_FIELDS, "signature"}
            reader.trust_key(publisher.public_key, label="publisher", actor="operator")
            reader.import_package(envelope)
            with reader_kernel.database.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM common_knowledge_import_provenance WHERE subject_id = ?",
                    (reader_kernel.subject_id,),
                ).fetchone()
            verified_envelope = {field: envelope[field] for field in SIGNED_FIELDS}
            verified_envelope["signature"] = envelope["signature"]
            assert row["envelope_json"] == canonical_json(verified_envelope)
            assert row["envelope_hash"] == content_hash(verified_envelope)
            assert row["payload_hash"] == envelope["payload_hash"]
            assert row["signer_key_id"] == envelope["key_id"]

            artifact = RuntimeLogExporter(reader_kernel.database).export(
                reader_kernel.subject_id, actor="test-operator"
            )
            with zipfile.ZipFile(io.BytesIO(artifact.content)) as archive:
                exported = archive.read("tables/common_knowledge_import_provenance.jsonl").decode(
                    "utf-8"
                )
            assert json.loads(exported.splitlines()[0])["envelope_hash"] == content_hash(
                verified_envelope
            )

            with (
                pytest.raises(sqlite3.IntegrityError, match="append-only"),
                reader_kernel.database.transaction() as connection,
            ):
                connection.execute(
                    "UPDATE common_knowledge_import_provenance SET envelope_hash = 'tampered' "
                    "WHERE provenance_id = ?",
                    (row["provenance_id"],),
                )
        finally:
            reader_kernel.close()
            publisher_kernel.close()


def test_usable_revalidates_signature_trust_and_compatibility_on_every_read() -> None:
    with (
        tempfile.TemporaryDirectory() as publisher_dir,
        tempfile.TemporaryDirectory() as reader_dir,
    ):
        publisher_kernel, publisher = make_store(Path(publisher_dir), "Noyra-read-publisher")
        reader_kernel, reader = make_store(Path(reader_dir), "Noyra-read-reader")
        try:
            envelope = publish_protocol(
                publisher, title="Read-time validation protocol", compatibility="noyra/v1"
            )
            reader.trust_key(publisher.public_key, label="publisher", actor="operator")
            reader.import_package(envelope)
            reader.accept(envelope["package_id"], reason="bounded and compatible")
            assert len(reader.usable()) == 1

            reader.compatibility = frozenset({"other/runtime"})
            assert reader.usable() == []
            reader.compatibility = frozenset({"noyra/v1"})
            assert len(reader.usable()) == 1

            with reader_kernel.database.transaction() as connection:
                connection.execute(
                    "UPDATE common_knowledge_trusted_keys SET status = 'revoked', revoked_at = ? "
                    "WHERE key_id = ?",
                    ("2026-08-17T00:00:00+00:00", envelope["key_id"]),
                )
            assert reader.usable() == []
            reader.trust_key(publisher.public_key, label="publisher", actor="operator")

            with reader_kernel.database.transaction() as connection:
                connection.execute(
                    "UPDATE common_knowledge_packages SET summary = 'tampered metadata' "
                    "WHERE package_id = ?",
                    (envelope["package_id"],),
                )
            with pytest.raises(IntegrityError, match="signature"):
                reader.usable()
        finally:
            reader_kernel.close()
            publisher_kernel.close()


def test_cognition_receives_only_non_private_advisory_context() -> None:
    with (
        tempfile.TemporaryDirectory() as publisher_dir,
        tempfile.TemporaryDirectory() as reader_dir,
    ):
        publisher_kernel, publisher = make_store(Path(publisher_dir), "Noyra-advisory-publisher")
        reader_kernel, reader = make_store(Path(reader_dir), "Noyra-advisory-reader")
        try:
            envelope = publish_protocol(
                publisher, title="Advisory protocol", compatibility="noyra/v1"
            )
            reader.trust_key(publisher.public_key, label="publisher", actor="operator")
            reader.import_package(envelope)
            reader.accept(envelope["package_id"], reason="safe advisory")
            thought = IntrinsicThought(
                reader_kernel.database,
                reader_kernel.subject_id,
                cast(Any, object()),
                CognitionSettings(),
                advisory_provider=reader.advisory_context,
                clock=lambda: "2026-08-17T00:00:00+00:00",
            )
            agenda = ThoughtAgendaRecord(
                agenda_id="agenda-common-knowledge",
                subject_id=reader_kernel.subject_id,
                source_type="subject",
                source_id=reader_kernel.subject_id,
                topic="Review bounded operational guidance",
                urgency=0.5,
                novelty=0.5,
                emotional_weight=0.0,
                recurrence_count=1,
                consecutive_no_change=0,
                status="open",
                cooldown_until=None,
                created_at="2026-08-17T00:00:00+00:00",
                updated_at="2026-08-17T00:00:00+00:00",
            )
            context = thought._context(agenda)
            payload = strict_json_loads(context.serialized)
            assert isinstance(payload, dict)
            advisory = payload["common_knowledge_advisories"][0]
            assert advisory["boundary"] == "advisory_non_private"
            assert advisory["package_id"] == envelope["package_id"]
            assert "publisher_subject_id" not in advisory
            assert "signature" not in advisory
            assert "subject_id" not in canonical_json(advisory)
        finally:
            reader_kernel.close()
            publisher_kernel.close()
