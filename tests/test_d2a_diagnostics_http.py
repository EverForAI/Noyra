from __future__ import annotations

from unittest.mock import patch

from noyra.core.lifecycle_diagnostics import LifecycleCatalogUnavailable
from noyra.service import NoyraService
from test_authentication_input_contract import call
from test_m42_p3_06_operator_controls import service as service


def test_lifecycle_catalog_http_is_operator_only_and_versioned_alias_matches(
    service: tuple[NoyraService, str, str],
) -> None:
    instance, token, reader = service
    path = "/api/admin/retention/lifecycle"
    for auth in ({}, {"Authorization": "Bearer " + reader}):
        assert call(instance.http, path, headers=auth)[0] == 401
    headers = {"Authorization": "Bearer " + token}
    status, first, _ = call(instance.http, path + "?limit=3", headers=headers)
    assert status == 200 and len(first["items"]) == 3
    assert first["next_cursor"]
    assert (
        call(instance.http, path.replace("/api/", "/api/v1/") + "?limit=3", headers=headers)[1]
        == first
    )
    assert instance.kernel.lifecycle.current().state == "active"
    assert instance.kernel.admission.accepting
    for query in ("?limit=0", "?limit=101", "?limit=5&limit=6", "?cursor=", "?unknown=x"):
        assert call(instance.http, path + query, headers=headers)[0] == 400
    with patch.object(
        instance.http.retention, "lifecycle_catalog", side_effect=LifecycleCatalogUnavailable
    ):
        assert call(instance.http, path, headers=headers)[0] == 503
        status, retention, _ = call(instance.http, "/api/admin/retention", headers=headers)
        assert status == 200 and retention["lifecycle_diagnostics"]["status"] == "unavailable"
        assert "settings" in retention and "evidence_capacity" in retention
    status, retention, _ = call(instance.http, "/api/admin/retention", headers=headers)
    assert status == 200
    assert retention["lifecycle_diagnostics"]["policy_coverage"]["advisory_only"]
    assert "items" not in retention["lifecycle_diagnostics"]
