from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import canonical_json, content_hash
from noyra.mind.memory import MemoryStore


@dataclass(frozen=True)
class SyntheticProfile:
    name: str
    event_count: int
    memory_count: int
    payload_bytes: int
    batch_size: int

    def __post_init__(self) -> None:
        if self.event_count < 1:
            raise ValueError("synthetic event_count must be positive")
        if self.memory_count < 1:
            raise ValueError("synthetic memory_count must be positive")
        if self.payload_bytes < 64:
            raise ValueError("synthetic payload_bytes must be at least 64")
        if not 1 <= self.batch_size <= self.event_count:
            raise ValueError("synthetic batch_size must be within the event count")


@dataclass(frozen=True)
class SyntheticHistory:
    subject_id: str
    event_count: int
    memory_count: int
    first_event_id: str
    last_event_id: str
    logical_payload_bytes: int


def load_synthetic_profiles(path: Path) -> dict[str, SyntheticProfile]:
    raw: Any = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or raw.get("format_version") != 1:
        raise ValueError("unsupported synthetic profile manifest")
    items = raw.get("profiles")
    if not isinstance(items, list) or not items:
        raise ValueError("synthetic profile manifest must contain profiles")
    profiles: dict[str, SyntheticProfile] = {}
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("synthetic profile entry must be an object")
        profile = SyntheticProfile(
            name=str(item["name"]),
            event_count=int(item["event_count"]),
            memory_count=int(item["memory_count"]),
            payload_bytes=int(item["payload_bytes"]),
            batch_size=int(item["batch_size"]),
        )
        if profile.name in profiles:
            raise ValueError(f"duplicate synthetic profile: {profile.name}")
        profiles[profile.name] = profile
    return profiles


def _payload(seed: str, index: int, size: int) -> dict[str, object]:
    block = hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()
    body = (block * ((size // len(block)) + 1))[:size]
    return {"index": index, "body": body}


def populate_synthetic_history(
    database: Database,
    profile: SyntheticProfile,
    *,
    subject_id: str = "Noyra-m41-synthetic",
    seed: str = "m41-v1",
) -> SyntheticHistory:
    """Create deterministic, integrity-valid history using bounded transactions."""
    IdentityStore(database).ensure(subject_id, content_hash({"seed": seed}))
    events = EventStore(database)
    origin = datetime(2020, 1, 1, tzinfo=UTC)
    first_event_id = ""
    last_event_id = ""
    logical_payload_bytes = 0

    for start in range(0, profile.event_count, profile.batch_size):
        stop = min(start + profile.batch_size, profile.event_count)
        with database.transaction() as connection:
            for index in range(start, stop):
                event_id = f"evt_m41_{index:012d}"
                payload = _payload(seed, index, profile.payload_bytes)
                events._append_connection(
                    connection,
                    subject_id,
                    "m41_synthetic_observation",
                    "m41-fixture",
                    payload,
                    privacy_level="public",
                    causal_parent_ids=(),
                    occurred_at=(origin + timedelta(seconds=index)).isoformat(),
                    event_id=event_id,
                )
                first_event_id = first_event_id or event_id
                last_event_id = event_id
                logical_payload_bytes += profile.payload_bytes

    # Populate the memory and revision tables with the same deterministic source events.
    # This gives retrieval and export tests a realistic large history without remote embeddings.
    for start in range(0, profile.memory_count, profile.batch_size):
        stop = min(start + profile.batch_size, profile.memory_count)
        with database.transaction() as connection:
            for index in range(start, stop):
                memory_id = f"mem_m41_{index:012d}"
                memory_digest = hashlib.sha256(f"{seed}:memory:{index}".encode()).hexdigest()
                content = f"m41 synthetic memory {index} {memory_digest}"
                now = (origin + timedelta(seconds=index)).isoformat()
                salience = 0.5
                confidence = 0.75
                state_hash = MemoryStore._state_hash(content, salience, confidence, "active")
                connection.execute(
                    """INSERT INTO memories(
                        memory_id, subject_id, memory_type, content, content_hash, state_hash,
                        salience, confidence, privacy_level, status, current_revision,
                        created_at, updated_at
                    ) VALUES (?, ?, 'semantic', ?, ?, ?, ?, ?, 'private', 'active', 1, ?, ?)""",
                    (
                        memory_id,
                        subject_id,
                        content,
                        content_hash(content),
                        state_hash,
                        salience,
                        confidence,
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """INSERT INTO memory_revisions(
                        revision_id, memory_id, revision_number, content, content_hash, state_hash,
                        salience, confidence, status, reason, source_event_ids_json, created_at
                    ) VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'active', 'm41 synthetic fixture', ?, ?)""",
                    (
                        f"mrev_m41_{index:012d}",
                        memory_id,
                        content,
                        content_hash(content),
                        state_hash,
                        salience,
                        confidence,
                        canonical_json([f"evt_m41_{index % profile.event_count:012d}"]),
                        now,
                    ),
                )

    return SyntheticHistory(
        subject_id=subject_id,
        event_count=profile.event_count,
        memory_count=profile.memory_count,
        first_event_id=first_event_id,
        last_event_id=last_event_id,
        logical_payload_bytes=logical_payload_bytes,
    )
