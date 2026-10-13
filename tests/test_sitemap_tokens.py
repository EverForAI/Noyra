from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidTag

from noyra.interaction.sitemap_tokens import SitemapTokens


def test_deterministic_routes_independent_owners_rotation_and_tampering() -> None:
    key, new_key = bytes(range(32)), bytes(reversed(range(32)))
    value = {"subject": "PRIVATE-SUBJECT", "kind": "goal", "range": [1, 2]}
    first = SitemapTokens((key,))
    token = first.seal(value)
    assert first.seal(value) == token
    assert SitemapTokens((key,)).open(token) == value
    rotated = SitemapTokens((new_key, key))
    assert rotated.open(token) == value
    assert rotated.seal(value) != token
    with pytest.raises(ValueError):
        SitemapTokens((new_key,)).open(token)
    with pytest.raises((ValueError, InvalidTag)):
        first.open(token[:20] + ("A" if token[20] != "A" else "B") + token[21:])


def test_managed_keyring_survives_new_process_and_preserves_files(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    first = SitemapTokens.managed(root)
    token = first.seal({"route": "synthetic"})
    before = {path.name: path.read_bytes() for path in root.iterdir()}
    second = SitemapTokens.managed(root)
    assert second.open(token) == {"route": "synthetic"}
    assert {path.name: path.read_bytes() for path in root.iterdir()} == before
    code = (
        "import sys; from pathlib import Path; "
        "from noyra.interaction.sitemap_tokens import SitemapTokens; "
        "print(SitemapTokens.from_file(Path(sys.argv[1])).seal({'route':'synthetic'}))"
    )
    child = subprocess.run(
        [sys.executable, "-c", code, str(root / "public-sitemap-keyring.json")],
        check=True,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert child.stdout.strip() == token


def test_corrupt_keyring_is_never_regenerated(tmp_path: Path) -> None:
    root = tmp_path / "secrets"
    root.mkdir(mode=0o700)
    path = root / "public-sitemap-keyring.json"
    path.write_text('{"bad":true}')
    path.chmod(0o600)
    with pytest.raises(ValueError):
        SitemapTokens.managed(root)
    assert json.loads(path.read_text()) == {"bad": True}
