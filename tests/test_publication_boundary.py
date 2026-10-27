from __future__ import annotations

import os
import runpy
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check-publication-boundary.py"
GUARD = runpy.run_path(str(SCRIPT))


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1"},
    ).stdout


def commit(root: Path) -> str:
    git(root, "-c", "commit.gpgSign=false", "commit", "--quiet", "-m", "synthetic fixture")
    return git(root, "rev-parse", "HEAD").decode().strip()


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "publication with spaces 源码"
    root.mkdir()
    git(root, "init", "--quiet", "--initial-branch=main")
    git(root, "config", "user.name", "Synthetic Tests")
    git(root, "config", "user.email", "tests@example.invalid")
    git(root, "config", "core.autocrlf", "false")
    (root / "README.md").write_text("synthetic public source\n", encoding="utf-8")
    git(root, "add", "--", "README.md")
    commit(root)
    return root


def command(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


@pytest.mark.parametrize(
    "path",
    [
        "output/report.txt",
        "OUTPUT/report.txt",
        "snapshot.bundle",
        "copy.BUNDLE",
        "docs/audit/synthetic-evidence/result.json",
        "docs/audit/synthetic-verification.json",
    ],
)
def test_blocks_forced_generated_paths_in_index_and_commit(repository: Path, path: str) -> None:
    file = repository / path
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text("synthetic marker not to echo", encoding="utf-8")
    git(repository, "add", "--", path)
    staged = command(repository, "--staged")
    assert staged.returncode == 1
    assert "synthetic marker not to echo" not in staged.stderr
    revision = commit(repository)
    assert command(repository, "--revision", revision).returncode == 1


@pytest.mark.parametrize(
    "reference",
    [
        "X:" + "/private-machine/results",
        "X:" + "\\private-machine\\results",
        "/mnt/" + "z/private-machine/results",
    ],
)
def test_blocks_new_audit_references_without_echoing_content(
    repository: Path,
    reference: str,
) -> None:
    file = repository / "docs/audit/summary.md"
    file.parent.mkdir(parents=True)
    file.write_text("Private marker " + reference + "\n", encoding="utf-8")
    git(repository, "add", "--", "docs/audit/summary.md")
    result = command(repository, "--staged")
    assert result.returncode == 1
    assert "summary.md:1:" in result.stderr
    assert reference not in result.stderr
    revision = commit(repository)
    assert command(repository, "--revision", revision).returncode == 1


def test_checks_staged_bytes_not_dirty_worktree(repository: Path) -> None:
    file = repository / "docs/audit/summary.md"
    file.parent.mkdir(parents=True)
    file.write_text("Reviewed summary only.\n", encoding="utf-8")
    git(repository, "add", "--", "docs/audit/summary.md")
    file.write_text("X:" + "/private-machine/not-staged\n", encoding="utf-8")
    assert command(repository, "--staged").returncode == 0


def test_does_not_rescan_legacy_lines_or_block_synthetic_and_deployment_examples(
    repository: Path,
) -> None:
    file = repository / "docs/audit/legacy.md"
    file.parent.mkdir(parents=True)
    file.write_text("X:" + "/legacy-synthetic-reference\n", encoding="utf-8")
    git(repository, "add", "--", "docs/audit/legacy.md")
    commit(repository)
    file.write_text(file.read_text(encoding="utf-8") + "Reviewed summary.\n", encoding="utf-8")
    for name in ["tests/synthetic.py", "docs/deployment/windows.md"]:
        target = repository / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("X:" + "/synthetic-path\n", encoding="utf-8")
    git(repository, "add", "--", "docs", "tests")
    assert command(repository, "--staged").returncode == 0
    revision = commit(repository)
    assert command(repository, "--revision", revision).returncode == 0


def test_cached_removal_is_allowed_and_keeps_local_copy(repository: Path) -> None:
    file = repository / "output/report.txt"
    file.parent.mkdir()
    file.write_text("synthetic evidence", encoding="utf-8")
    git(repository, "add", "--", "output/report.txt")
    commit(repository)
    assert command(repository, "--staged").returncode == 1
    git(repository, "rm", "--cached", "--", "output/report.txt")
    assert file.is_file()
    assert command(repository, "--staged").returncode == 0
    revision = commit(repository)
    assert command(repository, "--revision", revision).returncode == 0


def test_rename_cannot_hide_new_private_reference(repository: Path) -> None:
    source = repository / "notes.md"
    source.write_text("X:" + "/synthetic-private-reference\n", encoding="utf-8")
    git(repository, "add", "--", "notes.md")
    commit(repository)
    (repository / "docs/audit").mkdir(parents=True)
    git(repository, "mv", "notes.md", "docs/audit/renamed.md")
    assert command(repository, "--staged").returncode == 1


def test_root_commit_and_bad_revision_are_handled(repository: Path) -> None:
    revision = git(repository, "rev-list", "--max-parents=0", "HEAD").decode().strip()
    assert command(repository, "--revision", revision).returncode == 0
    result = command(repository, "--revision", "not-a-commit")
    assert result.returncode == 1
    assert "could not read" in result.stderr


def test_added_diff_metadata_looking_line_is_still_checked() -> None:
    reference = "X:" + "/private-machine/report"
    patch = "@@ -0,0 +1 @@\n++++ " + reference + "\n"
    assert GUARD["added_reference_lines"](patch) == [1]


def test_ci_and_native_hook_share_one_guard_and_preserve_existing_jobs() -> None:
    hook = (ROOT / ".githooks/pre-commit").read_text(encoding="utf-8")
    ci = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "python scripts/check-publication-boundary.py --staged" in hook
    public_source = ci[ci.index("  public-source:") : ci.index("\n  test:")]
    assert "python scripts/check-publication-boundary.py --revision" in public_source
    assert "fetch-depth: 3" in public_source
    assert "continue-on-error" not in public_source


def test_real_git_hook_blocks_forced_output_and_allows_safe_commit(repository: Path) -> None:
    scripts = repository / "scripts"
    scripts.mkdir()
    (scripts / SCRIPT.name).write_bytes(SCRIPT.read_bytes())
    hooks = repository / ".githooks"
    hooks.mkdir()
    hook = hooks / "pre-commit"
    hook.write_bytes((ROOT / ".githooks/pre-commit").read_bytes())
    hook.chmod(0o755)
    git(repository, "config", "core.hooksPath", ".githooks")
    output = repository / "output/report.txt"
    output.parent.mkdir()
    output.write_text("synthetic evidence", encoding="utf-8")
    git(repository, "add", "--", "output/report.txt")
    failed = subprocess.run(
        ["git", "-c", "commit.gpgSign=false", "commit", "-m", "must fail"],
        cwd=repository,
        capture_output=True,
        check=False,
    )
    assert failed.returncode != 0
    assert b"Publication boundary check failed" in failed.stderr
    git(repository, "rm", "--cached", "--", "output/report.txt")
    (repository / "README.md").write_text("safe update\n", encoding="utf-8")
    git(repository, "add", "--", "README.md")
    commit(repository)


def test_merge_revision_checks_first_parent_and_full_snapshot(repository: Path) -> None:
    git(repository, "checkout", "--quiet", "-b", "candidate")
    note = repository / "docs/audit/summary.md"
    note.parent.mkdir(parents=True)
    note.write_text("Reviewed summary.\n", encoding="utf-8")
    git(repository, "add", "--", "docs/audit/summary.md")
    commit(repository)
    git(repository, "checkout", "--quiet", "main")
    (repository / "README.md").write_text("safe main update\n", encoding="utf-8")
    git(repository, "add", "--", "README.md")
    commit(repository)
    git(
        repository,
        "-c",
        "commit.gpgSign=false",
        "merge",
        "--quiet",
        "--no-ff",
        "candidate",
        "-m",
        "synthetic merge",
    )
    assert command(repository, "--revision", "HEAD").returncode == 0
