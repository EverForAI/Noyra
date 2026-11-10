from __future__ import annotations

import runpy
import subprocess
from pathlib import Path
from typing import Any

import pytest

from test_publication_boundary import GUARD as BOUNDARY
from test_publication_boundary import commit, git
from test_publication_boundary import repository as repository

ROOT = Path(__file__).resolve().parents[1]
PAGES: dict[str, Any] = runpy.run_path(str(ROOT / "scripts/check-pages-publication.py"))


def site(root: Path, content: str = "<html>Public page</html>") -> str:
    (root / "site/assets").mkdir(parents=True)
    (root / "site/index.html").write_bytes(content.encode("utf-8"))
    (root / "site/assets/site.css").write_bytes(b"body { color: black }")
    (root / "site/.nojekyll").write_bytes(b"")
    git(root, "add", "--", "site")
    return commit(root)


def test_pages_stages_exact_git_blobs_without_source_or_extra_files(repository: Path) -> None:
    revision = site(repository, '<a href="https://example.invalid">Open reading</a>')
    output = repository.parent / "pages-artifact"
    PAGES["prepare"](repository, revision, output)
    assert {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} == {
        "index.html",
        "assets/site.css",
        ".nojekyll",
    }
    assert (output / "index.html").read_bytes() == (repository / "site/index.html").read_bytes()
    with pytest.raises(FileExistsError):
        PAGES["prepare"](repository, revision, output)


@pytest.mark.parametrize(
    "text",
    [
        "X:/synthetic-workstation/private",
        "X:\\synthetic-workstation\\private",
        "/mnt/x/synthetic/private",
        "/home/synthetic/private",
        "file:///synthetic/private",
        "X&#58;/synthetic/private",
        "X%3A%2Fsynthetic/private",
    ],
)
def test_pages_refuses_machine_local_content_before_creating_upload(
    repository: Path, text: str
) -> None:
    revision = site(repository, text)
    output = repository.parent / "refused-artifact"
    with pytest.raises(ValueError, match="machine-local") as error:
        PAGES["prepare"](repository, revision, output)
    assert text not in str(error.value) and not output.exists()


@pytest.mark.parametrize("change", ["modified", "untracked", "deleted", "wrong-sha", "ref-name"])
def test_pages_refuses_worktree_or_sha_drift(repository: Path, change: str) -> None:
    revision = site(repository)
    if change == "modified":
        (repository / "site/index.html").write_bytes(b"unreviewed change")
    elif change == "untracked":
        (repository / "site/private.txt").write_bytes(b"unreviewed evidence")
    elif change == "deleted":
        (repository / "site/assets/site.css").unlink()
    else:
        revision = "0" * 40 if change == "wrong-sha" else "HEAD"
    output = repository.parent / "refused-artifact"
    with pytest.raises(ValueError):
        PAGES["prepare"](repository, revision, output)
    assert not output.exists()


@pytest.mark.parametrize(
    "name,mode",
    [
        ("link.html", "120000"),
        ("private.bundle", "100644"),
        ("output/raw.txt", "100644"),
        ("audit-evidence/raw.txt", "100644"),
    ],
)
def test_pages_rejects_raw_evidence_and_git_links(repository: Path, name: str, mode: str) -> None:
    site(repository)
    content = b"synthetic link or raw evidence"
    oid = (
        subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=repository,
            input=content,
            capture_output=True,
            check=True,
        )
        .stdout.decode()
        .strip()
    )
    git(repository, "update-index", "--add", "--cacheinfo", mode + "," + oid + ",site/" + name)
    revision = commit(repository)
    with pytest.raises(ValueError, match="unpublishable"):
        PAGES["prepare"](repository, revision, repository.parent / "refused-artifact")


def test_range_gate_catches_private_site_content_removed_before_tip(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    site(repository, "Synthetic X:/private-workstation/transient")
    (repository / "site/index.html").write_bytes(b"Safe replacement")
    git(repository, "add", "--", "site")
    tip = commit(repository)
    assert BOUNDARY["check"](repository, tip) == []
    assert BOUNDARY["check"](repository, tip, base=base)


def test_pages_checks_exact_artifact_before_upload_without_full_runtime_matrix() -> None:
    workflow = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    assert "fetch-depth: 0" in workflow and "persist-credentials: false" in workflow
    assert "publication_base:" in workflow and "vars.PUBLICATION_REVIEW_BASE" in workflow
    checks = [
        "check-publication-boundary.py --github-event",
        "public-file-manifest.py check --revision",
        "python scripts/check-site.py",
        "check-pages-publication.py --revision",
        "- name: Upload static site",
    ]
    positions = [workflow.index(check) for check in checks]
    assert positions == sorted(positions)
    assert "path: ${{ runner.temp }}/noyra-pages" in workflow
    assert "pytest" not in workflow and "needs: build" in workflow
