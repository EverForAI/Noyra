from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from noyra.core import SubjectKernel
from noyra.core.actions import ActionLedger
from noyra.core.types import content_hash


def test_action_prepare_guard_is_atomic_default_noop_and_rolls_back_on_refusal(
    tmp_path: Path,
) -> None:
    with SubjectKernel(
        tmp_path / "action.sqlite3", "action-guard", content_hash("action-guard")
    ) as kernel:
        kernel.boot()
        kernel.orient()
        kernel.activate()
        ledger = ActionLedger(kernel.database)
        seen = []

        def guard(connection: Any) -> None:
            assert connection.in_transaction
            seen.append(connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0])
            raise RuntimeError("admission refused")

        with pytest.raises(RuntimeError, match="admission refused"):
            ledger.prepare(
                kernel.subject_id,
                "inspection",
                "fixture",
                "local",
                {},
                idempotency_key="fixture",
                before_prepare=guard,
            )
        assert seen == [0]
        with kernel.database.read_transaction() as connection:
            assert connection.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
        original = ledger.prepare(
            kernel.subject_id, "inspection", "fixture", "local", {}, idempotency_key="fixture"
        )
        assert original.status == "prepared"
        with pytest.raises(RuntimeError):
            ledger.prepare(
                kernel.subject_id,
                "inspection",
                "fixture",
                "local",
                {},
                idempotency_key="fixture",
                before_prepare=guard,
            )
        with kernel.database.read_transaction() as connection:
            rows = connection.execute("SELECT * FROM actions").fetchall()
            assert len(rows) == 1 and rows[0]["status"] == "prepared"
        assert (
            ledger.prepare(
                kernel.subject_id, "inspection", "fixture", "local", {}, idempotency_key="fixture"
            )
            == original
        )
