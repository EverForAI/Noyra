from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from noyra import __main__
from noyra.core.at_rest import _windows_harden
from noyra.core.credentials import CredentialError

PASSWORD = "  synthetic signer password  "
TOKEN = "synthetic-signer-token"
RPC_URL = "https://rpc.example/synthetic-only"


def private_file(path: Path, value: bytes) -> Path:
    path.write_bytes(value)
    if os.name == "nt":
        _windows_harden(path.parent, entire_tree=True)
    else:
        path.chmod(0o400)
    return path


def test_unit_reads_credentials_directly_without_persistent_copies() -> None:
    text = (Path(__file__).parents[1] / "deploy/systemd/noyra-signer.service").read_text()
    for option, credential in (
        ("wallet-password", "wallet-password"),
        ("rpc-url", "sepolia-rpc-url"),
        ("bearer-token", "signer-token"),
    ):
        assert f"--{option}-credential %d/{credential}" in text
    assert "PermissionsStartOnly=" not in text
    assert "ExecStopPost=" not in text
    assert "/usr/bin/install" not in text
    assert "/var/lib/noyra-signer/.wallet-password" not in text
    assert "/var/lib/noyra-signer/.sepolia-rpc-url" not in text
    assert "/var/lib/noyra-signer/.signer-token" not in text
    assert "User=noyra-signer" in text
    assert "ReadWritePaths=/var/lib/noyra-signer" in text
    assert text.count("LoadCredential=") == 3


@pytest.mark.parametrize(
    "raw",
    [b"", b"x" * 16385, b"token\nother", b"\xff"],
    ids=["empty", "oversized", "multiline", "bad-encoding"],
)
@pytest.mark.parametrize("which", ["rpc-url", "bearer-token"])
def test_signer_rejects_invalid_credentials_before_loading_wallet_or_opening_state(
    tmp_path: Path,
    raw: bytes,
    which: str,
) -> None:
    password = private_file(tmp_path / "password", PASSWORD.encode())
    token = private_file(tmp_path / "token", TOKEN.encode())
    rpc = private_file(tmp_path / "rpc", RPC_URL.encode())
    private_file(token if which == "bearer-token" else rpc, raw)
    with (
        patch("noyra.wallet.keystore.load_account") as account,
        patch("noyra.signer_service.SignerState.open") as state,
        pytest.raises(CredentialError),
    ):
        __main__._signer_command(
            [
                "--keystore",
                str(tmp_path / "unused.json"),
                "--state",
                str(tmp_path / "state.sqlite3"),
                "--wallet-password-credential",
                str(password),
                "--rpc-url-credential",
                str(rpc),
                "--bearer-token-credential",
                str(token),
            ]
        )
    account.assert_not_called()
    state.assert_not_called()


def test_signer_consumes_read_only_credentials_without_copying_or_trimming_password(
    tmp_path: Path,
) -> None:
    credentials = tmp_path / "credentials"
    credentials.mkdir(mode=0o700)
    password = private_file(credentials / "wallet-password", (PASSWORD + "\r\n").encode())
    rpc = private_file(credentials / "sepolia-rpc-url", (RPC_URL + "\n").encode())
    token = private_file(credentials / "signer-token", (TOKEN + "\n").encode())
    state_dir = tmp_path / "state"
    state_dir.mkdir(mode=0o700)
    state = Mock()
    account, transport, engine, server = Mock(), Mock(), Mock(), Mock()
    before = {p.name: p.read_bytes() for p in credentials.iterdir()}
    with (
        patch("noyra.wallet.keystore.load_account", return_value=account) as load,
        patch("noyra.signer_service.HTTPRPC", return_value=transport) as rpc_factory,
        patch("noyra.signer_service.SignerState.open", return_value=state),
        patch("noyra.signer_service.SignerEngine", return_value=engine),
        patch("noyra.signer_service.SignerHTTPServer", return_value=server) as http,
    ):
        __main__._signer_command(
            [
                "--keystore",
                str(state_dir / "wallet.json"),
                "--state",
                str(state_dir / "signer.sqlite3"),
                "--wallet-password-credential",
                str(password),
                "--rpc-url-credential",
                str(rpc),
                "--bearer-token-credential",
                str(token),
            ]
        )
    load.assert_called_once_with(state_dir / "wallet.json", PASSWORD)
    rpc_factory.assert_called_once_with(RPC_URL, 11155111)
    http.assert_called_once_with(engine, bearer_token=TOKEN, host="127.0.0.1", port=8780)
    server.serve_forever.assert_called_once()
    server.server_close.assert_called_once()
    state.close.assert_called_once()
    assert {p.name: p.read_bytes() for p in credentials.iterdir()} == before
    assert list(state_dir.iterdir()) == []


@pytest.mark.parametrize(
    "raw",
    [b"", b"short", b"first password\nsecond password", b"x" * 4097, b"\xff"],
    ids=["empty", "short", "multiline", "oversized", "bad-encoding"],
)
def test_password_credential_rejects_invalid_input(tmp_path: Path, raw: bytes) -> None:
    from noyra.wallet.keystore import read_password_credential

    path = private_file(tmp_path / "password", raw)
    with pytest.raises(ValueError):
        read_password_credential(path)


@pytest.mark.parametrize("reader", ["password", "text", "bytes"])
@pytest.mark.parametrize("link_kind", ["symlink", "hardlink"])
def test_signer_credentials_reject_links(tmp_path: Path, reader: str, link_kind: str) -> None:
    from noyra.core.credentials import read_secret_bytes, read_secret_file
    from noyra.wallet.keystore import read_password_credential

    source = private_file(tmp_path / "source", PASSWORD.encode())
    linked = tmp_path / "linked"
    try:
        if link_kind == "symlink":
            linked.symlink_to(source)
        else:
            os.link(source, linked)
    except OSError:
        pytest.skip("link creation unavailable")
    operation = {
        "password": read_password_credential,
        "text": read_secret_file,
        "bytes": read_secret_bytes,
    }[reader]
    with pytest.raises(ValueError):
        operation(linked)


@pytest.mark.skipif(os.name != "posix", reason="POSIX credential permissions")
@pytest.mark.parametrize("mode", [0o644, 0o640, 0o660, 0o604])
def test_password_credential_keeps_private_permissions(tmp_path: Path, mode: int) -> None:
    from noyra.wallet.keystore import read_password_credential

    path = private_file(tmp_path / "password", PASSWORD.encode())
    path.chmod(mode)
    with pytest.raises(CredentialError, match="must not be readable"):
        read_password_credential(path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX systemd ACL contract")
@pytest.mark.parametrize(
    "boundary",
    [
        "private",
        "writable-mount",
        "extra-user",
        "group-read",
        "wrong-user",
        "missing-env",
        "other-directory",
        "same-device",
        "world-access-directory",
        "foreign-directory-owner",
    ],
)
def test_systemd_acl_exception_requires_every_private_boundary(
    monkeypatch: pytest.MonkeyPatch,
    boundary: str,
) -> None:
    import stat
    import struct
    from types import SimpleNamespace

    import noyra.core.credentials as credentials

    directory = Mock()
    directory.is_absolute.return_value = True
    directory.lstat.return_value = SimpleNamespace(
        st_mode=stat.S_IFDIR | (0o555 if boundary == "world-access-directory" else 0o550),
        st_uid=1234 if boundary == "foreign-directory-owner" else 0,
        st_dev=2,
    )
    directory.parent.stat.return_value = SimpleNamespace(
        st_dev=2 if boundary == "same-device" else 1
    )
    candidate = Mock()
    candidate.parent = Mock() if boundary == "other-directory" else directory
    entries = [
        (1, 4, 0xFFFFFFFF),
        (2, 4, os.geteuid() + (1 if boundary == "wrong-user" else 0)),
        (4, 4 if boundary == "group-read" else 0, 0xFFFFFFFF),
        (16, 4, 0xFFFFFFFF),
        (32, 0, 0xFFFFFFFF),
    ]
    if boundary == "extra-user":
        entries.insert(2, (2, 4, os.geteuid() + 100))
    acl = struct.pack("<I", 2) + b"".join(struct.pack("<HHI", *entry) for entry in entries)
    monkeypatch.setenv(
        "CREDENTIALS_DIRECTORY", "" if boundary == "missing-env" else "/run/credentials/test"
    )
    monkeypatch.setattr(credentials, "Path", lambda _: directory)
    monkeypatch.setattr(
        os,
        "fstatvfs",
        lambda _: SimpleNamespace(f_flag=0 if boundary == "writable-mount" else os.ST_RDONLY),
    )
    monkeypatch.setattr(os, "getxattr", lambda *_: acl)
    metadata = SimpleNamespace(st_uid=0, st_mode=stat.S_IFREG | 0o440)
    assert credentials._private_systemd_acl(candidate, 123, metadata) == (boundary == "private")
