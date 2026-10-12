from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from pydantic import SecretStr

from noyra.core import SubjectKernel
from noyra.core.types import content_hash
from noyra.service import NoyraHTTPServer, ServiceSettings
from noyra.service_contract import route_contracts


class PublicArchiveHTTPTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.kernel = SubjectKernel(root / "data.sqlite3", "archive-http", content_hash("http"))
        self.kernel.boot()
        self.kernel.orient()
        self.kernel.activate()
        self.token = "synthetic-public-test-token-with-entropy"
        self.http = NoyraHTTPServer(
            self.kernel,
            ServiceSettings(
                data_dir=root,
                port=0,
                admin_token=SecretStr(self.token),
                subject_id=self.kernel.subject_id,
                genesis_hash=content_hash("http"),
            ),
        )
        self.http.start()
        self.base_url = f"http://127.0.0.1:{self.http.address[1]}"

    def tearDown(self) -> None:
        self.http.close()
        self.kernel.close()
        self.temp.cleanup()

    def get_json(self, path: str) -> Any:
        with urlopen(self.base_url + path, timeout=5) as response:
            return json.loads(response.read())

    def authorized_json(self, path: str, *, payload: dict[str, Any]) -> tuple[int, Any]:
        request = Request(
            self.base_url + path,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token},
        )
        with urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())

    def test_anonymous_public_v2_and_legacy_contract(self) -> None:
        for path, contract in (
            ("/api/state", "public-contract-v1"),
            ("/api/public/overview", "public-contract-v2"),
            ("/api/public/timeline", "public-contract-v2"),
        ):
            with urlopen(self.base_url + path, timeout=5) as response:
                self.assertEqual(response.headers["X-Noyra-Public-Contract"], contract)
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                payload = json.loads(response.read())
                self.assertNotIn("reason", str(payload))
        with self.assertRaises(HTTPError) as unauthorized:
            self.get_json("/api/admin/public-publications")
        self.assertEqual(unauthorized.exception.code, 401)

    def test_publish_edit_withdraw_and_anonymous_visibility(self) -> None:
        from test_public_archive import publication

        _, item = self.authorized_json("/api/admin/public-publications", payload=publication())
        assert isinstance(item, dict)
        records = self.get_json("/api/public/goals")
        assert isinstance(records, dict)
        self.assertEqual(records["items"][0]["id"], item["id"])
        self.assertNotIn("source_id", records["items"][0])
        self.assertEqual(records["items"][0]["body"], "")
        detail = self.get_json(f"/api/public/goals?id={item['id']}")
        self.assertEqual(detail["items"][0]["body"], item["body"])
        self.authorized_json(
            f"/api/admin/public-publications/{item['id']}/withdraw",
            payload={"expected_revision": 1},
        )
        records = self.get_json("/api/public/goals")
        assert isinstance(records, dict)
        self.assertEqual(records["items"], [])
        self.assertEqual(self.get_json(f"/api/public/goals?id={item['id']}")["items"], [])

    def test_public_routes_and_versioned_aliases_match_central_inventory(self) -> None:
        routes = (
            "overview",
            "timeline",
            "diary",
            "behavior",
            "interactions",
            "posts",
            "goals",
            "projects",
            "predictions",
            "research",
            "sleep",
            "lifecycle",
            "updates",
        )
        inventory = {route.path: route for route in route_contracts() if route.method == "GET"}
        for route in routes:
            versioned = f"/api/v1/public/{route}"
            self.assertEqual(inventory[versioned].role, "none")
            self.assertTrue(
                {200, 400, 413, 429, 503} <= set(inventory[versioned].effective_responses)
            )
            for prefix in ("/api/public/", "/api/v1/public/"):
                with (
                    self.subTest(route=route, prefix=prefix),
                    urlopen(self.base_url + prefix + route, timeout=5) as response,
                ):
                    self.assertEqual(response.status, 200)
                    self.assertEqual(
                        response.headers["X-Noyra-Public-Contract"], "public-contract-v2"
                    )
                    self.assertEqual(json.loads(response.read())["contract"], "public-contract-v2")
        from test_public_archive import publication

        _, item = self.authorized_json("/api/v1/admin/public-publications", payload=publication())
        self.authorized_json(
            f"/api/v1/admin/public-publications/{item['id']}/withdraw",
            payload={"expected_revision": 1},
        )
        self.assertEqual(self.get_json(f"/api/v1/public/goals?id={item['id']}")["items"], [])

    def test_invalid_queries_and_unauthenticated_mutations_are_rejected(self) -> None:
        for query in (
            "limit=0",
            "limit=1001",
            "limit=10&limit=20",
            "unknown=secret",
            "since=not-time",
            "cursor=bad!",
            "kind=private",
            "status=",
            "until=",
            "id=",
            "id=example&id=another",
        ):
            if query == "status=":
                continue  # An empty status means all public statuses.
            with self.assertRaises(HTTPError) as invalid:
                self.get_json("/api/public/timeline?" + query)
            self.assertEqual(invalid.exception.code, 400, query)
        request = Request(
            self.base_url + "/api/admin/public-publications",
            data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as unauthorized:
            urlopen(request, timeout=5)
        self.assertEqual(unauthorized.exception.code, 401)

    def test_cookie_session_publication_csrf_conflict_and_bounded_management(self) -> None:
        from http.cookiejar import CookieJar
        from urllib.request import HTTPCookieProcessor, build_opener

        from test_public_archive import publication

        opener = build_opener(HTTPCookieProcessor(CookieJar()))
        login = Request(
            self.base_url + "/admin/session",
            data=json.dumps({"token": self.token}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with opener.open(login, timeout=5) as response:
            session = json.loads(response.read())
        path = self.base_url + "/api/admin/public-publications"
        request = Request(
            path,
            data=json.dumps(publication()).encode(),
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as missing_csrf:
            opener.open(request, timeout=5)
        self.assertEqual(missing_csrf.exception.code, 401)
        request.add_header("X-CSRF-Token", session["csrf_token"])
        with opener.open(request, timeout=5) as response:
            self.assertEqual(json.loads(response.read())["revision"], 1)
        request = Request(
            path,
            data=json.dumps(publication(title="conflict")).encode(),
            headers={"Content-Type": "application/json", "X-CSRF-Token": session["csrf_token"]},
        )
        with self.assertRaises(HTTPError) as conflict:
            opener.open(request, timeout=5)
        self.assertEqual(conflict.exception.code, 409)
        with opener.open(path + "?view=management&limit=1", timeout=5) as response:
            self.assertEqual(len(json.loads(response.read())["items"]), 1)
        with self.assertRaises(HTTPError) as bad_limit:
            opener.open(path + "?view=management&limit=51", timeout=5)
        self.assertEqual(bad_limit.exception.code, 400)
