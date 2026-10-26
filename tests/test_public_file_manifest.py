from __future__ import annotations

import os
import stat
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "public-file-manifest.py"
ALLOWLIST = "docs/release/public-files.txt"
CHECKSUMS = "docs/release/public-files.sha256"
EXCLUSIONS = "docs/release/public-files-excluded.txt"


def _git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=root, input=data, capture_output=True, check=True
    ).stdout


def _command(
    root: Path, command: str, *args: str, ok: bool = True
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), command, "--repository", str(root), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "PYTHONUTF8": "1"},
        check=False,
    )
    assert (result.returncode == 0) == ok, result.stdout + result.stderr
    return result


def _commit(root: Path) -> str:
    _git(
        root,
        "-c",
        "user.name=Public Manifest Tests",
        "-c",
        "user.email=manifest-tests@example.invalid",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "--quiet",
        "-m",
        "reviewed fixture",
    )
    return _git(root, "rev-parse", "HEAD").decode().strip()


def _allow(root: Path, *extra: str) -> None:
    current = (root / ALLOWLIST).read_text(encoding="utf-8").splitlines()
    (root / ALLOWLIST).write_bytes(("\n".join(sorted([*current, *extra])) + "\n").encode())
    _git(root, "add", "--", ALLOWLIST)


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    root = tmp_path / "source with spaces 源码"
    root.mkdir()
    _git(root, "init", "--quiet", "--initial-branch=main")
    _git(root, "config", "core.autocrlf", "false")
    files = {
        ".gitattributes": (
            b"*.txt text eol=lf\n*.ps1 text eol=crlf\n*.bin binary\n"
            b"hello.txt export-ignore\nversion.txt export-subst\n"
        ),
        "hello.txt": b"hello from Git\n",
        "image.bin": bytes(range(256)) + b"\r\n\xff\x00",
        "run.ps1": b"Write-Output 'fixture'\r\n",
        "version.txt": b"$Format:%H$\n",
        "output/trace.log": b"synthetic private diagnostic, deliberately excluded\n",
        CHECKSUMS: b"",
        EXCLUSIONS: b"output/trace.log\traw diagnostic, not public source\n",
    }
    files[ALLOWLIST] = (
        "\n".join(sorted([ALLOWLIST, *(p for p in files if p != "output/trace.log")])) + "\n"
    ).encode()
    for name, value in files.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    _git(root, "add", "--all")
    _git(root, "update-index", "--chmod=+x", "run.ps1")
    _command(root, "generate", "--index")
    _git(root, "add", "--", CHECKSUMS)
    _commit(root)
    return root


def test_exact_git_bytes_ignore_binary_decoding_crlf_and_dirty_worktree(repository: Path) -> None:
    _command(repository, "check")
    (repository / "run.ps1").write_bytes(b"different worktree script\r\n")
    (repository / "image.bin").write_bytes(b"different binary\xff\x00")
    (repository / ".env").write_text("synthetic untracked secret", encoding="utf-8")
    _command(repository, "check")
    target = repository.parent / "exact.zip"
    _command(repository, "export", "--output", str(target))
    with zipfile.ZipFile(target) as archive:
        assert archive.read("image.bin") == bytes(range(256)) + b"\r\n\xff\x00"
        assert archive.read("run.ps1") == b"Write-Output 'fixture'\n"
        assert archive.read("hello.txt") == b"hello from Git\n"
        assert archive.read("version.txt") == b"$Format:%H$\n"
        assert stat.S_IMODE(archive.getinfo("run.ps1").external_attr >> 16) == 0o755
        assert ".env" not in archive.namelist()
        assert "output/trace.log" not in archive.namelist()


def test_index_generation_uses_staged_bytes_and_final_commit_is_verified(repository: Path) -> None:
    old_commit = _git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / "hello.txt").write_bytes(b"reviewed new version\n")
    _git(repository, "add", "--", "hello.txt")
    (repository / "hello.txt").write_bytes(b"unstaged changes must not enter checksums\n")
    _command(repository, "check")
    failure = _command(repository, "check", "--index", ok=False)
    assert "1 checksum mismatch" in failure.stderr
    _command(repository, "generate", "--index")
    # Generation never changes the caller's index.
    _command(repository, "check", "--index", ok=False)
    _git(repository, "add", "--", CHECKSUMS)
    _command(repository, "check", "--index")
    _commit(repository)
    _command(repository, "check")
    _command(repository, "check", "--revision", old_commit)


def test_new_tracked_file_requires_review_before_generate_or_export(repository: Path) -> None:
    (repository / "unreviewed.txt").write_bytes(b"not approved\n")
    _git(repository, "add", "--", "unreviewed.txt")
    failure = _command(repository, "generate", "--index", ok=False)
    assert "unreviewed.txt" in failure.stderr
    target = repository.parent / "blocked.zip"
    _command(repository, "export", "--index", "--output", str(target), ok=False)
    assert not target.exists()
    _allow(repository, "unreviewed.txt")
    _command(repository, "generate", "--index")
    _git(repository, "add", "--", CHECKSUMS)
    _command(repository, "check", "--index")


def test_missing_public_file_is_rejected(repository: Path) -> None:
    _git(repository, "rm", "--quiet", "--", "hello.txt")
    assert "missing=" in _command(repository, "generate", "--index", ok=False).stderr


@pytest.mark.parametrize(
    "entry",
    [
        "../escape.txt",
        "/absolute.txt",
        "C:/drive.txt",
        "a\\b.txt",
        "a/./b.txt",
        "a//b.txt",
        "a/../b.txt",
        "a/trailing.",
        "a/trailing ",
        "NUL.txt",
        ".git/config",
    ],
)
def test_nonportable_and_traversal_paths_are_rejected(repository: Path, entry: str) -> None:
    _allow(repository, entry)
    assert "path" in _command(repository, "generate", "--index", ok=False).stderr.lower()


def test_duplicate_and_case_colliding_paths_are_rejected(repository: Path) -> None:
    _allow(repository, "HELLO.txt")
    failure = _command(repository, "generate", "--index", ok=False)
    assert "case-colliding" in failure.stderr


def test_missing_exclusion_reason_cannot_approve_files(repository: Path) -> None:
    (repository / EXCLUSIONS).write_bytes(b"output/trace.log\n")
    _git(repository, "add", "--", EXCLUSIONS)
    failure = _command(repository, "generate", "--index", ok=False)
    assert "review reason" in failure.stderr


def test_public_and_excluded_sets_cannot_overlap(repository: Path) -> None:
    _allow(repository, "output/trace.log")
    assert (
        "both public and excluded" in _command(repository, "generate", "--index", ok=False).stderr
    )


def test_symlinks_cannot_be_published_even_on_windows(repository: Path) -> None:
    oid = _git(repository, "hash-object", "-w", "--stdin", data=b"hello.txt").decode().strip()
    _git(repository, "update-index", "--add", "--cacheinfo", f"120000,{oid},link")
    _allow(repository, "link")
    failure = _command(repository, "generate", "--index", ok=False)
    assert "regular Git blob" in failure.stderr


def test_unresolved_index_is_rejected(repository: Path) -> None:
    oid = _git(repository, "rev-parse", "HEAD:hello.txt").decode().strip()
    _git(repository, "update-index", "--force-remove", "hello.txt")
    _git(
        repository,
        "update-index",
        "--index-info",
        data=f"100644 {oid} 1\thello.txt\n100644 {oid} 2\thello.txt\n".encode(),
    )
    failure = _command(repository, "generate", "--index", ok=False)
    assert "Resolve index conflicts" in failure.stderr


@pytest.mark.parametrize("kind", ["stale", "missing", "extra", "duplicate", "malformed"])
def test_invalid_hash_manifests_fail_closed(repository: Path, kind: str) -> None:
    target = repository / CHECKSUMS
    rows = target.read_text(encoding="utf-8").splitlines()
    if kind == "stale":
        rows[0] = "0" * 64 + rows[0][64:]
    elif kind == "missing":
        rows = rows[1:]
    elif kind == "extra":
        rows.append("0" * 64 + "  zzz.txt")
    elif kind == "duplicate":
        rows.insert(0, rows[0])
    else:
        rows[0] = "not a checksum"
    target.write_bytes(("\n".join(rows) + "\n").encode())
    _git(repository, "add", "--", CHECKSUMS)
    _command(repository, "check", "--index", ok=False)
    _command(repository, "check")


def test_archive_is_repeatable_and_existing_output_is_never_overwritten(repository: Path) -> None:
    first, second = repository.parent / "first.zip", repository.parent / "second.zip"
    _command(repository, "export", "--output", str(first))
    _command(repository, "export", "--output", str(second))
    assert first.read_bytes() == second.read_bytes()
    original = first.read_bytes()
    _command(repository, "export", "--output", str(first), ok=False)
    assert first.read_bytes() == original
    _command(repository, "verify-export", "--archive", str(first))


@pytest.mark.parametrize(
    "kind", ["content", "missing", "extra", "duplicate", "traversal", "symlink"]
)
def test_export_verification_rejects_tampering(repository: Path, kind: str) -> None:
    source, target = repository.parent / "source.zip", repository.parent / "tampered.zip"
    _command(repository, "export", "--output", str(source))
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w") as changed:
        for info in original.infolist():
            value = original.read(info)
            if info.filename == "hello.txt":
                if kind == "missing":
                    continue
                if kind == "content":
                    value = b"X" + value[1:]
                if kind == "symlink":
                    info.external_attr = (stat.S_IFLNK | 0o644) << 16
            changed.writestr(info, value)
        if kind == "extra":
            changed.writestr("unreviewed.txt", b"extra")
        if kind == "traversal":
            changed.writestr("../escape.txt", b"extra")
        if kind == "duplicate":
            with pytest.warns(UserWarning, match="Duplicate name"):
                changed.writestr("hello.txt", b"duplicate")
    _command(repository, "verify-export", "--archive", str(target), ok=False)


@pytest.mark.parametrize("names", [("A", "a/file.txt"), ("A/one.txt", "a/two.txt")])
def test_file_directory_and_directory_case_collisions_are_rejected(
    repository: Path, names: tuple[str, str]
) -> None:
    # Synthesize the index directly so this also works on case-insensitive Windows hosts.
    oid = _git(repository, "hash-object", "-w", "--stdin", data=b"fixture\n").decode().strip()
    for name in names:
        _git(repository, "update-index", "--add", "--cacheinfo", f"100644,{oid},{name}")
    _allow(repository, *names)
    failure = _command(repository, "generate", "--index", ok=False)
    assert "collision" in failure.stderr


def test_exclusion_changes_require_new_checksums_and_remain_outside_archive(
    repository: Path,
) -> None:
    _git(repository, "show", "HEAD:" + EXCLUSIONS)
    (repository / EXCLUSIONS).write_bytes(b"output/trace.log\treviewed raw trace\n")
    _git(repository, "add", "--", EXCLUSIONS)
    assert "checksum mismatch" in _command(repository, "check", "--index", ok=False).stderr
    _command(repository, "generate", "--index")
    _git(repository, "add", "--", CHECKSUMS)
    archive = repository.parent / "new-exclusion.zip"
    _command(repository, "export", "--index", "--output", str(archive))
    with zipfile.ZipFile(archive) as package:
        assert "output/trace.log" not in package.namelist()
        assert package.read(EXCLUSIONS) == b"output/trace.log\treviewed raw trace\n"
