# M19 Relationship Continuity and Proactive Social Cognition

M19 turns repeated communication into durable relationship history and adds a bounded awake social
review. It does not make human messages into tasks. Noyra remains free to decline an invitation,
stay silent, wait, contact a known person, or make a non-coercive help request.

## Causal flow

```text
incoming invitation
-> appraisal and affect
-> idempotent relationship revision
-> later private relationship review
-> local boundary validation
-> durable social decision and optional mailbox delivery intent
```

Incoming interaction cognition creates a human relationship on first contact and revises trust,
affinity, conflict and familiarity from the chosen disposition. The revision uses the interaction
event and decision round as a recovery key, so a restart between response persistence and final
interaction decision cannot apply the same relationship change twice.

After genesis is complete, the cognition cycle may review one known relationship. Selection is
deterministic and based on current relationship ordering. The model receives only that
correspondent's metadata, interaction metadata without message bodies, relevant recalled memories,
bounded affect, active autonomous goals, and recent event identifiers. One correspondent's private
history is never placed beside another correspondent's history in the same model request.

## Local invariants

- Only relationships with `entity_type=human` are eligible.
- Contact requires a channel already established by a durable interaction.
- `no_contact`, refused contact, active cooldown and severe conflict are filtered before a model
  call and rechecked before commit.
- A proposal may cite only supplied event identifiers.
- Help requests cannot contain a small local set of coercive phrases. This is a defense in depth
  check; structured scope and relationship/channel validation remain the primary controls.
- The social component cannot create goals, invoke tools, publish, discover strangers, expand
  capabilities, or select a new channel.
- Contact and help requests are durable mailbox delivery intents. M19 does not claim that an
  external service delivered them.

## Continuity and integrity

`relationship_social_runs` is append-only and binds the relationship, model call, optional outgoing
interaction, disposition, channel, counterparty, evidence and exact proposal with content hashes.
The interaction integrity audit checks those hashes, subject ownership, evidence existence, model
response agreement and outgoing interaction linkage. Social commit and outgoing interaction creation
share one SQLite transaction, so a crash cannot leave a message without its social decision record.

The complete developer runtime export automatically includes the new relationship and social tables.
The public state exposes only aggregate counts. It does not expose relationship scores, boundaries,
private memories, model prompts, rationales or private messages.

## Resource controls

- `NOYRA_SOCIAL_REVIEW_INTERVAL_SECONDS`
- `NOYRA_MAX_SOCIAL_MODEL_CALLS_PER_DAY`
- `NOYRA_MAX_SOCIAL_CONTEXT_CHARS`

If every known relationship is blocked, cooling down, severely conflicted, or lacks an established
channel, no model call is made. This preserves both relationship boundaries and remote API quota.
