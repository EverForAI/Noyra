# M6 Fatigue, Sleep, and Recovery

M6 turns resource exhaustion and sleep into durable protocols. Fatigue is not a display-only
number: it is derived from resource pressure, cognitive load, frustration, goal conflict, and
information staleness, and its mode changes which actions are allowed.

## Fatigue

`FatigueTracker` stores the current state plus an append-only transition ledger. Hard resource
pressure is deliberately represented separately from emotional frustration. A daily model budget
can therefore force a sleep recommendation even after ordinary fatigue has been restored; the
budget itself is owned by the model ledger and is not reset by sleep.

### Pool samples and the legacy hard-pressure signal

`resource_pool_pressures` contains per-subject pressure samples, not the model-group configuration
registry. An empty table for a subject does not itself trigger sleep: new fatigue state starts with
`resource_pressure = 0` and `fatigue = 0`. The hard-budget rule is evaluated only after the separate
aggregate `fatigue_states.resource_pressure` reaches `1`.

| Aggregate pressure | Pool samples for this subject | Hard-budget recommendation |
| --- | --- | --- |
| Below `1` | Empty, healthy or exhausted samples | None from this rule |
| `1` | Empty | Retain the legacy single-pool exhaustion signal |
| `1` | At least one sampled pool below `1` | Continue available work; no global budget sleep |
| `1` | Every sampled pool at `1` | Recommend budget sleep |

This empty-sample fallback preserves callers using a single model gateway, where an actual ledger
budget denial reports aggregate pressure without independent pool samples. Treating empty samples
as healthy would discard that known hard-budget signal. Conversely, empty samples must not be used
to manufacture an exhaustion assessment during startup. Other fatigue, frustration, conflict and
staleness rules remain independent of this table.

The service samples routed model pools before a cognition cycle; embedding accounting updates its
own sample. Configuring a model group alone does not insert a pressure sample. A fresh runtime can
therefore reach its active sampling hook even with configured but not-yet-sampled groups. Individual
samples are upserted, and passing an empty update does not clear previously recorded samples.

Budget-triggered autonomous sleep schedules waking at the next UTC budget day. A completed budget
sleep clears the aggregate hard-pressure signal only after that day actually changes. Sleep and
wake never erase paid attempts or grant new model capacity: the ledger still determines whether
work can resume. Existing per-pool samples are not reset by this operation; the next active hook can
refresh them. Same-day early wake does not reset the daily budget. Normal fatigue restoration and
the other lifecycle/admission checks still apply.

The deterministic first-version weighting is:

```text
100 * (0.45 resource + 0.20 cognitive + 0.20 frustration
       + 0.10 goal conflict + 0.05 staleness)
```

The persisted fatigue cannot jump downward during ordinary assessment. Sleep restoration is a
separate recorded transition. Modes are `active` (0-39), `saving` (40-69), `conservative`
(70-89), `winding_down` (90-99), and `sleeping` (100).

## Sleep state machine

```text
active -> winding_down -> reflective_sleep -> deep_sleep -> waking -> active
```

The lifecycle transition and sleep-run record are committed in one SQLite transaction. Sleep
cannot begin while an action is prepared or executing, and the action ledger refuses new external
actions during winding down, reflection, deep sleep, and waking. The model gateway refuses calls in
deep sleep and waking; reflective sleep remains the explicit phase in which a model may propose a
structured reflection.

Every run has an append-only transition history, a versioned state hash, and at most one active run
per subject. Reflection is persisted as private data and includes the exact validated plan JSON,
so the integrity audit can detect changes to both the projection and its source plan.

## Reflection integration

`SleepReflectionPlan` is a strict boundary for cognition output. It may add reflection memories,
revise beliefs and goals through existing revision protocols, create retry barriers only for at
least two matching failed or unknown actions, and record personality candidates supported by at
least three causal sources. Any invalid target, evidence list, transition, or source causes the
entire reflection transaction to roll back, including its event.

Retry barriers are not permanent permissions or deletions. A matching action is refused until a
later event, recorded after the barrier's event boundary, explicitly releases it. This prevents a
failed strategy from retrying forever while still allowing new evidence to reopen the question.

## Deep sleep and restart

Entering deep sleep creates a normal subject checkpoint and updates identity continuity in the same
transaction as the lifecycle transition. Waking restores fatigue according to elapsed sleep while
leaving hard model budgets untouched. Restart recovery preserves an in-progress sleep state instead
of converting it to active work, so a process restart during deep sleep cannot trigger cognition or
repeat an external action.

M6 does not claim that a reflection proves consciousness. It provides a durable, inspectable
mechanism for memory consolidation, goal reconsideration, prediction-error notes, and candidate
personality formation that later modules can expose through a privacy-filtered interface.
