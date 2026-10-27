#!/usr/bin/env python3
"""Check publication paths and new machine-local references, without reading secrets."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

RAW_EVIDENCE = re.compile(r"^docs/audit/[^/]*-evidence/", re.IGNORECASE)
LOCAL_REFERENCE = re.compile(r"(?i)(?<![a-z0-9])[a-z]:[\\/]|/mnt/[a-z]/")


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout


def prohibited_path(path: str) -> bool:
    normalized = path.casefold()
    return (
        normalized.startswith("output/")
        or normalized.endswith(".bundle")
        or RAW_EVIDENCE.match(path) is not None
        or (normalized.startswith("docs/audit/") and normalized.endswith("-verification.json"))
    )


def checks_local_references(path: str) -> bool:
    # Deployment examples and synthetic source/tests may legitimately use absolute paths.
    return path.startswith(("docs/audit/", "docs/roadmap/")) and path.endswith(
        (".md", ".txt", ".log", ".xml", ".json")
    )


def added_reference_lines(patch: str) -> list[int]:
    line_number = 0
    in_hunk = False
    findings = []
    for line in patch.splitlines():
        if line.startswith("@@ "):
            match = re.search(r"\+(\d+)", line)
            if match is None:
                raise ValueError("Malformed diff hunk")
            line_number = int(match.group(1))
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            if LOCAL_REFERENCE.search(line[1:]):
                findings.append(line_number)
            line_number += 1
        elif in_hunk and line.startswith(" "):
            line_number += 1
        elif line.startswith(("diff --git ", "--- ", "+++ ")) and not in_hunk:
            continue
    return findings


def check(root: Path, revision: str | None) -> list[str]:
    diff_args: tuple[str, ...]
    if revision is None:
        names = git(root, "ls-files", "-z").decode("utf-8").split("\0")
        changed = (
            git(root, "diff", "--cached", "--no-renames", "--name-only", "-z", "--diff-filter=ACM")
            .decode("utf-8")
            .split("\0")
        )
        diff_args = ("diff", "--cached")
    else:
        commit = (
            git(root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
            .decode("ascii")
            .strip()
        )
        names = git(root, "ls-tree", "-rz", "--name-only", commit).decode("utf-8").split("\0")
        parents = git(root, "rev-list", "--parents", "-n", "1", commit).decode("ascii").split()
        if len(parents) > 1:
            diff_args = ("diff", parents[1], commit)
        else:
            diff_args = ("show", "--format=", "--root", commit)
        changed = (
            git(root, *diff_args, "--no-renames", "--name-only", "-z", "--diff-filter=ACM")
            .decode("utf-8")
            .split("\0")
        )
    findings = [
        f"{path}: generated/archive evidence must not be tracked"
        for path in names
        if path and prohibited_path(path)
    ]
    for path in changed:
        if not path or not checks_local_references(path):
            continue
        patch = git(
            root,
            *diff_args,
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--unified=0",
            "--no-color",
            "--",
            path,
        ).decode("utf-8", errors="replace")
        findings.extend(
            f"{path}:{number}: machine-local reference in new audit/validation text"
            for number in added_reference_lines(patch)
        )
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--staged", action="store_true", help="Check the Git index (default)")
    selection.add_argument("--revision", help="Check a commit and its new audit/validation lines")
    args = parser.parse_args()
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").strip())
        findings = check(root, args.revision)
    except (OSError, ValueError, subprocess.CalledProcessError):
        print(
            "Publication boundary check could not read the requested Git snapshot.", file=sys.stderr
        )
        return 1
    if findings:
        # Only locations and categories: never echo matching source lines or credential values.
        print("Publication boundary check failed:", file=sys.stderr)
        for item in findings:
            print(item, file=sys.stderr)
        return 1
    print("Publication boundary check passed (paths and new audit/validation references only).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
