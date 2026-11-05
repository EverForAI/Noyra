from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from noyra.cognition import CognitionSettings, ControlledSelfModification
from noyra.core import EventStore, SubjectKernel
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import canonical_json, content_hash


def test_simulated_proposal_with_changed_baseline_reaches_an_explained_terminal_state(
    tmp_path: Path,
) -> None:
    with SubjectKernel(
        tmp_path / "subject.sqlite3", "c1-selfmod", content_hash("selfmod")
    ) as kernel:
        kernel.boot()
        evidence = EventStore(kernel.database).append(
            kernel.subject_id, "metacognitive_outcome", "fixture", {}
        )
        original = ControlledSelfModification(
            kernel.database, kernel.subject_id, CognitionSettings()
        )
        proposal = original.propose(
            "thought_interval_seconds",
            1500.0,
            reason="Synthetic proposal",
            evidence_ids=(evidence.event_id,),
        )
        assert proposal.status == "simulated"
        changed = ControlledSelfModification(
            kernel.database, kernel.subject_id, CognitionSettings(thought_interval_seconds=2400)
        )
        assert changed.run_due() == "self_modification_expired"
        assert changed.get(proposal.proposal_id).status == "rejected"
        assert changed.effective("thought_interval_seconds") == 2400
        assert changed.run_due() is None
        with kernel.database.read_transaction() as connection:
            assert (
                connection.execute("SELECT COUNT(*) FROM self_modification_settings").fetchone()[0]
                == 0
            )
            assert (
                connection.execute("SELECT COUNT(*) FROM self_modification_revisions").fetchone()[0]
                == 0
            )


@pytest.fixture
def fixture(tmp_path: Path) -> Iterator[tuple[SubjectKernel, ControlledSelfModification, str]]:
    with SubjectKernel(
        tmp_path / "subject.sqlite3", "c1-expiry-matrix", content_hash("expiry")
    ) as kernel:
        kernel.boot()
        event = EventStore(kernel.database).append(
            kernel.subject_id, "metacognitive_outcome", "fixture", {}
        )
        manager = ControlledSelfModification(
            kernel.database, kernel.subject_id, CognitionSettings()
        )
        proposal = manager.propose(
            "thought_interval_seconds",
            1500.0,
            reason="Simulated change",
            evidence_ids=(event.event_id,),
        )
        yield kernel, manager, proposal.proposal_id


def changed(manager: ControlledSelfModification) -> ControlledSelfModification:
    return ControlledSelfModification(
        manager.database, manager.subject_id, CognitionSettings(thought_interval_seconds=2400)
    )


def rows(kernel: SubjectKernel) -> dict[str, list[tuple[Any, ...]]]:
    with kernel.database.read_transaction() as c:
        return {
            name: [tuple(row) for row in c.execute("SELECT * FROM " + name + " ORDER BY rowid")]
            for name in (
                "self_modification_proposals",
                "self_modification_settings",
                "self_modification_revisions",
            )
        }


def test_expiry_retains_original_validation_and_simulation_and_does_not_repeat(
    fixture: tuple[SubjectKernel, ControlledSelfModification, str],
) -> None:
    kernel, manager, identifier = fixture
    original = manager.get(identifier)
    replacement = changed(manager)
    result = replacement.apply(identifier)
    assert result.status == "rejected" and result.simulation == original.simulation
    assert result.validation == original.validation and result.old_value == original.old_value
    assert replacement.apply(identifier) == result
    event = EventStore(kernel.database).get("evt_selfmod_expired_" + identifier)
    assert event.payload["expected_value"] == 1800 and event.payload["observed_value"] == 2400
    assert event.payload["reason_code"] == "baseline_changed"
    assert replacement.verify_integrity()["self_modification_revisions"] == 0
    assert original.status == "simulated"
    new = replacement.propose(
        "thought_interval_seconds",
        2100.0,
        reason="Fresh baseline",
        evidence_ids=original.evidence_ids,
    )
    assert new.status == "simulated" and new.old_value == 2400
    assert replacement.run_due() == "self_modification_applied"
    assert replacement.effective("thought_interval_seconds") == 2100


@pytest.mark.parametrize("mode", ["apply", "expire"])
def test_parallel_workers_apply_or_expire_once_under_the_writer_transaction(
    fixture: tuple[SubjectKernel, ControlledSelfModification, str],
    mode: str,
) -> None:
    kernel, manager, identifier = fixture
    worker = manager if mode == "apply" else changed(manager)
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(lambda _: worker.apply(identifier), range(2)))
    assert {row.status for row in result} == ({"applied"} if mode == "apply" else {"rejected"})
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM self_modification_revisions").fetchone()[0] == (
            1 if mode == "apply" else 0
        )
        assert c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='self_modification_expired'"
        ).fetchone()[0] == (0 if mode == "apply" else 1)
    worker.verify_integrity()


@pytest.mark.parametrize("point", ["event", "status"])
def test_expiry_failure_rolls_back_event_and_status_then_retries_cleanly(
    fixture: tuple[SubjectKernel, ControlledSelfModification, str],
    point: str,
) -> None:
    kernel, manager, identifier = fixture
    worker = changed(manager)
    before = rows(kernel)
    target = (
        patch.object(
            EventStore, "_append_connection", side_effect=OSError("synthetic expiry failure")
        )
        if point == "event"
        else patch.object(worker, "_set_status", side_effect=OSError("synthetic expiry failure"))
    )
    with target, pytest.raises(OSError, match="synthetic expiry failure"):
        worker.apply(identifier)
    assert rows(kernel) == before
    with pytest.raises(NotFoundError):
        EventStore(kernel.database).get("evt_selfmod_expired_" + identifier)
    assert worker.apply(identifier).status == "rejected"
    worker.verify_integrity()


@pytest.mark.parametrize("damage", ["proposal", "setting_hash", "setting_value"])
def test_corruption_is_never_disguised_as_a_valid_baseline_expiry(
    fixture: tuple[SubjectKernel, ControlledSelfModification, str],
    damage: str,
) -> None:
    kernel, manager, identifier = fixture
    with kernel.database.transaction() as c:
        if damage == "proposal":
            c.execute(
                "UPDATE self_modification_proposals SET state_hash=? WHERE proposal_id=?",
                ("0" * 64, identifier),
            )
        else:
            value = "NaN" if damage == "setting_value" else canonical_json(2400.0)
            c.execute(
                "INSERT INTO self_modification_settings VALUES(?,?,?,?,?,?,?)",
                (
                    kernel.subject_id,
                    "thought_interval_seconds",
                    value,
                    identifier,
                    1,
                    "0" * 64,
                    "2026-11-06T00:00:00+00:00",
                ),
            )
    before = rows(kernel)
    with pytest.raises(IntegrityError):
        changed(manager).apply(identifier)
    assert rows(kernel) == before
    with kernel.database.read_transaction() as c:
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='self_modification_expired'"
            ).fetchone()[0]
            == 0
        )


def test_expiry_is_subject_scoped_and_configuration_restoration_cannot_revive_it(
    fixture: tuple[SubjectKernel, ControlledSelfModification, str],
) -> None:
    kernel, manager, identifier = fixture
    worker = changed(manager)
    assert worker.run_due() == "self_modification_expired"
    assert manager.apply(identifier).status == "rejected"
    assert manager.effective("thought_interval_seconds") == 1800
    outsider = ControlledSelfModification(kernel.database, "different-subject", CognitionSettings())
    with pytest.raises(ValueError, match="unavailable"):
        outsider.apply(identifier)
    assert worker.verify_integrity()["self_modification_revisions"] == 0


def test_stored_effective_setting_keeps_precedence_over_configuration_baseline(
    fixture: tuple[SubjectKernel, ControlledSelfModification, str],
) -> None:
    kernel, manager, identifier = fixture
    first = manager.apply(identifier)
    assert first.status == "applied"
    restarted = changed(manager)
    assert restarted.effective("thought_interval_seconds") == 1500
    assert restarted.apply(identifier).status == "applied"
    with kernel.database.read_transaction() as c:
        assert c.execute("SELECT COUNT(*) FROM self_modification_revisions").fetchone()[0] == 1
        assert (
            c.execute(
                "SELECT COUNT(*) FROM events WHERE event_type='self_modification_expired'"
            ).fetchone()[0]
            == 0
        )
