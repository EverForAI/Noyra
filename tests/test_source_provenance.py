from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

import pytest

from noyra.core.source_provenance import (
    SourceCategory,
    SourceProvenanceStatus,
    classify_source_event_ids_json,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE events ("
        "event_id TEXT PRIMARY KEY, subject_id TEXT NOT NULL, event_type TEXT NOT NULL, "
        "source TEXT NOT NULL, payload_json TEXT NOT NULL DEFAULT '{}', "
        "payload_archive_key TEXT, "
        "causal_parent_ids_json TEXT NOT NULL DEFAULT '[]')"
    )
    return connection


def _insert_event(
    connection: sqlite3.Connection,
    event_id: str,
    event_type: str,
    source: str,
    *,
    subject_id: str = "subject-1",
    payload: dict[str, object] | None = None,
    parents: tuple[str, ...] = (),
) -> None:
    connection.execute(
        "INSERT INTO events("
        "event_id,subject_id,event_type,source,payload_json,causal_parent_ids_json) "
        "VALUES (?,?,?,?,?,?)",
        (
            event_id,
            subject_id,
            event_type,
            source,
            json.dumps(payload or {}),
            json.dumps(list(parents)),
        ),
    )


def _encode(event_ids: Iterable[str]) -> str:
    return json.dumps(list(event_ids), separators=(",", ":"))


def test_classifies_only_exact_known_direct_event_source_pairs() -> None:
    connection = _connection()
    _insert_event(connection, "evt-human", "interaction_received", "human")
    _insert_event(connection, "evt-legacy-human", "human_message", "human")
    _insert_event(connection, "evt-help", "assistance_reply", "participant")
    _insert_event(connection, "evt-world", "world_observation", "safe_web_reader")
    try:
        human = classify_source_event_ids_json(connection, "subject-1", _encode(["evt-human"]))
        legacy_human = classify_source_event_ids_json(
            connection, "subject-1", _encode(["evt-legacy-human"])
        )
        help_reply = classify_source_event_ids_json(connection, "subject-1", _encode(["evt-help"]))
        world = classify_source_event_ids_json(connection, "subject-1", _encode(["evt-world"]))
        assert human.status is SourceProvenanceStatus.DIRECT_REFERENCES_CLASSIFIED
        assert legacy_human.categories == (SourceCategory.HUMAN_INTERACTION,)
        assert human.categories == (SourceCategory.HUMAN_INTERACTION,)
        assert help_reply.categories == (SourceCategory.HUMAN_ASSISTANCE_REPLY,)
        assert world.status is SourceProvenanceStatus.DIRECT_REFERENCES_CLASSIFIED
        assert world.categories == (SourceCategory.EXTERNAL_OBSERVATION,)
        assert human.lineage_scope == "direct_event_references_only"
        assert not hasattr(human, "trusted")
    finally:
        connection.close()


def test_known_event_type_with_an_unrecognized_source_string_stays_unknown() -> None:
    connection = _connection()
    _insert_event(connection, "evt-human", "interaction_received", "operator_control")
    _insert_event(connection, "evt-world", "world_observation", "unverified_reader")
    try:
        for event_id in ("evt-human", "evt-world"):
            provenance = classify_source_event_ids_json(
                connection, "subject-1", _encode([event_id])
            )
            assert provenance.status is SourceProvenanceStatus.UNKNOWN
            assert provenance.categories == (SourceCategory.UNKNOWN,)
    finally:
        connection.close()


def test_partial_mixed_or_unrecognized_sources_never_become_fully_classified() -> None:
    connection = _connection()
    _insert_event(connection, "evt-human", "interaction_received", "human")
    _insert_event(connection, "evt-other", "custom_future_event", "future_source")
    try:
        mixed = classify_source_event_ids_json(
            connection, "subject-1", _encode(["evt-human", "evt-other", "evt-missing"])
        )
        assert mixed.status is SourceProvenanceStatus.PARTIAL
        assert set(mixed.categories) == {SourceCategory.HUMAN_INTERACTION, SourceCategory.UNKNOWN}
        assert mixed.source_count == 3
        assert mixed.unclassified_count == 2
        assert mixed.withdrawn_count == 0

        unknown = classify_source_event_ids_json(connection, "subject-1", _encode(["evt-other"]))
        assert unknown.status is SourceProvenanceStatus.UNKNOWN
        assert unknown.categories == (SourceCategory.UNKNOWN,)
        assert unknown.unclassified_count == 1
    finally:
        connection.close()


@pytest.mark.parametrize(
    "source_json",
    [
        "[]",
        "not-json",
        "{}",
        '["evt-human","evt-human"]',
        '["", "evt-human"]',
        json.dumps([f"evt-{index}" for index in range(65)]),
    ],
)
def test_empty_or_malformed_legacy_source_lists_remain_unknown(source_json: str) -> None:
    connection = _connection()
    try:
        provenance = classify_source_event_ids_json(connection, "subject-1", source_json)
        assert provenance.status is SourceProvenanceStatus.UNKNOWN
        assert provenance.categories == (SourceCategory.UNKNOWN,)
        assert provenance.source_count == 0
        if source_json == "[]":
            assert provenance.input_valid
        else:
            assert not provenance.input_valid
    finally:
        connection.close()


def test_wrong_subject_reference_is_unknown_not_a_source_credential() -> None:
    connection = _connection()
    _insert_event(connection, "evt-human", "interaction_received", "human", subject_id="subject-2")
    try:
        provenance = classify_source_event_ids_json(connection, "subject-1", _encode(["evt-human"]))
        assert provenance.status is SourceProvenanceStatus.UNKNOWN
        assert provenance.unclassified_count == 1
        assert provenance.categories == (SourceCategory.UNKNOWN,)
    finally:
        connection.close()


def test_withdrawn_private_source_is_not_reported_as_classified() -> None:
    connection = _connection()
    _insert_event(
        connection,
        "evt-private",
        "interaction_received",
        "human",
        payload={"interaction_id": "interaction-1"},
    )
    connection.execute(
        "CREATE TABLE private_conversations(conversation_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE private_conversation_messages("
        "interaction_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO private_conversations VALUES('conversation-1','deleted')")
    connection.execute(
        "INSERT INTO private_conversation_messages VALUES('interaction-1','conversation-1')"
    )
    try:
        provenance = classify_source_event_ids_json(
            connection, "subject-1", _encode(["evt-private"])
        )
        assert provenance.status is SourceProvenanceStatus.WITHDRAWN
        assert provenance.categories == (SourceCategory.UNKNOWN,)
        assert provenance.withdrawn_count == 1
        assert provenance.unclassified_count == 0
    finally:
        connection.close()


def test_mixed_withdrawn_and_recognized_sources_are_partial() -> None:
    connection = _connection()
    _insert_event(connection, "evt-human", "interaction_received", "human")
    _insert_event(
        connection,
        "evt-private",
        "interaction_received",
        "human",
        payload={"interaction_id": "interaction-1"},
    )
    connection.execute(
        "CREATE TABLE private_conversations(conversation_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE private_conversation_messages("
        "interaction_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)"
    )
    connection.execute("INSERT INTO private_conversations VALUES('conversation-1','deleted')")
    connection.execute(
        "INSERT INTO private_conversation_messages VALUES('interaction-1','conversation-1')"
    )
    try:
        provenance = classify_source_event_ids_json(
            connection, "subject-1", _encode(["evt-human", "evt-private"])
        )
        assert provenance.status is SourceProvenanceStatus.PARTIAL
        assert set(provenance.categories) == {
            SourceCategory.HUMAN_INTERACTION,
            SourceCategory.UNKNOWN,
        }
        assert provenance.withdrawn_count == 1
    finally:
        connection.close()


def test_malformed_event_payload_does_not_break_source_classification() -> None:
    connection = _connection()
    connection.execute(
        "INSERT INTO events(event_id,subject_id,event_type,source,payload_json) "
        "VALUES ('evt-malformed','subject-1','interaction_received','human','{')"
    )
    connection.execute(
        "CREATE TABLE private_conversations(conversation_id TEXT PRIMARY KEY, status TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE private_conversation_messages("
        "interaction_id TEXT PRIMARY KEY, conversation_id TEXT NOT NULL)"
    )
    try:
        provenance = classify_source_event_ids_json(
            connection, "subject-1", _encode(["evt-malformed"])
        )
        assert provenance.status is SourceProvenanceStatus.DIRECT_REFERENCES_CLASSIFIED
        assert provenance.categories == (SourceCategory.HUMAN_INTERACTION,)
    finally:
        connection.close()


def test_archived_event_payload_is_unknown_not_a_verified_origin() -> None:
    connection = _connection()
    connection.execute(
        "INSERT INTO events("
        "event_id,subject_id,event_type,source,payload_json,payload_archive_key) "
        "VALUES ('evt-archived','subject-1','interaction_received','human','{}','segment-1')"
    )
    try:
        provenance = classify_source_event_ids_json(
            connection, "subject-1", _encode(["evt-archived"])
        )
        assert provenance.status is SourceProvenanceStatus.UNKNOWN
        assert provenance.categories == (SourceCategory.UNKNOWN,)
    finally:
        connection.close()


def test_direct_event_categories_do_not_follow_causal_parents() -> None:
    connection = _connection()
    _insert_event(connection, "evt-parent", "interaction_received", "human")
    _insert_event(
        connection,
        "evt-child",
        "sleep_reflection",
        "runtime",
        parents=("evt-parent",),
    )
    # The resolver intentionally uses only the event IDs explicitly referenced
    # by the record; it does not infer ancestry from another event.
    try:
        provenance = classify_source_event_ids_json(connection, "subject-1", _encode(["evt-child"]))
        assert provenance.status is SourceProvenanceStatus.UNKNOWN
        assert provenance.categories == (SourceCategory.UNKNOWN,)
    finally:
        connection.close()
