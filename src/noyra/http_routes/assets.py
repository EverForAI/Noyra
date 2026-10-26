"""Assets HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import logging
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler

LOGGER = logging.getLogger("noyra.service")


def admin_admin_html(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("admin.html", "text/html; charset=utf-8")
    return True
    return False


def admin_js(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("admin.js", "text/javascript; charset=utf-8")
    return True
    return False


def admin_css(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("admin.css", "text/css; charset=utf-8")
    return True
    return False


def admin_session(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._admin_session_status()
    return True
    return False


def index(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("index.html", "text/html; charset=utf-8")
    return True
    return False


def participant_js(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("participant.js", "text/javascript; charset=utf-8")
    return True
    return False


def app_js(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("app.js", "text/javascript; charset=utf-8")
    return True
    return False


def styles_css(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("styles.css", "text/css; charset=utf-8")
    return True
    return False


def assets_public_hero_webp(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("assets/public-hero.webp", "image/webp")
    return True
    return False


def assets_public_hero_mobile_webp(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("assets/public-hero-mobile.webp", "image/webp")
    return True
    return False


def assets_public_social_png(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._asset("assets/public-social.png", "image/png")
    return True
    return False


def robots_txt(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._robots_txt()
    return True
    return False


def sitemap_xml(self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult) -> bool:
    self._sitemap_xml(parsed)
    return True
    return False


def favicon_ico(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self.send_response(HTTPStatus.NO_CONTENT)
    self._headers("image/x-icon", 0)
    self.end_headers()
    return True
    return False
