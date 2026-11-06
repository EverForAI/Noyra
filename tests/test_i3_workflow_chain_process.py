from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from test_d2b_workflow_export_control import control as control
from test_i2_workflow_increment_control import context as context
from test_i3_workflow_chain_control import chain as chain

CHILD = r"""
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import noyra
assert Path(noyra.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.core import workflow_increment_bundle
subject='d2b-control'
genesis=content_hash({'subject_id':subject,'test':'p3-06'})
with SubjectKernel(sys.argv[2],subject,genesis,allow_subject_creation=False) as kernel:
    assert kernel.boot().state=='paused'
    if sys.argv[4]=='during_sqlite_recheck':
        original=workflow_increment_bundle._verified_records
        calls=0
        def stopped(*args,**kwargs):
            global calls
            calls+=1
            if calls==2:os._exit(86)
            yield from original(*args,**kwargs)
        workflow_increment_bundle._verified_records=stopped
    result=OperatorControlService(kernel).verify_workflow_chain(sys.argv[3])
    assert kernel.lifecycle.current().state=='paused'
    print(json.dumps(result))
"""


@pytest.mark.parametrize("mode", ["read", "during_sqlite_recheck"])
def test_fresh_process_verification_and_mid_reconstruction_exit_leave_source_intact(
    chain: Any,
    mode: str,
) -> None:
    controls, _base, _first, tip = chain
    path = controls.database.path
    with controls.database.read_transaction() as c:
        before = {
            table: [dict(r) for r in c.execute("SELECT * FROM " + table)]
            for table in (
                "workflow_checkpoints",
                "workflow_increment_publications",
                "events",
                "audit_records",
            )
        }
    controls.kernel.close()
    source = Path(__file__).resolve().parents[1] / "src"
    result = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-c", CHILD, str(source), str(path), tip, mode],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=path.parent,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert result.returncode == (86 if mode == "during_sqlite_recheck" else 0), result.stderr
    if mode == "read":
        summary = json.loads(result.stdout)
        assert summary["records"] == 8 and summary["chain_nodes"] == 3
    with SubjectKernel(
        path,
        "d2b-control",
        content_hash({"subject_id": "d2b-control", "test": "p3-06"}),
        allow_subject_creation=False,
    ) as reopened:
        assert reopened.boot().state == "paused"
        summary = OperatorControlService(reopened).verify_workflow_chain(tip)
        assert summary["complete_history_verified"] and summary["records"] == 8
        with reopened.database.read_transaction() as c:
            assert before == {
                table: [dict(r) for r in c.execute("SELECT * FROM " + table)] for table in before
            }
            assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
