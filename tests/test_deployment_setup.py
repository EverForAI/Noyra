import os
import platform
import socket
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


class FakeRunner:
    def __init__(self, healthy: bool = True):
        self.healthy = healthy
        self.calls: list[tuple[str, ...]] = []

    def run(self, argv, *, check=True, input_text=None):
        del check, input_text
        command = tuple(argv)
        self.calls.append(command)
        return CompletedProcess(
            command,
            0 if self.healthy else 1,
            "active\n" if command[:2] == ("systemctl", "is-active") else "",
            "" if self.healthy else "failed",
        )


def build_runner(runner: FakeRunner, tmp_path: Path, *, replace: bool = False) -> SetupRunner:
    env_path = tmp_path / "noyra.env"
    env_path.write_text("NOYRA_HOST=127.0.0.1\nNOYRA_PORT=8765\n", encoding="utf-8")
    return SetupRunner(
        SetupOptions(
            env_path=env_path,
            caddy_path=tmp_path / "Caddyfile",
            backup_root=tmp_path / "backups",
            replace=replace,
        ),
        runner,
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
    env_path.write_text("NOYRA_HOST=127.0.0.1\nNOYRA_OPERATOR_TOKEN=test-token\n", encoding="utf-8")
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


def test_public_mode_generates_two_https_origins_and_env_updates(tmp_path: Path) -> None:
    runner = FakeRunner(healthy=True)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    result = build_runner(runner, tmp_path).run_public(
        public_domain="hong168.win", admin_domain="admin.hong168.win", dry_run=False
    )
    monkeypatch.undo()
    assert result.exit_code == 0
    assert "https://hong168.win" in (tmp_path / "noyra.env").read_text(encoding="utf-8")
    caddy = (tmp_path / "Caddyfile").read_text(encoding="utf-8")
    assert "hong168.win" in caddy and "admin.hong168.win" in caddy
    assert "127.0.0.1:8765" in caddy
    assert "NOYRA_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128" in (tmp_path / "noyra.env").read_text(
        encoding="utf-8"
    )
    assert "NOYRA_ADMIN_SESSION_COOKIE_SECURE=true" in (tmp_path / "noyra.env").read_text(
        encoding="utf-8"
    )


def test_public_mode_refuses_existing_proxy_without_replace(tmp_path: Path) -> None:
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("existing\n", encoding="utf-8")
    result = build_runner(FakeRunner(), tmp_path).run_public(
        public_domain="example.com", admin_domain="admin.example.com", dry_run=False
    )
    assert result.exit_code != 0
    assert "NOYRA_SETUP_PROXY_EXISTS" in result.stderr


def test_public_mode_rolls_back_both_files_when_validation_fails(tmp_path: Path) -> None:
    runner = FakeRunner(healthy=False)
    setup = build_runner(runner, tmp_path, replace=True)
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("known-good\n", encoding="utf-8")
    env = tmp_path / "noyra.env"
    original_env = env.read_text(encoding="utf-8")
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert result.exit_code != 0
    assert env.read_text(encoding="utf-8") == original_env
    assert caddy.read_text(encoding="utf-8") == "known-good\n"


def test_public_mode_requires_loopback_listener_and_port(tmp_path: Path) -> None:
    runner = FakeRunner()
    setup = build_runner(runner, tmp_path)
    env = tmp_path / "noyra.env"
    env.write_text("NOYRA_HOST=0.0.0.0\nNOYRA_PORT=8765\n", encoding="utf-8")
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_UNSAFE_LISTENER" in result.stderr
    env.write_text("NOYRA_HOST=127.0.0.1\nNOYRA_PORT=8080\n", encoding="utf-8")
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_UNSAFE_PORT" in result.stderr


def test_public_mode_accepts_native_listener_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    runner = FakeRunner()
    setup = build_runner(runner, tmp_path)
    env = tmp_path / "noyra.env"
    env.write_text("OTHER=value\n", encoding="utf-8")
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert result.exit_code == 0


def test_public_mode_dns_pending_prevents_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    setup = build_runner(runner, tmp_path)
    env = tmp_path / "noyra.env"
    original = env.read_text(encoding="utf-8")

    def missing_dns(*args, **kwargs):
        raise socket.gaierror("not found")

    monkeypatch.setattr(socket, "getaddrinfo", missing_dns)
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_DNS_PENDING" in result.stderr
    assert env.read_text(encoding="utf-8") == original
    assert not (tmp_path / "Caddyfile").exists()


def test_public_mode_command_oserror_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    setup = build_runner(runner, tmp_path, replace=True)
    env = tmp_path / "noyra.env"
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("known-good\n", encoding="utf-8")
    original = env.read_text(encoding="utf-8")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )

    def command_error(*args, **kwargs):
        command = tuple(args[0])
        if command[:2] == ("caddy", "validate"):
            raise FileNotFoundError("caddy")
        return CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(runner, "run", command_error)
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_CADDY_VALIDATE_FAILED" in result.stderr
    assert env.read_text(encoding="utf-8") == original
    assert caddy.read_text(encoding="utf-8") == "known-good\n"


def test_public_mode_reports_rollback_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeRunner()
    setup = build_runner(runner, tmp_path, replace=True)
    caddy = tmp_path / "Caddyfile"
    caddy.write_text("known-good\n", encoding="utf-8")
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    deployment_module = __import__("noyra.deployment_setup", fromlist=["restore_backup"])
    original_restore = deployment_module.restore_backup

    def broken_restore(record):
        if record.original_path == caddy:
            raise OSError("restore failed")
        return original_restore(record)

    monkeypatch.setattr("noyra.deployment_setup.restore_backup", broken_restore)
    original_run = runner.run

    def fail_external_health(argv, *, check=True, input_text=None):
        if tuple(argv) == ("curl", "--fail", "https://admin.example.com/health/ready"):
            return CompletedProcess(argv, 1, "", "failed")
        return original_run(argv, check=check, input_text=input_text)

    monkeypatch.setattr(runner, "run", fail_external_health)
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_ROLLBACK_FAILED" in result.stderr


def test_public_mode_rejects_custom_caddy_path_for_real_runner(tmp_path: Path) -> None:
    env = tmp_path / "noyra.env"
    env.write_text("NOYRA_HOST=127.0.0.1\nNOYRA_PORT=8765\n", encoding="utf-8")
    from noyra.deployment_setup import SubprocessRunner

    setup = SetupRunner(
        SetupOptions(env_path=env, caddy_path=tmp_path / "custom.Caddyfile"), SubprocessRunner()
    )
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_CADDY_PATH_UNSUPPORTED" in result.stderr


def test_public_mode_real_runner_rejects_non_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "noyra.env"
    env.write_text("NOYRA_HOST=127.0.0.1\nNOYRA_PORT=8765\n", encoding="utf-8")
    from noyra.deployment_setup import SubprocessRunner

    monkeypatch.setattr("noyra.deployment_setup.os.geteuid", lambda: 1000, raising=False)
    setup = SetupRunner(
        SetupOptions(env_path=env, caddy_path=Path("/etc/caddy/Caddyfile")),
        SubprocessRunner(),
    )
    result = setup.run_public(public_domain="example.com", admin_domain="admin.example.com")
    assert "NOYRA_SETUP_ROOT_REQUIRED" in result.stderr
    assert env.read_text(encoding="utf-8") == "NOYRA_HOST=127.0.0.1\nNOYRA_PORT=8765\n"


def test_cloudflare_mode_writes_protected_token_file_without_logging_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "cf-secret-token-value"
    token_path = tmp_path / "cloudflare-tunnel-token"
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    result = build_runner(FakeRunner(), tmp_path).run_cloudflare(
        public_domain="hong168.win",
        admin_domain="admin.hong168.win",
        tunnel_token=secret,
        token_path=token_path,
        dry_run=False,
    )
    assert result.exit_code == 0
    assert token_path.read_text(encoding="utf-8") == secret + "\n"
    assert secret not in result.stdout
    if os.name != "nt":
        assert token_path.stat().st_mode & 0o077 == 0


def test_cloudflare_dry_run_does_not_write_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    token_path = tmp_path / "cloudflare-tunnel-token"
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    result = build_runner(FakeRunner(), tmp_path).run_cloudflare(
        public_domain="example.com",
        admin_domain="admin.example.com",
        tunnel_token="secret",
        token_path=token_path,
        dry_run=True,
    )
    assert result.exit_code == 0
    assert not token_path.exists()


def test_cloudflare_dns_preflight_and_connector_validation_happen_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket, "getaddrinfo", lambda *args, **kwargs: (_ for _ in ()).throw(socket.gaierror())
    )
    runner = FakeRunner()
    token_path = tmp_path / "cloudflare-tunnel-token"
    result = build_runner(runner, tmp_path).run_cloudflare(
        public_domain="example.com",
        admin_domain="admin.example.com",
        tunnel_token="secret",
        token_path=token_path,
        dry_run=False,
    )
    assert "NOYRA_SETUP_DNS_PENDING" in result.stderr
    assert not token_path.exists()
    assert ("cloudflared", "--version") not in runner.calls
    assert all(call[:2] != ("systemctl", "enable") for call in runner.calls)


def test_cloudflare_redacts_token_from_health_error_and_restores_service_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )
    secret = "secret-token"

    class Runner(FakeRunner):
        def run(self, argv, *, check=True, input_text=None):
            command = tuple(argv)
            self.calls.append(command)
            if command[:2] == ("systemctl", "is-enabled"):
                return CompletedProcess(command, 0, "enabled\n", "")
            if command[:2] == ("systemctl", "is-active"):
                return CompletedProcess(command, 0, "active\n", "")
            if command == ("curl", "--fail", "https://admin.example.com/health/ready"):
                return CompletedProcess(command, 1, "", secret)
            return CompletedProcess(command, 0, "", "")

    runner = Runner()
    token_path = tmp_path / "cloudflare-tunnel-token"
    result = build_runner(runner, tmp_path).run_cloudflare(
        public_domain="example.com",
        admin_domain="admin.example.com",
        tunnel_token=secret,
        token_path=token_path,
    )
    assert result.exit_code != 0
    assert secret not in result.stderr
    assert not token_path.exists()
    assert not (tmp_path / "cloudflared-noyra.service").exists()
    assert ("systemctl", "enable", "--now", "cloudflared-noyra") in runner.calls
    assert ("systemctl", "enable", "--now", "cloudflared-noyra") in runner.calls[-3:]


def test_cloudflare_rollback_preserves_static_service_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        socket,
        "getaddrinfo",
        lambda *args, **kwargs: [(0, 0, 0, "", ("127.0.0.1", 443))],
    )

    class Runner(FakeRunner):
        def run(self, argv, *, check=True, input_text=None):
            command = tuple(argv)
            self.calls.append(command)
            if command[:2] == ("systemctl", "is-enabled"):
                return CompletedProcess(command, 0, "static\n", "")
            if command[:2] == ("systemctl", "is-active"):
                return CompletedProcess(command, 0, "inactive\n", "")
            if command == ("curl", "--fail", "https://admin.example.com/health/ready"):
                return CompletedProcess(command, 1, "", "failed")
            return CompletedProcess(command, 0, "", "")

    runner = Runner()
    result = build_runner(runner, tmp_path).run_cloudflare(
        public_domain="example.com",
        admin_domain="admin.example.com",
        tunnel_token="secret",
        token_path=tmp_path / "cloudflare-tunnel-token",
    )
    assert result.exit_code != 0
    assert ("systemctl", "stop", "cloudflared-noyra") in runner.calls
    assert ("systemctl", "start", "cloudflared-noyra") not in runner.calls
    assert ("systemctl", "enable", "cloudflared-noyra") not in runner.calls


@pytest.mark.parametrize(
    ("enabled_state", "was_active", "expected"),
    [
        ("enabled", True, [("systemctl", "enable", "--now", "cloudflared-noyra")]),
        (
            "enabled",
            False,
            [
                ("systemctl", "stop", "cloudflared-noyra"),
                ("systemctl", "enable", "cloudflared-noyra"),
            ],
        ),
        (
            "enabled-runtime",
            True,
            [
                ("systemctl", "disable", "cloudflared-noyra"),
                ("systemctl", "enable", "--runtime", "--now", "cloudflared-noyra"),
            ],
        ),
        (
            "enabled-runtime",
            False,
            [
                ("systemctl", "disable", "cloudflared-noyra"),
                ("systemctl", "stop", "cloudflared-noyra"),
                ("systemctl", "enable", "--runtime", "cloudflared-noyra"),
            ],
        ),
        (
            "disabled",
            True,
            [
                ("systemctl", "start", "cloudflared-noyra"),
                ("systemctl", "disable", "cloudflared-noyra"),
            ],
        ),
        (
            "disabled",
            False,
            [
                ("systemctl", "stop", "cloudflared-noyra"),
                ("systemctl", "disable", "cloudflared-noyra"),
            ],
        ),
        ("static", True, [("systemctl", "start", "cloudflared-noyra")]),
        ("static", False, [("systemctl", "stop", "cloudflared-noyra")]),
        ("indirect", True, [("systemctl", "start", "cloudflared-noyra")]),
        ("indirect", False, [("systemctl", "stop", "cloudflared-noyra")]),
        ("generated", True, [("systemctl", "start", "cloudflared-noyra")]),
        ("generated", False, [("systemctl", "stop", "cloudflared-noyra")]),
        ("alias", True, [("systemctl", "start", "cloudflared-noyra")]),
        ("alias", False, [("systemctl", "stop", "cloudflared-noyra")]),
    ],
)
def test_cloudflare_rollback_restores_systemd_state_matrix(
    tmp_path: Path,
    enabled_state: str,
    was_active: bool,
    expected: list[tuple[str, ...]],
) -> None:
    class Runner:
        def __init__(self) -> None:
            self.calls: list[tuple[str, ...]] = []

        def run(self, argv, *, check=True, input_text=None):
            del check, input_text
            command = tuple(argv)
            self.calls.append(command)
            return CompletedProcess(command, 0, "", "")

    source = tmp_path / "env"
    source.write_text("known-good\n", encoding="utf-8")
    backup = create_backup(source, tmp_path / "backups", "cloudflare-env")
    runner = Runner()
    SetupRunner(SetupOptions(), runner)._rollback_cloudflare(
        backup,
        None,
        None,
        None,
        None,
        service_mutated=True,
        enabled_state=enabled_state,
        was_active=was_active,
    )
    service_calls = [
        call for call in runner.calls if call[:1] == ("systemctl",) and call[1] != "daemon-reload"
    ]
    assert service_calls == expected
