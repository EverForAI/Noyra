from pathlib import Path

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import PayloadLimitError
from noyra.core.integrity import IntegrityAuditLimits, IntegrityRegistry
from noyra.core.payload_codec import compress_text, decompress_text, payload_read_budget
from noyra.core.types import canonical_json, content_hash
from noyra.model.ledger import ModelLedger


def test_shared_codec_has_no_unbounded_escape() -> None:
    encoded = compress_text("x" * 100_000)
    with pytest.raises(PayloadLimitError):
        decompress_text(encoded, max_bytes=4096)
    with pytest.raises(ValueError):
        decompress_text(encoded, max_bytes=None)  # type: ignore[arg-type]
    consumed: list[int] = []
    with payload_read_budget(4096, consumed.append), pytest.raises(PayloadLimitError):
        decompress_text(encoded)
    assert consumed == []
    assert decompress_text(encoded) == "x" * 100_000


def test_nested_consumers_charge_expanded_bytes_and_reset_context() -> None:
    consumed: list[int] = []
    payload = "x" * 3000
    with payload_read_budget(4096, consumed.append):
        assert decompress_text(compress_text(payload)) == payload
    assert consumed == [3000]
    assert decompress_text(compress_text("x" * 5000)) == "x" * 5000
    assert consumed == [3000]


def test_model_integrity_rejects_hash_consistent_expansion(tmp_path: Path) -> None:
    subject = "Noyra-decompression-audit"
    db = Database(tmp_path / "noyra.sqlite3")
    IdentityStore(db).ensure(subject, content_hash({"subject": subject}))
    request = {"prompt": "x" * 2_000_000}
    call, _ = ModelLedger(db).prepare_call(
        subject,
        "provider",
        "model",
        "test",
        content_hash(request),
        "compressed-audit",
        request=request,
    )
    with db.transaction() as connection:
        connection.execute(
            "UPDATE model_calls SET request_json=? WHERE call_id=?",
            (compress_text(canonical_json(request)), call.call_id),
        )
    report = IntegrityRegistry().run(
        db,
        subject,
        tmp_path,
        profile="periodic_deep",
        policy_mode="alert",
        deadline_seconds=10,
        limits=IntegrityAuditLimits(
            max_rows_per_check=1000, max_bytes_per_check=4096, max_value_bytes=4096
        ),
        check_ids=("model.ledger",),
    )
    assert report.status != "ok"
    assert report.p1 == ("model.ledger:resource_limit",)
