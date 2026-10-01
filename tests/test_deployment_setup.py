from pathlib import Path

import pytest

from noyra.deployment_setup import (
    SetupError,
    create_backup,
    parse_env_file,
    redact_token,
    render_caddyfile,
    restore_backup,
    update_env_text,
    validate_hostname,
)


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
