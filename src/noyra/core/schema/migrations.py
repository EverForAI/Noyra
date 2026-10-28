"""Ordered historical migrations, collected without changing SQL or versions."""

from __future__ import annotations

from .migrations_v002_v005 import MIGRATIONS_V002_V005
from .migrations_v006_v017 import MIGRATIONS_V006_V017
from .migrations_v018_v021 import MIGRATIONS_V018_V021
from .migrations_v022_v034 import MIGRATIONS_V022_V034
from .migrations_v035_v042 import MIGRATIONS_V035_V042
from .migrations_v043_v052 import MIGRATIONS_V043_V052
from .migrations_v053_v054 import MIGRATIONS_V053_V054
from .migrations_v055_v069 import MIGRATIONS_V055_V069
from .migrations_v070_v107 import MIGRATIONS_V070_V107
from .world_poll import WORLD_POLL_SCHEMA

MIGRATIONS: dict[int, str] = {
    **MIGRATIONS_V002_V005,
    **MIGRATIONS_V006_V017,
    **MIGRATIONS_V018_V021,
    **MIGRATIONS_V022_V034,
    **MIGRATIONS_V035_V042,
    **MIGRATIONS_V043_V052,
    **MIGRATIONS_V053_V054,
    **MIGRATIONS_V055_V069,
    **MIGRATIONS_V070_V107,
    108: WORLD_POLL_SCHEMA,
    # v108 stays immutable: old installations must receive this guard through
    # a real additive migration, not a silently changed historical contract.
    109: """
CREATE TRIGGER IF NOT EXISTS prevent_world_feed_seen_update
BEFORE UPDATE ON world_feed_seen
BEGIN SELECT RAISE(ABORT,'world feed seen entries are immutable'); END;
""",
    110: """
CREATE INDEX IF NOT EXISTS idx_thought_agenda_revision_agenda
ON thought_agenda_revisions(agenda_id);
""",
    111: """
CREATE TABLE IF NOT EXISTS thought_round_closures (
    agenda_id TEXT NOT NULL REFERENCES thought_agenda_items(agenda_id),
    subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
    round_number INTEGER NOT NULL CHECK(round_number >= 0),
    reason TEXT NOT NULL CHECK(reason IN ('model_failed','proposal_rejected')),
    call_evidence_json TEXT NOT NULL,
    closed_at TEXT NOT NULL,
    retry_after TEXT NOT NULL,
    state_hash TEXT NOT NULL,
    PRIMARY KEY(agenda_id,round_number)
);
CREATE INDEX IF NOT EXISTS idx_thought_round_closures_subject
    ON thought_round_closures(subject_id,agenda_id,round_number);
CREATE TRIGGER IF NOT EXISTS validate_thought_round_closure_subject
BEFORE INSERT ON thought_round_closures
WHEN NOT EXISTS(SELECT 1 FROM thought_agenda_items a WHERE a.agenda_id=NEW.agenda_id
    AND a.subject_id=NEW.subject_id)
BEGIN SELECT RAISE(ABORT,'thought round closure subject mismatch'); END;
CREATE TRIGGER IF NOT EXISTS prevent_thought_round_closure_update
BEFORE UPDATE ON thought_round_closures
BEGIN SELECT RAISE(ABORT,'thought round closures are append-only'); END;
CREATE TRIGGER IF NOT EXISTS prevent_thought_round_closure_delete
BEFORE DELETE ON thought_round_closures
BEGIN SELECT RAISE(ABORT,'thought round closures are append-only'); END;
""",
}
