from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from noyra.core import IdentityStore
from noyra.core.database import Database
from noyra.core.types import content_hash
from noyra.mind import MemoryBlockStore


def test_memory_blocks_preserve_revisions_and_optimistic_concurrency() -> None:
    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "noyra.sqlite3")
        subject_id = "Noyra-memory-block-test"
        IdentityStore(database).ensure(subject_id, content_hash({"seed": subject_id}))
        blocks = MemoryBlockStore(database)
        block = blocks.create(
            subject_id,
            "working_context",
            "current inquiry",
            "Compare two evidence sources.",
            reason="a bounded context became relevant",
        )
        revised = blocks.revise(
            block.block_id,
            "Compare three evidence sources and record disagreement.",
            reason="new evidence broadened the inquiry",
            expected_version=1,
        )
        assert revised.version == 2
        with pytest.raises(ValueError):
            blocks.revise(
                block.block_id,
                "stale revision",
                reason="stale writer",
                expected_version=1,
            )
        assert blocks.verify_integrity(subject_id) == {
            "memory_blocks": 1,
            "memory_block_revisions": 2,
        }
