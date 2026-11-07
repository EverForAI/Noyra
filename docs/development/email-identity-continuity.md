# Email identity and private conversation continuity

Mail stays open to new correspondents. An incoming message is an invitation
that Noyra may accept, defer or decline. No sender allowlist or per-message
operator approval is enabled by this change.

## Two different assertions

The existing `X-Noyra-Signature` authenticates the configured gateway's HTTP
request. It does not, by itself, prove that an arbitrary Internet email's
`From` mailbox belongs to its author. Raw RFC 5322 submissions remain accepted
and readable. Each raw/unverified event receives an isolated participant
source; it cannot inherit a known mailbox's private history or creator priority.
A header claiming successful authentication inside MIME has no authority.

To attest a mailbox, the gateway posts this JSON body to the same
`/webhooks/email/{transport_id}` route and signs the complete original HTTP body
with the existing timestamp/nonce HMAC scheme:

```json
{
  "format": "noyra-email-ingress/v1",
  "event_id": "opaque-stable-gateway-delivery-event-id",
  "account": "noyra@example.com",
  "raw_message_b64": "BASE64_OF_ORIGINAL_MESSAGE",
  "sender_assertion": {
    "mailbox": "correspondent@example.com",
    "method": "provider_mailbox"
  }
}
```

`sender_assertion` is optional. Omission accepts an unverified invitation.
The entire JSON envelope, including the assertion, account and original
message bytes, is signed. Adding assertion HTTP/MIME headers to a raw message
does not attest anything. Duplicate JSON fields and unknown fields are rejected.
The existing total request limit also bounds the JSON/base64 envelope; encoding
therefore reduces the maximum original message size within that limit.

Supported proof methods describe the gateway's evidence, not user-controlled
mail labels:

- `mailbox_challenge`: the gateway completed a random mailbox-control challenge.
- `authenticated_submission`: the gateway authenticated a submission identity
  and separately authorized that exact `From` mailbox for that identity.
- `provider_mailbox`: a trusted provider authenticated the actual sending
  mailbox account and the gateway checked its exact mailbox binding.

The gateway is part of the trust boundary. Merely renaming an SPF, DKIM or
DMARC result to one of these methods is incorrect. Those checks alone do not
prove control of an arbitrary local part at a domain. The gateway must not
copy an assertion from incoming mail or untrusted webhook input. Compromise
of a trusted gateway or its signing secret defeats its attestations.

The asserted mailbox must match the parsed single `From` mailbox. Domain case
is normalized; local-part case is preserved. `account` is the actual receiving
account and may differ from visible `To` (Bcc, aliases, forwarded messages).
If the transport configures `settings.account_id`, it must match that receiving
account. Mailing lists and forwarders should omit the assertion when they
cannot prove the original author's exact mailbox. They remain readable as
unverified invitations. There is no automatic trust promotion based on ARC,
list headers or a familiar display name.

## Durable identity, threading and retries

Verified mailbox sources are scoped to subject, transport, receiving account
and sender. Proof methods may change without changing that identity.
Unverified sources are scoped to an event; familiar `From` and reply references
do not join them to verified or historical private identities. A bounded
rate/capacity check prevents isolated sources from being created indefinitely.
The optional `require_known_senders` restriction additionally requires proof
for email and stays disabled by default.

An internal event key is distinct from the original `Message-ID`. It includes
the sender and trust class, so an unverified event cannot occupy a verified
event's key. A gateway delivery uses the gateway's stable event ID; legacy raw
MIME uses its `Message-ID`, or an exact raw-message digest when absent.
The same event with changed content or evidence is rejected as a conflict.
It is not silently treated as a harmless duplicate. The existing HTTP rejection response
allows the gateway to retain and investigate that delivery.

`Message-ID`, `In-Reply-To` and `References` are bounded opaque bracketed IDs.
Duplicate headers, ambiguous multiple direct parents, malformed IDs and
oversized reference lists are rejected before durable event insertion.
Ordinary folded reference lists are supported. The last reference supplies
a single candidate parent if there is no `In-Reply-To`; matching the subject,
transport, account, recipient and unique delivery is still required.
Outbound reply headers use the saved original `Message-ID`, never the internal
hash or gateway event ID. Without an original ID no replacement is fabricated.
Unknown outbound delivery outcomes remain unknown and are never resent merely
because a matching reply arrives.

Schema v113 adds an explicit email identity version on inbound events, per-event
identity evidence, owner/immutable-update guards and an export ownership rule. Evidence binds parsed content, source,
thread, raw digest and proof type. Readers and delivery validate it; new events
with missing/corrupt evidence fail closed. Existing migration SQL and its
historical fingerprints stay unchanged.

## Upgrades and operational limits

Existing email bindings, messages and private history are retained. Upgrade
does not fabricate proof for legacy events, relabel their identities or merge
them with newly verified mailboxes. New proof starts a new authenticated source.
Legacy mailbox-only private history is withheld from automated context and
queued replies tied to that unverified private source are stopped before send.
Such failed deliveries are visible through the existing delivery state/error
reporting. Public legacy messages can still be replied to without granting
private-history access. No historical inbox is replayed by migration. A newly
submitted old raw message uses the new event namespace and is handled as an
isolated invitation rather than reviving its old private conversation.

Deploy the application and gateway protocol together when private mailbox
continuity is needed. Rolling the application back after v113 requires a
coordinated database/backup rollback; an old application must not open the
newer schema. Do not blindly restore a pre-upgrade database over new live mail.

Repository tests cover the signed envelope, raw spoofing, full local HTTP
callback, SMTP reply headers, private multi-round exchange, account/transport
boundaries, concurrent first replies, receive failures, tampering and old-schema
upgrade. They use synthetic providers and local servers. A real gateway staging
exercise is required to establish that its claimed proof method is actually
backed by mailbox control, including forwarding/list behavior and event retries.
No repository unit test establishes that external operational fact.
