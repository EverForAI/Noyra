# M22 Metacognitive Control and Cognitive Strategy Selection

M22 replaces the fixed post-genesis order of high-level cognition modules with a durable local
controller. The controller decides whether Noyra should think privately, gather evidence, perform
an authorized external evidence action, review
beliefs, reconsider goals, review a relationship, sleep or deliberately wait. It does not ask a
model what should run next and consumes no remote-model tokens itself.

## Strategy evidence

Each heartbeat derives bounded candidates from durable state:

- open intrinsic-thought agenda items;
- active or unsettled autonomous goals;
- analyzed observations awaiting epistemic review;
- established relationship pressure;
- fatigue, frustration, goal conflict, staleness and resource pressure;
- previous metacognitive choices and learned strategy outcomes.

Incoming human messages are not cognitive goals. They remain the responsibility of equal
interaction cognition and cannot force a selected strategy. Pending deterministic action or
research outcome evaluation causes the controller to wait for that evidence before starting
another high-level process.

## Local scoring and limits

Candidate scores combine current value, learned strategy confidence, fixation risk and expected
resource cost. Existing module interval and daily-call limits remain authoritative. A strategy is
eligible only when its underlying module is locally due and its prerequisites exist. High fatigue
or psychological pressure selects sleep directly instead of spending another model call.

Repeated selection of the same strategy raises fixation risk. Productive, stagnant and failed
outcomes update an append-only cognitive strategy profile. Outcomes are classified from durable
module records and actual model usage, never from a model's claim that its own reasoning helped.

## Persistence and privacy

`metacognitive_decisions`, `metacognitive_outcomes` and cognitive strategy profile revisions are
hashed durable histories. Public state exposes only the chosen strategy, reason code, score and
aggregate counts. Private uncertainty, fixation risk, resource pressure, outcome rationale and
token cost remain available only in the authenticated developer export and runtime database.

## Configuration

- `NOYRA_METACOGNITIVE_SLEEP_THRESHOLD`
- `NOYRA_METACOGNITIVE_SLEEP_SECONDS`
- `NOYRA_METACOGNITIVE_FIXATION_PENALTY`
- `NOYRA_METACOGNITIVE_PENDING_TIMEOUT_SECONDS`

A decision that remains unresolved beyond the pending timeout is durably recorded as
`unknown` with `workflow_timeout_quarantined`. This releases the metacognitive selector
without pretending that the workflow succeeded or failed.
