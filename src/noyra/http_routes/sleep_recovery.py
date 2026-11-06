"""Bounded operator sleep-recovery HTTP contract."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.core.errors import IntegrityError
from noyra.core.operator_controls import OperatorControlConflict, OperatorControlNotFound

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def dispatch(self: RequestHandler, owner: NoyraHTTPServer, *, write: bool) -> None:
    if not self._authorized():
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
            if set(payload) != {"sleep_id", "expected_version", "reason"}:
                raise ValueError("invalid sleep recovery fields")
            result = controls.abort_sleep(
                payload["sleep_id"],
                payload["expected_version"],
                actor=self._actor(),
                reason=payload["reason"],
            )
        else:
            result = controls.sleep_recovery()
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_sleep_recovery"})
        return
    except OperatorControlNotFound:
        self._json(HTTPStatus.NOT_FOUND, {"error": "sleep_run_not_found"})
        return
    except OperatorControlConflict as error:
        self._json(self._operator_error_status(error), {"error": error.code})
        return
    except IntegrityError:
        self._json(HTTPStatus.CONFLICT, {"error": "sleep_recovery_evidence_unavailable"})
        return
    self._json(HTTPStatus.OK, result)
