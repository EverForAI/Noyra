# M21 Intrinsic Attention and Autonomous Thought Scheduling

M21 gives Noyra a durable mechanism for selecting what to think about when no external invitation or
immediately due world task requires attention. It does not create a continuously running hidden model
process. The runtime heartbeat remains bounded; each eligible cycle may perform at most one private
thought episode.

## Agenda formation

The local scheduler derives agenda items without a model call from durable internal state:

- active or candidate autonomous goals with remaining tension;
- unresolved questions retained by reflective sleep;
- affect components above a local intensity threshold;
- uncertainties in the current operational self-model;
- human relationships with strong affinity or conflict.

Human message text is not an agenda source. Human-proposed goals are excluded. Agenda selection uses
urgency, novelty and emotional weight, with penalties for recurrence and repeated no-change episodes.
The model cannot choose an unknown agenda or invent the scheduler's priority inputs.

## Thought episode

One structured episode may reflect, reframe, defer, resolve or abandon the selected item. The model
receives bounded private events, memories, beliefs, autonomous goals, relationship state, affect and
self-model context. It can cite only supplied IDs. It cannot call tools, browse, publish, communicate,
spend funds, alter capabilities or claim an external action occurred.

A thought may propose one candidate self-origin goal. The local supervisor accepts it only when the
named motive matches a current affect component with sufficient intensity and the separate daily goal
limit has not been reached. The goal remains a candidate for normal goal governance; thought does not
activate or execute it.

## Loop prevention

`thought_agenda_items` maintains recurrence and consecutive no-change counts. Identical normalized
results for one agenda are rejected. After the configured no-change streak, the item enters a timed
cooldown and consumes no additional model calls until that cooldown expires. Resolve and abandon are
terminal. Model calls have their own daily limit and one failed or rejected durable call is not retried
indefinitely under the same idempotency key.

Agenda revisions and thought episodes are hashed, append-only histories. Integrity verification binds
each episode to its agenda and exact model response. The full private ledger is included in developer
runtime export. Public state exposes only aggregate agenda, episode, changed-state and goal counts.

## Resource controls

- `NOYRA_THOUGHT_INTERVAL_SECONDS`
- `NOYRA_MAX_THOUGHT_MODEL_CALLS_PER_DAY`
- `NOYRA_MAX_THOUGHT_CONTEXT_CHARS`
- `NOYRA_MAX_THOUGHT_NO_CHANGE_STREAK`
- `NOYRA_THOUGHT_COOLDOWN_SECONDS`
- `NOYRA_MAX_THOUGHT_GOALS_PER_DAY`
