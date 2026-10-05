"""Same-SHA release evidence and explicit development upgrade channels."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from .external_gates import validate_external_gates
from .types import strict_json_loads

CHANNEL_PATH = Path("/etc/noyra/upgrade-channel")
PUBLIC_KEY_PATH = Path("/etc/noyra/release-evidence-public-key")
CURRENT_PATH = Path("/opt/noyra/current")
EVIDENCE_NAME = ".noyra-external-gates.json"
CONTAINER_SOURCE_PATH = Path("/opt/noyra/.noyra-source-sha")
CONTAINER_EVIDENCE_PATH = Path("/run/noyra/release-assurance")


def controlled_text(path: Path, *, maximum: int) -> str:
    for parent in (path, *path.parents):
        metadata = parent.lstat()
        if parent.is_symlink() or (
            os.name == "posix" and (metadata.st_uid != 0 or metadata.st_mode & 0o022)
        ):
            raise ValueError("release_control_file_unsafe")
    if not stat.S_ISREG(path.stat().st_mode):
        raise ValueError("release_control_file_unsafe")
    with path.open("rb") as stream:
        raw = stream.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("release_control_file_oversized")
    return raw.decode("utf-8")


def upgrade_channel(path: Path = CHANNEL_PATH) -> str:
    try:
        value = controlled_text(path, maximum=64).strip()
    except FileNotFoundError:
        return "stable"
    if value not in {"stable", "development-main"}:
        raise ValueError("upgrade_channel_invalid")
    return value


def verify_evidence(
    record: dict[str, Any], sha: str, public_key: str, *, publication: datetime | None = None
) -> None:
    if not isinstance(record, dict):
        raise ValueError("release_evidence_invalid")
    errors = validate_external_gates(
        record,
        expected_sha=sha,
        public_key=public_key,
        now=publication,
        enforce_freshness=publication is not None,
    )
    if errors:
        raise ValueError("release_evidence_invalid:" + ",".join(errors))


def current_evidence() -> dict[str, Any]:
    if os.getenv("NOYRA_DEPLOYMENT_PROFILE", "").strip().lower() == "container_internal":
        # The source marker is baked into the read-only image independently of
        # the mounted evidence; an environment claim cannot replace either.
        sha = controlled_text(CONTAINER_SOURCE_PATH, maximum=65).strip()
        evidence = CONTAINER_EVIDENCE_PATH / "external-gates.json"
        public_key_path = CONTAINER_EVIDENCE_PATH / "public-key"
    else:
        pointer = CURRENT_PATH.lstat()
        if not CURRENT_PATH.is_symlink() or (os.name == "posix" and pointer.st_uid != 0):
            raise ValueError("release_pointer_unsafe")
        release = CURRENT_PATH.resolve(strict=True)
        if release.parent != CURRENT_PATH.parent / "releases":
            raise ValueError("release_pointer_unsafe")
        sha = controlled_text(release / ".noyra-source-sha", maximum=65).strip()
        evidence = release / EVIDENCE_NAME
        public_key_path = PUBLIC_KEY_PATH
    record = strict_json_loads(controlled_text(evidence, maximum=256_000))
    public_key = controlled_text(public_key_path, maximum=128).strip()
    verify_evidence(record, sha, public_key)
    return {"status": "verified", "commit_sha": sha, "reviewed_at": record["reviewed_at"]}


def activation_status() -> dict[str, Any]:
    try:
        return current_evidence()
    except Exception:
        return {"status": "unverified", "commit_sha": None, "reviewed_at": None}


def require_activation_evidence(*, production: bool | None = None) -> None:
    required = (
        os.getenv("NOYRA_PROFILE", "development") == "production"
        if production is None
        else production
    )
    if not required:
        return
    try:
        current_evidence()
    except Exception:
        raise ValueError("production_release_evidence_required") from None


def github_json(url: str, *, maximum: int = 256_000) -> Any:
    if not url.startswith("https://api.github.com/repos/"):
        raise ValueError("invalid_release_metadata_url")
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "Noyra-release-assurance"},
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        raw = response.read(maximum + 1)
    if len(raw) > maximum:
        raise ValueError("release_metadata_oversized")
    return strict_json_loads(raw.decode("utf-8"))


def stable_metadata(owner: str, repo: str) -> dict[str, Any]:
    base = f"https://api.github.com/repos/{owner}/{repo}"
    release = github_json(base + "/releases/latest")
    if not isinstance(release, dict):
        raise ValueError("stable_release_unavailable")
    tag = release.get("tag_name")
    if (
        not isinstance(tag, str)
        or not re.fullmatch(r"v[0-9][A-Za-z0-9._-]{0,99}", tag)
        or release.get("draft")
        or release.get("prerelease")
    ):
        raise ValueError("stable_release_unavailable")
    commit = github_json(base + "/commits/" + tag)
    if not isinstance(commit, dict) or not re.fullmatch(
        r"[0-9a-f]{40}", str(commit.get("sha", ""))
    ):
        raise ValueError("stable_release_unavailable")
    return {
        "sha": commit["sha"],
        "committed_at": commit["commit"]["committer"]["date"],
        "title": release.get("name") or tag,
        "tag": tag,
        "published_at": release["published_at"],
        "assets": release.get("assets", []),
    }


def verified_stable_target(expected_sha: str) -> tuple[str, dict[str, Any]]:
    metadata = stable_metadata("EverForAI", "Noyra")
    if metadata["sha"] != expected_sha:
        raise ValueError("upgrade_target_stale")
    # Require the latest quality workflow result for this exact source, not an
    # arbitrary successful check with a matching display name.
    runs = github_json(
        "https://api.github.com/repos/EverForAI/Noyra/actions/workflows/ci.yml/runs?head_sha="
        + expected_sha
        + "&per_page=10"
    )
    matching = [run for run in runs.get("workflow_runs", []) if run.get("head_sha") == expected_sha]
    latest: dict[str, Any] = max(matching, key=lambda run: int(run["id"]), default={})
    if latest.get("status") != "completed" or latest.get("conclusion") != "success":
        raise ValueError("upgrade_quality_required")
    url = (
        "https://github.com/EverForAI/Noyra/releases/download/"
        + metadata["tag"]
        + "/external-gates.json"
    )
    request = urllib.request.Request(url, headers={"User-Agent": "Noyra-release-assurance"})
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = response.read(256_001)
    if len(raw) > 256_000:
        raise ValueError("release_evidence_oversized")
    record = strict_json_loads(raw.decode("utf-8"))
    key = controlled_text(PUBLIC_KEY_PATH, maximum=128).strip()
    published = datetime.fromisoformat(metadata["published_at"].replace("Z", "+00:00"))
    if published.tzinfo is None:
        raise ValueError("release_publication_invalid")
    verify_evidence(record, expected_sha, key, publication=published)
    return metadata["tag"], record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", action="store_true")
    parser.add_argument("--verify-target")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.channel:
        print(upgrade_channel())
        return 0
    if not args.verify_target or args.output is None:
        parser.error("--verify-target and --output required")
    if not re.fullmatch(r"[0-9a-f]{40}", args.verify_target):
        raise ValueError("upgrade_target_invalid")
    tag, record = verified_stable_target(args.verify_target)
    if args.output.is_symlink() or args.output.exists():
        raise ValueError("release_evidence_output_unsafe")
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(record, stream, ensure_ascii=False, sort_keys=True)
        stream.write("\n")
    args.output.chmod(0o644)
    print(tag)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
