"""Explicit private storage fixtures for native key-rotation boundary tests."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture
def rotation_directory(tmp_path: Path) -> Iterator[Path]:
    if os.name == "nt":
        # Shared runner scratch drives can grant other accounts delete-child.
        # Use a fresh directory under the actual user profile, not that scratch
        # tree, and harden only this fixture (never its existing ancestors).
        from noyra.core.at_rest import _windows_harden

        with tempfile.TemporaryDirectory(prefix=".noyra-rotation-test-", dir=Path.home()) as folder:
            root = Path(folder)
            _windows_harden(root)
            yield root
    else:
        tmp_path.chmod(0o700)
        yield tmp_path
