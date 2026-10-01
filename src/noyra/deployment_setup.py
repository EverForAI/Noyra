"""Safe, testable primitives for Noyra deployment setup."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from subprocess import CompletedProcess
from typing import Protocol


class SetupError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"NOYRA_SETUP_{code}: {message}")


@dataclass(frozen=True)
class SetupOptions:
    mode: str = "local"
    public_domain: str | None = None
    admin_domain: str | None = None
    env_path: Path = Path("/etc/noyra/noyra.env")
    caddy_path: Path = Path("/etc/caddy/Caddyfile")
    tunnel_token_path: Path | None = None
    dry_run: bool = False


@dataclass
class SetupResult:
    ok: bool = True
    stdout: str = ""
    stderr: str = ""
    actions: list[str] = field(default_factory=list)


def validate_hostname(value: str) -> str:
    if not isinstance(value, str) or not value or any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise SetupError("INVALID_HOSTNAME", "hostname must be a DNS name")
    value = value.lower()
    if (
        len(value) > 253
        or value.endswith(".")
        or "*" in value
        or "/" in value
        or ":" in value
        or "." not in value
    ):
        raise SetupError("INVALID_HOSTNAME", "hostname must be a DNS name")
    labels = value.split(".")
    if any(
        not label
        or len(label) > 63
        or label[0] == "-"
        or label[-1] == "-"
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise SetupError("INVALID_HOSTNAME", "hostname must be a DNS name")
    return value


def parse_env_file(text: str) -> list[tuple[str, str | None]]:
    """Parse assignments while retaining comments and opaque lines as entries."""
    result: list[tuple[str, str | None]] = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        match = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$", body)
        if match:
            value = match.group(2)
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            result.append((match.group(1), value))
        else:
            result.append((body, None))
    if not text:
        return []
    return result


def update_env_text(text: str, updates: dict[str, str | None]) -> str:
    """Update NOYRA assignments and preserve all other text and line endings."""
    lines = text.splitlines(keepends=True)
    seen: set[str] = set()
    output: list[str] = []
    for line in lines:
        ending = (
            "\r\n"
            if line.endswith("\r\n")
            else "\n"
            if line.endswith("\n")
            else "\r"
            if line.endswith("\r")
            else ""
        )
        body = line[: -len(ending)] if ending else line
        match = re.match(r"^(\s*)(?:export\s+)?(NOYRA_[A-Za-z0-9_]+)(\s*=).*?$", body)
        if match and match.group(2) in updates:
            key = match.group(2)
            value = updates[key]
            seen.add(key)
            output.append(f"{match.group(1)}{key}={value or ''}{ending}")
        else:
            output.append(line)
    newline = "\r\n" if "\r\n" in text else "\n"
    missing = [
        f"{key}={value or ''}"
        for key, value in updates.items()
        if key.startswith("NOYRA_") and key not in seen
    ]
    if missing:
        if output and not (output[-1].endswith("\n") or output[-1].endswith("\r")):
            output.append(newline)
        output.append(newline.join(missing) + newline)
    return "".join(output)


def render_caddyfile(public_domain: str, admin_domain: str) -> str:
    public = validate_hostname(public_domain)
    admin = validate_hostname(admin_domain)

    def block(host: str) -> str:
        return "\n".join(
            [
                f"{host} {{",
                "    encode zstd gzip",
                "    reverse_proxy 127.0.0.1:8765 {",
                "        header_up Host {host}",
                "        header_up X-Forwarded-Proto {scheme}",
                "        header_up X-Forwarded-For {remote_host}",
                "    }",
                "}",
                "",
            ]
        )

    return block(public) + "\n" + block(admin)


def redact_token(value: str | None) -> str | None:
    return "<redacted>" if value else value


class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> CompletedProcess[str]: ...


class SubprocessRunner:
    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> CompletedProcess[str]:
        return subprocess.run(argv, check=check, input=input_text, text=True, capture_output=True)


@dataclass(frozen=True)
class BackupRecord:
    original_path: Path
    backup_path: Path
    metadata_path: Path
    sha256: str
    mode: int | None = None
    uid: int | None = None
    gid: int | None = None


def create_backup(path: str | Path, backup_root: str | Path, mode: str = "default") -> BackupRecord:
    source = Path(path)
    if source.is_symlink():
        raise SetupError("SYMLINK_PATH", "refusing to back up a symlink")
    if not source.exists() or not source.is_file():
        raise SetupError("BACKUP_SOURCE_MISSING", f"file does not exist: {source}")
    root = Path(backup_root)
    root.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    target = root / f"{source.name}.{digest[:12]}.bak"
    shutil.copy2(source, target)
    stat = source.stat()
    metadata_path = target.with_name(target.name + ".metadata.json")
    metadata = {
        "original_path": str(source),
        "backup_path": str(target),
        "sha256": digest,
        "mode": mode,
        "file_mode": stat.st_mode,
        "uid": getattr(stat, "st_uid", None),
        "gid": getattr(stat, "st_gid", None),
    }
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    return BackupRecord(
        source,
        target,
        metadata_path,
        digest,
        stat.st_mode,
        getattr(stat, "st_uid", None),
        getattr(stat, "st_gid", None),
    )


def restore_backup(record: BackupRecord) -> None:
    if hashlib.sha256(record.backup_path.read_bytes()).hexdigest() != record.sha256:
        raise SetupError("BACKUP_CHECKSUM", "backup checksum verification failed")
    record.original_path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{record.original_path.name}.", dir=record.original_path.parent
    )
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(record.backup_path, temp)
        os.replace(temp, record.original_path)
    finally:
        temp.unlink(missing_ok=True)


class SetupRunner:
    def __init__(self, options: SetupOptions, runner: CommandRunner | None = None):
        self.options = options
        self.runner = runner or SubprocessRunner()

    def run(self) -> SetupResult:
        actions = [f"validate mode {self.options.mode}"]
        if self.options.public_domain and self.options.admin_domain:
            render_caddyfile(self.options.public_domain, self.options.admin_domain)
            actions.append("render caddyfile")
        return SetupResult(actions=actions)

    run_local = run
    run_public = run
    run_cloudflare = run
