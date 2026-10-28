"""Read wallet HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import sqlite3
from http import HTTPStatus
from typing import TYPE_CHECKING
from urllib.parse import SplitResult, parse_qs

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.types import strict_json_loads
from noyra.wallet.admin_projection import WalletAdminProjection, WalletListLimit

from .validation import (
    _wallet_acquisition_attempt_payload,
    _wallet_acquisition_run_payload,
    _wallet_observation_health_breakdown,
)

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_wallet_lists_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult, wallet_list_path: str
) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        list_query = parse_qs(parsed.query, keep_blank_values=True)
        if set(list_query) - {"limit", "status", "network_id", "cursor"} or any(
            len(values) != 1 for values in list_query.values()
        ):
            raise ValueError("invalid wallet list query")
        raw_limit = list_query.get("limit", ["25"])[0]
        if not raw_limit.isascii() or not raw_limit.isdecimal():
            raise ValueError("invalid wallet list limit")
        projection = WalletAdminProjection(owner.kernel.database).page(
            owner.kernel.subject_id,
            wallet_list_path.removeprefix("/api/admin/wallet-lists/"),
            limit=int(raw_limit),
            status=list_query.get("status", [None])[0],
            network_id=list_query.get("network_id", [None])[0],
            cursor=list_query.get("cursor", [None])[0],
        )
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_list_query"})
        return True
    except WalletListLimit:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_list_budget_exceeded"},
            retry_after=60,
        )
        return True
    except (IntegrityError, sqlite3.DatabaseError):
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, projection)
    return True
    return False


def bounties(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    try:
        rows = owner.wallet_economy.list_bounties(
            owner.kernel.subject_id, status="published", limit=limit
        )
        self._json(
            HTTPStatus.OK,
            [
                {
                    "bounty_id": r.bounty_id,
                    "title": r.title,
                    "description": r.description,
                    "acceptance_criteria": list(r.acceptance_criteria),
                    "network_id": r.network_id,
                    "asset_id": r.asset_id,
                    "reward_amount": r.reward_amount,
                    "opens_at": r.opens_at,
                    "expires_at": r.expires_at,
                    "max_submissions": r.max_submissions,
                    "reward_slots": r.reward_slots,
                    "status": r.status,
                }
                for r in rows
            ],
        )
    except Exception:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_economy_unavailable"},
            retry_after=60,
        )
    return False


def bounties_submissions(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
    return False


def bounties_item(self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult) -> bool:
    bounty_id = parsed.path.removeprefix("/api/bounties/")
    if not bounty_id or "/" in bounty_id:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty"})
        return True
    try:
        record = owner.wallet_economy.get_bounty(bounty_id, owner.kernel.subject_id)
        if record.status != "published":
            self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
            return True
        self._json(
            HTTPStatus.OK,
            {
                "bounty_id": record.bounty_id,
                "title": record.title,
                "description": record.description,
                "acceptance_criteria": list(record.acceptance_criteria),
                "network_id": record.network_id,
                "asset_id": record.asset_id,
                "reward_amount": record.reward_amount,
                "opens_at": record.opens_at,
                "expires_at": record.expires_at,
                "max_submissions": record.max_submissions,
                "reward_slots": record.reward_slots,
                "status": record.status,
            },
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
    except Exception:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_economy_unavailable"},
            retry_after=60,
        )
    return False


def admin_wallet_bounties(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        self._json(
            HTTPStatus.OK,
            [
                r.__dict__
                for r in owner.wallet_economy.list_bounties(owner.kernel.subject_id, limit=limit)
            ],
        )
    except Exception:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_economy_unavailable"},
            retry_after=60,
        )
    return False


def admin_wallet_submissions(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    with owner.kernel.database.connection() as c:
        submission_rows = c.execute(
            "SELECT * FROM wallet_bounty_submissions "
            "WHERE subject_id=? ORDER BY created_at DESC LIMIT ?",
            (owner.kernel.subject_id, limit),
        ).fetchall()
    self._json(
        HTTPStatus.OK,
        [
            {
                "submission_id": row["submission_id"],
                "bounty_id": row["bounty_id"],
                "subject_id": row["subject_id"],
                "counterparty": row["counterparty"],
                "content": row["content"],
                "evidence": strict_json_loads(row["evidence_json"]),
                "recipient_address": row["recipient_address"],
                "status": row["status"],
                "decision_reason": row["decision_reason"],
                "created_at": row["created_at"],
                "decided_at": row["decided_at"],
            }
            for row in submission_rows
        ],
    )
    return False


def admin_wallet_orders(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [
            r.__dict__
            for r in owner.wallet_economy.list_orders(owner.kernel.subject_id, limit=limit)
        ],
    )
    return False


def admin_wallet_audits(self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        audit_query = parse_qs(parsed.query, keep_blank_values=True)
        if set(audit_query) - {"limit"} or len(audit_query.get("limit", [])) > 1:
            raise ValueError("invalid wallet audit query")
        raw_limit = audit_query.get("limit", ["100"])[0]
        audit_limit = int(raw_limit)
        audit_rows = owner.wallet_economy.list_audit_history(
            owner.kernel.subject_id, limit=audit_limit
        )
    except (ValueError, TypeError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_audit_query"},
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, audit_rows)
    return False


def admin_wallet_rewards(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [
            r.__dict__
            for r in owner.wallet_rewards.list_workflows(owner.kernel.subject_id, limit=limit)
        ],
    )
    return False


def admin_wallet_reward_incidents(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [r.__dict__ for r in owner.wallet_rewards.incidents(owner.kernel.subject_id, limit=limit)],
    )
    return False


def admin_wallet_executions(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    engine = owner.wallet_execution
    if engine is None:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_unavailable"},
            retry_after=60,
        )
        return True
    try:
        self._json(
            HTTPStatus.OK,
            [
                record.__dict__
                for record in engine.list_executions(owner.kernel.subject_id, limit=limit)
            ],
        )
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_integrity_unavailable"},
            retry_after=60,
        )
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_query"})
    return False


def admin_wallet_executions_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult, limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    engine = owner.wallet_execution
    if engine is None:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_execution_unavailable"},
            retry_after=60,
        )
        return True
    parts = parsed.path.removeprefix("/api/admin/wallet-executions/").split("/")
    if len(parts) == 1 and parts[0]:
        try:
            execution_record = engine.get_execution(parts[0], owner.kernel.subject_id)
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_execution_not_found"})
            return True
        self._json(HTTPStatus.OK, execution_record.__dict__)
        return True
    if len(parts) == 2 and parts[0] and parts[1] == "attempts":
        try:
            attempt_records = engine.list_attempts(parts[0], owner.kernel.subject_id, limit=limit)
        except NotFoundError:
            self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_execution_not_found"})
            return True
        except (ValueError, TypeError):
            self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_query"})
            return True
        self._json(HTTPStatus.OK, [record.__dict__ for record in attempt_records])
        return True
    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_path"})
    return False


def admin_wallet_policy(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        owner.wallet_economy.get_policy(owner.kernel.subject_id).__dict__,
    )
    return False


def admin_wallet_automation(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(HTTPStatus.OK, self._wallet_automation_projection())
    return False


def admin_wallet_ledger(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        [b.__dict__ for b in owner.wallet_economy.ledger_balances(owner.kernel.subject_id)],
    )
    return False


def config_wallet_networks(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        wallet_networks = owner.wallets.list_networks(owner.kernel.subject_id, limit=limit)
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_networks])
    return False


def config_wallet_assets(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        wallet_assets = owner.wallets.list_assets(
            owner.kernel.subject_id,
            network_id=query.get("network_id", [None])[0],
            limit=limit,
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_asset"})
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_assets])
    return False


def config_wallet_addresses(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        wallet_addresses = owner.wallets.list_addresses(
            owner.kernel.subject_id,
            network_id=query.get("network_id", [None])[0],
            limit=limit,
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_address"})
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_addresses])
    return False


def config_wallet_balances(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        wallet_balances = owner.wallets.latest_balances(
            owner.kernel.subject_id,
            network_id=query.get("network_id", [None])[0],
            asset_id=query.get("asset_id", [None])[0],
            address_id=query.get("address_id", [None])[0],
            limit=limit,
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_balance"})
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_balances])
    return False


def config_wallet_observation_health(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        (
            breakdown_group_by,
            breakdown_limit,
            breakdown_network_id,
            breakdown_source,
        ) = self._wallet_observation_health_query(query)
        projection = owner.wallets.observation_health_breakdown(
            owner.kernel.subject_id,
            group_by=breakdown_group_by,
            network_id=breakdown_network_id,
            source=breakdown_source,
            limit=breakdown_limit,
        )
        payload = _wallet_observation_health_breakdown(projection)
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_network_not_found"})
        return True
    except ValueError:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_observation_health_query"},
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, payload)
    return False


def config_wallet_balance_history(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        (
            history_limit,
            history_cursor,
            network_id,
            asset_id,
            address_id,
        ) = self._wallet_balance_history_query(query)
        history_page = owner.wallets.balance_history_page(
            owner.kernel.subject_id,
            network_id=network_id,
            asset_id=asset_id,
            address_id=address_id,
            limit=history_limit,
            cursor=history_cursor,
        )
    except ValueError:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_balance_history_query"},
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(
        HTTPStatus.OK,
        {
            "items": [record.__dict__ for record in history_page.items],
            "next_cursor": history_page.next_cursor,
            "has_more": history_page.has_more,
        },
    )
    return False


def admin_wallet_acquisitions(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        statuses = self._wallet_acquisition_statuses(query)
        wallet_acquisition_records = owner.wallet_acquisitions.list(
            owner.kernel.subject_id,
            limit=limit,
            statuses=statuses,
        )
    except (ValueError, TypeError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_query"},
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(
        HTTPStatus.OK,
        [_wallet_acquisition_run_payload(record) for record in wallet_acquisition_records],
    )
    return False


def admin_wallet_acquisition_budget(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]]
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    network_id = query.get("network_id", [None])[0]
    if network_id is None:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "wallet_acquisition_network_required"},
        )
        return True
    try:
        owner.wallets.get_network(
            network_id,
            subject_id=owner.kernel.subject_id,
        )
        budget = owner.wallet_acquisitions.budget_status(
            owner.kernel.subject_id,
            network_id,
        )
    except NotFoundError:
        self._json(
            HTTPStatus.NOT_FOUND,
            {"error": "wallet_network_not_found"},
        )
        return True
    except (ValueError, TypeError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_wallet_acquisition_network"},
        )
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "wallet_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, budget.__dict__)
    return False


def admin_wallet_acquisitions_item(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    suffix = parsed.path.removeprefix("/api/admin/wallet-acquisitions/")
    parts = suffix.strip("/").split("/") if suffix.strip("/") else []
    if len(parts) == 2 and parts[1] == "attempts" and parts[0]:
        try:
            attempts = owner.wallet_acquisitions.attempts(
                parts[0], subject_id=owner.kernel.subject_id
            )
        except ValueError:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_wallet_acquisition_id"},
            )
            return True
        except NotFoundError:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "wallet_acquisition_not_found"},
            )
            return True
        except IntegrityError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "wallet_integrity_unavailable"},
                retry_after=60,
            )
            return True
        self._json(
            HTTPStatus.OK,
            [_wallet_acquisition_attempt_payload(attempt) for attempt in attempts],
        )
    elif len(parts) == 1 and parts[0]:
        try:
            acquisition_record = owner.wallet_acquisitions.get(
                parts[0], subject_id=owner.kernel.subject_id
            )
        except ValueError:
            self._json(
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_wallet_acquisition_id"},
            )
            return True
        except NotFoundError:
            self._json(
                HTTPStatus.NOT_FOUND,
                {"error": "wallet_acquisition_not_found"},
            )
            return True
        except IntegrityError:
            self._json(
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "wallet_integrity_unavailable"},
                retry_after=60,
            )
            return True
        self._json(HTTPStatus.OK, _wallet_acquisition_run_payload(acquisition_record))
    else:
        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
    return False
