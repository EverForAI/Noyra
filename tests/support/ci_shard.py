"""Explicit CI-only pytest partitioning; normal pytest runs never load this plugin."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest


def shard_index(nodeid: str, count: int) -> int:
    return int.from_bytes(hashlib.sha256(nodeid.encode("utf-8")).digest()[:8], "big") % count


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ci-shard-index", type=int, required=True)
    parser.addoption("--ci-shard-count", type=int, required=True)
    parser.addoption("--ci-shard-manifest", type=Path, required=True)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    index = int(config.getoption("ci_shard_index"))
    count = int(config.getoption("ci_shard_count"))
    if count < 1 or not 0 <= index < count:
        raise pytest.UsageError("invalid CI shard index/count")
    all_ids = sorted(item.nodeid for item in items)
    if len(all_ids) != len(set(all_ids)):
        raise pytest.UsageError("duplicate collected node IDs cannot prove CI coverage")
    selected = [item for item in items if shard_index(item.nodeid, count) == index]
    deselected = [item for item in items if shard_index(item.nodeid, count) != index]
    Path(config.getoption("ci_shard_manifest")).write_text(
        json.dumps(
            {
                "index": index,
                "count": count,
                "all": all_ids,
                "selected": sorted(item.nodeid for item in selected),
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
