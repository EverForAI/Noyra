from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from noyra.core.at_rest import _windows_harden, validate_keyring_path

PRIVATE_KEY = "0x" + "00" * 31 + "01"
ADDRESS = "0x7e5f4552091a69125d5dfcb7b8c2659029395bdf"
PASSWORD = "local-wallet-test-password"


def _private_file(path: Path, raw: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_bytes(raw)
    if os.name == "nt":
        _windows_harden(path.parent, entire_tree=True)
    else:
        path.parent.chmod(0o700)
        path.chmod(0o600)
    return path


def test_import_encrypts_private_key_and_round_trips_with_private_permissions(
    tmp_path: Path,
) -> None:
    from noyra.wallet.keystore import create_keystore, load_account

    path = tmp_path / "wallet" / "account.json"
    assert create_keystore(path, PASSWORD, private_key=PRIVATE_KEY) == ADDRESS
    assert validate_keyring_path(path) == path.resolve()
    raw = path.read_text(encoding="utf-8")
    assert PRIVATE_KEY[2:] not in raw
    assert PASSWORD not in raw
    assert json.loads(raw)["crypto"]["kdf"] == "scrypt"
    account = load_account(path, PASSWORD)
    assert account.address.lower() == ADDRESS
    assert account.key.hex() == PRIVATE_KEY[2:]


def test_creation_never_overwrites_existing_wallet(tmp_path: Path) -> None:
    from noyra.wallet.keystore import create_keystore

    path = _private_file(tmp_path / "wallet" / "account.json", b"existing-wallet")
    with pytest.raises(ValueError):
        create_keystore(path, PASSWORD, private_key=PRIVATE_KEY)
    assert path.read_bytes() == b"existing-wallet"


@pytest.fixture(scope="module")
def encrypted_wallet(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    from noyra.wallet.keystore import create_keystore

    path = tmp_path_factory.mktemp("wallet-encrypted") / "private" / "account.json"
    create_keystore(path, PASSWORD, private_key=PRIVATE_KEY)
    payload: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return payload


def test_wrong_password_and_tampered_address_are_rejected(
    tmp_path: Path, encrypted_wallet: dict[str, Any]
) -> None:
    from noyra.wallet.keystore import load_account

    path = _private_file(
        tmp_path / "wallet" / "account.json", json.dumps(encrypted_wallet).encode()
    )
    with pytest.raises(ValueError):
        load_account(path, "incorrect-wallet-password")
    payload = {**encrypted_wallet, "address": "11" * 20}
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_account(path, PASSWORD)


@pytest.mark.parametrize(
    ("field", "value"),
    [("n", 2**40), ("n", True), ("r", 2**30), ("p", 2**30), ("dklen", 2**30)],
)
def test_untrusted_kdf_costs_are_rejected_before_decryption(
    tmp_path: Path,
    encrypted_wallet: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: object,
) -> None:
    from eth_account import Account

    from noyra.wallet.keystore import load_account

    payload = json.loads(json.dumps(encrypted_wallet))
    payload["crypto"]["kdfparams"][field] = value
    path = _private_file(tmp_path / "wallet" / "account.json", json.dumps(payload).encode())

    def unexpected_decrypt(*args: object, **kwargs: object) -> None:
        pytest.fail("untrusted KDF parameters reached the expensive decrypt operation")

    monkeypatch.setattr(Account, "decrypt", unexpected_decrypt)
    with pytest.raises(ValueError):
        load_account(path, PASSWORD)


@pytest.mark.parametrize("raw", [b"{}", b"[", b"x" * 16385, b'{"version":3,"version":3}'])
def test_malformed_and_oversized_keystores_are_rejected(tmp_path: Path, raw: bytes) -> None:
    from noyra.wallet.keystore import load_account

    path = _private_file(tmp_path / "wallet" / "account.json", raw)
    with pytest.raises(ValueError):
        load_account(path, PASSWORD)


def test_duplicate_keys_and_non_hex_fields_cannot_reach_decryption(
    tmp_path: Path, encrypted_wallet: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from eth_account import Account

    from noyra.wallet.keystore import load_account

    def unexpected_decrypt(*args: object, **kwargs: object) -> None:
        pytest.fail("invalid wallet structure reached decryption")

    monkeypatch.setattr(Account, "decrypt", unexpected_decrypt)
    duplicate = json.dumps(encrypted_wallet).replace('"version": 3', '"version": 3, "version": 3')
    path = _private_file(tmp_path / "wallet" / "account.json", duplicate.encode())
    with pytest.raises(ValueError):
        load_account(path, PASSWORD)
    invalid_hex = json.loads(json.dumps(encrypted_wallet))
    invalid_hex["crypto"]["cipherparams"]["iv"] = "+" + "1" * 31
    path.write_text(json.dumps(invalid_hex), encoding="utf-8")
    with pytest.raises(ValueError):
        load_account(path, PASSWORD)


def test_wallet_reader_never_reads_more_than_its_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import noyra.wallet.keystore as keystore

    path = _private_file(tmp_path / "wallet" / "account.json", b"x" * (64 * 1024 + 1))
    actual_read = os.read

    def bounded_read(descriptor: int, size: int) -> bytes:
        assert 0 <= size <= 64 * 1024 + 1
        return actual_read(descriptor, size)

    monkeypatch.setattr(os, "read", bounded_read)
    with pytest.raises(ValueError, match="size"):
        keystore.load_account(path, PASSWORD)


def test_scrypt_generation_uses_standard_strong_work_factor(
    tmp_path: Path, encrypted_wallet: dict[str, Any]
) -> None:
    # Account.encrypt's documented default uses 256 MiB of memory; pinning it
    # prevents an environment variable from silently weakening encryption.
    assert encrypted_wallet["crypto"]["kdfparams"] == {
        "n": 262144,
        "r": 8,
        "p": 1,
        "dklen": 32,
        "salt": encrypted_wallet["crypto"]["kdfparams"]["salt"],
    }


def test_hardlinked_keystore_and_password_are_rejected(tmp_path: Path) -> None:
    from noyra.wallet.keystore import load_account, read_password_file

    path = _private_file(tmp_path / "wallet" / "password", PASSWORD.encode())
    os.link(path, path.with_name("alias"))
    for operation in (lambda: load_account(path, PASSWORD), lambda: read_password_file(path)):
        with pytest.raises(ValueError):
            operation()


def test_password_file_preserves_spaces_and_removes_one_terminal_newline(tmp_path: Path) -> None:
    from noyra.wallet.keystore import read_password_file

    password = "  significant password spaces  "
    path = _private_file(tmp_path / "wallet" / "password", (password + "\r\n").encode())
    assert read_password_file(path) == password


@pytest.mark.parametrize("raw", [b"", b"short", b"first password\nsecond password", b"x" * 4097])
def test_invalid_password_files_are_rejected(tmp_path: Path, raw: bytes) -> None:
    from noyra.wallet.keystore import read_password_file

    path = _private_file(tmp_path / "wallet" / "password", raw)
    with pytest.raises(ValueError):
        read_password_file(path)


@pytest.mark.skipif(os.name == "nt", reason="POSIX ownership and mode contract")
def test_public_root_and_public_secret_files_are_rejected(tmp_path: Path) -> None:
    from noyra.wallet.keystore import create_keystore, read_password_file

    public = tmp_path / "public"
    public.mkdir(mode=0o755)
    public.chmod(0o755)
    with pytest.raises(ValueError):
        create_keystore(public / "account.json", PASSWORD)
    path = _private_file(tmp_path / "private" / "password", PASSWORD.encode())
    path.chmod(0o644)
    with pytest.raises(ValueError):
        read_password_file(path)


def test_symlink_parent_cannot_be_used_for_wallet_creation(tmp_path: Path) -> None:
    from noyra.wallet.keystore import create_keystore

    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    try:
        linked.symlink_to(real, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation requires additional OS privileges")
    with pytest.raises(ValueError):
        create_keystore(linked / "account.json", PASSWORD)
    assert not (real / "account.json").exists()
