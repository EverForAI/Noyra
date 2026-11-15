# Inbound authentication and provider retries

Authentication and durable event identity serve different purposes. An adapter
authenticates the wire message; `InboundStore` then binds it to an active transport,
account, sender and provider event. Message content remains an untrusted invitation.
Transport acknowledgement is not a subject decision or permission to execute work.

## Channel boundaries

| Channel | Authentication | Durable event identity |
| --- | --- | --- |
| Generic webhook | HMAC-SHA256 binds timestamp, optional nonce and original body; 300-second window | `event_id` or `id`, scoped to subject, transport and receiving account |
| Email gateway | Same HMAC over the original MIME or gateway wrapper; 300-second window | Existing sender-scoped email identity, gateway event or Message-ID/content evidence |
| QQ | Official Ed25519 binds signature timestamp and original body; 300-second window | Provider message/event ID; configured account and authenticated reply selector are retained |
| QQ explicit legacy mode | Separate configured HMAC secret; same body-bound HMAC boundary | Same durable QQ event identity; legacy mode is never selected by a missing public-key field |
| Feishu with Encrypt Key | Signature binds timestamp, nonce, Encrypt Key and original body, including an encrypted wrapper; 300-second window | Official event ID or existing documented message-ID fallback |
| Feishu without Encrypt Key | Configured verification token in the callback body | Same event identity; token-only mode is a bearer-secret boundary, not a body signature |
| Telegram | Configured secret-token HTTP header | Official `update_id`, scoped to the transport and receiving account |
| WeChat messages | Official encrypted XML only; signature binds token, timestamp, nonce and ciphertext; 300-second window, then decryption and receiver account verification | `MsgId` or the existing hash of authenticated decrypted fields |

Telegram and token-only Feishu callbacks do not acquire a timestamp signature by
being listed here. Protect their static secrets with HTTPS and trusted ingress;
never log authentication headers or secret-bearing callback bodies. Possession of
that secret is a different threat from repeating a previously authenticated event.

## WeChat configuration and compatibility

Configure the platform's encrypted callback mode and supply the matching
`webhook_secret`, `encoding_aes_key` and `app_id` in the existing transport
credentials. Message POSTs containing only plaintext XML or JSON now return an
authentication failure, including when an encryption key is already configured.
There is no automatic plaintext downgrade or custom replacement signature scheme.
Encrypted callbacks containing the official `Encrypt` field remain supported.

The plaintext WeChat signature authenticates only the token, timestamp and nonce;
it cannot authenticate a different body carrying a different `MsgId`. A nonce cache
would not protect the first use of a captured tuple with a changed body. The message
boundary therefore requires the ciphertext-bound protocol rather than trying to
repair the plaintext format with event deduplication.

Existing outbound transports, credential storage and GET URL-verification
challenges are unchanged. A successful GET challenge does not certify that encrypted
message delivery is configured correctly. Test actual encrypted message delivery
before enabling the callback in production. Existing plaintext message integrations
must switch to encrypted callbacks; valid plaintext signatures alone no longer admit
messages. Old stored events and interactions are not rewritten or replayed.

## Retry and recovery semantics

The inbound event table has a persistent unique key over subject, transport,
receiving account and provider event ID. Before acknowledging an existing event,
the store verifies content hash, sender, conversation, thread/reply context and
email evidence where applicable. Reusing an event ID with different evidence is
rejected. With body-bound authentication, changing the event ID or content without
re-signing is rejected by the adapter first.

An identical, already processed callback is acknowledged with the existing
interaction. It does not create another invitation, decision or model call. Re-signing
a platform retry at a fresh time, or re-encrypting the same WeChat message, does not
change its business identity. A legitimate retry after a local processing failure
can resume the existing event through the store's idempotent interaction key;
rejecting all reused nonces before this point could obstruct that recovery.

Concurrent duplicate deliveries and restarts retain the same persistent identity.
The HTTP layer may still record another technical receipt/audit entry for a repeated
request; this is not another subject interaction. Downstream reward submission keeps
its existing idempotent recovery path instead of being skipped merely because the
transport reports a duplicate.

The runtime does not promise one-time consumption of every nonce or every HTTP
request. Timestamp checks, signature verification and request handling still occur
on a retry; ingress rate limits remain a separate resource boundary. No new nonce
table, expiry worker or migration is introduced by this change. Local tests cover
synthetic signatures, duplicate delivery, body mutation, concurrent processing,
restart and recovery; real provider configuration remains a deployment acceptance
step.
