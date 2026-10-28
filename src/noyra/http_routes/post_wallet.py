"""Post wallet HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import secrets
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from typing import TYPE_CHECKING

from noyra.core.errors import NotFoundError

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_migration_tasks_wallet_approval(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    payload = self._request_json()
    if payload is None:
        return True
    task_id = self.path.split("/")[-2]
    try:
        task = owner.migration_manager.get_task(task_id)
        policy = owner.migration_store.read_policy(owner.kernel.subject_id)
        if (
            task.subject_id != owner.kernel.subject_id
            or task.status != "approved"
            or not policy.local_wallet_transfer_enabled
            or policy.wallet_mode != "local_wallet_transfer"
            or task.policy_revision != policy.revision
            or owner.wallet_signer is None
            or payload.get("authorize_local_wallet") is not True
        ):
            raise ValueError("local wallet approval unavailable")
        approval = {
            "approval_id": secrets.token_hex(24),
            "task_id": task_id,
            "address": getattr(owner.wallet_signer, "address", None),
            "channel_id": f"recipient-{task.target_id}",
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        }
        with owner.kernel.database.transaction() as connection:
            owner.migration_store._append_audit(
                connection,
                task.subject_id,
                "migration_local_wallet_approved",
                self._actor(),
                {
                    "task_id": task_id,
                    "policy_revision": policy.revision,
                    "approval": approval,
                },
            )
    except (ValueError, NotFoundError):
        self._json(HTTPStatus.CONFLICT, {"error": "migration_wallet_approval_failed"})
        return True
    self._json(HTTPStatus.OK, {"status": "approved", "expires_at": approval["expires_at"]})
    return True
    return False


def admin_wallet_bounties(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._create_wallet_bounty()
    return True
    return False


def admin_wallet_rewards(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._create_wallet_reward()
    return True
    return False


def admin_wallet_rewards_execute_ready(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._execute_wallet_rewards()
    return True
    return False


def admin_wallet_reward_incidents_resolve(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._resolve_wallet_reward_incident()
    return True
    return False


def admin_wallet_reward_submissions_decide(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._decide_wallet_reward_submission()
    return True
    return False


def admin_wallet_rewards_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._wallet_reward_operation()
    return True
    return False


def wallet_rewards_submissions(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._submit_wallet_reward()
    return True
    return False


def admin_wallet_bounties_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._wallet_bounty_operation()
    return True
    return False


def bounties_submissions(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._submit_wallet_bounty()
    return True
    return False


def admin_wallet_submissions_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._wallet_submission_operation()
    return True
    return False


def admin_wallet_executions_recover(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._recover_wallet_executions()
    return True
    return False


def admin_wallet_executions_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._wallet_execution_operation()
    return True
    return False


def admin_wallet_orders_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._wallet_order_operation()
    return True
    return False


def admin_wallet_policy(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._update_wallet_policy()
    return True
    return False


def admin_wallet_automation(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._update_wallet_automation()
    return True
    return False


def admin_wallet_automation_pause_admin_wallet_automation_resume(
    self: RequestHandler, owner: NoyraHTTPServer
) -> bool:
    self._set_wallet_automation_pause(self.path.endswith("/pause"))
    return True
    return False


def config_wallet_networks(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_wallet_network()
    return True
    return False


def config_wallet_assets(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_wallet_asset()
    return True
    return False


def config_wallet_addresses(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_wallet_address()
    return True
    return False


def admin_wallet_acquisitions(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._enqueue_wallet_acquisition()
    return True
    return False


def admin_wallet_acquisitions_run(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._run_wallet_acquisitions()
    return True
    return False


def admin_wallet_acquisitions_retry(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._retry_wallet_acquisition()
    return True
    return False


def admin_wallet_acquisitions_cancel(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._cancel_wallet_acquisition()
    return True
    return False


def config_wallet_networks_revoke(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._revoke_wallet_resource("network")
    return True
    return False


def config_wallet_assets_revoke(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._revoke_wallet_resource("asset")
    return True
    return False


def config_wallet_addresses_revoke(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._revoke_wallet_resource("address")
    return True
    return False
