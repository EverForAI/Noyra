# M45 Native Communication Boundaries

Stage 2 adds a native inbound boundary without changing the interaction rule:
external text is an untrusted conversation invitation, never a command.

## Inbound flow

1. A platform adapter verifies the provider-specific signature or challenge and
   decodes only the canonical message fields. Authenticated provider events
   that are not text messages are acknowledged and ignored; they are not put in
   the interaction queue.
2. The configured transport and explicit external account binding are checked.
3. `interaction_inbound_events` performs durable provider-event deduplication.
4. `InboundStore` writes an `interactions.receive` invitation and a durable
   conversation/thread mapping.
5. A creator binding records `scheduling_priority=100`; this is scheduling
   metadata only. It cannot force a reply, bypass a refusal, or execute an
   action.

The provider body is bounded to 1 MiB and is not stored or forwarded wholesale.
Changed content under an existing provider event ID is rejected as a replay
conflict. Inbound webhooks are exposed as `/webhooks/{channel}/{transport_id}`;
WeChat's GET challenge is handled at the same route.

## Supported adapters

Telegram, QQ, Feishu, WeChat, email, and generic webhook each have separate
inbound parsing and outbound payload builders. Provider credentials stay in the
existing private transport secret store. Outbound delivery retries, unknown
outcomes, and idempotency remain owned by `DeliveryDispatcher`.

The provider contracts are intentionally not interchangeable:

| Provider | Outbound contract | Credential/bootstrap contract |
| --- | --- | --- |
| QQ Bot | `POST /v2/users/{openid}/messages`, `POST /v2/groups/{group_openid}/messages`, `POST /channels/{channel_id}/messages`, or `POST /dms/{guild_id}/messages` with `Authorization: QQBot <access_token>` | `POST https://bots.qq.com/app/getAppAccessToken` with App ID/App Secret. Official event callbacks use `X-Signature-Ed25519` and `X-Signature-Timestamp`; Noyra derives the Ed25519 verification key from the configured AppSecret. QQ's intentionally unsigned `op=13` callback-address validation request is answered with `{plain_token, signature}`. |
| Feishu | App messaging uses `POST /open-apis/im/v1/messages` with `receive_id_type` and JSON-string `content`; custom bots use `/open-apis/bot/v2/hook/{key}` with object `content` and the platform signing formula | App messaging uses `tenant_access_token`, acquired from `/open-apis/auth/v3/tenant_access_token/internal` when App ID/App Secret are configured |
| WeChat Official Account | `POST /cgi-bin/message/custom/send?access_token=...` with `touser`, `msgtype`, and `text` | `GET /cgi-bin/token?grant_type=client_credential&appid=...&secret=...`; optional safe-mode callback uses `EncodingAESKey` |

The configured endpoint is pinned to the provider's official host. For QQ and
WeChat API delivery, the adapter supplies the official message path; Feishu app
delivery supplies `/open-apis/im/v1/messages`, while a Feishu custom-bot
endpoint keeps its configured `/open-apis/bot/v2/hook/{key}` path. Short-lived
provider tokens are memory-only and are never written to SQLite, backups, logs,
or the browser. SQLite, runtime exports, and the management API retain only the
endpoint origin and a digest binding. A complete custom-webhook path or query
key remains exclusively in the mode-`0600` transport secret file.

For a reply to an authenticated inbound message, Noyra persists the exact
source transport, current provider message ID, conversation ID, and reply
surface. That context overrides the transport's proactive-message default:
QQ replies select user, group, channel, or channel-DM endpoints from the
verified event, while Feishu replies use `receive_id_type=chat_id`. The current
QQ event message ID is the passive-reply target; `message_reference.message_id`
is only quoted context and is never substituted. Historical records that do
not contain this route context fail closed instead of guessing a recipient or
using another bot account. QQ C2C and group replies also carry a stable 16-bit
`msg_seq` derived from Noyra's delivery idempotency key, as required by the
platform contract. Replaying the same delivery therefore preserves the
provider-side deduplication tuple; channel and channel-DM replies do not receive
that field.

For Feishu inbound callbacks, the configured Verification Token is checked on
both URL verification and events. If an Encrypt Key is configured, the
`X-Lark-Request-Timestamp`, `X-Lark-Request-Nonce`, and `X-Lark-Signature`
headers are required and the signature covers the original request body. The
official `encrypt` wrapper is base64-decoded as a 16-byte IV prefix followed by
AES-CBC ciphertext, then the decrypted event is checked against the same
Verification Token. Authenticated non-message and non-text events are returned
successfully and ignored. A custom bot's outbound signing secret is only used
for that webhook payload and is never sent as an app access token.

QQ accepts the official Ed25519 callback envelope above. A legacy Noyra HMAC
callback is available only when an explicit compatibility `webhook_secret` is
configured; the AppSecret or any public-key field is never reused as an HMAC
key. Authenticated QQ non-message and non-text events are acknowledged and
ignored. Successfully handled QQ events return the platform-native `op=12`
acknowledgement.

Inbound event admission is retry-safe. A provider retry for the exact same
authenticated event can reopen a previously rejected local attempt, but only
after the transport, sender, conversation, body digest, route context, and
provider event ID all match. The state transition uses compare-and-swap and can
produce at most one interaction. Changed content or an inconsistent processed
link remains an integrity failure instead of becoming a second message.

## Endpoint binding migration

Schema 50 upgrades an older origin-only transport record before normal runtime
admission by binding a digest of the complete private endpoint. The upgrade
never copies a webhook key or query secret into SQLite. It verifies the legacy
state hash, the private-file location, the endpoint origin, and the current
provider host policy. A cryptographic/reference/origin mismatch still fails
closed as integrity damage. If all legacy evidence is authentic but the old
endpoint is no longer permitted by the current provider policy, only that
transport is atomically revoked and its private secret is deleted (or queued
for bounded cleanup); unrelated transports and the service continue starting.
The revoked transport must then be recreated with provider-specific settings
before it can deliver again.

The first upgrade is necessarily trust-on-first-use for a same-origin private
path: releases before schema 50 never persisted a value capable of proving that
path. Operators upgrading an existing custom-webhook deployment should compare
or re-enter its complete endpoint during the maintenance window. After the
digest is bound, same-origin path or query tampering is rejected.

WeChat callbacks may arrive as XML (the official account format) or the bounded
JSON compatibility envelope. The `Token`/timestamp/nonce signature is checked
before text is accepted. Safe-mode XML uses the configured `EncodingAESKey` and
the encrypted message signature. Both plaintext and encrypted callbacks must
fall within the configured replay window even when their signatures are valid;
non-text events are acknowledged and ignored.
Successfully handled POST callbacks return the required plain-text `success`
acknowledgement so WeChat does not retry a durably accepted event.
The provider's GET challenge is handled at the same `/webhooks/wechat/{transport_id}`
route and returns the validated plain or decrypted `echostr`. At-rest or
integrity quarantine returns `503` instead of accepting a challenge or event.

The email adapter is an authenticated HTTP ingress for a trusted mail gateway;
the `smtp://` / `smtps://` transport endpoint is outbound-only. Noyra does not
listen on an SMTP/MX port or accept unauthenticated Internet mail directly.
The gateway must forward the bounded original RFC 5322 message to
`/webhooks/email/{transport_id}` with the configured HMAC headers, after which
the normal account binding and replay checks apply.

Before production exposure, configure a platform-specific webhook secret,
bind at least one external account in `/admin`, and complete provider sandbox
contract tests. Real credentials and public listeners are intentionally outside
the repository test suite.
