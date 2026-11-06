from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from test_publication_boundary import GUARD, SCRIPT, command, commit, git
from test_publication_boundary import repository as repository


def note(repository: Path, content: str, name: str = "docs/audit/new.md") -> str:
    path = repository / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repository, "add", "--", name)
    return commit(repository)


def safe_tip(repository: Path) -> str:
    (repository / "README.md").write_text("Synthetic later safe change\n", encoding="utf-8")
    git(repository, "add", "--", "README.md")
    return commit(repository)


def test_range_catches_earlier_introduction_even_when_tip_is_safe(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    introduced = note(repository, "Synthetic marker X:/private-workstation/results\n")
    tip = safe_tip(repository)
    assert GUARD["check"](repository, tip) == []  # Existing single-commit contract.
    findings = GUARD["check"](repository, tip, base=base)
    assert findings and any(introduced[:12] in item for item in findings)
    assert all("private-workstation" not in item for item in findings)


@pytest.mark.parametrize("kind", ["text", "output", "bundle"])
def test_range_checks_history_even_if_tip_removes_the_unpublishable_content(
    repository: Path,
    kind: str,
) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    name = {
        "text": "docs/roadmap/temporary.md",
        "output": "output/synthetic.txt",
        "bundle": "old.bundle",
    }[kind]
    introduced = note(repository, "Synthetic X:/private-workstation/transient\n", name)
    git(repository, "rm", "--", name)
    commit(repository)
    tip = safe_tip(repository)
    assert GUARD["check"](repository, tip) == []
    assert any(introduced[:12] in item for item in GUARD["check"](repository, tip, base=base))


def test_side_history_is_checked_even_for_a_tree_unchanged_merge(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    git(repository, "checkout", "--quiet", "-b", "candidate")
    introduced = note(repository, "Synthetic /mnt/z/private-workstation/results\n")
    git(repository, "checkout", "--quiet", "main")
    safe_tip(repository)
    git(
        repository,
        "-c",
        "commit.gpgSign=false",
        "merge",
        "--quiet",
        "--no-ff",
        "-s",
        "ours",
        "candidate",
        "-m",
        "synthetic unchanged tree merge",
    )
    tip = git(repository, "rev-parse", "HEAD").decode().strip()
    assert GUARD["check"](repository, tip) == []
    findings = GUARD["check"](repository, tip, base=base)
    assert any(introduced[:12] in item for item in findings)


def test_resurfacing_legacy_parent_content_is_compared_to_the_trusted_base(
    repository: Path,
) -> None:
    legacy = note(repository, "Synthetic X:/legacy/private-workstation\n")
    git(repository, "rm", "--", "docs/audit/new.md")
    trusted = commit(repository)
    git(repository, "checkout", "--quiet", "-b", "older-branch", legacy)
    child = safe_tip(repository)
    git(repository, "checkout", "--quiet", "main")
    git(
        repository,
        "-c",
        "commit.gpgSign=false",
        "merge",
        "--quiet",
        "--no-ff",
        "older-branch",
        "-m",
        "synthetic resurfaced legacy snapshot",
    )
    tip = git(repository, "rev-parse", "HEAD").decode().strip()
    assert any(child[:12] in finding for finding in GUARD["check"](repository, tip, base=trusted))


def test_reviewed_baseline_exemption_is_path_bound_and_does_not_rescan_ancestors(
    repository: Path,
) -> None:
    old = note(repository, "Synthetic X:/reviewed-legacy-reference\n")
    note(repository, "Synthetic X:/reviewed-legacy-reference\nNew safe summary\n")
    (repository / "docs/deployment").mkdir(parents=True)
    (repository / "docs/deployment/example.md").write_text(
        "X:/synthetic/deployment\n", encoding="utf-8"
    )
    git(repository, "add", "--", "docs/deployment/example.md")
    tip = commit(repository)
    assert GUARD["check"](repository, tip, base=old) == []
    git(repository, "mv", "--", "docs/audit/new.md", "docs/audit/moved.md")
    moved = commit(repository)
    assert GUARD["check"](repository, moved, base=old)


def test_binary_attribute_cannot_hide_new_policy_text(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    (repository / ".gitattributes").write_text("docs/audit/*.md binary\n", encoding="utf-8")
    git(repository, "add", "--", ".gitattributes")
    introduced = note(repository, "Synthetic X:/private-workstation/binary-hidden\n")
    tip = safe_tip(repository)
    assert any(introduced[:12] in item for item in GUARD["check"](repository, tip, base=base))


@pytest.mark.parametrize("which", ["same", "unrelated", "reversed", "missing"])
def test_invalid_or_empty_trust_range_fails_instead_of_falling_back(
    repository: Path, which: str
) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    tip = safe_tip(repository)
    if which == "same":
        base = tip
    elif which == "reversed":
        base, tip = tip, base
    elif which == "missing":
        base = "0" * 40
    else:
        git(repository, "checkout", "--quiet", "--orphan", "unrelated")
        (repository / "README.md").write_text("Synthetic unrelated root\n", encoding="utf-8")
        git(repository, "add", "--", "README.md")
        base = commit(repository)
    with pytest.raises((ValueError, subprocess.CalledProcessError)):
        GUARD["check"](repository, tip, base=base)


def test_range_uses_pinned_git_blobs_not_dirty_worktree_or_index(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    tip = note(repository, "Synthetic safe summary\n")
    note_path = repository / "docs/audit/new.md"
    note_path.write_text("Synthetic X:/not-in-selected-commit\n", encoding="utf-8")
    git(repository, "add", "--", "docs/audit/new.md")
    assert GUARD["check"](repository, tip, base=base) == []


def test_range_budget_and_shallow_history_fail_closed(repository: Path, tmp_path: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    tip = note(repository, "Synthetic safe note\n")
    with (
        patch.dict(GUARD["check"].__globals__, {"MAX_RANGE_COMMITS": 0}),
        pytest.raises(ValueError),
    ):
        GUARD["check"](repository, tip, base=base)
    clone = tmp_path / "shallow"
    git(repository, "clone", "--quiet", "--depth", "2", repository.as_uri(), str(clone))
    assert git(clone, "rev-parse", "--is-shallow-repository").strip() == b"true"
    with pytest.raises(ValueError, match="shallow"):
        GUARD["check"](clone, tip, base=base)


def test_cli_requires_revision_for_base_and_accepts_valid_explicit_range(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    tip = note(repository, "Synthetic reviewed summary\n")
    assert command(repository, "--base", base, "--staged").returncode != 0
    assert command(repository, "--base", base).returncode != 0
    assert command(repository, "--base", base, "--revision", tip).returncode == 0


def event_environment(
    repository: Path, tmp_path: Path, name: str, payload: dict[str, Any], **extra: str
) -> dict[str, str]:
    path = tmp_path / "event.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return {
        **os.environ,
        "GITHUB_EVENT_PATH": str(path),
        "GITHUB_EVENT_NAME": name,
        "GITHUB_SHA": git(repository, "rev-parse", "HEAD").decode().strip(),
        "NOYRA_PUBLICATION_REVIEW_BASE": "",
        **extra,
    }


def run_event(repository: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--github-event"],
        cwd=repository,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_push_event_checks_all_introductions_and_emits_no_payload_text(
    repository: Path, tmp_path: Path
) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    note(repository, "Synthetic private marker X:/private-workstation/earlier\n")
    tip = safe_tip(repository)
    env = event_environment(
        repository,
        tmp_path,
        "push",
        {"ref": "refs/heads/main", "before": base, "after": tip},
        GITHUB_REF="refs/heads/main",
    )
    assert GUARD["github_scope"](repository, env) == (base, tip)
    result = run_event(repository, env)
    assert result.returncode == 1 and "new.md" in result.stderr
    assert "private-workstation" not in result.stderr and "earlier" not in result.stderr


def test_pull_request_uses_exact_merge_candidate_including_side_history(
    repository: Path, tmp_path: Path
) -> None:
    git(repository, "checkout", "--quiet", "-b", "candidate")
    introduced = note(repository, "Synthetic X:/private-workstation/branch\n")
    head = note(repository, "Synthetic unrelated safe source\n", "OTHER.md")
    git(repository, "checkout", "--quiet", "main")
    base = safe_tip(repository)
    git(
        repository,
        "-c",
        "commit.gpgSign=false",
        "merge",
        "--quiet",
        "--no-ff",
        "candidate",
        "-m",
        "synthetic PR merge",
    )
    tip = git(repository, "rev-parse", "HEAD").decode().strip()
    env = event_environment(
        repository,
        tmp_path,
        "pull_request",
        {
            "pull_request": {"base": {"sha": base}, "head": {"sha": head}},
            "body": "Synthetic untrusted $(do-not-execute) text",
        },
    )
    assert GUARD["github_scope"](repository, env) == (base, tip)
    assert any(introduced[:12] in item for item in GUARD["check"](repository, tip, base=base))
    result = run_event(repository, env)
    assert result.returncode == 1 and "do-not-execute" not in result.stderr


@pytest.mark.parametrize("mode", ["manual_input", "manual_variable", "tag", "annotated_tag"])
def test_manual_and_tag_review_require_explicit_immutable_baseline(
    repository: Path, tmp_path: Path, mode: str
) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    tip = note(repository, "Synthetic safe note\n")
    if mode.startswith("manual"):
        payload = (
            {"inputs": {"publication_base": base}} if mode == "manual_input" else {"inputs": {}}
        )
        env = event_environment(repository, tmp_path, "workflow_dispatch", payload)
        if mode == "manual_variable":
            env["NOYRA_PUBLICATION_REVIEW_BASE"] = base
    else:
        after = tip
        if mode == "annotated_tag":
            git(repository, "-c", "tag.gpgSign=false", "tag", "-a", "v-test", "-m", "synthetic tag")
            after = git(repository, "rev-parse", "v-test").decode().strip()
        env = event_environment(
            repository,
            tmp_path,
            "push",
            {"ref": "refs/tags/v-test", "before": "0" * 40, "after": after},
            GITHUB_REF="refs/tags/v-test",
            NOYRA_PUBLICATION_REVIEW_BASE=base,
        )
    assert GUARD["github_scope"](repository, env) == (base, tip)
    assert run_event(repository, env).returncode == 0


@pytest.mark.parametrize(
    "failure",
    [
        "manual_missing",
        "manual_same_tip",
        "tag_missing",
        "branch_before_zero",
        "wrong_after",
        "wrong_ref",
        "wrong_checkout",
        "unsupported_event",
        "input_shell_text",
        "incomplete_pr",
        "not_merge_pr",
        "bad_json",
        "large_event",
    ],
)
def test_bad_event_scope_never_falls_back_to_single_commit_review(
    repository: Path, tmp_path: Path, failure: str
) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    tip = note(repository, "Synthetic safe tip\n")
    payload: dict[str, Any] = {"ref": "refs/heads/main", "before": base, "after": tip}
    name = "push"
    extra = {"GITHUB_REF": "refs/heads/main"}
    if failure.startswith("manual") or failure == "input_shell_text":
        name = "workflow_dispatch"
        payload = {"inputs": {}}
        if failure == "manual_same_tip":
            payload["inputs"] = {"publication_base": tip}
        if failure == "input_shell_text":
            payload["inputs"] = {"publication_base": "$(do-not-execute)"}
    elif failure == "tag_missing":
        payload["ref"] = "refs/tags/v-test"
        extra["GITHUB_REF"] = payload["ref"]
    elif failure == "branch_before_zero":
        payload["before"] = "0" * 40
    elif failure == "wrong_after":
        payload["after"] = base
    elif failure == "wrong_ref":
        extra["GITHUB_REF"] = "refs/heads/different"
    elif failure == "wrong_checkout":
        extra["GITHUB_SHA"] = base
    elif failure == "unsupported_event":
        name = "pull_request_target"
    elif failure in {"incomplete_pr", "not_merge_pr"}:
        name = "pull_request"
        payload = (
            {}
            if failure == "incomplete_pr"
            else {"pull_request": {"base": {"sha": base}, "head": {"sha": tip}}}
        )
    env = event_environment(repository, tmp_path, name, payload, **extra)
    if failure == "bad_json":
        Path(env["GITHUB_EVENT_PATH"]).write_text("{", encoding="utf-8")
    if failure == "large_event":
        Path(env["GITHUB_EVENT_PATH"]).write_bytes(b" " * (GUARD["MAX_EVENT_BYTES"] + 1))
    result = run_event(repository, env)
    assert result.returncode != 0 and "passed" not in result.stdout
    assert "do-not-execute" not in result.stderr


def test_local_git_replace_cannot_hide_original_introduced_objects(repository: Path) -> None:
    base = git(repository, "rev-parse", "HEAD").decode().strip()
    introduced = note(repository, "Synthetic X:/private-workstation/replaced\n")
    git(repository, "rm", "--", "docs/audit/new.md")
    tip = commit(repository)
    git(repository, "replace", introduced, base)
    findings = GUARD["check"](repository, tip, base=base)
    assert any(introduced[:12] in item for item in findings)
