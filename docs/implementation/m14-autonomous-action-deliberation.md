# M14 Autonomous Action Deliberation

M14 closes the first safe goal-to-action loop. After genesis and goal governance, Noyra may choose
one read-only investigation that serves an existing active autonomous goal. The model proposes a
choice; local code owns authorization, execution, persistence, and recovery.

## Bounded loop

```text
active non-human goal
-> configured active public sources
-> current capability check
-> bounded private deliberation context
-> structured goal/source choice or deliberate wait
-> local evidence, ownership, limit, and concurrency validation
-> audited SafeWebReader action
-> durable observation and behavior log
-> later world cognition
```

The proposal contains only an existing goal ID, an existing source ID, a strategy label, an expected
observation, a reason, and supplied event IDs. It cannot carry a URL or arbitrary tool arguments.

## First-version action surface

M14 deliberately permits only `web_read` against a source that is simultaneously active in the
world registry and authorized by a current capability grant. It cannot write a file, execute a
shell, publish, send a message, access an account, transact, request a new capability, or change a
goal. Human messages and human-proposed goals do not enter action deliberation.

Each action carries the goal ID, a deterministic strategy ID, expected outcome, and idempotency key
into the existing action ledger. Successful content is recorded as a world observation for a later
cognition cycle. The behavior log exposes the public URL and safe goal reference without exposing
the private model reason or psychological context.

## Supervisor and loop controls

The Supervisor rejects unavailable goals or sources, forged evidence, revoked capabilities, open or
unknown actions, and exhausted per-goal daily limits. Remote calls have a separate daily limit and
feed existing fatigue and hard-budget behavior. The existing action ledger enforces retry blocks,
lifecycle sleep exclusion, action idempotency, interrupted-action quarantine, and behavior logging.

`action_deliberation_runs` is append-only. It binds the model call, goal, source, action,
observation, evidence, proposal hash, result hash, and state hash. It is a durable record of what was
chosen and what actually happened, without storing hidden chain-of-thought.
