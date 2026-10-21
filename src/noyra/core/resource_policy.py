"""Versioned resource limit switches, independent of historical group hashes."""

from __future__ import annotations

from typing import Any

from .errors import IntegrityError
from .types import content_hash

LIMIT_DIMENSIONS = ("attempts", "input_tokens", "output_tokens", "cost")


def resource_policy_hash(row: Any) -> str:
    if (
        type(row["revision"]) is not int
        or row["revision"] < 1
        or row["pricing_status"] not in ("unknown", "priced", "free")
        or any(
            type(row[name + "_enabled"]) is not int or row[name + "_enabled"] not in (0, 1)
            for name in LIMIT_DIMENSIONS
        )
    ):
        raise IntegrityError("invalid resource limit policy")
    return content_hash(
        {
            "group_id": row["group_id"],
            "subject_id": row["subject_id"],
            "enabled": {name: bool(row[name + "_enabled"]) for name in LIMIT_DIMENSIONS},
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "pricing_status": row["pricing_status"],
        }
    )


def read_resource_policy(connection: Any, group_id: str, subject_id: str) -> dict[str, bool]:
    row = connection.execute(
        "SELECT * FROM cognitive_resource_limit_policies WHERE group_id=? AND subject_id=?",
        (group_id, subject_id),
    ).fetchone()
    if row is None or resource_policy_hash(row) != row["state_hash"]:
        raise IntegrityError("cognitive resource limit policy mismatch")
    latest = connection.execute(
        "SELECT state_hash FROM cognitive_resource_limit_revisions WHERE group_id=? "
        "ORDER BY revision DESC LIMIT 1",
        (group_id,),
    ).fetchone()
    if latest is None or latest[0] != row["state_hash"]:
        raise IntegrityError("cognitive resource limit revision mismatch")
    return {name: bool(row[name + "_enabled"]) for name in LIMIT_DIMENSIONS}


def write_resource_policy(
    connection: Any,
    group_id: str,
    subject_id: str,
    enabled: dict[str, bool],
    *,
    actor: str,
    reason: str,
    now: str,
    pricing_status: str | None = None,
) -> None:
    if set(enabled) != set(LIMIT_DIMENSIONS) or any(type(v) is not bool for v in enabled.values()):
        raise ValueError("invalid resource limit switches")
    previous = connection.execute(
        "SELECT revision, pricing_status FROM cognitive_resource_limit_policies WHERE group_id=?",
        (group_id,),
    ).fetchone()
    revision = 1 if previous is None else int(previous[0]) + 1
    if pricing_status is None:
        prices = connection.execute(
            "SELECT input_microusd_per_million, output_microusd_per_million "
            "FROM cognitive_resource_groups WHERE group_id=?",
            (group_id,),
        ).fetchone()
        pricing_status = (
            "priced"
            if any(prices)
            else (previous["pricing_status"] if previous is not None else "unknown")
        )
    if pricing_status not in ("unknown", "priced", "free"):
        raise ValueError("invalid resource pricing status")
    values = {
        "group_id": group_id,
        "subject_id": subject_id,
        "revision": revision,
        "updated_at": now,
        "pricing_status": pricing_status,
        **{name + "_enabled": int(enabled[name]) for name in LIMIT_DIMENSIONS},
    }
    digest = resource_policy_hash(values)
    connection.execute(
        "INSERT INTO cognitive_resource_limit_policies VALUES(?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(group_id) DO UPDATE SET attempts_enabled=excluded.attempts_enabled, "
        "input_tokens_enabled=excluded.input_tokens_enabled, "
        "output_tokens_enabled=excluded.output_tokens_enabled, cost_enabled=excluded.cost_enabled, "
        "revision=excluded.revision, updated_at=excluded.updated_at, "
        "state_hash=excluded.state_hash, pricing_status=excluded.pricing_status",
        (
            group_id,
            subject_id,
            *(int(enabled[name]) for name in LIMIT_DIMENSIONS),
            revision,
            now,
            digest,
            pricing_status,
        ),
    )
    connection.execute(
        "INSERT INTO cognitive_resource_limit_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            group_id,
            revision,
            subject_id,
            *(int(enabled[name]) for name in LIMIT_DIMENSIONS),
            now,
            digest,
            actor,
            reason,
            pricing_status,
        ),
    )


MIGRATION_V81 = """
CREATE TABLE cognitive_resource_groups_v81 (
 group_id TEXT PRIMARY KEY,
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 pool TEXT NOT NULL CHECK(pool IN ('economy','deep')), label TEXT NOT NULL,
 provider_type TEXT NOT NULL CHECK(provider_type='openai_compatible'),
 base_url TEXT NOT NULL, model TEXT NOT NULL,
 priority INTEGER NOT NULL CHECK(priority >= 0 AND priority <= 1000),
 weight INTEGER NOT NULL CHECK(weight >= 1 AND weight <= 1000),
 daily_attempts INTEGER NOT NULL CHECK(daily_attempts >= 0),
 daily_input_tokens INTEGER NOT NULL CHECK(daily_input_tokens >= 0),
 daily_output_tokens INTEGER NOT NULL CHECK(daily_output_tokens >= 0),
 daily_cost_microusd INTEGER NOT NULL CHECK(daily_cost_microusd >= 0),
 input_microusd_per_million INTEGER NOT NULL CHECK(input_microusd_per_million >= 0),
 output_microusd_per_million INTEGER NOT NULL CHECK(output_microusd_per_million >= 0),
 max_attempts INTEGER NOT NULL CHECK(max_attempts >= 1 AND max_attempts <= 10),
 status TEXT NOT NULL CHECK(status IN ('active','disabled','revoked')),
 state_hash TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
INSERT INTO cognitive_resource_groups_v81(
 group_id, subject_id, pool, label, provider_type, base_url, model,
 priority, weight, daily_attempts, daily_input_tokens, daily_output_tokens,
 daily_cost_microusd, input_microusd_per_million, output_microusd_per_million,
 max_attempts, status, state_hash, created_at, updated_at
)
SELECT group_id, subject_id, pool, label, provider_type, base_url, model,
 priority, weight, daily_attempts, daily_input_tokens, daily_output_tokens,
 daily_cost_microusd, input_microusd_per_million, output_microusd_per_million,
 max_attempts, status, state_hash, created_at, updated_at
FROM cognitive_resource_groups;
DROP TABLE cognitive_resource_groups;
ALTER TABLE cognitive_resource_groups_v81 RENAME TO cognitive_resource_groups;
CREATE INDEX idx_cognitive_resource_group_pool
 ON cognitive_resource_groups(subject_id, pool, status, priority);
CREATE UNIQUE INDEX idx_cognitive_resource_live_label
 ON cognitive_resource_groups(subject_id, pool, label) WHERE status != 'revoked';
CREATE TABLE IF NOT EXISTS cognitive_resource_limit_policies (
 group_id TEXT PRIMARY KEY REFERENCES cognitive_resource_groups(group_id),
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 attempts_enabled INTEGER NOT NULL CHECK(attempts_enabled IN (0,1)),
 input_tokens_enabled INTEGER NOT NULL CHECK(input_tokens_enabled IN (0,1)),
 output_tokens_enabled INTEGER NOT NULL CHECK(output_tokens_enabled IN (0,1)),
 cost_enabled INTEGER NOT NULL CHECK(cost_enabled IN (0,1)),
 revision INTEGER NOT NULL CHECK(revision >= 1), updated_at TEXT NOT NULL, state_hash TEXT NOT NULL,
 pricing_status TEXT NOT NULL CHECK(pricing_status IN ('unknown','priced','free'))
);
CREATE TABLE IF NOT EXISTS cognitive_resource_limit_revisions (
 group_id TEXT NOT NULL REFERENCES cognitive_resource_groups(group_id),
 revision INTEGER NOT NULL CHECK(revision >= 1),
 subject_id TEXT NOT NULL REFERENCES subject_identity(subject_id),
 attempts_enabled INTEGER NOT NULL CHECK(attempts_enabled IN (0,1)),
 input_tokens_enabled INTEGER NOT NULL CHECK(input_tokens_enabled IN (0,1)),
 output_tokens_enabled INTEGER NOT NULL CHECK(output_tokens_enabled IN (0,1)),
 cost_enabled INTEGER NOT NULL CHECK(cost_enabled IN (0,1)),
 updated_at TEXT NOT NULL, state_hash TEXT NOT NULL, actor TEXT NOT NULL, reason TEXT NOT NULL,
 pricing_status TEXT NOT NULL CHECK(pricing_status IN ('unknown','priced','free')),
 PRIMARY KEY(group_id, revision)
);
CREATE TRIGGER IF NOT EXISTS prevent_resource_limit_revision_update
BEFORE UPDATE ON cognitive_resource_limit_revisions BEGIN
 SELECT RAISE(ABORT, 'resource limit revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_resource_limit_revision_delete
BEFORE DELETE ON cognitive_resource_limit_revisions BEGIN
 SELECT RAISE(ABORT, 'resource limit revisions are immutable'); END;
CREATE TRIGGER IF NOT EXISTS prevent_resource_limit_policy_delete
BEFORE DELETE ON cognitive_resource_limit_policies BEGIN
 SELECT RAISE(ABORT, 'resource limit policy cannot be deleted'); END;
"""
