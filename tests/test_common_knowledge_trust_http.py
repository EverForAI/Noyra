from __future__ import annotations

from pathlib import Path

from noyra.service import NoyraHTTPServer
from test_m42_p3_02_common_knowledge_sync import _proposal, _store
from test_participant_http import Client
from test_participant_http import http as http


def test_operator_trust_route_requires_exact_publisher_before_import(
    http: tuple[NoyraHTTPServer, str], tmp_path: Path
) -> None:
    owner, base = http
    publisher_kernel, publisher = _store(tmp_path, "Publisher-HTTP-Binding")
    try:
        client = Client(base)
        headers = {"Authorization": "Bearer admin-test-key-with-enough-entropy"}
        route = "/api/v1/config/common-knowledge/trust"
        payload = {"public_key": publisher.public_key, "label": "HTTP publisher"}
        assert client.request(route, payload)[0] == 401
        assert client.request(route, payload, **headers)[0] == 400
        assert (
            client.request(
                route, {**payload, "publisher_subject_id": owner.kernel.subject_id}, **headers
            )[0]
            == 400
        )
        assert (
            client.request(
                route, {**payload, "publisher_subject_id": publisher.subject_id}, **headers
            )[0]
            == 201
        )
        package = publisher.publish(_proposal("HTTP verified package"))
        envelope = publisher.export_package(package.package_id)
        import_route = "/api/v1/config/common-knowledge/import"
        assert client.request(import_route, envelope, **headers)[0] == 201
        body = {key: value for key, value in envelope.items() if key != "signature"}
        body["publisher_subject_id"] = owner.kernel.subject_id
        body["package_id"] += "-forged"
        forged = {**body, "signature": publisher._sign(body)}
        code, response = client.request(import_route, forged, **headers)
        assert code == 403 and response == {"error": "publisher_not_trusted"}
        with owner.kernel.database.read_transaction() as connection:
            assert (
                connection.execute(
                    "SELECT COUNT(*) FROM common_knowledge_imports WHERE subject_id=?",
                    (owner.kernel.subject_id,),
                ).fetchone()[0]
                == 1
            )
    finally:
        publisher_kernel.close()
