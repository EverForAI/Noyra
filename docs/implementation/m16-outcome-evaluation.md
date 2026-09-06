# M16 - Outcome Evaluation and Strategy Learning

## Purpose

M16 closes the loop between autonomous goals, research, external actions, and later strategy
selection. It deliberately does not ask a model whether a task succeeded. Outcome classification is
derived from durable local facts so a model cannot fabricate progress or reward its own proposal.

## Outcome Rules

Action outcomes are classified as follows:

- `progress`: a succeeded action produced a new observation that later completed local analysis;
- `no_change`: the action succeeded but the observation was unchanged;
- `failure`: the action ledger and deliberation record report failure;
- `unknown`: completion evidence is unavailable or ambiguous.

Research outcomes are classified separately:

- `informative`: one or more candidate sources were registered;
- `no_change`: the bounded search produced no candidate sources;
- `failure`: the durable research run failed.

Candidate source discovery never advances goal progress. Only a locally analyzed new observation can
do so,
and each evaluation is capped by `NOYRA_MAX_GOAL_PROGRESS_DELTA_PER_OUTCOME`. Progress is capped below
completion; achieving a goal remains an explicit goal-governance decision with separate evidence.

## Strategy Profiles

Each `(goal, strategy)` pair receives an append-only revision history containing attempts,
successful or informative outcomes, failures, inconclusive outcomes, last outcome, and confidence.
Confidence uses a bounded smoothed evidence ratio. It is supplied to later research and action
deliberation contexts as historical evidence, not as an instruction.

Strategies are keyed by the action strategy ID or by a hash of the research goal, query hash, and
final method. Raw research queries remain private.

## Atomic Commit

One database transaction creates:

1. a private `outcome_evaluated` event;
2. an optional bounded goal revision when new observation evidence exists;
3. a strategy profile revision;
4. an immutable outcome evaluation.

Restarting after commit cannot evaluate the same source twice because `(subject, source type,
source ID)` is unique. Stale or human-proposed goals are excluded from autonomous learning.

## Public Projection

`GET /api/outcomes` and the dashboard `学习` tab expose only goal title, strategy kind, outcome,
bounded progress/confidence changes, and a public summary. Private rationale codes, evidence IDs,
strategy IDs, observation IDs, and internal event links are withheld.

## Configuration

- `NOYRA_OUTCOME_EVALUATION_INTERVAL_SECONDS`
- `NOYRA_MAX_GOAL_PROGRESS_DELTA_PER_OUTCOME`

Outcome evaluation itself consumes no remote-model calls.

## Audit

Windows:

```powershell
./scripts/audit-outcome-learning.ps1
```

Ubuntu:

```bash
./scripts/audit-outcome-learning.sh
```
