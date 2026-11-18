"""Explicit private storage fixtures for native key-rotation boundary tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def child_processes_do_not_inherit_pytest_coverage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Test subprocesses are outside shard coverage; forced exits can leave
    # partial DBs, and temporary source copies disappear before reporting.
    for name in tuple(os.environ):
        if name.startswith("COV_CORE_") or name == "COVERAGE_PROCESS_START":
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def rotation_directory(tmp_path: Path) -> Iterator[Path]:
    uid = getattr(os, "geteuid", None)
    if os.name == "nt":
        # Shared runner scratch drives can grant other accounts delete-child.
        # Use a fresh directory under the actual user profile, not that scratch
        # tree, and harden only this fixture (never its existing ancestors).
        from noyra.core.at_rest import _windows_harden

        with tempfile.TemporaryDirectory(prefix=".noyra-rotation-test-", dir=Path.home()) as folder:
            root = Path(folder)
            _windows_harden(root)
            yield root
    elif callable(uid) and uid() == 0:
        # A sudo pytest run still inherits the runner-owned scratch ancestors.
        # Root rotation must reject those paths; keep its key material in a
        # fresh root-owned directory without changing any existing ancestors.
        with tempfile.TemporaryDirectory(
            prefix="noyra-root-rotation-test-", dir="/var/tmp"
        ) as folder:
            yield Path(folder)
    else:
        tmp_path.chmod(0o700)
        yield tmp_path
