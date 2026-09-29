from __future__ import annotations

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
