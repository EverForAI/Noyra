# M13 Autonomous Goal Governance

M13 gives Noyra an awake, durable process for choosing which of its existing autonomous goals to
focus on. It does not add a planner or tool execution. World cognition and reflective sleep may form
or revise goal candidates; goal governance decides whether eligible goals should become active,
remain active, pause, enter reconsideration, or be abandoned.

## Governance cycle

```text
genesis and first sleep complete
-> eligible non-human goals exist
-> interval, change, and daily-call checks
-> bounded private context
-> remote structured governance proposal
-> local transition, evidence, weight, and capacity validation
-> atomic goal revisions and append-only focus record
-> public safe goal projection
```

Every existing active goal must receive an explicit decision. The proposal may select one resulting
active goal as the current focus and describe a private near-term intention. An intention is only an
internal orientation for a later planner; it is not an action, command, tool request, message, or
capability grant.

## Human-message and side-effect boundary

Goals with origin `human_proposal` are excluded from the model context even if another subject-owned
process has accepted one as a candidate. Raw human messages, interaction IDs, communication
decisions, and interaction events are also excluded from governance evidence. A human conversation
therefore cannot directly become an active goal or raise its priority through M13.

The governance schema has no action or tool field. Local commit code touches only goal revisions, a
private governance event, fatigue accounting, and the append-only governance record. Tests assert
that successful and rejected governance produce no action rows and no outbound messages.

## Local supervisor

The model may cite only supplied event IDs and existing eligible goal IDs. The Supervisor rejects:

- forged, duplicated, terminal, or human-proposed goals;
- missing or interaction-derived evidence;
- invalid goal-state transitions;
- priority or commitment jumps larger than 0.25 per review;
- implicit treatment of an active goal that lacks an explicit decision;
- a focus that is not active or was not explicitly reviewed;
- more active goals than `NOYRA_MAX_ACTIVE_GOALS`.

The same active-goal limit is applied to reflective sleep so sleep cannot bypass awake governance.
Progress and emotional pressure are preserved; M13 cannot claim that work was completed or change
psychological pressure. All validated goal revisions and the new focus record commit in one SQLite
transaction using the governance event as an additional causal source.

## Recovery, limits, and integrity

Model-call purposes are numbered by committed governance rounds. A crash after the provider returns
reuses the successful stored response and the call-derived idempotency key. Invalid successful
responses are ignored and privately audited. The separate daily call cap prevents a malformed model
or an unstable goal set from starving world cognition. Hard model-budget denial becomes resource
pressure and leads into the existing budget-sleep path.

`goal_governance_runs` is append-only and stores the private proposal hash, focus, intention, causal
event IDs, model call, and state hash. Integrity verification checks hashes, event ownership, and the
successful model call. Database schema version 8 creates the table and immutable triggers.

## Goal visibility

`GET /api/goals` and the dashboard's “目标” view expose the explicitly approved safe projection:
title, description, origin, lifecycle status, priority, commitment, progress, timestamps, and whether
the goal is the current focus. These fields require a read token. Anonymous `/api/state` is the
versioned `PublicSubjectStateV1` projection and contains no goal count, focus, or title; aggregate
operational state is available only through the authenticated `/api/state/details` route.

The projection does not expose emotional pressure, model summaries, decision reasons, evidence IDs,
revision history, private intention text, memories, beliefs, or affect. These public reads cannot
modify a goal.
