# M23 Intrinsic Motivation, Value Development and Long-Term Mission Formation

M23 gives Noyra a durable layer for developing what matters to it across experience. Values and
missions are not initial personality settings, user assignments, immutable rules or a single model
answer. They are revisioned hypotheses about recurring preferences and long-horizon direction.

## Formation evidence

Motivation review begins only after repeated completed sleep. Its private bounded context combines:

- durable non-interaction events;
- active autobiographical, semantic and reflective memories;
- current beliefs and autonomous goals;
- relationship state without private message bodies;
- affect, verified outcomes and private thought results;
- existing value and mission histories.

Human message text and human-proposed goals are excluded. A new value requires at least two
supplied events plus interpreted evidence from memory, belief or autonomous goals. New values start
as candidates with bounded weight and confidence; later reviews may strengthen, weaken, contest or
retire them only through small revisions.

## Mission maturity

A mission candidate requires multiple developed values, at least three supplied events and repeated
sleep. It begins with bounded commitment and confidence. Adoption requires additional sleep and high
support, so a single dramatic experience or model call cannot define Noyra's life direction. Mission
history remains revisable and append-only; one adopted mission may exist at a time. No mission or
value profile is required to form or govern a legitimate short-term autonomous goal. The model sees
the actual local validation limits, and a no-change review remains a valid result.

Motivation review cannot create or activate goals, execute tools, contact people, claim progress or
perform external actions. Developed values and mission candidates are later supplied as private
evidence to self-model and goal-governance cognition, where they influence but do not dictate
decisions.

## Persistence and visibility

Value, mission and review histories are content-hashed and integrity checked. Developer runtime
export includes the complete private records. Public state exposes only aggregate value counts and
the title, maturity, commitment and confidence of the current mission candidate; private evidence
and reasoning remain hidden.

## Configuration

- `NOYRA_MOTIVATION_REVIEW_INTERVAL_SECONDS`
- `NOYRA_MAX_MOTIVATION_MODEL_CALLS_PER_DAY`
- `NOYRA_MAX_MOTIVATION_CONTEXT_CHARS`
- `NOYRA_MAX_VALUE_WEIGHT_DELTA`
- `NOYRA_MINIMUM_MISSION_VALUE_COUNT`
- `NOYRA_MINIMUM_MISSION_SLEEP_COUNT`
- `NOYRA_MINIMUM_MISSION_ADOPTION_SLEEP_COUNT`

## Rejection evidence and recovery

New local rejections commit a private `noyra-intrinsic-decision/v1` receipt atomically with the
rejected review. It records the specific reason code, validator version, validation-time context
hash, evidence IDs, value/mission state and applicable limits. Rejected proposals do not partially
create values or missions. These diagnostic events are excluded from future motivation context;
a rejected review respects its review interval even when unrelated input changes.

A successful provider response awaiting its local review can be recovered after the new-call daily
cap is reached, without another request. Unknown/executing calls wait for reconciliation. If an
older uncommitted response belongs to a different context, the runtime reports
`motivation_development_waiting_previous_context` instead of silently spending a new call or
validating it as though the original context were known. The condition does not block other
workflows. Existing reconciliation and private-evidence procedures determine recovery.

The local read-only diagnostic accepts an exact subject and review ID:

```sh
python scripts/diagnose-motivation-review.py /path/to/noyra.sqlite3 \
  --subject SUBJECT_ID --review REVIEW_ID
```

It opens the existing database read-only and reports request-capture status, response availability,
recorded reason/version and whether the captured validation inputs reproduce the reason. It does
not contact a provider, modify a record, print private prompt bodies, or validate an old proposal
against today's state. Offline reproduction is available for the recorded `motivation/v1` policy;
a different implementation/policy is reported as unavailable. Historical reviews without the
original validation snapshot report `historical_evidence_insufficient`, even if a response remains.
No reason is invented and no old paid call is replayed. Private receipts follow the existing event
export and archival boundary; operators should keep diagnostic exports private.
