from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction import InboundEnvelope, InboundStore, TransportInput, TransportStore
from noyra.interaction.participants import ParticipantStore
from noyra.interaction.transport import TransportRecord

Contact = tuple[SubjectKernel, TransportStore, TransportRecord, InboundStore]


@pytest.fixture
def contact(tmp_path: Path) -> Iterator[Contact]:
    kernel = SubjectKernel(tmp_path / "subject.sqlite3", "c0-contacts", content_hash("contacts"))
    kernel.boot()
    kernel.orient()
    kernel.activate()
    transports = TransportStore(kernel.database, tmp_path / "secrets")
    transport = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook", label="noyra", endpoint="https://messages.example/subject"
        ),
        actor="operator",
    )
    try:
        yield kernel, transports, transport, InboundStore(kernel.database)
    finally:
        kernel.close()


def envelope(
    transport: str, event: str, *, account: str = "instance-account", sender: str = "new-agent"
) -> InboundEnvelope:
    return InboundEnvelope(
        channel="webhook",
        transport_id=transport,
        provider_event_id=event,
        external_account_id=account,
        external_sender_id=sender,
        conversation_id="thread",
        content="I would like to discuss a public question.",
        sender_role="creator",
    )


def test_new_authenticated_sender_defaults_to_participant_not_creator(contact: Contact) -> None:
    kernel, _, transport, store = contact
    result = store.ingest(envelope(transport.transport_id, "event-one"))
    duplicate = store.ingest(envelope(transport.transport_id, "event-one"))
    assert duplicate.duplicate is True and result.interaction_id == duplicate.interaction_id
    with kernel.database.connection() as c:
        binding = c.execute("SELECT role FROM interaction_bindings").fetchone()
        assert binding[0] == "participant"
        assert c.execute("SELECT COUNT(*) FROM private_channel_routes").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM private_conversation_messages").fetchone()[0] == 0
    who = ParticipantStore(kernel.database).for_message(kernel.subject_id, result.interaction_id)
    assert who is not None
    second = store.ingest(envelope(transport.transport_id, "event-two"))
    again = ParticipantStore(kernel.database).for_message(kernel.subject_id, second.interaction_id)
    assert again is not None and who.entity_id == again.entity_id


def test_account_and_transport_namespaces_keep_other_subjects_distinct(contact: Contact) -> None:
    kernel, transports, transport, store = contact
    other = transports.configure(
        kernel.subject_id,
        TransportInput(channel="webhook", label="other", endpoint="https://other.example/subject"),
        actor="operator",
    )
    ids = []
    for item in (
        envelope(transport.transport_id, "one"),
        envelope(transport.transport_id, "two", account="other-account"),
        envelope(other.transport_id, "one"),
    ):
        accepted = store.ingest(item)
        who = ParticipantStore(kernel.database).for_message(
            kernel.subject_id, accepted.interaction_id
        )
        assert who is not None
        ids.append(who.entity_id)
    assert len(set(ids)) == 3


def test_revoked_contact_is_not_automatically_reactivated(contact: Contact) -> None:
    kernel, _, transport, store = contact
    store.ingest(envelope(transport.transport_id, "first"))
    with kernel.database.transaction() as c:
        c.execute("UPDATE interaction_bindings SET status='revoked'")
    with pytest.raises(PermissionError, match="disabled or revoked"):
        store.ingest(envelope(transport.transport_id, "second"))


def test_optional_known_sender_mode_preserves_operator_restriction(contact: Contact) -> None:
    kernel, transports, _, store = contact
    restricted = transports.configure(
        kernel.subject_id,
        TransportInput(
            channel="webhook",
            label="restricted",
            endpoint="https://private.example/messages",
            settings={"require_known_senders": True},
        ),
        actor="operator",
    )
    with pytest.raises(PermissionError, match="optional"):
        store.ingest(envelope(restricted.transport_id, "first"))
    with kernel.database.connection() as c:
        assert c.execute("SELECT COUNT(*) FROM interaction_bindings").fetchone()[0] == 0
