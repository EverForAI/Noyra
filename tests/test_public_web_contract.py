from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
from urllib.request import Request, urlopen
from xml.etree import ElementTree

import pytest
from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.interaction.public_reading import PUBLIC_VIEWS
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.service_contract import PUBLIC_WEB_ROUTE_CONTRACTS, APIRouteContract


def test_public_web_inventory_is_separate_and_matches_handlers(tmp_path: Path) -> None:
    paths = {route.path for route in PUBLIC_WEB_ROUTE_CONTRACTS}
    assert paths == {"/public/{kind}/{id}", "/sitemap.xml"}
    assert len(paths) == len(PUBLIC_WEB_ROUTE_CONTRACTS)
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

    # Exercise the registered routes rather than their source file location.
    with ExitStack() as cleanup:
        kernel = SubjectKernel(
            tmp_path / "noyra.sqlite3", "public-web-contract", content_hash("public-web-contract")
        )
        cleanup.callback(kernel.close)
        kernel.boot()
        kernel.orient()
        kernel.activate()
        owner = NoyraHTTPServer(
            kernel,
            ServiceSettings(
                data_dir=tmp_path,
                subject_id=kernel.subject_id,
                genesis_hash=content_hash("public-web-contract"),
                host="127.0.0.1",
                port=0,
                admin_token=SecretStr("synthetic-public-contract-admin-token"),
                public_site_url="https://public.example",
            ),
        )
        cleanup.callback(owner.close)
        owner.start()
        base_url = f"http://127.0.0.1:{owner.address[1]}"
        lifecycle = owner.public_archive.page(kernel.subject_id, kind="lifecycle", limit=1)
        article_path = f"/public/lifecycle/{lifecycle['items'][0]['id']}"
        examples = {"/public/{kind}/{id}": article_path, "/sitemap.xml": "/sitemap.xml"}
        for route in PUBLIC_WEB_ROUTE_CONTRACTS:
            path = examples[route.path]
            request = Request(base_url + path, method=route.method)
            with urlopen(request, timeout=5) as response:
                assert response.status == 200
                assert response.headers["Content-Type"] == route.success_content_type
                assert response.headers["Cache-Control"] == route.cache_control
                body = response.read()
            if route.path == "/sitemap.xml":
                assert ElementTree.fromstring(body).tag == (
                    "{http://www.sitemaps.org/schemas/sitemap/0.9}sitemapindex"
                )
            else:
                assert f'href="https://public.example{path}"' in body.decode("utf-8")


def test_public_web_parameter_and_response_semantics_are_documented() -> None:
    contract = Path("docs/api/public-contract-v2.md").read_text(encoding="utf-8")
    for marker in ("theme", "return", "part", "400", "404", "413", "429", "503", "no-store"):
        assert marker in contract
