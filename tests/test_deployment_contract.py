from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CADDY = (ROOT / "deploy" / "caddy" / "noyra.Caddyfile.example").read_text(encoding="utf-8")
NGINX = (ROOT / "deploy" / "nginx" / "noyra.conf.example").read_text(encoding="utf-8")
ENV = (ROOT / "deploy" / "noyra.env.example").read_text(encoding="utf-8")
DEPLOYMENT = (ROOT / "docs" / "deployment" / "ubuntu.md").read_text(encoding="utf-8")


def test_reverse_proxy_examples_publish_the_complete_https_security_contract() -> None:
    required = (
        "NOYRA_PUBLIC_SITE_URL=https://archive.example.com",
        "NOYRA_TRUSTED_PROXY_CIDRS=127.0.0.1/32,::1/128",
        "NOYRA_ADMIN_SESSION_COOKIE_SECURE=true",
        "NOYRA_ADMIN_SESSION_TTL_SECONDS=43200",
        "127.0.0.1:8765",
    )
    for template in (CADDY, NGINX):
        for marker in required:
            assert marker in template, marker
    assert "admin.example.com" in CADDY
    assert "admin.example.com" in NGINX
    assert "header_up X-Real-IP {remote_host}" in CADDY
    assert "Generated setup path: noyra setup --mode public" in CADDY


def test_deployment_docs_match_proxy_and_session_contract() -> None:
    for marker in (
        "NOYRA_TRUSTED_PROXY_CIDRS",
        "NOYRA_ADMIN_SESSION_COOKIE_SECURE=true",
        "NOYRA_ADMIN_SESSION_TTL_SECONDS",
        "admin.example.com",
        "127.0.0.1:8765",
        "production preflight",
    ):
        assert marker in DEPLOYMENT
    assert "NOYRA_ADMIN_SESSION_TTL_SECONDS=43200" in ENV


def test_setup_modes_document_all_three_entry_points() -> None:
    text = (ROOT / "docs" / "deployment" / "setup-modes.md").read_text(encoding="utf-8")
    for marker in (
        "noyra setup --mode local",
        "noyra setup --mode public",
        "noyra setup --mode cloudflare",
    ):
        assert marker in text
    assert "127.0.0.1:8765" in text
    assert "cloudflare-tunnel-token" in text


def test_nginx_logs_exclude_client_targets_headers_and_unvalidated_methods() -> None:
    text = "\n".join(line for line in NGINX.splitlines() if not line.lstrip().startswith("#"))
    format_match = re.search(r"log_format\s+noyra_privacy\s+escape=json\s+([^;]+);", text)
    assert format_match is not None
    assert set(re.findall(r"\$[a-z_]+", format_match[1])) == {
        "$noyra_log_method",
        "$status",
        "$request_time",
    }
    method_map = re.search(r"map\s+\$request_method\s+\$noyra_log_method\s*\{([^}]+)}", text)
    assert method_map is not None
    entries = dict(re.findall(r"(\w+)\s+(\w+);", method_map[1]))
    assert entries.pop("default") == "UNKNOWN"
    assert set(entries) == {
        "GET",
        "POST",
        "PUT",
        "PATCH",
        "DELETE",
        "HEAD",
        "OPTIONS",
        "TRACE",
        "CONNECT",
    }
    assert all(method == value for method, value in entries.items())
    servers = text.split("server {")[1:]
    assert len(servers) == 2
    for server in servers:
        assert re.findall(r"access_log\s+([^;]+);", server) == [
            "/var/log/nginx/noyra-access.log noyra_privacy"
        ]
        assert re.findall(r"error_log\s+([^;]+);", server) == ["/dev/null crit"]
