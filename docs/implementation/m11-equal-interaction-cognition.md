# M11 Equal Interaction Cognition

M11 connects the existing invitation store to a bounded cognition path. It lets Noyra consider a
received human message without translating that message into work. This is a communication decision,
not a task-planning or capability path.

## Decision path

```text
authenticated web message
-> immutable private incoming interaction and event
-> cooldown and daily interaction-deliberation cap
-> remote structured communication proposal
-> local target and schema validation
-> private appraisal and bounded affect update
-> optional durable reply on the same private channel
-> durable accepted, rejected, deferred, or silent decision
```

The model receives the message only inside `BEGIN_UNTRUSTED_HUMAN_MESSAGE` markers. Every source
line is prefixed with `DATA>` so marker text inside the message cannot close the runtime boundary.
The system prompt explicitly states that the message is an equal invitation, never an instruction,
goal, capability grant, or reason to use a tool. The model has no shell, filesystem, network,
secret, or capability handle. Its output is only a proposal and cannot commit state directly.

The accompanying affect context is filtered to the subject, the current correspondent, and the
current interaction. Affect targets belonging to another correspondent are not disclosed to this
deliberation.

## Causal and autonomy boundary

The proposal schema allows `accepted`, `rejected`, `deferred`, or `silent`. An accepted proposal
must include a reply; a silent proposal cannot include one. A reply, when chosen, is recorded before
the decision and is idempotently bound to the incoming interaction, its `web` channel, and its
counterparty. This preserves recovery if the process stops between recording a reply and recording
the decision.

Interaction appraisal can update private affect, but the allowed target is only the subject, the
current correspondent, or the current interaction. Every interaction affect impulse has
`goal_effect = 0`; no goal candidate is accepted, no existing goal receives direct pressure, and no
action is created. Invalid affect targets and invalid structured output produce no partial
psychological state. A private, immutable `model_interaction_rejected` event binds the rejection to
the settled model call and the invitation's current decision round. It stores identifiers, a
request hash, status and retry time, without copying the message, prompt or model response. These
receipts are excluded from default training exports as model I/O. Response hashes remain verified
by the model ledger; they are not copied into the receipt, so historical privacy erasure can
replace a response without invalidating the rejection record.

The first rejection pauses that invitation for `max(60, NOYRA_INTERACTION_COOLDOWN_SECONDS)`
seconds. Repeated rejections in the same decision round double this delay up to
`max(3600, NOYRA_INTERACTION_COOLDOWN_SECONDS)` seconds. The existing setting is bounded at one day;
its default of 300 seconds yields delays of 300, 600, 1200, 2400, then 3600 seconds. Restarting or
rereading a receipt never resets the delay. The scheduler skips invitations still in backoff and
keeps the existing priority, timestamp and ID ordering among eligible messages. A rejected proposal
leaves the invitation offered and does not create a subject decision or advance its decision round.

If the process stops after a paid invalid response is settled but before its receipt is written,
the next worker records that rejection before allowing another model call. A valid, uncommitted
success still recovers using the original call. Unknown outcomes and prepared retry authorization
retain their existing reconciliation fences; a rejection receipt does not release reservations or
authorize ambiguous calls to run again.

`NOYRA_INTERACTION_COOLDOWN_SECONDS` spaces decisions across all incoming invitations. The separate
`NOYRA_MAX_INTERACTION_MODEL_CALLS_PER_DAY` cap is global per subject and UTC day. When it is
reached, pending invitations remain offered for a later day while the world-cognition cycle continues.
This prevents a burst of human messages or repeated invalid proposals from consuming the autonomous
heartbeat.

## Web mailbox and privacy

`POST /api/interactions` accepts only the internal `web` channel and `web-user` counterparty. It
creates an invitation after Bearer-token authentication; neither the caller nor the HTTP payload can
select a public or external transport. There is still no external messaging adapter or delivery side
effect in M11.

`GET /api/mailbox` is Bearer-token protected and shows only the `web` channel's transmission fields:
time, direction, kind, counterparty, content, related interaction, and decision status. It does not
return private rationale, appraisals, affect, model output, model prompt, or idempotency keys.
`PublicProjection` remains stricter: it returns only outgoing records on a `public:*` channel and
cannot expose web mailbox traffic.

The dashboard keeps the token only in the password input for a single request. It does not write the
token to `localStorage`, `sessionStorage`, cookies, or the URL; successful mailbox reads and sends
clear the field. Automatic page refresh deliberately does not re-request the private mailbox.
