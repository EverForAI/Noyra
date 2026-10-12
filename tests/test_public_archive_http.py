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

    def test_damaged_public_interaction_returns_safe_503(self) -> None:
        from noyra.interaction import InteractionStore

        message = InteractionStore(self.kernel.database).send(
            self.kernel.subject_id, "public:local", "PRIVATE-PEER", "approved text"
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE interactions SET content=? WHERE interaction_id=?",
                ("TAMPERED-SECRET", message.interaction_id),
            )
        with self.assertRaises(HTTPError) as failure:
            self.get_json("/api/public/interactions")
        self.assertEqual(failure.exception.code, 503)
        self.assertEqual(
            json.loads(failure.exception.read()), {"error": "public_content_unavailable"}
        )

    def test_reconciliation_requires_authentication_and_reads_exact_identity(self) -> None:
        from test_public_archive import publication

        _, item = self.authorized_json("/api/admin/public-publications", payload=publication())
        for identifier, expected in [(item["id"], item["id"]), ("absent-id", None)]:
            path = "/api/admin/public-publications?view=management&id=" + identifier
            with self.assertRaises(HTTPError) as failure:
                self.get_json(path)
            self.assertEqual(failure.exception.code, 401)
            with urlopen(
                Request(self.base_url + path, headers={"Authorization": "Bearer " + self.token}),
                timeout=5,
            ) as response:
                result = json.loads(response.read())["item"]
                self.assertEqual(result["id"] if result else None, expected)

    def test_time_overflow_and_budget_failures_keep_http_contract(self) -> None:
        from unittest.mock import patch
        from urllib.parse import urlencode

        from noyra.interaction.public_archive import PublicQueryBudgetExceeded
        from test_public_archive import publication

        timestamp = "9999-12-31T23:59:59-01:00"
        with self.assertRaises(HTTPError) as invalid:
            self.get_json("/api/public/goals?" + urlencode({"since": timestamp}))
        self.assertEqual(invalid.exception.code, 400)
        with self.assertRaises(HTTPError) as invalid:
            self.authorized_json(
                "/api/admin/public-publications",
                payload=publication(kind="sleep", details={"started_at": timestamp}),
            )
        self.assertEqual(invalid.exception.code, 400)
        with (
            patch.object(
                self.http.public_archive, "overview", side_effect=PublicQueryBudgetExceeded()
            ),
            self.assertRaises(HTTPError) as exhausted,
        ):
            self.get_json("/api/public/overview")
        self.assertEqual(exhausted.exception.code, 503)
        self.assertEqual(exhausted.exception.headers["Retry-After"], "5")
        self.assertEqual(
            json.loads(exhausted.exception.read()), {"error": "public_query_budget_exceeded"}
        )
        self.assertEqual(self.get_json("/api/public/overview")["contract"], "public-contract-v2")

    def test_standalone_public_article_and_sitemap_recheck_withdrawal(self) -> None:
        from test_public_archive import publication

        self.http.settings = self.http.settings.model_copy(
            update={"public_site_url": "https://public.example"}
        )
        _, item = self.authorized_json(
            "/api/admin/public-publications",
            payload=publication(
                title="Share title", body="# First\nBody\n\n## Second\n<script>bad()</script>"
            ),
        )
        path = f"/public/goal/{item['id']}"
        with urlopen(
            self.base_url + path + "?lang=en&theme=dark&return=timeline", timeout=5
        ) as response:
            markup = response.read().decode()
            self.assertIn('data-theme="dark"', markup)
            self.assertIn("#timeline", markup)
            self.assertIn("lang=en", markup)
        for query in ("?theme=unknown", "?return=https://other.test", "?lang=en&lang=zh"):
            with self.assertRaises(HTTPError) as invalid:
                urlopen(self.base_url + path + query, timeout=5)
            self.assertEqual(invalid.exception.code, 400)
        with urlopen(self.base_url + path, timeout=5) as response:
            markup = response.read().decode()
            self.assertEqual(response.headers["Cache-Control"], "no-store")
            self.assertIn("<title>Share title · Noyra</title>", markup)
            self.assertIn("https://public.example" + path, markup)
            self.assertNotIn("<script>", markup)
            self.assertIn("reading-toc", markup)
        with urlopen(self.base_url + "/sitemap.xml", timeout=5) as response:
            from urllib.parse import urlsplit
            from xml.etree.ElementTree import fromstring

            index = fromstring(response.read())
            locations = [
                node.text for node in index.iter("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
            ]
        shards = []
        for location in locations:
            parsed = urlsplit(location)
            with urlopen(self.base_url + parsed.path + "?" + parsed.query, timeout=5) as response:
                shards.append(response.read().decode())
        self.assertIn(path, "".join(shards))
        self.authorized_json(
            f"/api/admin/public-publications/{item['id']}/withdraw",
            payload={"expected_revision": 1},
        )
        with self.assertRaises(HTTPError) as absent:
            urlopen(self.base_url + path, timeout=5)
        self.assertEqual(absent.exception.code, 404)
        self.assertEqual(absent.exception.headers["Cache-Control"], "no-store")
        shards = []
        for location in locations:
            parsed = urlsplit(location)
            with urlopen(self.base_url + parsed.path + "?" + parsed.query, timeout=5) as response:
                shards.append(response.read().decode())
        self.assertNotIn(path, "".join(shards))

    def test_standalone_reader_uses_public_visibility_and_safe_failures(self) -> None:
        from unittest.mock import patch

        from noyra.interaction import InteractionStore
        from noyra.interaction.public_archive import PublicQueryBudgetExceeded

        store = InteractionStore(self.kernel.database)
        for channel in ("private:local", "PUBLIC:local", "Public:local"):
            message = store.send(
                self.kernel.subject_id, channel, "private-peer", "PRIVATE-SENTINEL"
            )
            with self.assertRaises(HTTPError) as absent:
                urlopen(self.base_url + "/public/interaction/" + message.interaction_id, timeout=5)
            self.assertEqual(absent.exception.code, 404)
            self.assertNotIn(b"PRIVATE-SENTINEL", absent.exception.read())
        message = store.send(self.kernel.subject_id, "public:local", "private-peer", "Public text")
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE interactions SET content=? WHERE interaction_id=?",
                ("TAMPERED-SENTINEL", message.interaction_id),
            )
        with self.assertRaises(HTTPError) as damaged:
            urlopen(self.base_url + "/public/interaction/" + message.interaction_id, timeout=5)
        self.assertEqual(damaged.exception.code, 503)
        self.assertNotIn(b"TAMPERED-SENTINEL", damaged.exception.read())
        with (
            patch.object(self.http.public_archive, "page", side_effect=PublicQueryBudgetExceeded()),
            self.assertRaises(HTTPError) as exhausted,
        ):
            urlopen(self.base_url + "/public/goal/example", timeout=5)
        self.assertEqual(exhausted.exception.code, 503)
        self.assertEqual(exhausted.exception.headers["Retry-After"], "5")
        self.assertEqual(
            json.loads(exhausted.exception.read()), {"error": "public_query_budget_exceeded"}
        )
        for suffix in ("?lang=en&lang=zh", "?private=1", "/%FF", "/%00"):
            path = (
                "/public/goal/example" + suffix
                if suffix.startswith("?")
                else "/public/goal" + suffix
            )
            with self.assertRaises(HTTPError) as invalid:
                urlopen(self.base_url + path, timeout=5)
            self.assertEqual(invalid.exception.code, 400)

    def test_standalone_render_failure_is_safe_and_next_request_recovers(self) -> None:
        from unittest.mock import patch

        from test_public_archive import publication

        _, item = self.authorized_json("/api/admin/public-publications", payload=publication())
        path = "/public/goal/" + item["id"]
        with (
            patch("noyra.service.public_article_html", side_effect=TypeError("PRIVATE-SENTINEL")),
            self.assertRaises(HTTPError) as failed,
        ):
            urlopen(self.base_url + path, timeout=5)
        self.assertEqual(failed.exception.code, 503)
        self.assertEqual(
            json.loads(failed.exception.read()), {"error": "public_content_unavailable"}
        )
        with urlopen(self.base_url + path, timeout=5) as response:
            self.assertEqual(response.status, 200)

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
