from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from types import ModuleType

import pytest


def _tool() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts/verify-committed.py"
    spec = importlib.util.spec_from_file_location("committed_verifier", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_exact_commit_excludes_dirty_and_untracked_files(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    def git(*arguments: str) -> str:
        return subprocess.check_output(["git", *arguments], cwd=repository, text=True).strip()

    git("init", "-q")
    git("config", "user.name", "test")
    git("config", "user.email", "test@example.invalid")
    tracked = repository / "tracked.txt"
    tracked.write_text("committed")
    git("add", "tracked.txt")
    git("commit", "-qm", "fixture")
    sha = git("rev-parse", "HEAD")
    tracked.write_text("dirty")
    experiment = repository / "untracked.py"
    experiment.write_text("not valid Python")
    checkout = tmp_path / "clean"
    checkout.mkdir()
    assert _tool().export_commit(repository, sha, checkout) == sha
    assert (checkout / "tracked.txt").read_text() == "committed"
    assert not (checkout / "untracked.py").exists()
    assert tracked.read_text() == "dirty" and experiment.exists()


def test_external_template_cannot_be_mistaken_for_passed_evidence() -> None:
    from noyra.core.external_gates import EXPECTED_GATE_IDS, validate_external_gates

    path = Path(__file__).resolve().parents[1] / "scripts/prepare-external-validation.py"
    spec = importlib.util.spec_from_file_location("external_template", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = module.pending_record("a" * 40)
    assert {gate["id"] for gate in record["gates"]} == set(EXPECTED_GATE_IDS)
    assert record["status"] == "pending" and "signature" not in record
    errors = validate_external_gates(record, expected_sha="a" * 40, public_key="")
    assert "status" in errors and "signature" in errors
    with pytest.raises(ValueError):
        module.pending_record("main")
