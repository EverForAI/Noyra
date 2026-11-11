from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import pytest

from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from support.workflow_recovery_schema import publication_upgrade_runtime
from test_workflow_source_recovery_control import RecoverablePublication
from test_workflow_source_recovery_control import control as control
from test_workflow_source_recovery_control import recoverable as recoverable

CHILD = r"""
import json,os,sys
from pathlib import Path
from unittest.mock import patch
sys.path[:0]=[sys.argv[1],sys.argv[2]]
from noyra.core import SubjectKernel
from noyra.core.operator_controls import OperatorControlService
from noyra.core.types import content_hash
from noyra.core.workflow_export_files import WorkflowArtifactStore
from noyra.core.workflow_increment_files import WorkflowIncrementStore
from noyra.core.workflow_source_recovery import RecoverySchemaPair
from support.workflow_recovery_schema import publication_upgrade_runtime
from test_d2b_workflow_export_control import run
from test_i2_workflow_increment_control import publish
pair=RecoverySchemaPair(**json.loads(sys.argv[4]))
parent=sys.argv[5]
files=WorkflowArtifactStore if not parent else WorkflowIncrementStore
with publication_upgrade_runtime(pair):
    if os.name=='nt':
        # Explicit simulated capability, never a claim of native directory fsync.
        WorkflowArtifactStore.creation_supported=staticmethod(lambda:True)
        files.flush=lambda self:True
    if sys.argv[6]=='before_acceptance':
        native_flush=files.flush
        def interrupted(self):
            native_flush(self)
            os._exit(86)
        files.flush=interrupted
    with SubjectKernel(Path(sys.argv[3]),'d2b-control',
        content_hash({'subject_id':'d2b-control','test':'p3-06'}),
        allow_subject_creation=False) as kernel:
        assert kernel.boot().state=='paused'
        controls=OperatorControlService(kernel)
        result=run(controls) if not parent else publish((controls,parent))
        print(json.dumps(result))
"""


@pytest.mark.parametrize("mode", ("read", "before_acceptance"))
def test_new_process_resumes_the_old_intent_and_interrupted_upgrade_recovery_is_idempotent(
    recoverable: RecoverablePublication, mode: str
) -> None:
    controls = recoverable.controls
    before = recoverable.rows()
    stored = recoverable.files()
    with publication_upgrade_runtime(recoverable.pair):
        recoverable.upgrade()
        controls.kernel.close()
        root = Path(__file__).resolve().parents[1]
        args = [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            CHILD,
            str(root / "src"),
            str(root / "tests"),
            str(controls.database.path),
            json.dumps(asdict(recoverable.pair)),
            recoverable.parent or "",
        ]
        result = subprocess.run(
            [*args, mode],
            capture_output=True,
            text=True,
            timeout=90,
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
        assert result.returncode == (86 if mode == "before_acceptance" else 0), result.stderr
        if mode == "before_acceptance":
            with controls.database.read_transaction() as c:
                unresolved = recoverable.catalog().get(c, recoverable.saved["publication_id"])
            assert unresolved is not None and unresolved["status"] == recoverable.saved["status"]
            result = subprocess.run(
                [*args, "read"],
                capture_output=True,
                text=True,
                timeout=90,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            assert result.returncode == 0, result.stderr
        accepted = json.loads(result.stdout)
        assert accepted["status"] == "accepted"
        assert accepted["publication_id"] == recoverable.saved["publication_id"]
        assert accepted["readback_verified"] and accepted["durability_confirmed"]
        recoverable.assert_history_and_fees(before)
        if recoverable.saved["status"] == "published_durability_unknown":
            assert recoverable.files() == stored
        # A fresh source runtime reads the original acceptance without rewriting it.
        with SubjectKernel(
            controls.database.path,
            "d2b-control",
            content_hash({"subject_id": "d2b-control", "test": "p3-06"}),
            allow_subject_creation=False,
        ) as kernel:
            assert kernel.boot().state == "paused"
            fresh = OperatorControlService(kernel)
            evidence = (
                fresh.verify_workflow_export(accepted["publication_id"])
                if recoverable.parent is None
                else fresh.verify_workflow_chain(accepted["publication_id"])
            )
            assert evidence["records"] == (4 if recoverable.parent is None else 6)
