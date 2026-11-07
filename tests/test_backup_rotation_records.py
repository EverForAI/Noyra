from __future__ import annotations

import base64
import json
from typing import Any

import pytest

from noyra.core.backup_rotation_records import (
    MAX_INTENT_BYTES,
    RotationAcceptance,
    RotationHeader,
    RotationIntent,
    RotationJournalError,
    encode,
    sha256,
)


def snapshot(generation: int = 1) -> bytes:
    keys = [
        {
            "key_id": f"synthetic-{i}",
            "status": "active" if i == generation else "retired",
            "key_b64": base64.urlsafe_b64encode(bytes([i]) * 32).decode(),
        }
        for i in range(1, generation + 1)
    ]
    return encode(
        {
            "format": "noyra-backup-keyring/v1",
            "generation": generation,
            "active_key_id": keys[-1]["key_id"],
            "keys": keys,
        }
    )


def intent() -> RotationIntent:
    header = RotationHeader("b" * 32, "c" * 64)
    source, target = snapshot(), snapshot(2)
    return RotationIntent(
        "a" * 32,
        header.slot_id,
        sha256(header.to_bytes()),
        1,
        sha256(source),
        sha256(target),
        source,
        target,
    )


def test_strict_records_round_trip_and_secret_free_repr() -> None:
    item = intent()
    assert RotationIntent.from_bytes(item.to_bytes()) == item
    header = RotationHeader("b" * 32, "c" * 64)
    assert RotationHeader.from_bytes(header.to_bytes()) == header
    receipt = RotationAcceptance(sha256(item.to_bytes()), item.target_hash, item.request_id)
    assert RotationAcceptance.from_bytes(receipt.to_bytes()) == receipt
    text = repr(item) + json.dumps(item.metadata())
    assert "source_b64" not in text and "key_b64" not in text
    assert base64.urlsafe_b64encode(b"\x01" * 32).decode() not in text


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "unknown"),
        ("request_id", "../escape"),
        ("slot_id", "A" * 32),
        ("header_hash", "not-a-hash"),
        ("expected_generation", True),
        ("expected_generation", 0),
        ("target_generation", True),
        ("target_generation", 3),
        ("source_hash", "0" * 64),
        ("target_hash", "0" * 64),
        ("source_b64", "bad%%"),
        ("target_b64", {}),
        ("unexpected", "extra"),
    ],
)
def test_bad_intent_fields_fail_closed(field: str, value: Any) -> None:
    item = json.loads(intent().to_bytes())
    item[field] = value
    with pytest.raises(RotationJournalError):
        RotationIntent.from_bytes(encode(item))


@pytest.mark.parametrize(
    "fault", ["drop-old", "old-secret", "old-status", "same-active", "two-new"]
)
def test_hash_consistent_but_invalid_rotation_is_rejected(fault: str) -> None:
    item = json.loads(intent().to_bytes())
    target = json.loads(base64.b64decode(item["target_b64"]))
    if fault == "drop-old":
        target["keys"].pop(0)
    if fault == "old-secret":
        target["keys"][0]["key_b64"] = base64.urlsafe_b64encode(b"z" * 32).decode()
    if fault == "old-status":
        target["keys"][0]["status"] = "active"
    if fault == "same-active":
        target["active_key_id"] = "synthetic-1"
    if fault == "two-new":
        target["keys"].append(
            {
                "key_id": "extra",
                "status": "retired",
                "key_b64": base64.urlsafe_b64encode(b"z" * 32).decode(),
            }
        )
    raw = encode(target)
    item["target_hash"], item["target_b64"] = sha256(raw), base64.b64encode(raw).decode()
    with pytest.raises(RotationJournalError):
        RotationIntent.from_bytes(encode(item))


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"{}", b"null", b"\xff"])
def test_malformed_records_reject_without_echoing_content(raw: bytes) -> None:
    for parser in [
        RotationHeader.from_bytes,
        RotationIntent.from_bytes,
        RotationAcceptance.from_bytes,
    ]:
        with pytest.raises(RotationJournalError):
            parser(raw)


def test_duplicate_noncanonical_and_over_budget_records_are_rejected() -> None:
    raw = RotationHeader("a" * 32, "b" * 64).to_bytes()
    duplicate = raw[:-1] + b',"slot_id":"' + b"a" * 32 + b'"}'
    for bad in [duplicate, raw + b" ", b"x" * (MAX_INTENT_BYTES + 1)]:
        with pytest.raises(RotationJournalError):
            RotationHeader.from_bytes(bad)


@pytest.mark.parametrize(
    "field,value",
    [
        ("format", "wrong"),
        ("profile", "pretend-sync"),
        ("intent_hash", "bad"),
        ("request_id", "bad"),
    ],
)
def test_acceptance_cannot_invent_a_durability_profile(field: str, value: str) -> None:
    item = intent()
    receipt = json.loads(
        RotationAcceptance(sha256(item.to_bytes()), item.target_hash, item.request_id).to_bytes()
    )
    receipt[field] = value
    with pytest.raises(RotationJournalError):
        RotationAcceptance.from_bytes(encode(receipt))
