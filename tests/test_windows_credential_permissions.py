from __future__ import annotations

import base64
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from noyra.core.at_rest import _powershell_command, _run_command, _windows_harden
from noyra.core.credentials import CredentialError, read_env_secret, read_secret_file
from noyra.core.types import canonical_json
from noyra.core.windows_credential_acl import (
    _require_trusted_acl,
    require_private_credential_handle,
)
from noyra.interaction.sitemap_tokens import SitemapTokens

WINDOWS = pytest.mark.skipif(os.name != "nt", reason="Native Windows credential ACL contract")
SECRET = "synthetic-m08-private-credential"


def set_acl(
    path: Path, *, extra_sid: str = "", readonly: bool = False, directory: bool = False
) -> None:
    encoded = base64.b64encode(str(path).encode()).decode()
    rights = "Read" if readonly else "FullControl"
    acl_type = "DirectorySecurity" if directory else "FileSecurity"
    inheritance = "ContainerInherit, ObjectInherit" if directory else "None"
    script = f"""
$ErrorActionPreference = 'Stop'
$path = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
$current = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = New-Object System.Security.AccessControl.{acl_type}
$acl.SetOwner($current)
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($current.Value, 'S-1-5-18', 'S-1-5-32-544')) {{
  $identity = New-Object System.Security.Principal.SecurityIdentifier($sid)
  $rule = New-Object System.Security.AccessControl.FileSystemAccessRule(
    $identity, '{rights}', '{inheritance}', 'None', 'Allow')
  [void]$acl.AddAccessRule($rule)
}}
if ('{extra_sid}') {{
  $identity = New-Object System.Security.Principal.SecurityIdentifier('{extra_sid}')
  [void]$acl.AddAccessRule((New-Object System.Security.AccessControl.FileSystemAccessRule(
    $identity, 'Read', '{inheritance}', 'None', 'Allow')))
}}
(Get-Item -LiteralPath $path).SetAccessControl($acl)
"""
    result = _run_command(_powershell_command(script), timeout_seconds=30)
    assert result.returncode == 0, result.stderr


@WINDOWS
@pytest.mark.parametrize("sid", ["S-1-1-0", "S-1-5-11", "S-1-5-32-545"])
def test_credential_rejects_broad_read_grants_before_reading_bytes(
    tmp_path: Path, sid: str
) -> None:
    path = tmp_path / "credential.key"
    path.write_text(SECRET, encoding="utf-8")
    set_acl(path, extra_sid=sid)
    with patch("noyra.core.credentials.os.read", wraps=os.read) as read:
        with pytest.raises(CredentialError, match="Windows ACL"):
            read_secret_file(path)
        read.assert_not_called()


@WINDOWS
@pytest.mark.parametrize("readonly", [False, True])
def test_private_credential_accepts_current_system_admin_and_readonly_files(
    tmp_path: Path, readonly: bool
) -> None:
    path = tmp_path / "private.key"
    path.write_text(SECRET + "\n", encoding="utf-8")
    set_acl(path, readonly=readonly)
    assert read_secret_file(path) == SECRET


@WINDOWS
def test_private_inherited_acl_and_unicode_metacharacter_path_are_accepted(tmp_path: Path) -> None:
    root = tmp_path / "synthetic-凭据 ' [space]"
    root.mkdir()
    _windows_harden(root, entire_tree=True)
    path = root / "key '[1].txt"
    path.write_text(SECRET, encoding="utf-8")
    assert read_secret_file(path) == SECRET


@WINDOWS
@pytest.mark.parametrize("source", ["file", "credential"])
def test_managed_environment_sources_cannot_bypass_windows_acl_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    path = tmp_path / "provider.key"
    path.write_text(SECRET, encoding="utf-8")
    set_acl(path, extra_sid="S-1-1-0")
    monkeypatch.setenv("M08_VALUE", "synthetic-inline-fallback")
    monkeypatch.setenv("M08_FILE", str(path) if source == "file" else "")
    monkeypatch.setenv("M08_CREDENTIAL", path.name if source == "credential" else "")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    with pytest.raises(CredentialError, match="Windows ACL"):
        read_env_secret(
            value_var="M08_VALUE",
            file_var="M08_FILE",
            credential_var="M08_CREDENTIAL",
            label="test",
        )


@WINDOWS
def test_native_acl_check_does_not_invoke_powershell_or_modify_acl(tmp_path: Path) -> None:
    path = tmp_path / "private.key"
    path.write_text(SECRET, encoding="utf-8")
    set_acl(path)
    with patch(
        "subprocess.run", side_effect=AssertionError("Credential reads must stay in process")
    ):
        assert read_secret_file(path) == SECRET
    set_acl(path, extra_sid="S-1-1-0")
    with (
        patch("subprocess.run", side_effect=AssertionError("Do not repair external ACLs on read")),
        pytest.raises(CredentialError, match="Windows ACL"),
    ):
        read_secret_file(path)
    # Independently verify a failed read did not remove the unsafe grant.
    encoded = base64.b64encode(str(path).encode()).decode()
    script = f"""
$p = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{encoded}'))
(Get-Item -LiteralPath $p).GetAccessControl().GetSecurityDescriptorSddlForm('Access')
"""
    result = _run_command(_powershell_command(script), timeout_seconds=30)
    assert result.returncode == 0 and "WD" in result.stdout


@WINDOWS
def test_acl_backend_failure_closes_descriptor_without_reading_or_falling_back(
    tmp_path: Path,
) -> None:
    path = tmp_path / "private.key"
    path.write_text(SECRET, encoding="utf-8")
    set_acl(path)
    with (
        patch(
            "noyra.core.credentials.require_private_credential_handle", side_effect=OSError(SECRET)
        ),
        patch("noyra.core.credentials.os.read", wraps=os.read) as read,
        patch("noyra.core.credentials.os.close", wraps=os.close) as close,
        pytest.raises(CredentialError, match="Windows ACL") as rejected,
    ):
        read_secret_file(path)
    read.assert_not_called()
    assert close.call_count == 1
    assert SECRET not in str(rejected.value)


@WINDOWS
def test_null_dacl_is_rejected_before_any_credential_bytes(tmp_path: Path) -> None:
    import ctypes
    from ctypes import wintypes
    from typing import Any

    path = tmp_path / "null-dacl.key"
    path.write_text(SECRET, encoding="utf-8")
    set_acl(path)
    api: Any = ctypes
    security = api.WinDLL("advapi32", use_last_error=True)
    setter = security.SetNamedSecurityInfoW
    setter.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD] + [ctypes.c_void_p] * 4
    setter.restype = wintypes.DWORD
    try:
        assert setter(str(path), 1, 0x00000004, None, None, None, None) == 0
        with (
            patch("noyra.core.credentials.os.read", wraps=os.read) as read,
            pytest.raises(CredentialError, match="Windows ACL"),
        ):
            read_secret_file(path)
        read.assert_not_called()
    finally:
        set_acl(path)


@WINDOWS
def test_open_handle_is_rechecked_after_its_acl_changes(tmp_path: Path) -> None:
    path = tmp_path / "drift.key"
    path.write_text(SECRET, encoding="utf-8")
    set_acl(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_BINARY)
    try:
        require_private_credential_handle(descriptor)
        set_acl(path, extra_sid="S-1-1-0")
        with pytest.raises(OSError, match="another principal"):
            require_private_credential_handle(descriptor)
        set_acl(path)
        require_private_credential_handle(descriptor)
    finally:
        os.close(descriptor)


@WINDOWS
def test_new_managed_sitemap_key_is_private_before_serializing_key_bytes(tmp_path: Path) -> None:
    root = tmp_path / "owned-secrets"
    root.mkdir()
    set_acl(root, extra_sid="S-1-5-32-545", directory=True)
    checked: list[Path] = []

    def serialize(value: object) -> str:
        temporary = list(root.glob(".sitemap-*.tmp"))
        assert len(temporary) == 1 and temporary[0].stat().st_size == 0
        descriptor = os.open(temporary[0], os.O_RDONLY | os.O_BINARY)
        try:
            require_private_credential_handle(descriptor)
        finally:
            os.close(descriptor)
        checked.append(temporary[0])
        return canonical_json(value)

    with patch("noyra.interaction.sitemap_tokens.canonical_json", side_effect=serialize):
        tokens = SitemapTokens.managed(root)
    assert len(checked) == 1
    assert tokens.open(tokens.seal({"test": "m08"})) == {"test": "m08"}
    assert read_secret_file(root / "public-sitemap-keyring.json")
    assert not list(root.glob(".sitemap-*.tmp"))


@WINDOWS
def test_existing_managed_key_with_acl_drift_is_not_silently_repaired(tmp_path: Path) -> None:
    root = tmp_path / "owned-secrets"
    root.mkdir()
    _windows_harden(root, entire_tree=True)
    SitemapTokens.managed(root)
    path = root / "public-sitemap-keyring.json"
    before = path.read_bytes()
    set_acl(path, extra_sid="S-1-1-0")
    with (
        patch("noyra.interaction.sitemap_tokens._windows_harden") as harden,
        pytest.raises(CredentialError, match="Windows ACL"),
    ):
        SitemapTokens.managed(root)
    harden.assert_not_called()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    ("owner", "entries"),
    [
        ("S-1-5-21-other", [(0, 0, 1, "service")]),
        ("service", [(0, 0, 1, "S-1-1-0")]),
        ("service", [(0, 0, 0x40000, "S-1-5-11")]),
        ("service", [(5, 0, 1, "service")]),
        ("service", [(9, 0, 1, "service")]),
    ],
    ids=["untrusted-owner", "public-read", "other-can-change-acl", "object-ace", "callback-ace"],
)
def test_acl_policy_rejects_untrusted_owners_and_unparsed_grants(
    owner: str, entries: list[tuple[int, int, int, str]]
) -> None:
    with pytest.raises(OSError):
        _require_trusted_acl(owner, "service", entries)


@pytest.mark.parametrize(
    "entries",
    [
        [(0, 0, 0x1F01FF, "service"), (0, 0, 0x1F01FF, "S-1-5-18")],
        [(0, 0x10, 1, "S-1-5-32-544"), (0, 0x10, 1, "service")],
        [(1, 0, 1, "S-1-1-0"), (0, 0, 1, "service")],
        [(0, 0x08, 1, "S-1-1-0"), (0, 0, 1, "service")],
    ],
    ids=["trusted-full-control", "private-inheritance", "deny-rule", "inherit-only-rule"],
)
@pytest.mark.parametrize("owner", ["service", "S-1-5-18", "S-1-5-32-544"])
def test_acl_policy_does_not_mistake_non_grant_rules_for_public_access(
    entries: list[tuple[int, int, int, str]], owner: str
) -> None:
    _require_trusted_acl(owner, "service", entries)
