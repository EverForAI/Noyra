#!/usr/bin/env python3
"""Check publication paths and new machine-local references, without reading secrets."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

RAW_EVIDENCE = re.compile(r"^docs/audit/[^/]*-evidence/", re.IGNORECASE)
MAX_RANGE_COMMITS = 4096
MAX_EVENT_BYTES = 2 * 1024 * 1024

LOCAL_REFERENCE = re.compile(r"(?i)(?<![a-z0-9])[a-z]:[\\/]|/mnt/[a-z]/")


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-c", "core.quotePath=false", *args],
        cwd=root,
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_NO_REPLACE_OBJECTS": "1"},
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


def resolve_commit(root: Path, revision: str) -> str:
    return (
        git(root, "rev-parse", "--verify", "--end-of-options", revision + "^{commit}")
        .decode("ascii")
        .strip()
    )


def _snapshot_paths(root: Path, commit: str) -> list[str]:
    return git(root, "ls-tree", "-rz", "--name-only", commit).decode("utf-8").split("\0")


def _path_findings(names: list[str]) -> list[str]:
    return [
        f"{path}: generated/archive evidence must not be tracked"
        for path in names
        if path and prohibited_path(path)
    ]


def _text_findings(root: Path, diff_args: tuple[str, ...]) -> list[str]:
    # Force policy text to remain visible even if a candidate .gitattributes
    # marks it binary. Content and diagnostics are always from fixed Git blobs.
    changed = (
        git(
            root,
            *diff_args,
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--name-only",
            "-z",
            "--diff-filter=ACMRT",
        )
        .decode("utf-8")
        .split("\0")
    )
    findings: list[str] = []
    for path in changed:
        if not path or not checks_local_references(path):
            continue
        patch = git(
            root,
            *diff_args,
            "--no-ext-diff",
            "--no-textconv",
            "--no-renames",
            "--text",
            "--unified=0",
            "--no-color",
            "--",
            path,
        ).decode("utf-8")
        findings.extend(
            f"{path}:{number}: machine-local reference in new audit/validation text"
            for number in added_reference_lines(patch)
        )
    return findings


def _range_findings(root: Path, commit: str, base: str) -> list[str]:
    if git(root, "rev-parse", "--is-shallow-repository").strip() != b"false":
        raise ValueError("publication range requires non-shallow history")
    trusted = resolve_commit(root, base)
    if trusted == commit:
        raise ValueError("publication base must precede the selected commit")
    git(root, "merge-base", "--is-ancestor", trusted, commit)
    introduced = (
        git(
            root,
            "rev-list",
            "--reverse",
            "--topo-order",
            f"--max-count={MAX_RANGE_COMMITS + 1}",
            trusted + ".." + commit,
        )
        .decode("ascii")
        .splitlines()
    )
    if not introduced or len(introduced) > MAX_RANGE_COMMITS:
        raise ValueError("publication range exceeds reviewed work bound")
    findings: list[str] = []
    for candidate in introduced:
        # Inspect every newly reachable snapshot against the trusted tree.
        # This covers transient/deleted content and side history whose final
        # merge has no tree change, as well as old parent content resurfacing.
        current = _path_findings(_snapshot_paths(root, candidate))
        current.extend(_text_findings(root, ("diff", trusted, candidate)))
        findings.extend(f"[{candidate[:12]}] {finding}" for finding in current)
    return findings


def check(root: Path, revision: str | None, *, base: str | None = None) -> list[str]:
    diff_args: tuple[str, ...]
    if base is not None:
        if revision is None:
            raise ValueError("publication base requires a selected revision")
        return _range_findings(root, resolve_commit(root, revision), base)
    if revision is None:
        names = git(root, "ls-files", "-z").decode("utf-8").split("\0")
        diff_args = ("diff", "--cached")
    else:
        commit = resolve_commit(root, revision)
        names = _snapshot_paths(root, commit)
        parents = git(root, "rev-list", "--parents", "-n", "1", commit).decode("ascii").split()
        diff_args = (
            ("diff", parents[1], commit)
            if len(parents) > 1
            else (
                "show",
                "--format=",
                "--root",
                commit,
            )
        )
    return [*_path_findings(names), *_text_findings(root, diff_args)]


def _event_oid(value: Any) -> str:
    if not isinstance(value, str) or re.fullmatch(r"(?:[a-f0-9]{40}|[a-f0-9]{64})", value) is None:
        raise ValueError("publication event requires an exact object ID")
    if not value.strip("0"):
        raise ValueError("publication event has no reviewed before commit")
    return value


def _field(value: Any, *names: str) -> Any:
    for name in names:
        if not isinstance(value, dict) or name not in value:
            raise ValueError("publication event scope is incomplete")
        value = value[name]
    return value


def github_scope(root: Path, environment: Mapping[str, str]) -> tuple[str, str]:
    """Resolve immutable event scope without shell interpolation or guessing a base.

    The event file is platform metadata. Manual/tag baselines are a maintainer
    trust decision, never a candidate-controlled file or a guessed parent.
    """
    with Path(environment.get("GITHUB_EVENT_PATH", "")).open("rb") as stream:
        raw = stream.read(MAX_EVENT_BYTES + 1)
    if not raw or len(raw) > MAX_EVENT_BYTES:
        raise ValueError("publication event exceeds its metadata bound")
    event = json.loads(raw)
    if not isinstance(event, dict):
        raise ValueError("invalid publication event")
    tip = resolve_commit(root, _event_oid(environment.get("GITHUB_SHA")))
    if resolve_commit(root, "HEAD") != tip:
        raise ValueError("publication checkout does not match the event commit")
    kind = environment.get("GITHUB_EVENT_NAME")
    if kind == "pull_request":
        base = _event_oid(_field(event, "pull_request", "base", "sha"))
        head = resolve_commit(root, _event_oid(_field(event, "pull_request", "head", "sha")))
        parents = git(root, "rev-list", "--parents", "-n", "1", tip).decode("ascii").split()
        if parents != [tip, resolve_commit(root, base), head]:
            raise ValueError("publication PR checkout is not the event's exact merge candidate")
    elif kind == "push":
        ref = _field(event, "ref")
        if not isinstance(ref, str) or ref != environment.get("GITHUB_REF"):
            raise ValueError("publication push ref differs from event")
        after = resolve_commit(root, _event_oid(_field(event, "after")))
        if after != tip or event.get("deleted") is True:
            raise ValueError("publication push commit differs from event")
        if ref.startswith("refs/heads/"):
            base = _event_oid(_field(event, "before"))
        elif ref.startswith("refs/tags/"):
            base = _event_oid(environment.get("NOYRA_PUBLICATION_REVIEW_BASE"))
        else:
            raise ValueError("unsupported publication push ref")
    elif kind == "workflow_dispatch":
        inputs = event.get("inputs", {})
        if not isinstance(inputs, dict):
            raise ValueError("invalid publication dispatch inputs")
        supplied = inputs.get("publication_base")
        if supplied is None or supplied == "":
            supplied = environment.get("NOYRA_PUBLICATION_REVIEW_BASE")
        base = _event_oid(supplied)
    else:
        raise ValueError("unsupported publication event")
    return base, tip


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--staged", action="store_true", help="Check the Git index (default)")
    selection.add_argument(
        "--revision", help="Check a single commit; add --base for candidate-range review"
    )
    selection.add_argument(
        "--github-event", action="store_true", help="Use the trusted GitHub event scope"
    )
    parser.add_argument("--base", help="Trusted reviewed ancestor; inspect every introduced commit")
    args = parser.parse_args()
    if args.base is not None and args.revision is None:
        parser.error("--base requires --revision")
    try:
        root = Path(git(Path.cwd(), "rev-parse", "--show-toplevel").decode("utf-8").strip())
        if args.github_event:
            base, tip = github_scope(root, os.environ)
            findings = check(root, tip, base=base)
        else:
            findings = check(root, args.revision, base=args.base)
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
    scope = (
        "complete candidate range"
        if args.base is not None or args.github_event
        else "single snapshot/index"
    )
    print(
        f"Publication boundary check passed ({scope}; paths and new audit/validation references)."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
