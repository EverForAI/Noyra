from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from noyra.core import SubjectKernel
from noyra.core import integrity as integrity_module
from noyra.core.integrity import (
    IntegrityCheckOutcome,
    IntegrityCheckSpec,
    IntegrityRegistry,
    IntegrityRuntimeController,
)
from noyra.core.types import content_hash


@pytest.fixture
def kernel(tmp_path: Path) -> Iterator[SubjectKernel]:
    instance = SubjectKernel(tmp_path / "noyra.sqlite3", "b3-evidence", content_hash("b3-evidence"))
    instance.boot()
    instance.orient()
    instance.activate()
    try:
        yield instance
    finally:
        instance.close()


def registry(outcome: IntegrityCheckOutcome | None = None) -> IntegrityRegistry:
    return IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.evidence",
                1,
                "test",
                frozenset({"startup_light", "periodic_deep", "manual"}),
                lambda _context: outcome or IntegrityCheckOutcome(),
                "light",
            ),
        )
    )


def test_atomic_json_keeps_a_verified_generation_when_legacy_digest_publication_fails(
    tmp_path: Path,
) -> None:
    path = tmp_path / "evidence.json"
    integrity_module._atomic_json(path, {"generation": 1})
    original = os.replace

    def fail_digest(source: Path, destination: Path) -> None:
        if str(destination).endswith(".sha256"):
            raise OSError("synthetic digest publication interruption")
        original(source, destination)

    with patch("os.replace", side_effect=fail_digest):
        integrity_module._atomic_json(path, {"generation": 2})
    assert integrity_module._read_hashed_json(path) == {"generation": 2}


def test_no_completed_integrity_check_is_not_reported_as_ok(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert controller.summary()["status"] == "not_checked"


def test_missing_current_state_digest_does_not_become_a_clean_report(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    controller.run_startup()
    # Simulate the historical two-file format, without its required checksum.
    import json

    controller._state_path.write_text(
        json.dumps({"subject_id": kernel.subject_id}), encoding="utf-8"
    )
    controller._state_path.with_suffix(".json.sha256").unlink()
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert reopened.summary()["status"] == "evidence_unavailable"


def test_current_finding_survives_failure_of_the_derived_latest_report(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    controller.run_startup()
    controller.registry = registry(
        IntegrityCheckOutcome("corrupt", "p0", "synthetic_current_corruption")
    )
    original = integrity_module._atomic_json

    def fail_latest(path: Path, payload: object) -> None:
        if path == controller._latest_path:
            raise OSError("synthetic latest projection failure")
        original(path, cast(Mapping[str, Any], payload))

    with patch.object(integrity_module, "_atomic_json", side_effect=fail_latest):
        controller.run_periodic_if_due(force=True)
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert reopened.summary()["status"] == "corrupt"
    assert reopened.summary()["p0"] == 1


def test_current_corruption_is_not_cleared_by_an_unrelated_clean_shard(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    bad = registry(IntegrityCheckOutcome("corrupt", "p0", "current_corruption"))
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=bad)
    controller.run_startup()
    clean = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.other",
                1,
                "test",
                frozenset({"periodic_deep", "manual", "startup_light"}),
                lambda _context: IntegrityCheckOutcome(),
                "light",
            ),
            bad.checks[0],
        )
    )
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=clean)
    report = reopened.run_periodic_if_due(force=True)
    assert report is not None
    assert reopened.summary()["p0"] == 1
    assert reopened.summary()["status"] == "corrupt"


def test_legacy_sidecar_pair_remains_readable_after_marker_upgrade(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    report = controller.run_startup()
    assert report is not None
    controller._state_path.with_suffix(".json.sha256").unlink()
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert reopened.latest_report is not None
    assert reopened.summary()["status"] == "ok"
    assert reopened.summary()["evidence"]["source"] == "commit"


def write_legacy(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")
    path.with_suffix(path.suffix + ".sha256").write_text(content_hash(payload), encoding="ascii")


def test_real_legacy_pair_requires_its_checksum_and_new_marker_never_falls_back(
    tmp_path: Path,
) -> None:
    from noyra.core.integrity_evidence import COMMIT_KEY, read_evidence

    path = tmp_path / "legacy.json"
    write_legacy(path, {"legacy": True})
    assert read_evidence(path).payload == {"legacy": True}
    assert read_evidence(path).source == "legacy_sidecar"
    path.with_suffix(".json.sha256").unlink()
    result = read_evidence(path)
    assert result.status == "evidence_unavailable" and result.payload is None
    integrity_module._atomic_json(path, {"current": True})
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored[COMMIT_KEY]["payload_hash"] = "0" * 64
    path.write_text(json.dumps(stored), encoding="utf-8")
    path.with_suffix(".json.sha256").write_text(content_hash(stored), encoding="ascii")
    assert read_evidence(path).status == "corrupt" and read_evidence(path).payload is None


@pytest.mark.parametrize("damage", ["invalid_json", "duplicate", "array", "oversized", "nan"])
def test_invalid_evidence_is_not_accepted_even_with_a_plausible_legacy_checksum(
    tmp_path: Path, damage: str
) -> None:
    from noyra.core.integrity_evidence import MAX_EVIDENCE_BYTES, read_evidence

    path = tmp_path / "bad.json"
    raw = {
        "invalid_json": b"{",
        "duplicate": b'{"x":1,"x":1}',
        "array": b"[]",
        "oversized": b" " * (MAX_EVIDENCE_BYTES + 1),
        "nan": b'{"x":NaN}',
    }[damage]
    path.write_bytes(raw)
    path.with_suffix(".json.sha256").write_text("0" * 64, encoding="ascii")
    assert read_evidence(path).status == "corrupt" and read_evidence(path).payload is None


def test_actual_process_exit_after_single_commit_keeps_new_generation_readable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "kill.json"
    integrity_module._atomic_json(path, {"generation": 1})
    program = """
import os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import noyra
assert Path(noyra.__file__).resolve().is_relative_to(Path(sys.argv[1]))
from noyra.core.integrity_evidence import atomic_json
original=os.replace
def cut(source,destination):
    original(source,destination)
    if Path(destination)==Path(sys.argv[2]):
        os._exit(73)
os.replace=cut
atomic_json(Path(sys.argv[2]),{'generation':2})
"""
    result = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            program,
            str(Path(__file__).resolve().parents[1] / "src"),
            str(path),
        ],
        timeout=15,
        check=False,
    )
    assert result.returncode == 73
    assert integrity_module._read_hashed_json(path) == {"generation": 2}


def test_failure_before_replacement_preserves_old_generation(tmp_path: Path) -> None:
    path = tmp_path / "before.json"
    integrity_module._atomic_json(path, {"generation": 1})
    with (
        patch("os.replace", side_effect=OSError("synthetic replace failure")),
        pytest.raises(OSError),
    ):
        integrity_module._atomic_json(path, {"generation": 2})
    assert integrity_module._read_hashed_json(path) == {"generation": 1}
    assert not list(tmp_path.glob("*.tmp"))


def test_corrupt_authoritative_state_cannot_use_old_clean_projection_as_current(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    report = controller.run_startup()
    assert report is not None and report.status == "ok"
    controller._state_path.write_bytes(b'{"broken":')
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert reopened.summary()["status"] == "evidence_unavailable"
    assert reopened.summary()["evidence"]["recheck_pending"]
    assert reopened.summary()["p0"] == 0
    retried = reopened.run_periodic_if_due(force=True)
    assert retried is not None and retried.profile == "manual"
    assert reopened.summary()["status"] == "ok"
    assert not reopened.summary()["evidence"]["recheck_pending"]


def test_incomplete_reconstruction_survives_restart_and_cannot_be_cleared_by_a_partial_shard(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    controller.run_startup()
    controller._state_path.write_bytes(b"invalid")
    limited = registry(IntegrityCheckOutcome("incomplete", "p1", "synthetic_unavailable"))
    first = IntegrityRuntimeController(kernel, tmp_path, registry=limited)
    report = first.run_periodic_if_due(force=True)
    assert report is not None and report.profile == "manual"
    assert first.summary()["evidence"]["recheck_pending"]
    second = IntegrityRuntimeController(kernel, tmp_path, registry=limited)
    assert second.summary()["evidence"]["recheck_pending"]
    assert second.summary()["coverage"]["status"] == "not_checked"


def test_authoritative_record_keeps_known_p0_when_latest_projection_was_overwritten_green(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    clean = controller.run_startup()
    assert clean is not None
    controller.registry = registry(IntegrityCheckOutcome("corrupt", "p0", "real_current_finding"))
    controller.run_periodic_if_due(force=True)
    integrity_module._atomic_json(controller._latest_path, clean.to_dict())
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert reopened.latest_report is not None and reopened.latest_report.status == "corrupt"
    assert reopened.summary()["p0"] == 1


def test_startup_coverage_does_not_claim_deep_only_checks_were_run(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    checks = (
        *registry().checks,
        IntegrityCheckSpec(
            "test.deep",
            1,
            "test",
            frozenset({"manual", "periodic_deep"}),
            lambda _context: IntegrityCheckOutcome(),
            "deep",
        ),
    )
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=IntegrityRegistry(checks))
    controller.run_startup()
    coverage = controller.summary()["coverage"]
    assert coverage["status"] == "partial" and coverage["remaining"] == ["test.deep"]
    assert controller.summary()["status"] == "ok"  # Only the observed scope is clean.


def test_invalid_current_report_cannot_fall_back_to_derived_green(
    kernel: SubjectKernel, tmp_path: Path
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    controller.run_startup()
    state = integrity_module._read_hashed_json(controller._state_path)
    assert isinstance(state, dict)
    state["last_report"]["summary"]["checks"] = 999
    integrity_module._atomic_json(controller._state_path, state)
    reopened = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    assert reopened.latest_report is None
    assert reopened.summary()["status"] == "evidence_unavailable"


def test_state_repair_does_not_clear_proven_corruption_from_an_unchecked_domain(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    deep = IntegrityCheckSpec(
        "test.deep",
        1,
        "test",
        frozenset({"manual", "periodic_deep"}),
        lambda _context: IntegrityCheckOutcome("corrupt", "p0", "deep_corruption"),
        "deep",
    )
    selected = IntegrityRegistry((*registry().checks, deep))
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=selected)
    controller._state["next_cursor"] = 1
    controller.run_periodic_if_due(force=True)
    assert controller.summary()["p0"] == 1
    controller._state_path.write_text("broken", encoding="utf-8")
    repaired = IntegrityRuntimeController(kernel, tmp_path, registry=selected)
    assert repaired.summary()["p0"] == 1
    assert repaired.summary()["evidence"]["recheck_pending"]
    result = repaired.run_periodic_if_due(force=True)
    assert result is not None and result.profile == "manual"
    assert repaired.summary()["p0"] == 1


def test_failed_authority_publication_is_visible_and_does_not_publish_a_clean_projection(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    controller.run_startup()
    before = controller._latest_path.read_bytes()
    original = integrity_module._atomic_json

    def fail_state(path: Path, payload: Mapping[str, Any]) -> None:
        if path == controller._state_path:
            raise OSError("synthetic state commit failure")
        original(path, payload)

    with (
        patch.object(integrity_module, "_atomic_json", side_effect=fail_state),
        pytest.raises(OSError),
    ):
        controller.run_periodic_if_due(force=True)
    assert controller._latest_path.read_bytes() == before
    assert controller.summary()["status"] == "evidence_unavailable"
    assert controller.summary()["evidence"]["recheck_pending"]


def test_incomplete_full_recheck_resumes_as_bounded_shards_and_eventually_finishes(
    kernel: SubjectKernel,
    tmp_path: Path,
) -> None:
    calls = []

    def slow_first(context: Any) -> IntegrityCheckOutcome:
        calls.append("one")
        return (
            IntegrityCheckOutcome("incomplete", "p1", "synthetic_limit")
            if len(calls) == 1
            else IntegrityCheckOutcome()
        )

    checks = IntegrityRegistry(
        (
            IntegrityCheckSpec(
                "test.one",
                1,
                "test",
                frozenset({"manual", "periodic_deep", "startup_light"}),
                slow_first,
                "light",
            ),
            IntegrityCheckSpec(
                "test.two",
                1,
                "test",
                frozenset({"manual", "periodic_deep"}),
                lambda c: IntegrityCheckOutcome(),
                "deep",
            ),
        )
    )
    previous = IntegrityRuntimeController(kernel, tmp_path, registry=registry())
    previous.run_startup()
    previous._state_path.write_text("broken", encoding="utf-8")
    controller = IntegrityRuntimeController(kernel, tmp_path, registry=checks)
    first = controller.run_periodic_if_due(force=True)
    assert first is not None and len(first.checks) == 2
    assert controller.summary()["evidence"]["recheck_pending"]
    resumed = IntegrityRuntimeController(kernel, tmp_path, registry=checks)
    second = resumed.run_periodic_if_due(force=True)
    assert second is not None and len(second.checks) == 1 and second.checks[0].id == "test.one"
    assert not resumed.summary()["evidence"]["recheck_pending"]
    assert (
        resumed.summary()["status"] == "ok"
        and resumed.summary()["coverage"]["status"] == "complete"
    )


def test_unreadable_evidence_is_not_misclassified_as_subject_corruption(tmp_path: Path) -> None:
    from noyra.core.integrity_evidence import read_evidence

    if os.name != "posix" or getattr(os, "geteuid", lambda: 0)() == 0:
        pytest.skip("requires non-root POSIX permissions")
    path = tmp_path / "private.json"
    integrity_module._atomic_json(path, {"private": True})
    path.chmod(0)
    try:
        assert read_evidence(path).status == "evidence_unavailable"
    finally:
        path.chmod(0o600)
