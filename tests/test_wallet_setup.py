from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from noyra.core.at_rest import _windows_harden

PRIVATE_KEY = "0x" + "11" * 32
PASSWORD = "wallet-setup-test-password"


def _private_file(path: Path, value: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.write_text(value, encoding="utf-8")
    if os.name == "nt":
        _windows_harden(path.parent, entire_tree=True)
    else:
        path.parent.chmod(0o700)
        path.chmod(0o600)
    return path


def _run(*args: str, input_text: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(Path(".venv/Scripts/python.exe")), "-m", "noyra", *args],
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def test_wallet_setup_init_uses_password_file_and_prints_only_public_address(
    tmp_path: Path,
) -> None:
    password_file = _private_file(tmp_path / "secrets" / "wallet-password", PASSWORD + "\n")
    keystore = tmp_path / "wallet" / "account.json"
    result = _run(
        "wallet-setup",
        "--path",
        str(keystore),
        "--password-file",
        str(password_file),
    )
    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    assert set(output) == {"address"}
    assert output["address"].startswith("0x") and output["address"] == output["address"].lower()
    assert PASSWORD not in result.stdout and PASSWORD not in result.stderr
    assert keystore.exists()


def test_wallet_setup_import_prompts_for_private_key_without_echoing_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from noyra import __main__

    password_file = _private_file(tmp_path / "secrets" / "wallet-password", PASSWORD)
    keystore = tmp_path / "wallet" / "account.json"
    prompts: list[str] = []

    def hidden(prompt: str) -> str:
        prompts.append(prompt)
        return PRIVATE_KEY

    monkeypatch.setattr("getpass.getpass", hidden)
    __main__._wallet_setup_command(
        ["import", "--path", str(keystore), "--password-file", str(password_file)]
    )
    assert prompts and all(PRIVATE_KEY not in prompt for prompt in prompts)
    payload = json.loads(keystore.read_text(encoding="utf-8"))
    assert payload["address"].lower() == "19e7e376e7c213b7e7e7e46cc70a5dd086daff2a"


def test_wallet_setup_rejects_private_key_as_command_argument(tmp_path: Path) -> None:
    result = _run("wallet-setup", "import", "--path", str(tmp_path / "wallet.json"), PRIVATE_KEY)
    assert result.returncode != 0
    assert PRIVATE_KEY not in result.stdout
    assert PRIVATE_KEY not in result.stderr


def test_wallet_setup_errors_are_sanitized(tmp_path: Path) -> None:
    result = _run(
        "wallet-setup",
        "--path",
        str(tmp_path / "wallet.json"),
        "--password-file",
        str(tmp_path / "missing-password"),
    )
    assert result.returncode != 0
    assert "Traceback" not in result.stderr
    assert "missing-password" not in result.stderr
