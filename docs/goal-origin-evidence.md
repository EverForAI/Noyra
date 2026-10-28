# Goal origin evidence

Schema 106 gives each durable goal an immutable provenance witness covering
`goal_id`, `subject_id`, `origin`, `created_at`, and its evidence class. The witness
is checked whenever `GoalStore` loads a goal and by the `mind.state` integrity
check. A legal enum change from `human_proposal` to `self` is therefore detected,
even though the older mutable-state/revision hash format omitted origin.
SQLite also rejects changes to those identity and provenance fields. Normal
subject decisions can still accept proposals, activate goals, revise their
content or priority, and complete or abandon them through the existing rules.

`GoalRecord.origin_evidence` and diagnostic exports distinguish two durable
classes:

- `created`: the application recorded the witness in the same transaction that
  created the goal and its first revision after this upgrade.
- `legacy_baseline`: schema migration recorded the origin value present during
  upgrade. Old hashes never covered origin, so this does not prove its original
  ancestry or rule out earlier changes. Later acceptance or revision keeps this
  class; it cannot turn the baseline into creation-time proof.

Migration is atomic and leaves existing goal state hashes and revision rows
unchanged. It neither infers acceptance nor changes a goal's origin, status, or
behavior. If upgrade fails, the old schema and rows remain available for retry.
New inserts cannot request the legacy class. There is no administrative endpoint
for rewriting origin or changing its evidence class.

The evidence class describes the database record, not a guarantee that every
model inference about a purpose is correct. A synthetic in-memory `GoalRecord`
that did not load a durable witness defaults to `unverified`.
