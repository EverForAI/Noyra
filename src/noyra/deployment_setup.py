"""Safe, testable primitives for Noyra deployment setup."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat as stat_module
import subprocess
import tempfile
from collections.abc import Sequence
from contextlib import suppress
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
    non_interactive: bool = False
    replace: bool = False
    backup_root: Path = Path("/var/backups/noyra")
    server: str = "server"


@dataclass
class SetupResult:
    ok: bool = True
    stdout: str = ""
    stderr: str = ""
    actions: list[str] = field(default_factory=list)

    @property
    def exit_code(self) -> int:
        """CLI-compatible status while retaining the structured result."""
        return 0 if self.ok else 1


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
                "        header_up X-Real-IP {remote_host}",
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
    if source.is_symlink() or source.parent.is_symlink():
        raise SetupError("SYMLINK_PATH", "refusing to back up a symlink")
    if not source.exists() or not source.is_file():
        raise SetupError("BACKUP_SOURCE_MISSING", f"file does not exist: {source}")
    root = Path(backup_root)
    root.mkdir(parents=True, exist_ok=True)
    try:
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise SetupError("BACKUP_UNREADABLE", f"cannot read source: {source}") from exc
    target = root / f"{source.name}.{digest[:12]}.bak"
    shutil.copy2(source, target)
    stat = source.stat()
    metadata_path = target.with_name(target.name + ".metadata.json")
    metadata = {
        "original_path": str(source),
        "backup_path": str(target),
        "sha256": digest,
        "mode": mode,
        "file_mode": stat_module.S_IMODE(stat.st_mode),
        "uid": getattr(stat, "st_uid", None),
        "gid": getattr(stat, "st_gid", None),
    }
    metadata_path.write_text(json.dumps(metadata, sort_keys=True), encoding="utf-8")
    return BackupRecord(
        source,
        target,
        metadata_path,
        digest,
        stat_module.S_IMODE(stat.st_mode),
        getattr(stat, "st_uid", None),
        getattr(stat, "st_gid", None),
    )


def restore_backup(record: BackupRecord) -> None:
    euid = os.geteuid() if hasattr(os, "geteuid") else None

    def has_symlink_component(path: Path) -> bool:
        current = path
        while current != current.parent:
            try:
                if current.is_symlink():
                    return True
            except OSError as exc:
                raise SetupError("BACKUP_PATH", "cannot inspect restore path") from exc
            current = current.parent
        return False

    if has_symlink_component(record.backup_path) or has_symlink_component(record.original_path):
        raise SetupError("SYMLINK_PATH", "refusing to restore through a symlink")
    if euid is not None and euid != 0 and record.uid is not None and record.uid != euid:
        raise SetupError("BACKUP_PERMISSION", "recorded owner requires root")
    try:
        backup_stat = record.backup_path.stat()
        if euid is not None and euid != 0 and getattr(backup_stat, "st_uid", None) == 0:
            raise SetupError("BACKUP_PERMISSION", "root-owned backup requires root")
        if (
            record.uid is not None
            and hasattr(backup_stat, "st_uid")
            and backup_stat.st_uid != record.uid
        ):
            raise SetupError("BACKUP_OWNERSHIP", "backup ownership does not match metadata")
        digest = hashlib.sha256(record.backup_path.read_bytes()).hexdigest()
    except SetupError:
        raise
    except (OSError, ValueError) as exc:
        raise SetupError("BACKUP_UNREADABLE", f"cannot read backup: {record.backup_path}") from exc
    if digest != record.sha256:
        raise SetupError("BACKUP_CHECKSUM", "backup checksum verification failed")
    if record.original_path.exists() and record.uid is not None:
        try:
            target_stat = record.original_path.stat()
            if euid is not None and euid != 0 and getattr(target_stat, "st_uid", None) != euid:
                raise SetupError("BACKUP_PERMISSION", "target ownership requires root")
            if (
                euid is not None
                and euid != 0
                and hasattr(target_stat, "st_uid")
                and target_stat.st_uid != record.uid
            ):
                raise SetupError("BACKUP_OWNERSHIP", "target ownership does not match metadata")
        except SetupError:
            raise
        except OSError as exc:
            raise SetupError("BACKUP_TARGET", "cannot inspect restore target") from exc
    try:
        record.original_path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(
            prefix=f".{record.original_path.name}.", dir=record.original_path.parent
        )
    except OSError as exc:
        raise SetupError("BACKUP_TARGET", "cannot create atomic restore target") from exc
    os.close(fd)
    temp = Path(temp_name)
    try:
        shutil.copy2(record.backup_path, temp)
        os.chmod(temp, record.mode if record.mode is not None else 0o600)
        if record.uid is not None and hasattr(os, "chown"):
            os.chown(temp, record.uid, record.gid if record.gid is not None else -1)
        os.replace(temp, record.original_path)
    except OSError as exc:
        raise SetupError("BACKUP_RESTORE", "atomic restore failed") from exc
    finally:
        try:
            temp.unlink(missing_ok=True)
        except OSError as exc:
            raise SetupError("BACKUP_CLEANUP", "cannot remove temporary restore file") from exc


class SetupRunner:
    def __init__(self, options: SetupOptions, runner: CommandRunner | None = None):
        self.options = options
        self.runner = runner or SubprocessRunner()

    def run(self) -> SetupResult:
        if self.options.mode == "local":
            return self.run_local()
        if self.options.mode == "public":
            return self.run_public()
        if self.options.mode == "cloudflare":
            return self.run_cloudflare()
        raise SetupError("INVALID_MODE", f"unsupported deployment mode: {self.options.mode}")

    def run_local(self) -> SetupResult:
        actions = [f"validate mode {self.options.mode}"]
        if self.options.env_path.is_symlink():
            raise SetupError("ENV_SYMLINK", "environment path must not be a symlink")
        if not self.options.env_path.exists():
            raise SetupError(
                "ENV_MISSING", f"environment file does not exist: {self.options.env_path}"
            )
        if not self.options.env_path.is_file():
            raise SetupError(
                "ENV_NOT_FILE", f"environment path is not a file: {self.options.env_path}"
            )
        actions.append(f"validate environment file {self.options.env_path}")
        assignments = {
            key: value
            for key, value in parse_env_file(
                self.options.env_path.read_text(encoding="utf-8")
            )
            if value is not None and key and not key.startswith("#")
        }
        if assignments.get("NOYRA_HOST") != "127.0.0.1":
            raise SetupError("UNSAFE_LISTENER", "local mode requires NOYRA_HOST=127.0.0.1")
        actions.append("validate NOYRA_HOST=127.0.0.1")

        token_file = assignments.get("NOYRA_OPERATOR_TOKEN_FILE")
        token_inline = assignments.get("NOYRA_OPERATOR_TOKEN")
        has_token = bool(token_inline)
        if token_file:
            token_path = Path(token_file)
            with suppress(OSError):
                has_token = has_token or bool(token_path.read_text(encoding="utf-8").strip())
        if not has_token:
            raise SetupError("OPERATOR_TOKEN_MISSING", "an operator token source is required")
        actions.append("validate operator token source")

        if self.options.dry_run:
            actions.extend(
                [
                    "check root execution (deferred in dry-run)",
                    "check /var/lib/noyra mount visibility (deferred in dry-run)",
                    "check systemctl is-active noyra (deferred in dry-run)",
                    "check live and ready health endpoints (deferred in dry-run)",
                    f"SSH: ssh -N -L 8765:127.0.0.1:8765 {self.options.server}",
                ]
            )
            return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")

        if platform.system().lower() == "linux":
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                raise SetupError("ROOT_REQUIRED", "local checks require root")
            mount = self.runner.run(
                ("findmnt", "-n", "-o", "SOURCE", "--target", "/var/lib/noyra"),
                check=False,
            )
            if mount.returncode != 0 or not mount.stdout.strip():
                raise SetupError("DATA_MOUNT_MISSING", "/var/lib/noyra is not visible")
            actions.append("validate /var/lib/noyra mount visibility")
            active = self.runner.run(("systemctl", "is-active", "noyra"), check=False)
            if active.returncode != 0 or active.stdout.strip() != "active":
                raise SetupError("SERVICE_INACTIVE", "noyra service is not active")
            actions.append("validate systemctl is-active noyra")
            for endpoint in ("live", "ready"):
                check = self.runner.run(
                    ("curl", "--fail", f"http://127.0.0.1:8765/health/{endpoint}"), check=False
                )
                if check.returncode != 0:
                    raise SetupError(
                        "HEALTHCHECK_FAILED", f"health endpoint is unavailable: {endpoint}"
                    )
                actions.append(f"validate /health/{endpoint}")
        return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")

    def run_public(
        self,
        *,
        public_domain: str | None = None,
        admin_domain: str | None = None,
        dry_run: bool | None = None,
    ) -> SetupResult:
        """Publish the public HTTPS proxy and roll back both files on failure."""
        actions: list[str] = ["validate mode public"]
        selected_public = public_domain if public_domain is not None else self.options.public_domain
        selected_admin = admin_domain if admin_domain is not None else self.options.admin_domain
        selected_dry_run = self.options.dry_run if dry_run is None else dry_run

        def failure(error: SetupError) -> SetupResult:
            return SetupResult(ok=False, stderr=str(error), actions=actions)

        try:
            if not selected_public or not selected_admin:
                raise SetupError(
                    "DOMAINS_REQUIRED", "public and admin domains are required for public mode"
                )
            public = validate_hostname(selected_public)
            admin = validate_hostname(selected_admin)
            actions.extend([f"validate public domain {public}", f"validate admin domain {admin}"])

            env_path = self.options.env_path
            caddy_path = self.options.caddy_path
            if env_path.is_symlink():
                raise SetupError("ENV_SYMLINK", "environment path must not be a symlink")
            if not env_path.exists() or not env_path.is_file():
                raise SetupError("ENV_MISSING", f"environment file does not exist: {env_path}")
            if caddy_path.is_symlink():
                raise SetupError("CADDY_SYMLINK", "Caddyfile path must not be a symlink")
            if caddy_path.exists() and not caddy_path.is_file():
                raise SetupError("CADDY_NOT_FILE", f"Caddy path is not a file: {caddy_path}")
            if caddy_path.exists() and not self.options.replace:
                raise SetupError(
                    "PROXY_EXISTS", f"refusing to replace existing Caddyfile: {caddy_path}"
                )
            actions.extend(
                [f"validate environment file {env_path}", f"validate Caddy path {caddy_path}"]
            )

            env_text = env_path.read_text(encoding="utf-8")
            updated_env = update_env_text(
                env_text,
                {
                    "NOYRA_PUBLIC_SITE_URL": f"https://{public}",
                    "NOYRA_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
                    "NOYRA_ADMIN_SESSION_COOKIE_SECURE": "true",
                },
            )
            rendered_caddy = render_caddyfile(public, admin)
            actions.append("render HTTPS public and admin proxy blocks")
            if selected_dry_run:
                actions.extend(
                    [
                        "write environment file atomically (deferred in dry-run)",
                        "write Caddyfile atomically (deferred in dry-run)",
                        "validate Caddy configuration (deferred in dry-run)",
                        "reload caddy and restart noyra (deferred in dry-run)",
                        "validate local and HTTPS health endpoints (deferred in dry-run)",
                    ]
                )
                return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")

            env_backup = create_backup(env_path, self.options.backup_root, "public-env")
            caddy_backup = (
                create_backup(caddy_path, self.options.backup_root, "public-caddy")
                if caddy_path.exists()
                else None
            )
            replaced_caddy = False

            try:
                self._atomic_write(env_path, updated_env)
                self._atomic_write(caddy_path, rendered_caddy)
                replaced_caddy = True
                actions.extend(["publish environment file", "publish Caddyfile"])
                self._run_checked(
                    ("caddy", "validate", "--config", str(caddy_path)),
                    "CADDY_VALIDATE_FAILED",
                )
                actions.append("validate Caddy configuration")
                self._run_checked(("systemctl", "reload", "caddy"), "CADDY_RELOAD_FAILED")
                actions.append("reload caddy")
                self._run_checked(("systemctl", "restart", "noyra"), "SERVICE_RESTART_FAILED")
                actions.append("restart noyra")
                self._run_health_checks(public, admin, actions)
            except SetupError:
                self._rollback_public(
                    env_backup, caddy_backup, caddy_path if replaced_caddy else None
                )
                raise
            return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")
        except (SetupError, OSError, UnicodeError) as exc:
            error = (
                exc
                if isinstance(exc, SetupError)
                else SetupError("PUBLIC_SETUP_FAILED", str(exc))
            )
            return failure(error)

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write a root-readable temporary file and atomically publish it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(temp_name)
        try:
            os.chmod(temp, 0o640)
            if hasattr(os, "geteuid") and os.geteuid() == 0 and hasattr(os, "chown"):
                os.chown(temp, 0, -1)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temp, path)
        except OSError as exc:
            with suppress(OSError):
                os.close(fd)
            raise SetupError("ATOMIC_WRITE_FAILED", f"cannot publish {path}") from exc
        finally:
            with suppress(OSError):
                temp.unlink()

    def _run_checked(self, argv: Sequence[str], code: str) -> CompletedProcess[str]:
        result = self.runner.run(argv, check=False)
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "command failed").strip()
            raise SetupError(code, detail)
        return result

    def _run_health_checks(self, public: str, admin: str, actions: list[str]) -> None:
        for endpoint in ("live", "ready"):
            self._run_checked(
                ("curl", "--fail", f"http://127.0.0.1:8765/health/{endpoint}"),
                "HEALTHCHECK_FAILED",
            )
            actions.append(f"validate local /health/{endpoint}")
        for domain in (public, admin):
            self._run_checked(
                ("curl", "--fail", f"https://{domain}/health/ready"),
                "HTTPS_CHECK_FAILED",
            )
            actions.append(f"validate HTTPS health for {domain}")

    def _rollback_public(
        self,
        env_backup: BackupRecord,
        caddy_backup: BackupRecord | None,
        new_caddy_path: Path | None,
    ) -> None:
        with suppress(SetupError):
            restore_backup(env_backup)
        if caddy_backup is not None:
            with suppress(SetupError):
                restore_backup(caddy_backup)
        elif new_caddy_path is not None:
            with suppress(OSError):
                new_caddy_path.unlink()
        with suppress(Exception):
            self.runner.run(("systemctl", "restart", "noyra"), check=False)
        with suppress(Exception):
            self.runner.run(("systemctl", "reload", "caddy"), check=False)

    def run_cloudflare(self) -> SetupResult:
        raise SetupError(
            "NOT_IMPLEMENTED", "cloudflare deployment mode is implemented in a later task"
        )
