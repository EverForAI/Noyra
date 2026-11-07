from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from noyra.core import Database
from noyra.core.types import content_hash
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
from test_i1_workflow_increment_catalog import SUBJECT, accepted, advance, prepare
from test_i1_workflow_increment_catalog import database as database
from test_i1_workflow_increment_catalog import metadata as metadata
from test_i1_workflow_increment_catalog import template as template

CHILD = r"""
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
import noyra
assert Path(noyra.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
from noyra.core import Database
from noyra.core.types import content_hash
from noyra.core.workflow_increment_catalog import WorkflowIncrementCatalog
subject='export-subject'
database=Database(sys.argv[2],strict_schema_contract=True)
catalog=WorkflowIncrementCatalog(database,subject)
with database.read_transaction() as c:
    record=catalog.requested(c,content_hash('request-one'),sys.argv[3],'workflow',4)
    assert record is not None
if sys.argv[4]=='exit_after_receipt':
    append=catalog._append
    def stopped(c,event_id,kind,payload):
        append(c,event_id,kind,payload)
        if kind=='workflow_increment_accepted':os._exit(86)
    catalog._append=stopped
    with database.transaction() as c:
        catalog.finish(c,record,{'name':'manifest.enc','stored_bytes':137,
            'stored_hash':'a'*64,'plaintext_hash':'b'*64},durable=True)
else:
    print(json.dumps({'publication_id':record['publication_id'],'status':record['status']}))
"""


@pytest.mark.parametrize("mode", ["accepted_read", "exit_after_receipt"])
def test_fresh_process_resolves_original_request_and_crash_rolls_back_acceptance(
    metadata: tuple[Any, ...],
    mode: str,
) -> None:
    db, _catalog, _ring, base = metadata
    advance(db)
    record = accepted(metadata) if mode == "accepted_read" else prepare(metadata)
    src = Path(__file__).resolve().parents[1] / "src"
    child = subprocess.run(
        [
            sys.executable,
            "-X",
            "utf8",
            "-B",
            "-c",
            CHILD,
            str(src),
            str(db.path),
            base["publication_id"],
            mode,
        ],
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        cwd=db.path.parent,
        capture_output=True,
        text=True,
        timeout=90,
    )
    assert child.returncode == (86 if mode == "exit_after_receipt" else 0), child.stderr
    if mode == "accepted_read":
        assert json.loads(child.stdout) == {
            "publication_id": record["publication_id"],
            "status": "accepted",
        }
    reopened = Database(db.path, strict_schema_contract=True)
    catalog = WorkflowIncrementCatalog(reopened, SUBJECT)
    with reopened.read_transaction() as c:
        restored = catalog.requested(
            c, content_hash("request-one"), base["publication_id"], "workflow", 4
        )
        assert restored == record
        count = c.execute(
            "SELECT COUNT(*) FROM events WHERE event_type='workflow_increment_accepted'"
        ).fetchone()[0]
        assert count == int(mode == "accepted_read")
        assert c.execute("PRAGMA foreign_key_check").fetchone() is None
