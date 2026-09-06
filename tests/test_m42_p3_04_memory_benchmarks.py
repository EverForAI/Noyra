from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import noyra.mind.benchmarks as benchmark_module
from noyra.core import Database, EventStore, IdentityStore
from noyra.core.types import content_hash
from noyra.mind import MemoryStore, PinnedMemoryBenchmark

FIXTURE = Path(__file__).parent / "fixtures" / "benchmarks" / "memory_locomo_pinned_v1.json"


def test_pinned_memory_fixture_has_provenance_and_reproducible_score(tmp_path: Path) -> None:
    benchmark = PinnedMemoryBenchmark.load(FIXTURE)
    database = Database(tmp_path / "noyra.sqlite3")
    subject_id = "Noyra-p304-benchmark"
    IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
    event = EventStore(database).append(subject_id, "p304_fixture", "test", {"kind": "memory"})
    store = MemoryStore(database)
    records = (
        ("amber lighthouse", "The amber lighthouse journal was marked amber."),
        ("winter archive protocol", "The team rehearsed the winter archive protocol."),
        ("observatory key", "The observatory key was kept in a sealed drawer."),
    )
    for _marker, content in records:
        store.create(
            subject_id,
            "semantic",
            content,
            salience=0.8,
            confidence=0.9,
            source_event_ids=(event.event_id,),
        )
    for index in range(24):
        store.create(
            subject_id,
            "episodic",
            f"Unrelated benchmark distractor {index}.",
            salience=0.2,
            confidence=0.5,
            source_event_ids=(event.event_id,),
        )

    def retrieve(query: str, limit: int) -> list[Any]:
        return store.recall(
            subject_id,
            query,
            context_type="p304",
            context_id=f"query-{query}",
            limit=limit,
            record_access=False,
        )

    try:
        first = benchmark.run(retrieve, key=lambda item: item.memory.memory_id)
        second = benchmark.run(retrieve, key=lambda item: item.memory.memory_id)
        assert benchmark.metadata()["source_url"].startswith("https://")
        assert benchmark.metadata()["source_revision"] == "pinned-fixture-v1"
        assert first.dataset_hash == second.dataset_hash
        assert first.precision_at_k >= benchmark.quality_floor["precision_at_k"]
        assert first.recall_at_k >= benchmark.quality_floor["recall_at_k"]
        assert first.mean_reciprocal_rank >= benchmark.quality_floor["mean_reciprocal_rank"]
        assert first.hit_rate == 1.0

        curve = benchmark.run_curve(
            ((32, retrieve), (64, retrieve)),
            key=lambda item: item.memory.memory_id,
            max_latency_ms=2_000,
            max_peak_bytes=10_000_000,
        )
        assert curve["passed"] is True
        assert curve["quality_passed"] is True
        assert len(curve["points"]) == 2
        assert len(curve["curve_hash"]) == 64
    finally:
        del database


def test_external_pin_and_report_persistence_are_bounded(tmp_path: Path, monkeypatch: Any) -> None:
    payload = FIXTURE.read_bytes()

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *_: object) -> None:
            return None

        def read(self, _limit: int) -> bytes:
            return payload

    monkeypatch.setattr(benchmark_module, "urlopen", lambda *_args, **_kwargs: Response())
    benchmark = PinnedMemoryBenchmark.fetch(
        "https://example.test/locomo.json",
        expected_sha256=hashlib.sha256(payload).hexdigest(),
    )
    report = {"dataset": benchmark.metadata(), "passed": True}
    report_hash = PinnedMemoryBenchmark.write_report(tmp_path / "report.json", report)
    assert json.loads((tmp_path / "report.json").read_text(encoding="utf-8")) == report
    assert report_hash == hashlib.sha256((tmp_path / "report.json").read_bytes()).hexdigest()
