"""Stage only exact reviewed site blobs after checking the complete upload surface."""

from __future__ import annotations

import argparse
import html
import os
import re
import subprocess
from pathlib import Path
from urllib.parse import unquote

TEXT = {".html", ".css", ".js", ".xml", ".txt"}
ALLOWED = TEXT | {".webp", ".png"}
LOCAL_REFERENCE = re.compile(
    r"(?i)(?<![a-z0-9])[a-z]:[\\/]|/mnt/[a-z]/|file:/|"
    r"/(?:root|home|Users|var/tmp|tmp)/|"
    r"\.(?:workbuddy[\\/]private-audit|codex[\\/]worktrees)[\\/]"
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
    ).stdout


def validate_artifact(directory: Path, expected: dict[str, bytes]) -> None:
    if directory.is_symlink() or not directory.is_dir():
        raise ValueError("invalid Pages artifact directory")
    actual = set()
    for path in directory.rglob("*"):
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            raise ValueError("Pages artifact contains a link or special file")
        if path.is_file():
            name = path.relative_to(directory).as_posix()
            actual.add(name)
            if (
                name not in expected
                or path.stat().st_size != len(expected[name])
                or path.read_bytes() != expected[name]
            ):
                raise ValueError("Pages artifact differs from reviewed Git blobs")
    if actual != set(expected):
        raise ValueError("Pages artifact has missing or additional files")


def prepare(root: Path, revision: str, output: Path) -> None:
    if re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", revision) is None:
        raise ValueError("Pages requires an exact commit SHA")
    if git(root, "rev-parse", "HEAD").decode().strip() != revision:
        raise ValueError("Pages checkout differs from requested commit")
    expected: dict[str, bytes] = {}
    records = git(root, "ls-tree", "-r", "-z", revision, "--", "site/")
    for record in filter(None, records.split(b"\0")):
        metadata, raw_name = record.split(b"\t", 1)
        mode, kind, oid = metadata.decode("ascii").split()
        name = raw_name.decode("utf-8").removeprefix("site/")
        path = Path(name)
        if (
            mode not in {"100644", "100755"}
            or kind != "blob"
            or path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or (path.suffix not in ALLOWED and name != ".nojekyll")
            or any(
                part.casefold() in {"output", "private-audit", ".git"}
                or part.casefold().endswith("-evidence")
                for part in path.parts
            )
        ):
            raise ValueError("Pages contains an unpublishable path or object")
        size = int(git(root, "cat-file", "-s", oid))
        if size > 1_200_000 or sum(map(len, expected.values())) + size > 4_000_000:
            raise ValueError("Pages file exceeds publication budget")
        content = git(root, "cat-file", "blob", oid)
        if path.suffix in TEXT:
            text = unquote(html.unescape(content.decode("utf-8")))
            if LOCAL_REFERENCE.search(text):
                raise ValueError("Pages contains a machine-local reference")
        expected[name] = content
    if "index.html" not in expected or sum(map(len, expected.values())) > 4_000_000:
        raise ValueError("invalid Pages publication inventory or byte budget")
    validate_artifact(root / "site", expected)
    # Exclusive destination: never reuse another run's upload directory.
    output.mkdir(mode=0o755)
    for name, content in expected.items():
        target = output / name
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(content)
    validate_artifact(output, expected)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    try:
        prepare(Path(__file__).resolve().parents[1], args.revision, args.output)
    except (OSError, ValueError, UnicodeError, subprocess.CalledProcessError):
        print("pages_publication=failed reason=unreviewed_artifact")
        return 1
    print("pages_publication=passed exact_commit_and_artifact_verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
