"""Fixed HTTP log fields; request data never becomes a log argument."""

from __future__ import annotations

import re

from noyra.service_contract import route_contracts

_METHODS = frozenset(
    {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "CONNECT"}
)
_ASSETS = frozenset(
    {
        "/",
        "/admin",
        "/admin.html",
        "/admin.js",
        "/admin.css",
        "/app.js",
        "/styles.css",
        "/assets/public-hero.webp",
        "/assets/public-hero-mobile.webp",
        "/assets/public-social.png",
        "/robots.txt",
        "/sitemap.xml",
        "/favicon.ico",
        "/admin/session",
        "/admin/session/logout",
    }
)
_TEMPLATES = frozenset(route.path for route in route_contracts()) | _ASSETS
_STATIC_ROUTES = {path: path for path in _TEMPLATES if "{" not in path}
_DYNAMIC_ROUTES = tuple(
    (
        re.compile(
            "/".join(
                "[^/]+" if part.startswith("{") else re.escape(part) for part in path.split("/")
            )
        ),
        path,
    )
    for path in sorted(_TEMPLATES, key=lambda value: (value.count("{"), value))
    if "{" in path
)


def http_log_fields(method: object, target: object, code: object) -> tuple[str, str, int | str]:
    """Return only a known method, a constant route template and a numeric status.

    This is log classification, not routing or URL validation. Do not decode,
    truncate into a valid route, or retain an unknown target. Paths as well as
    queries can contain credentials, private IDs and control characters.
    """
    safe_method = method if isinstance(method, str) and method in _METHODS else "UNKNOWN"
    safe_code: int | str = "unavailable"
    if isinstance(code, int) and not isinstance(code, bool) and 100 <= code <= 599:
        safe_code = int(code)
    elif isinstance(code, str) and len(code) == 3 and code.isascii() and code.isdecimal():
        numeric = int(code)
        if 100 <= numeric <= 599:
            safe_code = numeric
    route = "unmatched"
    if isinstance(target, str):
        path = target.partition("?")[0].partition("#")[0]
        if len(path) <= 8_192 and path.startswith("/"):
            if path.startswith("/api/") and not path.startswith("/api/v1/"):
                path = "/api/v1/" + path.removeprefix("/api/")
            elif path.startswith("/webhooks/"):
                path = "/api/v1/webhooks/" + path.removeprefix("/webhooks/")
            route = _STATIC_ROUTES.get(path, "unmatched")
            if route == "unmatched":
                for pattern, template in _DYNAMIC_ROUTES:
                    if pattern.fullmatch(path):
                        route = template
                        break
    return safe_method, route, safe_code
