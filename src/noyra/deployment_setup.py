"""Safe, testable primitives for Noyra deployment setup."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
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
    # A token collected through the CLI's hidden prompt.  It is intentionally
    # kept out of command output and is never persisted in the environment.
    tunnel_token: str | None = None
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


def _redact_secret(text: str, secret: str | None) -> str:
    if secret:
        return text.replace(secret, "<redacted>")
    return text


class CommandRunner(Protocol):
    def run(
        self, argv: Sequence[str], *, check: bool = True, input_text: str | None = None
    ) -> CompletedProcess[str]: ...


class SubprocessRunner:
    enforce_host_privileges = True

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
            for key, value in parse_env_file(self.options.env_path.read_text(encoding="utf-8"))
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
            if (
                not selected_dry_run
                and isinstance(self.runner, SubprocessRunner)
                and caddy_path != Path("/etc/caddy/Caddyfile")
            ):
                raise SetupError(
                    "CADDY_PATH_UNSUPPORTED",
                    "real public setup requires the default /etc/caddy/Caddyfile path",
                )
            if (
                not selected_dry_run
                and getattr(self.runner, "enforce_host_privileges", False)
                and hasattr(os, "geteuid")
                and os.geteuid() != 0
            ):
                raise SetupError("ROOT_REQUIRED", "public setup requires root")
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
            assignments = {
                key: value
                for key, value in parse_env_file(env_text)
                if value is not None and key and not key.startswith("#")
            }
            effective_host = assignments.get("NOYRA_HOST", "127.0.0.1")
            effective_port = assignments.get("NOYRA_PORT", "8765")
            if effective_host != "127.0.0.1":
                raise SetupError("UNSAFE_LISTENER", "public mode requires NOYRA_HOST=127.0.0.1")
            if effective_port != "8765":
                raise SetupError("UNSAFE_PORT", "public mode requires NOYRA_PORT=8765")
            actions.extend(["validate NOYRA_HOST=127.0.0.1", "validate NOYRA_PORT=8765"])
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

            self._require_dns_ready(public, admin)
            actions.append("validate DNS readiness for both HTTPS origins")

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
            except (SetupError, OSError, UnicodeError) as exc:
                try:
                    self._rollback_public(
                        env_backup, caddy_backup, caddy_path if replaced_caddy else None
                    )
                except SetupError as rollback_error:
                    raise rollback_error from exc
                if not isinstance(exc, SetupError):
                    raise SetupError("PUBLIC_SETUP_FAILED", str(exc)) from exc
                raise
            return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")
        except (SetupError, OSError, UnicodeError) as exc:
            error = (
                exc if isinstance(exc, SetupError) else SetupError("PUBLIC_SETUP_FAILED", str(exc))
            )
            return failure(error)

    def _atomic_write(self, path: Path, content: str) -> None:
        """Write a root-readable temporary file and atomically publish it."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(temp_name)
        try:
            os.chmod(temp, 0o640)
            chown = getattr(os, "chown", None)
            if hasattr(os, "geteuid") and os.geteuid() == 0 and callable(chown):
                chown(temp, 0, -1)
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

    def _atomic_write_protected(self, path: Path, content: str) -> None:
        """Publish a credential with mode 0600 without exposing its value."""
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temp = Path(temp_name)
        try:
            os.chmod(temp, 0o600)
            chown = getattr(os, "chown", None)
            if hasattr(os, "geteuid") and os.geteuid() == 0 and callable(chown):
                chown(temp, 0, -1)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                stream.write(content)
            os.replace(temp, path)
        except OSError as exc:
            with suppress(OSError):
                os.close(fd)
            raise SetupError(
                "TOKEN_WRITE_FAILED", "cannot publish Cloudflare tunnel credential"
            ) from exc
        finally:
            with suppress(OSError):
                temp.unlink()

    def _run_checked(
        self, argv: Sequence[str], code: str, *, secret: str | None = None
    ) -> CompletedProcess[str]:
        try:
            result = self.runner.run(argv, check=False)
        except OSError as exc:
            raise SetupError(code, f"command could not run: {argv[0]}") from exc
        if result.returncode != 0:
            detail = _redact_secret(
                (result.stderr or result.stdout or "command failed").strip(), secret
            )
            raise SetupError(code, detail)
        return result

    def _require_dns_ready(self, public: str, admin: str) -> None:
        for domain in (public, admin):
            try:
                socket.getaddrinfo(domain, 443)
            except OSError as exc:
                raise SetupError(
                    "DNS_PENDING",
                    f"DNS is not ready for {domain}; create A/AAAA records before setup",
                ) from exc

    def _run_health_checks(
        self, public: str, admin: str, actions: list[str], *, secret: str | None = None
    ) -> None:
        for endpoint in ("live", "ready"):
            self._run_checked(
                ("curl", "--fail", f"http://127.0.0.1:8765/health/{endpoint}"),
                "HEALTHCHECK_FAILED",
                secret=secret,
            )
            actions.append(f"validate local /health/{endpoint}")
        for domain in (public, admin):
            self._run_checked(
                ("curl", "--fail", f"https://{domain}/health/ready"),
                "HTTPS_CHECK_FAILED",
                secret=secret,
            )
            actions.append(f"validate HTTPS health for {domain}")

    def _rollback_public(
        self,
        env_backup: BackupRecord,
        caddy_backup: BackupRecord | None,
        new_caddy_path: Path | None,
    ) -> None:
        failures: list[str] = []
        try:
            restore_backup(env_backup)
        except Exception as exc:
            failures.append(f"environment restoration failed ({type(exc).__name__})")
        if caddy_backup is not None:
            try:
                restore_backup(caddy_backup)
            except Exception as exc:
                failures.append(f"Caddy restoration failed ({type(exc).__name__})")
        elif new_caddy_path is not None:
            try:
                new_caddy_path.unlink()
            except OSError as exc:
                failures.append(f"Caddy removal failed ({type(exc).__name__})")
        for command, label in (
            (("systemctl", "restart", "noyra"), "Noyra service restoration failed"),
            (("systemctl", "reload", "caddy"), "Caddy service restoration failed"),
        ):
            try:
                result = self.runner.run(command, check=False)
                if result.returncode != 0:
                    failures.append(label)
            except Exception:
                failures.append(label)
        if failures:
            raise SetupError("ROLLBACK_FAILED", "environment/Caddy/service restoration failed")

    def _rollback_cloudflare(
        self,
        env_backup: BackupRecord,
        token_backup: BackupRecord | None,
        unit_backup: BackupRecord | None,
        new_token_path: Path | None,
        new_unit_path: Path | None,
        *,
        service_mutated: bool,
        enabled_state: str,
        was_active: bool,
    ) -> None:
        failures: list[str] = []
        try:
            restore_backup(env_backup)
        except Exception:
            failures.append("environment restoration failed")
        if token_backup is not None:
            try:
                restore_backup(token_backup)
            except Exception:
                failures.append("credential restoration failed")
        elif new_token_path is not None:
            try:
                new_token_path.unlink(missing_ok=True)
            except OSError:
                failures.append("credential removal failed")
        if unit_backup is not None:
            try:
                restore_backup(unit_backup)
            except Exception:
                failures.append("unit restoration failed")
        elif new_unit_path is not None:
            try:
                new_unit_path.unlink(missing_ok=True)
            except OSError:
                failures.append("unit removal failed")
        if service_mutated:
            restore_commands: list[tuple[str, ...]] = [("systemctl", "daemon-reload")]
            if enabled_state == "enabled":
                if was_active:
                    restore_commands.append(("systemctl", "enable", "--now", "cloudflared-noyra"))
                else:
                    restore_commands.extend(
                        [
                            ("systemctl", "stop", "cloudflared-noyra"),
                            ("systemctl", "enable", "cloudflared-noyra"),
                        ]
                    )
            elif enabled_state == "enabled-runtime":
                # Setup starts the unit with persistent enablement. Remove that
                # accidental persistent state before restoring runtime-only.
                restore_commands.append(("systemctl", "disable", "cloudflared-noyra"))
                if was_active:
                    restore_commands.append(
                        ("systemctl", "enable", "--runtime", "--now", "cloudflared-noyra")
                    )
                else:
                    restore_commands.extend(
                        [
                            ("systemctl", "stop", "cloudflared-noyra"),
                            ("systemctl", "enable", "--runtime", "cloudflared-noyra"),
                        ]
                    )
            elif enabled_state == "disabled":
                restore_commands.append(
                    ("systemctl", "start" if was_active else "stop", "cloudflared-noyra")
                )
                restore_commands.append(("systemctl", "disable", "cloudflared-noyra"))
            else:
                # Static, indirect, generated and alias units have no mutable
                # enablement to restore; preserve only their active state.
                restore_commands.append(
                    ("systemctl", "start" if was_active else "stop", "cloudflared-noyra")
                )
            for command in restore_commands:
                try:
                    result = self.runner.run(command, check=False)
                    if result.returncode != 0:
                        failures.append("service restoration failed")
                except Exception:
                    failures.append("service restoration failed")
        if failures:
            raise SetupError(
                "ROLLBACK_FAILED", "Cloudflare environment/credential/unit restoration failed"
            )

    def run_cloudflare(
        self,
        *,
        public_domain: str | None = None,
        admin_domain: str | None = None,
        tunnel_token: str | None = None,
        token_path: Path | None = None,
        dry_run: bool | None = None,
    ) -> SetupResult:
        """Configure a Cloudflare connector using a systemd credential.

        The token is deliberately handled separately from the environment file
        and unit arguments.  This keeps it out of logs, command output, and
        exception text while still making setup usable from both the CLI and
        tests with an injected destination path.
        """
        actions: list[str] = ["validate mode cloudflare"]
        selected_public = public_domain if public_domain is not None else self.options.public_domain
        selected_admin = admin_domain if admin_domain is not None else self.options.admin_domain
        selected_dry_run = self.options.dry_run if dry_run is None else dry_run
        env_path = self.options.env_path
        credential_path = token_path or Path("/etc/noyra/credentials/cloudflare-tunnel-token")
        # A custom credential path is useful for an injected runner and keeps
        # tests from touching /etc.  Real deployments always use /etc paths.
        unit_path = (
            credential_path.parent / "cloudflared-noyra.service"
            if token_path is not None and credential_path.parent != Path("/etc/noyra/credentials")
            else Path("/etc/systemd/system/cloudflared-noyra.service")
        )

        def failure(error: SetupError) -> SetupResult:
            return SetupResult(ok=False, stderr=str(error), actions=actions)

        env_backup: BackupRecord | None = None
        token_backup: BackupRecord | None = None
        unit_backup: BackupRecord | None = None
        token_created = False
        unit_created = False
        service_mutated = False
        service_enabled_state = "disabled"
        service_was_active = False

        def has_symlink_component(path: Path) -> bool:
            current = path
            while current != current.parent:
                if current.is_symlink():
                    return True
                current = current.parent
            return path.is_symlink()

        try:
            if not selected_public or not selected_admin:
                raise SetupError(
                    "DOMAINS_REQUIRED",
                    "public and admin domains are required for cloudflare mode",
                )
            public = validate_hostname(selected_public)
            admin = validate_hostname(selected_admin)
            actions.extend([f"validate public domain {public}", f"validate admin domain {admin}"])

            if env_path.is_symlink():
                raise SetupError("ENV_SYMLINK", "environment path must not be a symlink")
            if not env_path.exists() or not env_path.is_file():
                raise SetupError("ENV_MISSING", f"environment file does not exist: {env_path}")
            if has_symlink_component(credential_path):
                raise SetupError("TOKEN_SYMLINK", "Cloudflare token path must not be a symlink")
            if has_symlink_component(unit_path):
                raise SetupError("UNIT_SYMLINK", "Cloudflare unit path must not be a symlink")
            env_text = env_path.read_text(encoding="utf-8")
            assignments = {
                key: value
                for key, value in parse_env_file(env_text)
                if value is not None and key and not key.startswith("#")
            }
            if assignments.get("NOYRA_HOST", "127.0.0.1") != "127.0.0.1":
                raise SetupError("UNSAFE_LISTENER", "cloudflare mode requires NOYRA_HOST=127.0.0.1")
            if assignments.get("NOYRA_PORT", "8765") != "8765":
                raise SetupError("UNSAFE_PORT", "cloudflare mode requires NOYRA_PORT=8765")
            actions.extend(["validate NOYRA_HOST=127.0.0.1", "validate NOYRA_PORT=8765"])

            selected_token = tunnel_token if tunnel_token is not None else self.options.tunnel_token
            source_path = self.options.tunnel_token_path
            if selected_token is None and source_path is not None:
                if (
                    has_symlink_component(source_path)
                    or not source_path.exists()
                    or not source_path.is_file()
                ):
                    raise SetupError("TOKEN_SOURCE_INVALID", "Cloudflare token source is invalid")
                try:
                    source_mode = stat_module.S_IMODE(source_path.stat().st_mode)
                except OSError as exc:
                    raise SetupError(
                        "TOKEN_SOURCE_INVALID", "cannot inspect Cloudflare token source"
                    ) from exc
                if os.name != "nt" and source_mode & 0o077:
                    raise SetupError("TOKEN_PERMISSIONS", "Cloudflare token source must be private")
                try:
                    selected_token = source_path.read_text(encoding="utf-8").strip()
                except (OSError, UnicodeError) as exc:
                    raise SetupError(
                        "TOKEN_SOURCE_INVALID", "cannot read Cloudflare token source"
                    ) from exc
            if selected_token is None:
                raise SetupError("TUNNEL_TOKEN_REQUIRED", "a Cloudflare tunnel token is required")
            selected_token = selected_token.strip()
            if not selected_token:
                raise SetupError("TUNNEL_TOKEN_EMPTY", "Cloudflare tunnel token cannot be empty")
            actions.append("validate protected Cloudflare tunnel token")
            actions.append("validate origin http://127.0.0.1:8765")

            updated_env = update_env_text(
                env_text,
                {
                    "NOYRA_PUBLIC_SITE_URL": f"https://{public}",
                    "NOYRA_TRUSTED_PROXY_CIDRS": "127.0.0.1/32,::1/128",
                    "NOYRA_ADMIN_SESSION_COOKIE_SECURE": "true",
                },
            )
            template_path = (
                Path(__file__).resolve().parents[2]
                / "deploy"
                / "systemd"
                / "cloudflared-noyra.service.example"
            )
            try:
                unit_text = template_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                raise SetupError(
                    "UNIT_TEMPLATE_MISSING", "Cloudflare systemd unit template is unavailable"
                ) from exc

            self._require_dns_ready(public, admin)
            actions.append("validate DNS readiness for both HTTPS origins")
            version = self.runner.run(("cloudflared", "--version"), check=False)
            if version.returncode != 0 and selected_dry_run:
                raise SetupError("CLOUDFLARED_UNAVAILABLE", "cloudflared is not installed")
            if version.returncode != 0:
                try:
                    installed = self.runner.run(
                        ("apt-get", "install", "-y", "cloudflared"), check=False
                    )
                except OSError as exc:
                    raise SetupError(
                        "CLOUDFLARED_UNAVAILABLE", "cloudflared is not installed"
                    ) from exc
                if installed.returncode != 0:
                    raise SetupError("CLOUDFLARED_UNAVAILABLE", "cloudflared is not installed")
                version = self.runner.run(("cloudflared", "--version"), check=False)
                if version.returncode != 0:
                    raise SetupError("CLOUDFLARED_UNAVAILABLE", "cloudflared is not installed")
            self._run_checked(
                ("cloudflared", "tunnel", "--help"),
                "CLOUDFLARED_CONFIG_FAILED",
                secret=selected_token,
            )
            actions.append("validate cloudflared installation and tunnel configuration")
            if selected_dry_run:
                actions.extend(
                    [
                        (
                            f"write protected tunnel credential {credential_path} "
                            "(deferred in dry-run)"
                        ),
                        f"publish systemd unit {unit_path} (deferred in dry-run)",
                        "reload systemd and enable cloudflared-noyra (deferred in dry-run)",
                        "validate local and HTTPS health endpoints (deferred in dry-run)",
                    ]
                )
                return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")

            if (
                getattr(self.runner, "enforce_host_privileges", False)
                and hasattr(os, "geteuid")
                and os.geteuid() != 0
            ):
                raise SetupError("ROOT_REQUIRED", "cloudflare setup requires root")

            for command, state_name in (
                (("systemctl", "is-enabled", "cloudflared-noyra"), "enabled"),
                (("systemctl", "is-active", "cloudflared-noyra"), "active"),
            ):
                try:
                    state = self.runner.run(command, check=False)
                    value = state.stdout.strip()
                except OSError:
                    value = ""
                if state_name == "enabled":
                    service_enabled_state = value or "disabled"
                else:
                    service_was_active = value == "active"

            env_backup = create_backup(env_path, self.options.backup_root, "cloudflare-env")
            if credential_path.exists():
                if not credential_path.is_file():
                    raise SetupError("TOKEN_PATH_INVALID", "Cloudflare token path is not a file")
                token_backup = create_backup(
                    credential_path, self.options.backup_root, "cloudflare-token"
                )
            if unit_path.exists():
                if not unit_path.is_file():
                    raise SetupError("UNIT_PATH_INVALID", "Cloudflare unit path is not a file")
                unit_backup = create_backup(unit_path, self.options.backup_root, "cloudflare-unit")

            self._atomic_write(env_path, updated_env)
            self._atomic_write_protected(credential_path, selected_token + "\n")
            token_created = True
            self._atomic_write(unit_path, unit_text)
            unit_created = True
            actions.extend(
                [
                    "publish environment file",
                    "publish protected tunnel credential",
                    "publish cloudflared systemd unit",
                ]
            )
            self._run_checked(("systemctl", "daemon-reload"), "SYSTEMD_RELOAD_FAILED")
            service_mutated = True
            self._run_checked(
                ("systemctl", "enable", "--now", "cloudflared-noyra"), "CLOUDFLARED_SERVICE_FAILED"
            )
            actions.append("enable and start cloudflared-noyra")
            self._run_health_checks(public, admin, actions, secret=selected_token)
        except (SetupError, OSError, UnicodeError) as exc:
            if env_backup is not None:
                try:
                    self._rollback_cloudflare(
                        env_backup,
                        token_backup,
                        unit_backup,
                        credential_path if token_created else None,
                        unit_path if unit_created else None,
                        service_mutated=service_mutated,
                        enabled_state=service_enabled_state,
                        was_active=service_was_active,
                    )
                except SetupError as rollback_error:
                    return failure(rollback_error)
            error = (
                exc
                if isinstance(exc, SetupError)
                else SetupError("CLOUDFLARE_SETUP_FAILED", "Cloudflare setup failed")
            )
            if selected_token:
                error = SetupError(error.code, _redact_secret(error.message, selected_token))
            return failure(error)
        return SetupResult(actions=actions, stdout="\n".join(actions) + "\n")
