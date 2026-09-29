"""Fail-closed loading of provider credentials from private files.

Inline environment values remain a compatibility fallback for development, but
production deployments should use an explicit file or a systemd credential.
Credential values never enter configuration projections or diagnostic output.
"""

from __future__ import annotations

import os
import re
import stat
from pathlib import Path

_MAX_CREDENTIAL_BYTES = 16 * 1024
_MAX_CONFIG_BYTES = 1024 * 1024
_CREDENTIAL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")


class CredentialError(ValueError):
    """A configured credential source is missing or unsafe."""


def _absolute_path(value: str, *, label: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise CredentialError(f"{label} path must be absolute")
    return path


def _reject_reparse_components(path: Path, *, label: str) -> None:
    current = path
    while True:
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if current.parent == current:
                return
            current = current.parent
            continue
        except OSError as error:
            raise CredentialError(f"{label} metadata is unavailable") from error
        if stat.S_ISLNK(metadata.st_mode) or bool(
            getattr(metadata, "st_file_attributes", 0)
            & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        ):
            raise CredentialError(f"{label} path cannot contain a symlink or reparse point")
        if current.parent == current:
            return
        current = current.parent


def _validate_permissions(metadata: os.stat_result, *, label: str) -> None:
    if os.name != "posix":
        return
    get_effective_uid = getattr(os, "geteuid", None)
    get_effective_gid = getattr(os, "getegid", None)
    get_effective_groups = getattr(os, "getgroups", None)
    if (
        not callable(get_effective_uid)
        or not callable(get_effective_gid)
        or not callable(get_effective_groups)
    ):
        return
    mode = metadata.st_mode & 0o777
    effective_uid = get_effective_uid()
    effective_groups = {get_effective_gid(), *get_effective_groups()}
    if metadata.st_uid == effective_uid:
        if mode & 0o077:
            raise CredentialError(f"{label} must not be readable or writable by group/other")
        return
    if metadata.st_uid == 0 and metadata.st_gid in effective_groups:
        if mode & 0o027 or not mode & 0o040:
            raise CredentialError(f"{label} must use root-owned service-group mode 0640")
        return
    raise CredentialError(f"{label} owner is not the service account or root")


def read_secret_file(
    path: Path | str,
    *,
    label: str = "credential",
    single_line: bool = True,
    max_bytes: int = _MAX_CREDENTIAL_BYTES,
) -> str:
    """Read one bounded private credential/configuration file."""

    candidate = _absolute_path(str(path), label=label)
    _reject_reparse_components(candidate, label=label)
    try:
        path_metadata = candidate.lstat()
    except OSError as error:
        raise CredentialError(f"{label} is unavailable") from error
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(candidate, flags)
    except OSError as error:
        raise CredentialError(f"{label} is unreadable") from error
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or (metadata.st_dev, metadata.st_ino) != (path_metadata.st_dev, path_metadata.st_ino)
        ):
            raise CredentialError(f"{label} must be a stable regular, non-hard-linked file")
        _validate_permissions(metadata, label=label)
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    except OSError as error:
        raise CredentialError(f"{label} is unreadable") from error
    finally:
        os.close(descriptor)
    if not raw or len(raw) > max_bytes:
        raise CredentialError(f"{label} size is invalid")
    try:
        value = raw.decode("utf-8").strip()
    except UnicodeDecodeError as error:
        raise CredentialError(f"{label} encoding is invalid") from error
    if not value or any(
        ord(character) < 0x20 and (single_line or character not in "\r\n\t") for character in value
    ):
        raise CredentialError(f"{label} content is invalid")
    return value


def _systemd_credential_path(name: str, *, label: str) -> Path:
    if not _CREDENTIAL_NAME.fullmatch(name):
        raise CredentialError(f"{label} name is invalid")
    directory = os.getenv("CREDENTIALS_DIRECTORY", "").strip()
    if not directory:
        raise CredentialError(f"{label} requires CREDENTIALS_DIRECTORY")
    root = _absolute_path(directory, label="system credential directory")
    return root / name


def read_env_secret(
    *,
    value_var: str,
    file_var: str,
    credential_var: str,
    label: str,
    allow_inline: bool | None = None,
) -> str:
    """Resolve a secret from file, system credential, or an explicit dev fallback.

    File and system credential sources are mutually exclusive and take
    precedence over the inline variable.  Production profiles reject inline
    values so a deployment cannot silently bypass its managed secret source.
    """

    file_value = os.getenv(file_var, "").strip()
    credential_value = os.getenv(credential_var, "").strip()
    if file_value and credential_value:
        raise CredentialError(f"choose one secret source for {label}")
    if file_value:
        return read_secret_file(file_value, label=label)
    if credential_value:
        return read_secret_file(
            _systemd_credential_path(credential_value, label=label), label=label
        )
    if allow_inline is None:
        profile = os.getenv("NOYRA_PROFILE", "development").strip().lower()
        if profile == "production":
            allow_inline = False
        else:
            configured = os.getenv("NOYRA_ALLOW_INLINE_SECRETS")
            if configured is None:
                allow_inline = profile in {"development", "test"}
            else:
                normalized = configured.strip().lower()
                if normalized not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
                    raise CredentialError("NOYRA_ALLOW_INLINE_SECRETS must be true or false")
                allow_inline = normalized in {"1", "true", "yes", "on"}
    if not allow_inline and os.getenv(value_var, "").strip():
        raise CredentialError(f"inline secret is disabled for {label}")
    return os.getenv(value_var, "").strip()


__all__ = ["CredentialError", "read_env_secret", "read_secret_file"]
