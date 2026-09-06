# M12 Autonomous Reflective Sleep

M12 replaces the service's count-only sleep summary with bounded remote-model reflection over a
durable experience window. The existing `SleepEngine` remains the only component allowed to commit
reflection effects. The model proposes a `SleepReflectionPlan`; it never writes memories, goals,
beliefs, retry barriers, personality candidates, diary entries, or lifecycle state directly.

## Frozen reflection window

The first sleep uses identity creation as its lower boundary. Later sleeps start after the previous
completed wake. Every window ends at the current sleep run's `started_at`, so events arriving while
Noyra is winding down or sleeping wait for the next reflection. Retries and process restarts rebuild
the same bounded window.

The context contains at most a small selected set of:

- event IDs, types, timestamps, payload key names, and payload hashes, but not event payload values;
- appraisals and current affect;
- non-terminal goals and non-retracted beliefs;
- high-salience private memories and recent interaction text;
- current world claims and predictions;
- failed or uncertain actions eligible for retry analysis;
- earlier personality candidates.

Strings and list counts are clipped, then whole low-priority records are removed until the serialized
context fits `NOYRA_MAX_SLEEP_CONTEXT_CHARS`. The complete JSON is one `DATA>` line inside
`BEGIN_UNTRUSTED_REFLECTION_CONTEXT`; stored text and human messages therefore remain data even if
they contain marker text or apparent instructions.

This context is private from the operator-facing dashboard but is sent to the configured remote
model provider. It intentionally omits event payload values, API keys, capability handles, action
inputs, and filesystem file contents. Deployment must use a provider whose privacy and retention
terms are acceptable for this exposure.

## Local validation and atomic integration

Before `SleepEngine` sees a proposal, the reflection supervisor verifies:

- every new memory cites only an event present in the frozen window;
- each goal or belief appears once and was present in the supplied context;
- every goal transition follows the deterministic goal state machine;
- a belief revision supplies at least one in-window supporting or counter event;
- retry barriers cite distinct, supplied failed actions whose tool, target, goal, and strategy match;
- personality candidates cite at least three distinct in-window events.

`SleepEngine.commit_reflection` then revalidates ownership and causal references and applies all
effects in one SQLite transaction. Any failure rolls back the reflection event and every integration.
Personality output remains a candidate, not an editable or immediately adopted persona. Public diary
text remains a private candidate until a separate subject-owned publication action exists.

## Recovery and bounded failure

The model call purpose and idempotency key are tied to the sleep ID. A crash after a successful call
reuses the stored structured response. Invalid successful responses are skipped and privately
audited. Provider, schema, or local-validation failures can consume at most
`NOYRA_MAX_SLEEP_MODEL_CALLS_PER_RUN` calls.

When that limit is reached, or the hard remote-model budget denies the call, M12 returns a
deterministic no-change reflection containing only counts. This deliberately prefers losing one
opportunity for integration over leaving the same identity permanently trapped in reflective sleep.
Deep sleep, checkpointing, waking, and fatigue restoration continue through the existing restart-safe
sleep state machine.
