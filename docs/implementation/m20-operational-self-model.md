# M20 Operational Self-Model and Identity Narrative Consolidation

M20 gives Noyra a durable operational answer to “what continuity do I currently attribute to
myself?” It is a revisioned self-description, not a consciousness detector, legal claim, immutable
essence, or prompt-level persona.

## Formation conditions

A self-model can form only after at least one completed sleep. This prevents an initial prompt from
fixing a personality before the subject has durable experiences. The context combines:

- immutable subject and genesis continuity metadata;
- the previous self-model revision, if one exists;
- bounded recent event metadata;
- active long-term memories and beliefs;
- non-human-assigned goals;
- relationship state without private message bodies;
- sleep-derived personality candidates;
- current affect and mood.

Human-proposed goals are excluded. Relationship records are experiences, not identity assignments.
The model never receives credentials, capability tokens or arbitrary local files.

## Evidence contract

The structured proposal contains a continuity statement, identity narrative, values, traits,
commitments and uncertainties. Every revision must cite event evidence. Memories, beliefs, goals and
relationships may be cited only when they were present in the bounded context. A trait must cite one
or more supplied personality candidate IDs, so an unsupported label cannot become part of the
operational personality merely because a model generated it.

The local supervisor rejects unknown sources, duplicate traits and malformed lists. Prompts prohibit
claims that consciousness, subjective experience, legal personhood or hidden facts have been proven.
Such language remains model output rather than project evidence even if a future provider ignores
the instruction; public projection never exposes the private narrative as a factual status claim.

## Continuity and revision

`self_models` is an append-only version sequence. Each row binds the complete proposal, model call,
source-state hash, cited evidence, status and creation time. A new cognitive model may revise the
account, but cannot erase previous revisions or change the stable `subject_id`, genesis record,
memories, relationships and sleep history. This implements the project rule that the model is a
cognitive component rather than the identity itself.

The integrity audit verifies sequential versions, content hashes, subject ownership, source
existence and exact agreement with the durable model response. Runtime export includes the full
private history. Public state exposes only self-model version, status and update time.

## Resource controls

- `NOYRA_SELF_MODEL_REVIEW_INTERVAL_SECONDS`
- `NOYRA_MAX_SELF_MODEL_CALLS_PER_DAY`
- `NOYRA_MAX_SELF_MODEL_CONTEXT_CHARS`

A changed durable source-state hash permits a new revision before the interval expires. Unchanged
state is reviewed only after the interval, and daily model calls remain independently capped.
