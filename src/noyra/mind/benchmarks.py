"""Pinned, reproducible memory-retrieval benchmark helpers.

The runner deliberately measures a supplied retriever instead of embedding a
particular provider.  A benchmark file carries its source URL and SHA-256 so
external public datasets can be pinned without treating an unexecuted remote
run as local evidence.
"""

from __future__ import annotations

import hashlib
import os
import time
import tracemalloc
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Generic, TypeVar
from urllib.error import URLError
from urllib.request import Request, urlopen

from noyra.core.types import canonical_json, content_hash, strict_json_loads

T = TypeVar("T")
MAX_BENCHMARK_BYTES = 8_000_000


@dataclass(frozen=True)
class MemoryBenchmarkCase:
    case_id: str
    query: str
    relevant_terms: tuple[str, ...]
    limit: int


@dataclass(frozen=True)
class MemoryBenchmarkScore:
    dataset_id: str
    dataset_hash: str
    case_count: int
    precision_at_k: float
    recall_at_k: float
    mean_reciprocal_rank: float
    hit_rate: float
    elapsed_ms: float
    peak_bytes: int
    cost_units: int

    def public(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash,
            "case_count": self.case_count,
            "precision_at_k": self.precision_at_k,
            "recall_at_k": self.recall_at_k,
            "mean_reciprocal_rank": self.mean_reciprocal_rank,
            "hit_rate": self.hit_rate,
            "elapsed_ms": self.elapsed_ms,
            "peak_bytes": self.peak_bytes,
            "cost_units": self.cost_units,
        }


@dataclass(frozen=True)
class MemoryBenchmarkCurvePoint:
    corpus_size: int
    score: MemoryBenchmarkScore

    def public(self) -> dict[str, Any]:
        return {"corpus_size": self.corpus_size, **self.score.public()}


class PinnedMemoryBenchmark(Generic[T]):
    """Run a pinned set of query/relevance cases against a bounded retriever."""

    FORMAT = "noyra-memory-benchmark/v1"

    def __init__(
        self,
        *,
        dataset_id: str,
        source_url: str,
        source_revision: str,
        raw_dataset: dict[str, Any],
        cases: tuple[MemoryBenchmarkCase, ...],
        quality_floor: dict[str, float],
    ):
        if not dataset_id.strip() or not source_url.strip() or not source_revision.strip():
            raise ValueError("benchmark provenance is required")
        if not cases:
            raise ValueError("benchmark must contain at least one case")
        self.dataset_id = dataset_id
        self.source_url = source_url
        self.source_revision = source_revision
        self.raw_dataset = raw_dataset
        self.cases = cases
        self.quality_floor = quality_floor
        self.dataset_hash = content_hash(raw_dataset)

    @classmethod
    def load(cls, path: Path | str) -> PinnedMemoryBenchmark[Any]:
        source = Path(path)
        with source.open("rb") as stream:
            payload = stream.read(MAX_BENCHMARK_BYTES + 1)
        return cls._load_payload(payload, str(source))

    @classmethod
    def fetch(
        cls,
        url: str,
        *,
        expected_sha256: str,
        timeout_seconds: float = 20.0,
        max_bytes: int = MAX_BENCHMARK_BYTES,
    ) -> PinnedMemoryBenchmark[Any]:
        """Fetch a pinned benchmark without treating network availability as evidence."""
        if not url.startswith("https://") or len(url) > 2_048:
            raise ValueError("benchmark URL must be bounded HTTPS")
        if (
            type(timeout_seconds) not in {int, float}
            or not 0 < float(timeout_seconds) <= 300
            or type(max_bytes) is not int
            or not 1 <= max_bytes <= MAX_BENCHMARK_BYTES
            or not isinstance(expected_sha256, str)
            or len(expected_sha256) != 64
            or any(character not in "0123456789abcdef" for character in expected_sha256)
        ):
            raise ValueError("benchmark fetch pin or limit is invalid")
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=float(timeout_seconds)) as response:
                payload = response.read(max_bytes + 1)
        except (OSError, URLError) as error:
            raise RuntimeError("benchmark download is unavailable") from error
        if len(payload) > max_bytes:
            raise ValueError("benchmark download exceeds its byte limit")
        actual = hashlib.sha256(payload).hexdigest()
        if actual != expected_sha256:
            raise ValueError("benchmark download hash does not match its pin")
        return cls._load_payload(payload, url)

    @classmethod
    def _load_payload(cls, payload: bytes, source: str) -> PinnedMemoryBenchmark[Any]:
        if len(payload) > MAX_BENCHMARK_BYTES:
            raise ValueError(f"benchmark exceeds byte limit: {source}")
        try:
            document = strict_json_loads(payload)
        except (TypeError, ValueError) as error:
            raise ValueError("memory benchmark is invalid JSON") from error
        if not isinstance(document, dict) or document.get("format") != cls.FORMAT:
            raise ValueError("memory benchmark format is unsupported")
        expected_sha = document.get("sha256")
        unsigned_document = dict(document)
        unsigned_document.pop("sha256", None)
        calculated_sha = hashlib.sha256(
            canonical_json(unsigned_document).encode("utf-8")
        ).hexdigest()
        if not isinstance(expected_sha, str) or calculated_sha != expected_sha:
            raise ValueError("memory benchmark file hash does not match its pin")
        dataset_id = document.get("dataset_id")
        source_url = document.get("source_url")
        source_revision = document.get("source_revision")
        if (
            not isinstance(dataset_id, str)
            or not dataset_id.strip()
            or not isinstance(source_url, str)
            or not source_url.startswith("https://")
            or not isinstance(source_revision, str)
            or not source_revision.strip()
        ):
            raise ValueError("memory benchmark provenance is invalid")
        cases_raw = document.get("cases")
        if not isinstance(cases_raw, list) or not 1 <= len(cases_raw) <= 10_000:
            raise ValueError("memory benchmark case list is invalid")
        cases: list[MemoryBenchmarkCase] = []
        for raw in cases_raw:
            if not isinstance(raw, dict):
                raise ValueError("memory benchmark case is invalid")
            case_id = raw.get("case_id")
            query = raw.get("query")
            terms = raw.get("relevant_terms")
            limit = raw.get("limit")
            if (
                not isinstance(case_id, str)
                or not case_id.strip()
                or not isinstance(query, str)
                or not query.strip()
                or not isinstance(terms, list)
                or not terms
                or not all(isinstance(term, str) and term.strip() for term in terms)
                or len(set(terms)) != len(terms)
                or type(limit) is not int
                or not 1 <= limit <= 32
            ):
                raise ValueError("memory benchmark case fields are invalid")
            cases.append(MemoryBenchmarkCase(case_id, query, tuple(terms), limit))
        floor = document.get("quality_floor")
        if not isinstance(floor, dict):
            raise ValueError("memory benchmark quality floor is missing")
        quality_floor = {
            key: float(floor[key])
            for key in ("precision_at_k", "recall_at_k", "mean_reciprocal_rank", "hit_rate")
            if key in floor
        }
        if set(quality_floor) != {
            "precision_at_k",
            "recall_at_k",
            "mean_reciprocal_rank",
            "hit_rate",
        } or any(not 0 <= value <= 1 for value in quality_floor.values()):
            raise ValueError("memory benchmark quality floor is invalid")
        return cls(
            dataset_id=dataset_id,
            source_url=source_url,
            source_revision=source_revision,
            raw_dataset=document,
            cases=tuple(cases),
            quality_floor=quality_floor,
        )

    def run(
        self,
        retrieve: Callable[[str, int], Sequence[T]],
        *,
        key: Callable[[T], str],
        relevant: Callable[[MemoryBenchmarkCase, T], bool] | None = None,
        cost_units_per_query: int = 1,
    ) -> MemoryBenchmarkScore:
        if type(cost_units_per_query) is not int or cost_units_per_query < 0:
            raise ValueError("benchmark cost units are invalid")
        matcher = relevant or self._term_matcher
        tracemalloc.start()
        started = time.perf_counter()
        precision_total = 0.0
        recall_total = 0.0
        reciprocal_total = 0.0
        hits = 0
        try:
            for case in self.cases:
                results = tuple(retrieve(case.query, case.limit))
                if len(results) > case.limit:
                    raise ValueError("retriever exceeded requested benchmark limit")
                result_keys = [key(item) for item in results]
                if any(not isinstance(item, str) or not item for item in result_keys):
                    raise ValueError("retriever returned an invalid benchmark key")
                relevant_count = sum(matcher(case, item) for item in results)
                denominator = max(1, len(results))
                precision_total += relevant_count / denominator
                recall_total += min(1.0, relevant_count / len(case.relevant_terms))
                first_rank = next(
                    (rank for rank, item in enumerate(results, start=1) if matcher(case, item)),
                    None,
                )
                if first_rank is not None:
                    hits += 1
                    reciprocal_total += 1 / first_rank
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            elapsed_ms = (time.perf_counter() - started) * 1_000
            tracemalloc.stop()
        count = len(self.cases)
        return MemoryBenchmarkScore(
            self.dataset_id,
            self.dataset_hash,
            count,
            round(precision_total / count, 6),
            round(min(1.0, recall_total / count), 6),
            round(reciprocal_total / count, 6),
            round(hits / count, 6),
            round(elapsed_ms, 3),
            int(peak_bytes),
            len(self.cases) * cost_units_per_query,
        )

    def run_curve(
        self,
        points: Iterable[tuple[int, Callable[[str, int], Sequence[T]]]],
        *,
        key: Callable[[T], str],
        relevant: Callable[[MemoryBenchmarkCase, T], bool] | None = None,
        max_latency_ms: float | None = None,
        max_peak_bytes: int | None = None,
    ) -> dict[str, Any]:
        curve: list[MemoryBenchmarkCurvePoint] = []
        seen_sizes: set[int] = set()
        for corpus_size, retrieve in points:
            if type(corpus_size) is not int or corpus_size < 1:
                raise ValueError("benchmark corpus size is invalid")
            if corpus_size in seen_sizes:
                raise ValueError("benchmark curve corpus sizes must be unique")
            seen_sizes.add(corpus_size)
            score = self.run(retrieve, key=key, relevant=relevant)
            curve.append(MemoryBenchmarkCurvePoint(corpus_size, score))
        if not curve:
            raise ValueError("benchmark curve requires at least one point")
        quality_passed = all(
            all(
                getattr(point.score, metric) >= threshold
                for metric, threshold in self.quality_floor.items()
            )
            for point in curve
        )
        latency_passed = max_latency_ms is None or all(
            point.score.elapsed_ms <= max_latency_ms for point in curve
        )
        memory_passed = max_peak_bytes is None or all(
            point.score.peak_bytes <= max_peak_bytes for point in curve
        )
        return {
            "version": "noyra-memory-curve/v1",
            "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash,
            "source_url": self.source_url,
            "source_revision": self.source_revision,
            "quality_floor": self.quality_floor,
            "points": [point.public() for point in curve],
            "thresholds": {
                "max_latency_ms": max_latency_ms,
                "max_peak_bytes": max_peak_bytes,
            },
            "quality_passed": quality_passed,
            "latency_passed": latency_passed,
            "memory_passed": memory_passed,
            "passed": quality_passed and latency_passed and memory_passed,
            "curve_hash": content_hash([point.public() for point in curve]),
        }

    @staticmethod
    def write_report(path: Path | str, report: dict[str, Any]) -> str:
        """Atomically persist a canonical score/curve report and return its hash."""
        if not isinstance(report, dict) or not report:
            raise ValueError("benchmark report must be a nonempty object")
        payload = canonical_json(report).encode("utf-8")
        if len(payload) > MAX_BENCHMARK_BYTES:
            raise ValueError("benchmark report exceeds byte limit")
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            temporary.write_bytes(payload)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return hashlib.sha256(payload).hexdigest()

    @staticmethod
    def _term_matcher(case: MemoryBenchmarkCase, item: Any) -> bool:
        content = getattr(getattr(item, "memory", item), "content", item)
        if not isinstance(content, str):
            return False
        normalized = content.casefold()
        return any(term.casefold() in normalized for term in case.relevant_terms)

    def metadata(self) -> dict[str, Any]:
        return {
            "format": self.FORMAT,
            "dataset_id": self.dataset_id,
            "source_url": self.source_url,
            "source_revision": self.source_revision,
            "dataset_hash": self.dataset_hash,
            "quality_floor": self.quality_floor,
            "case_count": len(self.cases),
            "metadata_hash": content_hash(canonical_json(self.raw_dataset)),
        }
