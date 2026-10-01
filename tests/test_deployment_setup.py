import platform
from pathlib import Path
from subprocess import CompletedProcess
from types import SimpleNamespace

import pytest

from noyra.__main__ import _setup_command
from noyra.deployment_setup import (
    SetupError,
    SetupOptions,
    SetupRunner,
    create_backup,
    parse_env_file,
    redact_token,
    render_caddyfile,
    restore_backup,
    update_env_text,
    validate_hostname,
)


def invoke_setup(arguments: list[str]) -> SimpleNamespace:
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            _setup_command(arguments)
    except SystemExit as exc:
        return SimpleNamespace(
            exit_code=exc.code, stdout=stdout.getvalue(), stderr=stderr.getvalue()
        )
    return SimpleNamespace(exit_code=0, stdout=stdout.getvalue(), stderr=stderr.getvalue())


def test_validate_hostname_accepts_dns_name() -> None:
    assert validate_hostname("Admin.Example.com") == "admin.example.com"


@pytest.mark.parametrize(
    "value", ["", "https://example.com", "../admin", "127.0.0.1:443", "*.example.com"]
)
def test_validate_hostname_rejects_non_dns_host(value: str) -> None:
    with pytest.raises(SetupError, match="NOYRA_SETUP_INVALID_HOSTNAME"):
        validate_hostname(value)


def test_env_update_preserves_unknown_lines() -> None:
    text = "# hi\r\nNOYRA_HOST=old\r\nOTHER=yes\r\n"
    assert (
        update_env_text(text, {"NOYRA_HOST": "127.0.0.1", "NOYRA_NEW": "x"})
        == "# hi\r\nNOYRA_HOST=127.0.0.1\r\nOTHER=yes\r\nNOYRA_NEW=x\r\n"
    )
    assert parse_env_file(text)[0] == ("# hi", None)


def test_render_and_redact() -> None:
    rendered = render_caddyfile("www.example.com", "admin.example.com")
    assert rendered.count("reverse_proxy 127.0.0.1:8765") == 2
    assert "encode zstd gzip" in rendered
    assert redact_token("secret") == "<redacted>"
    assert redact_token("") == ""


def test_backup_restore(tmp_path: Path) -> None:
    source = tmp_path / "env"
    source.write_text("secret", encoding="utf-8")
    record = create_backup(source, tmp_path / "backups", "local")
    source.write_text("changed", encoding="utf-8")
    restore_backup(record)
    assert source.read_text(encoding="utf-8") == "secret"
    assert "secret" not in record.metadata_path.read_text(encoding="utf-8")


def test_setup_local_dry_run_does_not_mutate(tmp_path: Path) -> None:
    env_path = tmp_path / "noyra.env"
    env_path.write_text(
        "NOYRA_HOST=127.0.0.1\nNOYRA_OPERATOR_TOKEN=test-token\n", encoding="utf-8"
    )
    result = invoke_setup(["--mode", "local", "--dry-run", "--env-file", str(env_path)])
    assert result.exit_code == 0
    assert env_path.read_text(encoding="utf-8") == (
        "NOYRA_HOST=127.0.0.1\nNOYRA_OPERATOR_TOKEN=test-token\n"
    )
    assert "SSH" in result.stdout


def test_setup_local_rejects_public_listener(tmp_path: Path) -> None:
    env_path = tmp_path / "noyra.env"
    env_path.write_text("NOYRA_HOST=0.0.0.0\n", encoding="utf-8")
    result = invoke_setup(["--mode", "local", "--env-file", str(env_path)])
    assert result.exit_code != 0
    assert "NOYRA_SETUP_UNSAFE_LISTENER" in result.stderr


def test_setup_local_requires_explicit_host_and_token(tmp_path: Path) -> None:
    env_path = tmp_path / "noyra.env"
    env_path.write_text("NOYRA_HOST=127.0.0.1\n", encoding="utf-8")
    result = invoke_setup(["--mode", "local", "--dry-run", "--env-file", str(env_path)])
    assert result.exit_code != 0
    assert "NOYRA_SETUP_OPERATOR_TOKEN_MISSING" in result.stderr

    env_path.write_text("NOYRA_OPERATOR_TOKEN=test-token\n", encoding="utf-8")
    result = invoke_setup(["--mode", "local", "--dry-run", "--env-file", str(env_path)])
    assert result.exit_code != 0
    assert "NOYRA_SETUP_UNSAFE_LISTENER" in result.stderr


def test_setup_requires_domains_with_stable_error(tmp_path: Path) -> None:
    result = invoke_setup(
        ["--mode", "public", "--non-interactive", "--env-file", str(tmp_path / "env")]
    )
    assert result.exit_code != 0
    assert "NOYRA_SETUP_DOMAINS_REQUIRED" in result.stderr


def test_local_mount_check_uses_findmnt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    env_path = tmp_path / "noyra.env"
    env_path.write_text("NOYRA_HOST=127.0.0.1\nNOYRA_OPERATOR_TOKEN=test-token\n", encoding="utf-8")

    class Runner:
        def run(self, argv, *, check=True, input_text=None):
            del check, input_text
            assert argv[:1] == ("findmnt",)
            return CompletedProcess(argv, 1, "", "")

    monkeypatch.setattr(platform, "system", lambda: "Linux")
    monkeypatch.setattr("os.geteuid", lambda: 0, raising=False)
    with pytest.raises(SetupError, match="NOYRA_SETUP_DATA_MOUNT_MISSING"):
        SetupRunner(SetupOptions(env_path=env_path), Runner()).run_local()
