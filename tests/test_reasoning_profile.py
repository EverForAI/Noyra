from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from noyra.core import Database, IdentityStore
from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.openai_compatible import OpenAICompatibleProvider
from noyra.model.reasoning import (
    normalize_reasoning_effort,
    reasoning_profile,
    reasoning_profile_payload,
)
from noyra.model.types import CompletionRequest, ModelMessage


def test_known_reasoning_models_expose_bounded_efforts() -> None:
    profile = reasoning_profile("gpt-5-mini")
    assert profile.supported is True
    assert profile.efforts == ("low", "medium", "high", "xhigh")
    assert profile.parameter == "reasoning_effort"
    assert normalize_reasoning_effort("gpt-5-mini", "high") == "high"


def test_unknown_models_fail_closed_without_reasoning() -> None:
    profile = reasoning_profile("deepseek-chat")
    assert profile.supported is False
    assert normalize_reasoning_effort("deepseek-chat", "high") is None


def test_known_model_rejects_unknown_effort() -> None:
    with pytest.raises(ValueError, match="not supported"):
        normalize_reasoning_effort("o3-mini", "extreme")


def test_public_profile_is_server_owned_and_clears_unsupported_selection() -> None:
    supported = reasoning_profile_payload("gpt-5-mini", selected="high")
    unsupported = reasoning_profile_payload("deepseek-chat", selected="high")
    assert supported["supported"] is True
    assert supported["selected"] == "high"
    assert isinstance(supported["revision"], str)
    assert unsupported["supported"] is False
    assert unsupported["efforts"] == []
    assert unsupported["selected"] is None


def test_capability_observations_are_subject_endpoint_and_model_scoped(tmp_path: Any) -> None:
    database = Database(tmp_path / "capabilities.sqlite3")
    IdentityStore(database).ensure("Noyra-capabilities", content_hash("capabilities"))
    store = CapabilityProfileStore(database)
    first = store.record_probe_success(
        "Noyra-capabilities",
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )
    assert first is not None
    assert first.status == "known"
    assert first.tested_efforts == ("high",)
    assert first.efforts == ("low", "medium", "high", "xhigh")
    assert (
        store.get("another-subject", "openai_compatible", "https://models.example/v1", "gpt-5-mini")
        is None
    )
    assert (
        store.get(
            "Noyra-capabilities", "openai_compatible", "https://other.example/v1", "gpt-5-mini"
        )
        is None
    )
    assert (
        store.get(
            "Noyra-capabilities", "openai_compatible", "https://models.example/v1", "other-model"
        )
        is None
    )


def test_successful_probes_accumulate_only_current_observations(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-revisions.sqlite3")
    subject_id = "Noyra-capability-revisions"
    IdentityStore(database).ensure(subject_id, content_hash("revision"))
    store = CapabilityProfileStore(database)
    first = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "low",
    )
    second = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )

    assert first is not None and second is not None
    assert first.revision == 1
    assert second.revision == 2
    assert second.tested_efforts == ("low", "high")


def test_unsupported_model_probe_cannot_persist_a_capability(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-unsupported.sqlite3")
    subject_id = "Noyra-capability-unsupported"
    IdentityStore(database).ensure(subject_id, content_hash("unsupported"))
    store = CapabilityProfileStore(database)

    with pytest.raises(ValueError, match="not a candidate"):
        store.record_probe_success(
            subject_id,
            "openai_compatible",
            "https://models.example/v1",
            "deepseek-chat",
            "high",
        )

    assert (
        store.get(subject_id, "openai_compatible", "https://models.example/v1", "deepseek-chat")
        is None
    )


def test_expired_capability_requires_each_effort_to_be_retested(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-expiry.sqlite3")
    subject_id = "Noyra-capability-expiry"
    IdentityStore(database).ensure(subject_id, content_hash("expiry"))
    store = CapabilityProfileStore(database)
    initial = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "low",
    )
    assert initial is not None

    with database.transaction() as connection:
        row = connection.execute(
            "SELECT * FROM model_capability_profiles WHERE subject_id=?",
            (subject_id,),
        ).fetchone()
        state = dict(row)
        state.pop("state_hash")
        expired_at = (datetime.now(UTC) - timedelta(days=1)).isoformat()
        state["expires_at"] = expired_at
        connection.execute(
            "UPDATE model_capability_profiles SET expires_at=?, state_hash=? WHERE subject_id=?",
            (expired_at, content_hash(state), subject_id),
        )

    stale = store.get(subject_id, "openai_compatible", "https://models.example/v1", "gpt-5-mini")
    assert stale is not None and stale.status == "stale"
    refreshed = store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )
    assert refreshed is not None
    assert refreshed.revision == 2
    assert refreshed.tested_efforts == ("high",)


def test_capability_profile_detects_unhashed_mutation(tmp_path: Any) -> None:
    database = Database(tmp_path / "capability-integrity.sqlite3")
    subject_id = "Noyra-capability-integrity"
    IdentityStore(database).ensure(subject_id, content_hash("integrity"))
    store = CapabilityProfileStore(database)
    store.record_probe_success(
        subject_id,
        "openai_compatible",
        "https://models.example/v1",
        "gpt-5-mini",
        "high",
    )
    with database.transaction() as connection:
        connection.execute(
            "UPDATE model_capability_profiles SET capabilities_json='{}' WHERE subject_id=?",
            (subject_id,),
        )

    with pytest.raises(IntegrityError, match="hash mismatch"):
        store.get(subject_id, "openai_compatible", "https://models.example/v1", "gpt-5-mini")


def test_provider_payload_only_contains_optional_field_when_selected() -> None:
    base: dict[str, Any] = dict(
        model="gpt-5-mini",
        messages=(ModelMessage(role="user", content="hello"),),
        max_output_tokens=8,
        temperature=0,
        schema_name="Output",
        output_schema={"type": "object"},
    )
    without = OpenAICompatibleProvider._request_payload(CompletionRequest(**base))
    with_effort = OpenAICompatibleProvider._request_payload(
        CompletionRequest(**base, reasoning_effort="high")
    )
    assert "reasoning_effort" not in without
    assert with_effort["reasoning_effort"] == "high"
    json.dumps(with_effort)
