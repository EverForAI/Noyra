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
action is created. Invalid affect targets produce no partial psychological state and may be retried
only within the configured daily limit.

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
