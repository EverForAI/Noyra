"""Operator-only publication directory and controlled workflow export."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import parse_qs

from noyra.core.admission import OperationInvalidated
from noyra.core.at_rest import AtRestError
from noyra.core.errors import (
    ArchiveKeyUnavailableError,
    IntegrityError,
    RuntimeOwnershipError,
    SchemaContractError,
)
from noyra.core.operator_controls import OperatorControlConflict

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def dispatch(
    self: RequestHandler, owner: NoyraHTTPServer, *, write: bool, verify: bool = False
) -> None:
    if not self._authorized("operator"):
        if write:
            self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    controls = owner.operator_controls
    if controls is None:
        if write:
            self._discard_small_request_body()
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "operator_controls_unavailable"})
        return
    try:
        if write:
            if self.headers.get_content_type() != "application/json":
                self._discard_small_request_body()
                self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                return
            payload = self._request_json()
            if payload is None:
                return
            if verify:
                if set(payload) != {"publication_id"} or not isinstance(
                    payload["publication_id"], str
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_workflow_export_request"})
                    return
                result = controls.verify_workflow_export(payload["publication_id"])
            else:
                if set(payload) != {"workflow_id", "expected_version", "request_key", "reason"}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_workflow_export_request"})
                    return
                if (
                    not isinstance(payload["workflow_id"], str)
                    or not isinstance(payload["request_key"], str)
                    or not isinstance(payload["reason"], str)
                    or type(payload["expected_version"]) is not int
                    or payload["expected_version"] < 1
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_workflow_export_request"})
                    return
                result = controls.export_workflow(
                    payload["workflow_id"],
                    payload["expected_version"],
                    payload["request_key"],
                    actor=self._actor(),
                    reason=payload["reason"],
                )
        else:
            query = parse_qs(self.path.partition("?")[2], keep_blank_values=True)
            if set(query) - {"limit"} or any(len(values) != 1 for values in query.values()):
                raise ValueError("invalid directory query")
            result = controls.workflow_exports(limit=int(query.get("limit", ["50"])[0]))
    except ValueError:
        self._json(
            HTTPStatus.CONFLICT if write else HTTPStatus.BAD_REQUEST,
            {
                "error": "workflow_export_snapshot_or_request_conflict"
                if write
                else "invalid_workflow_export_query"
            },
        )
        return
    except OperatorControlConflict as error:
        self._json(self._operator_error_status(error), {"error": error.code})
        return
    except (
        AtRestError,
        ArchiveKeyUnavailableError,
        OperationInvalidated,
        RuntimeOwnershipError,
        OSError,
        sqlite3.Error,
    ):
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "workflow_export_control_or_storage_unavailable"},
        )
        return
    except (IntegrityError, SchemaContractError, PermissionError):
        self._json(HTTPStatus.CONFLICT, {"error": "workflow_export_evidence_unavailable"})
        return
    self._json(HTTPStatus.OK, result)
