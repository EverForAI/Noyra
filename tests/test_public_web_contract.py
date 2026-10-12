from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from noyra.interaction.public_reading import PUBLIC_VIEWS
from noyra.service import NoyraHTTPServer
from noyra.service_contract import PUBLIC_WEB_ROUTE_CONTRACTS, APIRouteContract


def test_public_web_inventory_is_separate_and_matches_handlers() -> None:
    paths = {route.path for route in PUBLIC_WEB_ROUTE_CONTRACTS}
    assert paths == {"/public/{kind}/{id}", "/sitemap.xml"}
    assert len(paths) == len(PUBLIC_WEB_ROUTE_CONTRACTS)
    source = inspect.getsource(NoyraHTTPServer)
    assert 'parsed.path == "/sitemap.xml"' in source
    assert 'parsed.path.startswith("/public/")' in source
    for route in PUBLIC_WEB_ROUTE_CONTRACTS:
        assert route.method == "GET" and route.cache_control == "no-store"
        assert set(route.responses) == {200, 400, 404, 413, 429, 503}
        with pytest.raises(ValueError):
            APIRouteContract("GET", route.path, "none", route.responses)
    assert set(PUBLIC_VIEWS) == {
        "diary",
        "behavior",
        "interaction",
        "post",
        "lifecycle",
        "goal",
        "project",
        "prediction",
        "research",
        "sleep",
        "update",
    }


def test_public_web_parameter_and_response_semantics_are_documented() -> None:
    contract = Path("docs/api/public-contract-v2.md").read_text(encoding="utf-8")
    for marker in ("theme", "return", "part", "400", "404", "413", "429", "503", "no-store"):
        assert marker in contract
