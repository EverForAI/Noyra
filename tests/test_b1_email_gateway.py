from __future__ import annotations

import json
from email import policy
from email.parser import BytesParser

import pytest

from noyra.interaction import EmailInboundAdapter, InboundAuthenticationError
from test_b1_email_identity import gateway_mail, parse, raw_mail, signed


@pytest.mark.parametrize(
    "method", ["mailbox_challenge", "authenticated_submission", "provider_mailbox"]
)
def test_signed_gateway_mailbox_assertion_is_distinct_from_raw_mime(method: str) -> None:
    raw = raw_mail()
    body = gateway_mail(raw, method=method)
    verified = parse(body, "mail-transport")
    unverified = parse(raw, "mail-transport")
    assert verified.email_identity is not None and verified.email_identity.verified
    assert unverified.email_identity is not None and not unverified.email_identity.verified
    assert verified.email_identity.message_id == "<same@example.com>"
    assert verified.provider_event_id != unverified.provider_event_id
    assert verified.content == unverified.content


def test_raw_mime_cannot_supply_its_own_sender_assertion_or_authentication_results() -> None:
    message = BytesParser(policy=policy.default).parsebytes(raw_mail())
    message.replace_header(
        "Authentication-Results", "claimed.example; dkim=pass; spf=pass; dmarc=pass"
    )
    message["X-Noyra-Sender-Verified"] = "helper@example.com"
    message["X-Noyra-Sender-Assertion"] = "provider_mailbox"
    identity = parse(message.as_bytes(), "mail-transport").email_identity
    assert identity is not None and not identity.verified


def test_gateway_event_identity_and_mail_reply_header_are_separate() -> None:
    raw = raw_mail()
    first = parse(gateway_mail(raw, event="one"), "mail-transport")
    second = parse(gateway_mail(raw, event="two"), "mail-transport")
    assert first.provider_event_id != second.provider_event_id
    assert first.email_identity is not None and second.email_identity is not None
    assert (
        first.email_identity.message_id == second.email_identity.message_id == "<same@example.com>"
    )


def test_sender_assertion_is_covered_by_the_wire_signature() -> None:
    body = gateway_mail(raw_mail(), method=None)
    payload = json.loads(body)
    payload["sender_assertion"] = {"mailbox": "helper@example.com", "method": "provider_mailbox"}
    tampered = json.dumps(payload).encode()
    with pytest.raises(InboundAuthenticationError, match="signature is invalid"):
        EmailInboundAdapter().parse(
            tampered, signed(body), transport_id="mail", secret="synthetic-gateway-secret"
        )


@pytest.mark.parametrize("method", ["spf", "dkim", "dmarc", "trusted", "", True])
def test_domain_or_arbitrary_checks_are_not_mailbox_control_proofs(method: object) -> None:
    payload = json.loads(gateway_mail(raw_mail()))
    payload["sender_assertion"]["method"] = method
    with pytest.raises(ValueError):
        parse(json.dumps(payload).encode(), "mail")


def test_assertion_cannot_certify_another_mailbox_or_casefold_the_local_part() -> None:
    for sender in ("different@example.com", "Helper@example.com"):
        with pytest.raises(InboundAuthenticationError, match="does not match From"):
            parse(gateway_mail(raw_mail(), sender=sender), "mail")
    accepted = parse(gateway_mail(raw_mail(), sender="helper@EXAMPLE.COM"), "mail")
    assert accepted.external_sender_id == "helper@example.com"


def test_actual_gateway_delivery_account_supports_bcc_and_is_bound_to_transport() -> None:
    message = BytesParser(policy=policy.default).parsebytes(raw_mail())
    del message["To"]
    body = gateway_mail(message.as_bytes())
    adapter = EmailInboundAdapter()
    accepted = adapter.parse(
        body,
        signed(body),
        transport_id="mail",
        secret="synthetic-gateway-secret",
        account_id="noyra@example.com",
    )
    assert accepted.external_account_id == "noyra@example.com"
    with pytest.raises(InboundAuthenticationError, match="receiving account"):
        adapter.parse(
            body,
            signed(body),
            transport_id="mail",
            secret="synthetic-gateway-secret",
            account_id="different@example.com",
        )


@pytest.mark.parametrize("field", ["From", "To", "Message-ID", "In-Reply-To", "References"])
def test_duplicate_identity_and_threading_headers_are_rejected(field: str) -> None:
    value = (
        "helper@example.com"
        if field == "From"
        else "noyra@example.com"
        if field == "To"
        else "<one@example.com>"
    )
    raw = raw_mail()
    # Construct on the wire; EmailMessage deliberately forbids duplicate singleton headers.
    body = f"{field}: {value}\n{field}: {value}\n".encode() + raw
    with pytest.raises(ValueError, match="duplicate"):
        parse(body, "mail")


@pytest.mark.parametrize(
    "name,value",
    [
        ("In-Reply-To", "<one@example.com> <two@example.com>"),
        ("Message-ID", "plain-unbracketed-id"),
        ("References", "junk <one@example.com>"),
        ("References", " ".join(f"<{i}@example.com>" for i in range(33))),
        ("Message-ID", "<" + "x" * 512 + ">"),
    ],
)
def test_ambiguous_or_oversized_threading_does_not_choose_a_convenient_substring(
    name: str, value: str
) -> None:
    message = BytesParser(policy=policy.default).parsebytes(raw_mail())
    if name in message:
        del message[name]
    # Raw insertion avoids headerregistry repairing malformed input for us.
    body = f"{name}: {value}\n".encode() + message.as_bytes()
    with pytest.raises(ValueError):
        parse(body, "mail")


def test_folded_references_choose_one_latest_parent_without_fabricating_message_id() -> None:
    message = BytesParser(policy=policy.default).parsebytes(raw_mail())
    del message["Message-ID"]
    body = b"References: <first@example.com>\n <latest@example.com>\n" + message.as_bytes()
    envelope = parse(body, "mail")
    assert envelope.thread_id == "<latest@example.com>"
    assert envelope.email_identity is not None and envelope.email_identity.message_id is None
    assert envelope.provider_event_id.startswith("mail-v1:")


@pytest.mark.parametrize(
    "kind", ["duplicate", "base64", "version", "missing", "unknown", "bad_event"]
)
def test_invalid_gateway_structure_fails_before_any_invitation(kind: str) -> None:
    body = gateway_mail(raw_mail())
    payload = json.loads(body)
    if kind == "duplicate":
        body = body[:-1] + b',"event_id":"different"}'
    else:
        if kind == "base64":
            payload["raw_message_b64"] = "not base64!!!"
        elif kind == "version":
            payload["format"] = "noyra-email-ingress/v0"
        elif kind == "missing":
            del payload["account"]
        elif kind == "unknown":
            payload["verified"] = True
        else:
            payload["event_id"] = "ambiguous event"
        body = json.dumps(payload).encode()
    with pytest.raises(ValueError):
        parse(body, "mail")


def test_forwarded_and_list_mail_remain_readable_without_fabricating_author_control() -> None:
    message = BytesParser(policy=policy.default).parsebytes(raw_mail())
    message["List-ID"] = "Discussion <discussion.example.com>"
    message["ARC-Authentication-Results"] = "i=1; list.example.com; dkim=pass"
    body = gateway_mail(message.as_bytes(), method=None)
    envelope = parse(body, "mail")
    assert envelope.content == "A public suggestion."
    assert envelope.email_identity is not None and not envelope.email_identity.verified


def test_nested_duplicate_assertion_fields_and_deep_json_fail_closed() -> None:
    body = gateway_mail(raw_mail())
    payload = body.replace(
        b'"method":"provider_mailbox"', b'"method":"provider_mailbox","method":"mailbox_challenge"'
    )
    assert payload != body
    with pytest.raises(ValueError, match="duplicate"):
        parse(payload, "mail")
    with pytest.raises(ValueError):
        parse(b'{"nested":' + b"[" * 1100 + b"0" + b"]" * 1100 + b"}", "mail")
