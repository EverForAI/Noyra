# M25: Resource-Bounded Autonomous Projects

M25 adds a durable project layer between intrinsic goals and individual cognition
workflows. A project is a self-formed, small, inspectable undertaking with a
single autonomous goal, a concrete deliverable, ordered phases, acceptance
criteria, an estimated time horizon, and an explicit resource envelope.

Supported first-version project types are research, prediction tracking,
knowledge collection, a small software prototype, self-development experiments,
and collaboration requests. The project layer does not create a shell, grant
permissions, publish, transact, or treat a human request as a project goal.
Those capabilities remain separate operator-granted resources.

## Formation

After an active autonomous goal exists, the local supervisor periodically gives
the economy model a bounded context containing recent evidence, values, mission
candidates, available capability summaries, and existing project state. The model
may return `wait` or one project proposal. A proposal must cite local evidence,
use a supplied goal, contain two to eight ordered phases, and fit the configured
hard limits. Existing non-terminal project work is excluded from formation for
that goal.

The supervisor rejects large or vague proposals, missing evidence, forged IDs,
unavailable permissions, cyclic phase dependencies, and budgets above the hard
limits. Formation is idempotent by a content-derived project key and the model
call ID is recorded as the formation authority, not as identity.

## Execution and Review

M25 deliberately separates project state from tool execution. Each review picks
one current phase and can activate, continue, complete a phase, scale down,
pause, abandon, wait, or request help. Completion requires supplied durable
evidence. A help request is an append-only, bounded request visible through the
public project projection; it never automatically grants a capability.

Resource usage is reconciled from model calls, research runs, and actions. The
local supervisor pauses a project when a budget, cycle, or no-progress limit is
reached. It never raises a budget during a review. Every project and phase
revision is hash checked and append-only history is preserved.

The cognition cycle reviews project formation and project state before asking
the metacognitive selector for an unrelated one-step workflow. Once a project is
active, the existing research and action planners receive its safe goal/phase
summary and continue to choose only already-authorized bounded operations. M25
does not yet add a general artifact authoring engine; software projects are
planning and prototype work within an existing filesystem-write capability.

Sleep creates a project reflection record for every non-terminal project. This
lets the next awake cycle see resource exhaustion and repeated stagnation without
calling the model during sleep.

## Public and Private Views

`/api/projects` exposes project title, type, purpose, deliverable, status,
progress, phase counts, and whether a help request is open. It excludes causal
IDs, private reasoning, model prompts, hidden psychology, and resource secrets.
Project reviews and state changes are included in the authenticated runtime log
and complete export.

## Initial limits

The default envelope is at most two non-terminal projects, eight phases, seven
days, 24 project cognition cycles, 16 model calls, 12 searches, eight external
actions, and 2 MB of artifact storage. These are supervisor ceilings; a project
may choose lower values and may only scale down later.
