from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from test_d2b_workflow_export_control import control as control
from test_i2_workflow_increment_control import context as context
from test_i2_workflow_increment_control import publish

CHILD = r"""
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import noyra
assert Path(noyra.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
# Simulated protocol capability; does not assert native Windows durability.
WorkflowArtifactStore.creation_supported=staticmethod(lambda:True)
subject='d2b-control'
genesis=content_hash({'subject_id':subject,'test':'p3-06'})
with SubjectKernel(sys.argv[2],subject,genesis,allow_subject_creation=False) as kernel:
    assert kernel.boot().state=='paused'
    if sys.argv[4]=='after_manifest':
        original=WorkflowIncrementStore.put
        def interrupted(self,name,data):
            result=original(self,name,data)
            if name=='manifest.enc':os._exit(86)
            return result
        WorkflowIncrementStore.put=interrupted
    elif sys.argv[4]=='after_acceptance_receipt':
        original=WorkflowIncrementCatalog._append
        def interrupted(self,c,event_id,kind,payload):
            original(self,c,event_id,kind,payload)
            if kind=='workflow_increment_accepted':os._exit(86)
        WorkflowIncrementCatalog._append=interrupted
        WorkflowIncrementStore.flush=lambda self:True
    else:
        def no_write(self,*args,**kwargs):raise AssertionError('accepted rewrite')
        WorkflowIncrementStore.put=no_write
    result=OperatorControlService(kernel).export_workflow_increment(
        'export-workflow',sys.argv[3],6,'increment-original-request',
        actor='operator',reason='Synthetic child publication')
    assert kernel.lifecycle.current().state=='paused'
    print(json.dumps(result))
"""


@pytest.mark.parametrize("mode", ["after_manifest", "after_acceptance_receipt", "accepted_read"])
def test_real_process_exit_keeps_preparation_recoverable_without_duplicate_acceptance(
    context: tuple[OperatorControlService, str],
    mode: str,
) -> None:
    controls, base = context
    if mode == "accepted_read":
        with patch.object(WorkflowIncrementStore, "flush", return_value=True):
            publish(context)
    path = controls.database.path
    controls.kernel.close()
    source = Path(__file__).resolve().parents[1] / "src"
    child = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-c", CHILD, str(source), str(path), base, mode],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=path.parent,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert child.returncode == (0 if mode == "accepted_read" else 86), child.stderr
    if mode == "accepted_read":
        result = json.loads(child.stdout)
        assert result["idempotent"] and result["readback_verified"]
    with SubjectKernel(
        path,
        "d2b-control",
        content_hash({"subject_id": "d2b-control", "test": "p3-06"}),
        allow_subject_creation=False,
    ) as reopened:
        assert reopened.boot().state == "paused"
        fresh = OperatorControlService(reopened)
        before = fresh.workflow_increments()["items"][0]
        assert before["status"] == ("accepted" if mode == "accepted_read" else "prepared")
        with patch.object(WorkflowIncrementStore, "flush", return_value=True):
            result = publish((fresh, base))
        assert result["status"] == "accepted" and result["source_history_preserved"]
        with reopened.database.read_transaction() as c:
            assert (
                c.execute("SELECT COUNT(*) FROM workflow_increment_publications").fetchone()[0] == 1
            )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
                ).fetchone()[0]
                == 1
            )
            assert (
                c.execute(
                    "SELECT COUNT(*) FROM audit_records WHERE action='workflow_increment_accepted'"
                ).fetchone()[0]
                == 1
            )
            assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 6
            assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
