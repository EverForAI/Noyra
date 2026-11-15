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

Human message text is not an agenda source. Human-proposed goals are excluded. An absence of
continuable autonomous goals can itself become an internal question; cognition can be enabled with
no configured seed source. This gives internal attention an opportunity without creating synthetic
affect, assigning an interest, or requiring that a new goal result.

The worker and metacognitive selector share a read-only batch of at most 64 agenda representatives.
Never-served questions precede repeats; durable episode/known-failure history orders subsequent
turns, with urgency and novelty as tie breakers. Equivalent question text in the same discovery
snapshot shares one scheduling opportunity while all original records remain. A later discovery
has its own evidence epoch. Cooling, daily budgets, current round inspection and quarantined
unknown calls remain binding. This selection changes no ledger, budget or cooldown and survives
worker restarts. The model cannot choose an unknown agenda or invent priority inputs.

## Thought episode

One structured episode may reflect, reframe, defer, resolve or abandon the selected item. The model
receives bounded private events, memories, beliefs, autonomous goals, relationship state, affect and
self-model context. It can cite only supplied IDs. It cannot call tools, browse, publish, communicate,
spend funds, alter capabilities or claim an external action occurred.

The context limit measures the final escaped UTF-8 bytes, including JSON syntax and escaped
control markers. The historical `NOYRA_MAX_THOUGHT_CONTEXT_CHARS` setting name remains available;
the fixed JSON envelope is additional overhead of fewer than 100 bytes. A context that already
fits keeps its original representation and prompt.

The worker first checks whether the dictionaries and one event anchor can fit together. If they
can, it only prunes optional source rows. Otherwise, it projects agenda descriptions and the
current self-model, reserving a quarter of the budget for agenda metadata and a third for the
self-model. Only the agenda topic is shortened; agenda/source IDs, attention
values, status and timestamps remain exact. The self-model keeps its version and bounded prefixes
of continuity, values, commitments, uncertainties and trait descriptions. Its nested JSON columns
remain complete JSON arrays, with retained trait provenance IDs and numeric values unchanged.
Trailing optional entries may be omitted if text shortening alone is insufficient.

Projected dictionaries are marked by `context_projection` with hashes of their full originals.
The model is told that omitted details remain unknown. These hashes describe truncation and do
not authorize citations. At least one available event anchor survives list pruning; source-ID
validation uses exactly the rows still transmitted. If immutable identifiers themselves cannot
fit, context construction reports an integrity error rather than shortening an identity. The
durable agenda and self-model stay intact, and context construction does not write to the database.
Existing budget, cooldown, unknown-call reconciliation and successful-call recovery rules still
govern provider requests and commits.

A thought may propose one candidate self-origin goal. The local supervisor accepts it only when the
named motive matches a current affect component with sufficient intensity and the separate daily goal
limit has not been reached. The goal remains a candidate for normal goal governance; thought does not
activate or execute it. Neither an established value nor a mission is required for such a bounded
short-term goal. Affect and parent-agenda eligibility are checked again inside the atomic commit;
a model response cannot reserve emotional support that disappeared during the request.

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

## Orientation and decision evidence

Before genesis completes, a missing or unchanged source, or a known fetch failure, may yield to
internal attention, goal governance and autonomous discovery. Outcome evaluation can also settle
available results. Genesis completion and its evidence requirements are preserved; these internal
steps do not manufacture orientation cycles or skip sleep.

New locally rejected proposals save a private, versioned reason code and validation-time evidence
snapshot together with their failed-round closure. The receipt preserves the context hash and local
limits, contains no inferred historical explanation, and is excluded from subsequent attention
context so it cannot become its own trigger. No new database table or history rewrite is needed.
