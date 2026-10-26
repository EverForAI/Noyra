"""Wallet resources HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.wallet import (
    ACQUISITION_RUN_STATES,
    WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH,
    WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE,
    WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
    WALLET_BALANCE_OBSERVATION_MAX_GROUPS,
    WalletAcquisitionConflictError,
    WalletAcquisitionRunError,
    WalletAddressInput,
    WalletAssetInput,
    WalletNetworkInput,
)

from .validation import _wallet_acquisition_run_payload

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def configure_wallet_network(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.wallets.register_network(
            owner.kernel.subject_id,
            WalletNetworkInput.model_validate(payload),
            actor=self._actor(),
        )
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_network"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "network_id": record.network_id,
            "label": record.label,
            "chain_family": record.chain_family,
            "chain_id": record.chain_id,
            "status": record.status,
        },
    )


def configure_wallet_asset(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.wallets.register_asset(
            owner.kernel.subject_id,
            WalletAssetInput.model_validate(payload),
            actor=self._actor(),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_network_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_asset"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "asset_id": record.asset_id,
            "network_id": record.network_id,
            "asset_type": record.asset_type,
            "symbol": record.symbol,
            "status": record.status,
        },
    )


def configure_wallet_address(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.wallets.register_address(
            owner.kernel.subject_id,
            WalletAddressInput.model_validate(payload),
            actor=self._actor(),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_network_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_address"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "address_id": record.address_id,
            "network_id": record.network_id,
            "label": record.label,
            "address": record.address,
            "status": record.status,
        },
    )


def revoke_wallet_resource(
    self: RequestHandler, owner: NoyraHTTPServer, resource_type: str
) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    prefix, identifier_field = {
        "network": ("/api/config/wallet-networks/", "network_id"),
        "asset": ("/api/config/wallet-assets/", "asset_id"),
        "address": ("/api/config/wallet-addresses/", "address_id"),
    }[resource_type]
    resource_id = self.path.removeprefix(prefix).removesuffix("/revoke")
    reason = payload.get("reason")
    if (
        set(payload) != {"reason"}
        or not isinstance(reason, str)
        or not resource_id
        or "/" in resource_id
    ):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": f"invalid_wallet_{resource_type}"},
        )
        return
    try:
        if resource_type == "network":
            resource_status = owner.wallets.revoke_network(
                resource_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            ).status
        elif resource_type == "asset":
            resource_status = owner.wallets.revoke_asset(
                resource_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            ).status
        else:
            resource_status = owner.wallets.revoke_address(
                resource_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            ).status
    except NotFoundError:
        self._json(
            HTTPStatus.NOT_FOUND,
            {"error": f"wallet_{resource_type}_not_found"},
        )
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": f"invalid_wallet_{resource_type}"},
        )
        return
    self._json(
        HTTPStatus.OK,
        {identifier_field: resource_id, "status": resource_status},
    )


def wallet_observation_health_query(
    owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> tuple[str, int, str | None, str | None]:
    allowed = {"group_by", "limit", "network_id", "source"}
    if set(query) - allowed:
        raise ValueError("wallet observation health query is invalid")

    def one(name: str) -> str | None:
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1:
            raise ValueError("wallet observation health query is invalid")
        return values[0]

    raw_group_value = one("group_by")
    raw_group = "network" if raw_group_value is None else raw_group_value
    if raw_group not in {"network", "source"}:
        raise ValueError("wallet observation health group is invalid")
    raw_limit = one("limit")
    if raw_limit is None:
        bounded_limit = WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT
    elif (
        not raw_limit.isascii()
        or not raw_limit.isdecimal()
        or not 1 <= int(raw_limit) <= WALLET_BALANCE_OBSERVATION_MAX_GROUPS
    ):
        raise ValueError("wallet observation health limit is invalid")
    else:
        bounded_limit = int(raw_limit)
    network_id = one("network_id")
    source = one("source")
    if network_id == "" or source == "":
        raise ValueError("wallet observation health filter is invalid")
    return raw_group, bounded_limit, network_id, source


def wallet_balance_history_query(
    owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> tuple[int, str | None, str | None, str | None, str | None]:
    allowed = {"limit", "cursor", "network_id", "asset_id", "address_id"}
    if set(query) - allowed:
        raise ValueError("wallet balance history query is invalid")

    def one(name: str) -> str | None:
        values = query.get(name)
        if values is None:
            return None
        if len(values) != 1:
            raise ValueError("wallet balance history query is invalid")
        return values[0]

    raw_limit = one("limit")
    if raw_limit is None:
        limit = 100
    elif (
        not raw_limit.isascii()
        or not raw_limit.isdecimal()
        or not 1 <= int(raw_limit) <= WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE
    ):
        raise ValueError("wallet balance history limit is invalid")
    else:
        limit = int(raw_limit)
    cursor = one("cursor")
    if cursor is not None and (
        not cursor or len(cursor) > WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH
    ):
        raise ValueError("wallet balance history cursor is invalid")
    return limit, cursor, one("network_id"), one("asset_id"), one("address_id")


def wallet_acquisition_id(owner: NoyraHTTPServer, path: str, operation: str) -> str | None:
    prefix = "/api/admin/wallet-acquisitions/"
    if not path.startswith(prefix) or not path.endswith(f"/{operation}"):
        return None
    run_id = path.removeprefix(prefix).removesuffix(f"/{operation}")
    if not run_id or "/" in run_id:
        return None
    return run_id


def wallet_acquisition_statuses(
    owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> tuple[str, ...] | None:
    values = query.get("status")
    if not values:
        return None
    raw = values[0]
    statuses = tuple(value.strip() for value in raw.split(","))
    if not statuses or any(
        not status or status not in ACQUISITION_RUN_STATES for status in statuses
    ):
        raise ValueError("invalid wallet acquisition status filter")
    if len(set(statuses)) != len(statuses):
        raise ValueError("duplicate wallet acquisition status filter")
    return statuses


def enqueue_wallet_acquisition(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    required = {"asset_id", "address_id"}
    optional = {"idempotency_key", "max_attempts", "not_before"}
    if set(payload) - required - optional or not required <= set(payload):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition"},
        )
        return
    asset_id = payload.get("asset_id")
    address_id = payload.get("address_id")
    if not isinstance(asset_id, str) or not isinstance(address_id, str):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition"},
        )
        return
    try:
        record = owner.wallet_acquisitions.enqueue(
            owner.kernel.subject_id,
            asset_id=asset_id,
            address_id=address_id,
            actor=self._actor(),
            idempotency_key=payload.get("idempotency_key"),
            max_attempts=payload.get("max_attempts", 5),
            not_before=payload.get("not_before"),
        )
    except NotFoundError:
        self._json(
            HTTPStatus.NOT_FOUND,
            {"error": "wallet_acquisition_target_not_found"},
        )
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except WalletAcquisitionConflictError:
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "wallet_acquisition_conflict"},
        )
        return
    except ValueError:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition"},
        )
        return
    except (TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition"},
        )
        return
    self._json(HTTPStatus.ACCEPTED, _wallet_acquisition_run_payload(record))


def run_wallet_acquisitions(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    if set(payload) - {"limit"}:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_run"},
        )
        return
    limit = payload.get("limit", 1)
    if isinstance(limit, bool) or not isinstance(limit, int):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_run"},
        )
        return
    try:
        records = owner.wallet_acquisition_runner.run_due(
            owner.kernel.subject_id,
            actor=self._actor(),
            limit=limit,
        )
    except WalletAcquisitionRunError as error:
        self._json(HTTPStatus.CONFLICT, {"error": error.code})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_run"},
        )
        return
    self._json(
        HTTPStatus.OK,
        {
            "processed": len(records),
            "runs": [_wallet_acquisition_run_payload(record) for record in records],
        },
    )


def retry_wallet_acquisition(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    run_id = self._wallet_acquisition_id(self.path, "retry")
    if run_id is None or set(payload) - {"reason", "not_before"}:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_retry"},
        )
        return
    reason = payload.get("reason")
    if not isinstance(reason, str):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_retry"},
        )
        return
    try:
        record = owner.wallet_acquisitions.retry_unknown(
            run_id,
            subject_id=owner.kernel.subject_id,
            actor=self._actor(),
            reason=reason,
            not_before=payload.get("not_before"),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_acquisition_not_found"})
        return
    except WalletAcquisitionConflictError:
        self._json(HTTPStatus.CONFLICT, {"error": "wallet_acquisition_conflict"})
        return
    except WalletAcquisitionRunError as error:
        status = (
            HTTPStatus.BAD_REQUEST
            if error.code.startswith("wallet_acquisition_invalid_")
            else HTTPStatus.CONFLICT
        )
        self._json(status, {"error": error.code})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_retry"},
        )
        return
    self._json(HTTPStatus.OK, _wallet_acquisition_run_payload(record))


def cancel_wallet_acquisition(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    payload = self._wallet_payload()
    if payload is None:
        return
    run_id = self._wallet_acquisition_id(self.path, "cancel")
    if run_id is None or set(payload) != {"reason"}:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_cancel"},
        )
        return
    reason = payload.get("reason")
    if not isinstance(reason, str):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_cancel"},
        )
        return
    try:
        record = owner.wallet_acquisitions.cancel(
            run_id,
            subject_id=owner.kernel.subject_id,
            actor=self._actor(),
            reason=reason,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_acquisition_not_found"})
        return
    except WalletAcquisitionRunError as error:
        self._json(HTTPStatus.CONFLICT, {"error": error.code})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_cancel"},
        )
        return
    self._json(HTTPStatus.OK, _wallet_acquisition_run_payload(record))
