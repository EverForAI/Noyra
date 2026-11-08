from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from test_d2b_workflow_export_control import control as control
from test_d2b_workflow_export_control import run

CHILD = r"""
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import noyra
assert Path(noyra.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.types import content_hash
subject='d2b-control'
genesis=content_hash({'subject_id':subject,'test':'p3-06'})
with SubjectKernel(sys.argv[2],subject,genesis) as kernel:
    assert kernel.boot().state=='paused'
    if sys.argv[3]=='exit_after_manifest':
        original=WorkflowArtifactStore.put
        def interrupted(self,name,payload):
            result=original(self,name,payload)
            if name=='manifest.enc':
                os._exit(86)
            return result
        WorkflowArtifactStore.put=interrupted
    result=OperatorControlService(kernel).export_workflow(
        'export-workflow',4,'synthetic-request',
        actor='operator',reason='Synthetic child export')
    assert kernel.lifecycle.current().state=='paused'
    print(json.dumps(result))
"""


@pytest.mark.parametrize("mode", ["exit_after_manifest", "read_published"])
def test_actual_process_exit_and_fresh_process_readback(
    control: OperatorControlService,
    mode: str,
) -> None:
    if mode == "read_published":
        run(control)
    path = control.database.path
    control.kernel.close()
    source = Path(__file__).resolve().parents[1] / "src"
    child = subprocess.run(
        [sys.executable, "-X", "utf8", "-B", "-c", CHILD, str(source), str(path), mode],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=path.parent,
        text=True,
        capture_output=True,
        timeout=90,
    )
    if mode == "exit_after_manifest":
        assert child.returncode == 86, child.stderr
    else:
        assert child.returncode == 0, child.stderr
        result = json.loads(child.stdout)
        assert result["readback_verified"] and result["idempotent"]
    with SubjectKernel(
        path, "d2b-control", content_hash({"subject_id": "d2b-control", "test": "p3-06"})
    ) as reopened:
        assert reopened.boot().state == "paused"
        recovered = run(OperatorControlService(reopened))
        assert recovered["readback_verified"] and recovered["source_history_preserved"]
        with reopened.database.read_transaction() as c:
            assert c.execute("SELECT COUNT(*) FROM workflow_export_publications").fetchone()[0] == 1
            assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 4
            assert c.execute("SELECT COUNT(*) FROM model_attempts").fetchone()[0] == 0
