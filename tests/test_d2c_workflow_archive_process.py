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
from noyra.core.workflow_export_files import WorkflowArtifactStore
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
from noyra.core.types import content_hash
from noyra.core import workflow_reconstruction
subject='d2b-control'
genesis=content_hash({'subject_id':subject,'test':'p3-06'})
with SubjectKernel(sys.argv[2],subject,genesis) as kernel:
    assert kernel.boot().state=='paused'
    if sys.argv[4]=='exit_during_reconstruction':
        def stopped(connection):
            os._exit(86)
            yield
        workflow_reconstruction._records=stopped
    result=OperatorControlService(kernel).verify_workflow_export(sys.argv[3])
    assert kernel.lifecycle.current().state=='paused'
    print(json.dumps(result))
"""


@pytest.mark.parametrize("mode", ["read_accepted", "exit_during_reconstruction"])
def test_fresh_process_reads_or_discards_isolated_reconstruction(
    control: OperatorControlService,
    mode: str,
) -> None:
    with patch.object(WorkflowArtifactStore, "flush", return_value=True):
        publication = run(control)["publication_id"]
    path = control.database.path
    control.kernel.close()
    source = Path(__file__).resolve().parents[1] / "src"
    child = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            CHILD,
            str(source),
            str(path),
            publication,
            mode,
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=path.parent,
        text=True,
        capture_output=True,
        timeout=90,
    )
    assert child.returncode == (86 if mode == "exit_during_reconstruction" else 0), child.stderr
    if mode == "read_accepted":
        assert json.loads(child.stdout)["isolated_reconstruction_verified"]
    with SubjectKernel(
        path, "d2b-control", content_hash({"subject_id": "d2b-control", "test": "p3-06"})
    ) as reopened:
        assert reopened.boot().state == "paused"
        result = OperatorControlService(reopened).verify_workflow_export(publication)
        assert result["records"] == 4 and result["source_history_preserved"]
        with reopened.database.read_transaction() as c:
            assert c.execute("SELECT COUNT(*) FROM model_calls").fetchone()[0] == 0
            assert c.execute("SELECT COUNT(*) FROM workflow_checkpoints").fetchone()[0] == 4
