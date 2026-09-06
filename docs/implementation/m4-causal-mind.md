# M4 Causal Mind State

## Purpose

M4 gives Noyra durable private state whose changes have explicit causes. It does not claim that
the resulting system is conscious. The measurable claim is narrower: events, appraisals, affect,
goals, beliefs, memories, relationships, and later behavior can form an inspectable causal chain.

## State flow

```text
event -> appraisal -> affect transition -> goal pressure/candidate -> mood -> psychology snapshot
```

One transaction commits the complete flow. A subject/event processing key makes event replay
idempotent. Reusing the same key with different input is rejected, and a failed proposal rolls the
whole experience back.

## Affect

Emotion names are open text rather than an approved enum. Anger, disappointment, curiosity, joy,
or a more specific learned label use the same validated structure: intensity, valence, arousal,
dominance, decay, target, and goal effect. The runtime does not suppress a negative label.

Affect is not cosmetic. A transition targeted at a goal changes that goal's durable emotional
pressure and therefore its selection score. Sufficient negative pressure moves an active goal into
`reconsidering`. As the causal emotion decays, its contribution to goal pressure recedes. A goal
candidate from Noyra must cite a matching affect transition above its declared threshold.

Human proposals are different: they enter `proposed`, never `active`. Only the subject-side method
can accept them as candidates and later activate them. Supplying `actor="human"` is rejected.

## Memory and belief integrity

Memories, beliefs, goals, relationships, causal links, appraisals, affect transitions, and
psychological snapshots retain append-only history. SQLite triggers reject deletion or rewriting of
ledger rows. Mutable current-state rows carry deterministic content/state hashes, and optimistic
revision numbers reject stale writers.

Belief revision requires supporting or counterevidence from this subject's event ledger. Memory and
relationship revisions likewise require source events. No delete API is provided; obsolete state is
qualified, retracted, superseded, archived, achieved, or abandoned with a reason.

## Resource bounds

An appraisal can commit at most 32 distinct affect components and eight goal candidates. Evidence
lists, text fields, relationship boundaries, causal metadata, and retrieval result counts are
bounded. Affect decay ignores negligible changes and collapses very small intensity to zero. These
limits keep adversarial or malformed cognition output from causing unbounded work per event.

## Privacy boundary

This module exposes internal stores to the Supervisor only. No public endpoint is added. Public
diary and behavior-log projection will be implemented separately and must never return appraisal
narratives, private memories, complete beliefs, or psychological snapshots.
