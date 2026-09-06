from __future__ import annotations

import tempfile
from pathlib import Path

from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import content_hash


def test_historical_causal_timestamp_anomaly_is_reported() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "Noyra-event-anomaly"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": "event-anomaly"}))
        events = EventStore(database)
        parent = events.append(
            subject_id,
            "historical-parent",
            "import",
            {"value": 1},
            occurred_at="2026-08-14T12:00:00+00:00",
        )
        child = events.append(
            subject_id,
            "historical-child",
            "import",
            {"value": 2},
            causal_parent_ids=(parent.event_id,),
            occurred_at="2026-08-14T11:00:00+00:00",
        )
        anomalies = events.causal_anomalies(subject_id)
        assert anomalies == [
            {
                "event_id": child.event_id,
                "parent_event_id": parent.event_id,
                "event_occurred_at": "2026-08-14T11:00:00.000+00:00",
                "parent_occurred_at": "2026-08-14T12:00:00.000+00:00",
            }
        ]
