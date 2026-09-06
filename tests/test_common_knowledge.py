from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from noyra.core import SubjectKernel
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.knowledge import CommonKnowledgeProposal, CommonKnowledgeStore
from noyra.knowledge import common as common_module


def make_store(root: Path, subject_id: str) -> tuple[SubjectKernel, CommonKnowledgeStore]:
    kernel = SubjectKernel(
        root / "noyra.sqlite3",
        subject_id,
        content_hash({"seed": subject_id}),
    )
    return kernel, CommonKnowledgeStore(kernel.database, subject_id, root / "secrets")


def test_private_key_is_written_without_windows_newline_translation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw_key = bytes(range(31)) + b"\n"

    class FixedPrivateKeyFactory:
        @staticmethod
        def generate() -> Ed25519PrivateKey:
            return Ed25519PrivateKey.from_private_bytes(raw_key)

        @staticmethod
        def from_private_bytes(raw: bytes) -> Ed25519PrivateKey:
            return Ed25519PrivateKey.from_private_bytes(raw)

    monkeypatch.setattr(common_module, "Ed25519PrivateKey", FixedPrivateKeyFactory)
    kernel, _store = make_store(tmp_path, "Noyra-knowledge-binary-key")
    try:
        key_path = tmp_path / "secrets" / "common-knowledge-ed25519.key"
        assert key_path.read_bytes() == raw_key
        CommonKnowledgeStore(kernel.database, kernel.subject_id, tmp_path / "secrets")
    finally:
        kernel.close()


def test_signed_common_knowledge_is_quarantined_before_subject_acceptance() -> None:
    with (
        tempfile.TemporaryDirectory() as publisher_dir,
        tempfile.TemporaryDirectory() as reader_dir,
    ):
        _, publisher = make_store(Path(publisher_dir), "Noyra-knowledge-publisher")
        reader_kernel, reader = make_store(Path(reader_dir), "Noyra-knowledge-reader")
        with reader_kernel.database.connection() as connection:
            identity_before = dict(
                connection.execute(
                    "SELECT personal_name, identity_status FROM subject_identity "
                    "WHERE subject_id = ?",
                    (reader_kernel.subject_id,),
                ).fetchone()
            )
        package = publisher.publish(
            CommonKnowledgeProposal(
                scope="pitfall",
                title="Avoid retrying ambiguous deliveries",
                summary="An unknown result can already have reached the remote service.",
                payload={
                    "symptom": "network response was lost",
                    "cause": "the provider outcome cannot be proven",
                    "avoidance": "quarantine and reconcile before retry",
                    "environment": "external messaging",
                    "tags": ["idempotency", "delivery"],
                },
            )
        )
        envelope = publisher.export_package(package.package_id)
        reader.trust_key(publisher.public_key, label="test publisher", actor="operator")
        imported = reader.import_package(envelope)
        assert imported.package_id == package.package_id
        assert reader.usable() == []
        assert reader.review_queue(limit=1)[0]["import_status"] == "quarantined"
        reader.accept(
            package.package_id,
            reason="validated against local delivery evidence",
            actor="subject",
        )
        assert reader.usable(scope="pitfall")[0].title == package.title
        with reader_kernel.database.connection() as connection:
            identity = connection.execute(
                "SELECT personal_name, identity_status FROM subject_identity WHERE subject_id = ?",
                (reader_kernel.subject_id,),
            ).fetchone()
        assert dict(identity) == identity_before
        publisher.revoke(package.package_id, reason="superseded by a newer procedure")
        assert publisher.get(package.package_id).status == "revoked"


def test_common_knowledge_rejects_private_state_and_tampered_signatures() -> None:
    with (
        tempfile.TemporaryDirectory() as publisher_dir,
        tempfile.TemporaryDirectory() as reader_dir,
    ):
        _, publisher = make_store(Path(publisher_dir), "Noyra-knowledge-private")
        _, reader = make_store(Path(reader_dir), "Noyra-knowledge-verifier")
        with pytest.raises(ValueError):
            CommonKnowledgeProposal(
                scope="skill",
                title="Forbidden state",
                summary="This tries to export identity state.",
                payload={"steps": ["overwrite personality goal"]},
            )
        package = publisher.publish(
            CommonKnowledgeProposal(
                scope="protocol",
                title="Bounded retry protocol",
                summary="A provider-neutral reliability procedure.",
                payload={
                    "procedure": ["classify outcome", "reconcile unknown result"],
                    "compatibility": ["http"],
                    "validation": ["one durable delivery record"],
                    "tags": ["reliability"],
                },
            )
        )
        envelope = publisher.export_package(package.package_id)
        reader.trust_key(publisher.public_key, label="test publisher", actor="operator")
        envelope["summary"] = "tampered"
        with pytest.raises(IntegrityError):
            reader.import_package(envelope)
