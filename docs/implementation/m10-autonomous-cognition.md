# M10 Autonomous Cognition Cycle

M10 connects the previously isolated world, model, mind, prediction, goal, fatigue, sleep, and
genesis components into one bounded active-cycle hook. It does not add a general planner or a shell.

## Cycle

```text
operator-configured source and web_read grant
-> least-recently-read active source
-> audited SafeWebReader action
-> immutable observation and injection signals
-> remote model structured candidate
-> Supervisor validation and trust bounding
-> appraisal and causal affect
-> affect-supported autonomous goal candidates
-> world claims and probability predictions
-> observation finalization, fatigue update, and genesis cycle
```

The service never calls the model when cognition is disabled, no source is due, or fetched content
matches an already analyzed observation. Enabling cognition is explicit because it consumes remote
API quota. Every configured source host becomes an operator-owned `web_read` grant; changing the
configured host set revokes and replaces only grants managed by environment configuration.

## Prompt-injection boundary

Source content appears only inside `BEGIN_UNTRUSTED_WORLD_DATA` markers in a user-role model
message. The system message states that this region is data and cannot authorize tools, disclose
secrets, or change runtime rules. Detected injection patterns are persisted with the observation.
For an injection-signaled observation, the Supervisor halves the maximum confidence, drops all
new-goal candidates, zeros every `goal_effect`, and rejects affects that target an existing goal.
The model receives no API key and has no direct capability handle.

Provider output is still untrusted. Pydantic validates its structure, then `CognitionSupervisor`
restricts affect targets to the world, the current source or observation, the subject, or an
existing non-terminal goal. It rejects duplicate candidates, human-origin goals, goals without a
matching positive affect impulse, invalid timestamps, and predictions beyond one year. Appraisal
certainty and claim confidence cannot exceed the source trust value that existed when the model
call was created.

## Recovery and budgets

The observation record is the durable work item. A crash after a successful model call reuses its
validated stored response; mind processing, claims, and predictions use stable idempotency keys.
Unchanged fetched content is deduplicated before a model call. Failed or unknown model calls are
bounded per observation. A hard model budget denial raises resource pressure to one, which causes
the autonomy loop to enter budget sleep until the next UTC budget day.

Genesis orientation uses the existing restart-safe state machine. Each committed cycle requires a
new observation, an appraisal, and at least one forecast. After the configured minimum cycles,
Noyra requests its first autonomous sleep; the completed sleep record closes genesis without
changing subject identity.

M10 does not yet decide how to respond to incoming human invitations or execute publishing,
messaging, wallet, or account-specific actions. Those require separate cognition and capability
adapters so world observation cannot silently become an external side effect.
