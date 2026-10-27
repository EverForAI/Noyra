"""Read admin HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import hmac
import ipaddress
import json
from datetime import datetime
from http import HTTPStatus
from typing import TYPE_CHECKING, Any
from urllib.parse import SplitResult, parse_qs

from noyra.core.errors import IntegrityError, NotFoundError
from noyra.core.runtime_observation import ObservationBusy
from noyra.core.upgrade import UpgradeError
from noyra.interaction.conversations import ConversationConflict

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_public_publications(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        params = parse_qs(parsed.query, keep_blank_values=True)
        if not params:
            management_result: Any = owner.public_archive.admin(owner.kernel.subject_id)
        elif (
            set(params) == {"view", "id"}
            and params.get("view") == ["management"]
            and len(params["id"]) == 1
        ):
            management_result = {
                "item": owner.public_archive.admin_item(owner.kernel.subject_id, params["id"][0])
            }
        else:
            if (
                set(params) - {"view", "limit", "cursor"}
                or any(len(value) != 1 for value in params.values())
                or params.get("view") != ["management"]
            ):
                raise ValueError("invalid public management query")
            management_result = owner.public_archive.admin_page(
                owner.kernel.subject_id,
                limit=int(params.get("limit", ["20"])[0]),
                cursor=params.get("cursor", [None])[0],
            )
    except (ValueError, TypeError, UnicodeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_publication_query"})
        return True
    except IntegrityError:
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "public_content_unavailable"})
        return True
    self._json(HTTPStatus.OK, management_result)
    return False


def behavior(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    self._json(
        HTTPStatus.OK,
        owner.projection.behavior_logs(owner.kernel.subject_id, limit=limit),
        public_contract=True,
    )
    return False


def goals(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        owner.projection.goals_view(owner.kernel.subject_id, limit=limit),
    )
    return False


def outcomes(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        owner.projection.outcomes_view(owner.kernel.subject_id, limit=limit),
    )
    return False


def runtime_logs_admin_activity_logs(
    self: RequestHandler,
    owner: NoyraHTTPServer,
    parsed: SplitResult,
    query: dict[str, list[str]],
    limit: int,
) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    if "offset" in query:
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "offset_pagination_unsupported"},
        )
        return True
    try:
        page = owner.projection.runtime_logs(
            owner.kernel.subject_id,
            limit=limit,
            cursor=self._cursor(query),
            category=query.get("category", [None])[0],
            status=query.get("status", [None])[0],
            since=query.get("since", [None])[0],
            until=query.get("until", [None])[0],
            summary_only=parsed.path == "/api/admin/activity-logs",
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
        return True
    self._json(HTTPStatus.OK, page)
    return False


def admin_lifecycle_summary(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(HTTPStatus.OK, owner.projection.lifecycle_summary(owner.kernel.subject_id))
    return False


def admin_runtime_summary(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("operator"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        observation = owner.runtime_observer.snapshot(
            cognition_enabled=owner.cognition_is_enabled(),
            interval_seconds=owner.settings.active_interval_seconds,
        )
    except ObservationBusy:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "runtime_observation_busy"},
        )
        return True
    self._json(HTTPStatus.OK, observation)
    return False


def diagnostics(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(HTTPStatus.OK, owner.diagnostics())
    return False


def admin_upgrade_check_admin_upgrade_status_admin_upgrade_installed(
    self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        payload = (
            owner.upgrade_manager.check_version()
            if parsed.path.endswith("/check")
            else owner.upgrade_manager.installed_version()
            if parsed.path.endswith("/installed")
            else owner.upgrade_manager.status()
        )
    except UpgradeError as error:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {
                "error": error.code,
                "recommendation": owner.upgrade_manager.recommendation(error.code),
            },
        )
        return True
    self._json(HTTPStatus.OK, payload)
    return False


def admin_recoverable_work(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    if owner.operator_controls is None:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "operator_controls_unavailable"},
        )
        return True
    self._json(
        HTTPStatus.OK,
        owner.operator_controls.recoverable_work(limit=limit),
    )
    return False


def admin_lifecycle(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    self._json(
        HTTPStatus.OK,
        {"lifecycle": owner.kernel.lifecycle.current().__dict__},
    )
    return False


def interactions(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    self._json(
        HTTPStatus.OK,
        owner.projection.interactions_view(owner.kernel.subject_id, limit=limit),
        public_contract=True,
    )
    return False


def admin_public_posts(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        status_filter = query.get("status", [None])[0]
        cursor = query.get("cursor", [None])[0]
        posts = [
            record.__dict__
            for record in owner.public_posts.admin(
                owner.kernel.subject_id,
                limit=limit,
                status=status_filter,
                cursor=cursor,
            )
        ]
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_query"})
        return True
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, posts)
    return False


def admin_public_post_controls(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        controls = owner.public_post_controls()
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "public_post_integrity_unavailable"},
            retry_after=60,
        )
        return True
    self._json(HTTPStatus.OK, controls)
    return False


def admin_conversations(
    self: RequestHandler, owner: NoyraHTTPServer, query: dict[str, list[str]], limit: int
) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        page = owner.conversations.list(
            owner.kernel.subject_id,
            archived=query.get("archived", ["false"])[0] == "true",
            limit=min(limit, 100),
            offset=int(query.get("offset", ["0"])[0]),
        )
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_conversation_query"})
        return True
    self._json(HTTPStatus.OK, page)
    return False


def admin_conversations_messages(
    self: RequestHandler,
    owner: NoyraHTTPServer,
    parsed: SplitResult,
    query: dict[str, list[str]],
    limit: int,
) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        page = owner.conversations.messages(
            owner.kernel.subject_id,
            parsed.path.split("/")[4],
            before=query.get("before", [None])[0],
            query=query.get("query", [""])[0],
            status=query.get("status", [""])[0],
            limit=min(limit, 100),
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "conversation_not_found"})
        return True
    except ConversationConflict:
        self._json(HTTPStatus.CONFLICT, {"error": "conversation_conflict"})
        return True
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_conversation_query"})
        return True
    self._json(HTTPStatus.OK, page)
    return False


def mailbox(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    records = owner.interactions.list_channel(
        owner.kernel.subject_id,
        "web",
        limit=limit,
    )
    self._json(
        HTTPStatus.OK,
        [
            {
                "interaction_id": record.interaction_id,
                "direction": record.direction,
                "kind": record.kind,
                "channel": record.channel,
                "counterparty": record.counterparty,
                "content": record.content,
                "related_interaction_id": record.related_interaction_id,
                "status": record.status,
                "created_at": record.created_at,
                "decided_at": record.decided_at,
            }
            for record in records
        ],
    )
    return False


def admin_retention(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    if not self._authorized():
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    retention_policy = owner.retention.policy(owner.kernel.subject_id)
    settings = retention_policy["settings"]
    self._json(
        HTTPStatus.OK,
        {
            "settings": {
                **settings,
                "revision": retention_policy["revision"],
                "source": retention_policy["source"],
            },
            "estimate": owner.retention.estimate(owner.kernel.subject_id),
            "latest": owner.retention.latest(owner.kernel.subject_id),
            "sqlite_space": owner.retention.sqlite_space(),
            "plan": owner.retention.retention_plan(owner.kernel.subject_id),
            "evidence_capacity": owner.retention.evidence_capacity(owner.kernel.subject_id),
        },
    )
    return False


def self_modification(self: RequestHandler, owner: NoyraHTTPServer, limit: int) -> bool:
    if not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    mod_records: list[dict[str, Any]] = []
    with owner.kernel.database.connection() as connection:
        modification_rows = connection.execute(
            "SELECT proposal_id FROM self_modification_proposals "
            "WHERE subject_id = ? ORDER BY updated_at DESC LIMIT ?",
            (owner.kernel.subject_id, limit),
        ).fetchmany(limit)
    mod_records = [
        owner.self_modification.get(str(row["proposal_id"])).__dict__ for row in modification_rows
    ]
    self._json(HTTPStatus.OK, mod_records)
    return False


def monitoring_soak(self: RequestHandler, owner: NoyraHTTPServer, parsed: SplitResult) -> bool:
    from noyra.monitoring import operational_summary

    token = owner.monitoring_token
    supplied = self.headers.get("X-Noyra-Monitor-Token", "")
    try:
        local = ipaddress.ip_address(str(self.client_address[0])).is_loopback
    except ValueError:
        local = False
    local_monitor = (
        local
        and token is not None
        and supplied.isascii()
        and hmac.compare_digest(supplied, token.get_secret_value())
    )
    if not local_monitor and not self._authorized("read"):
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return True
    try:
        if len(parsed.query) > 4096:
            raise ValueError("monitor state too large")
        params = parse_qs(parsed.query, keep_blank_values=True)
        if set(params) - {"state"} or any(len(v) != 1 for v in params.values()):
            raise ValueError("invalid monitor query")
        state = json.loads(params.get("state", ["{}"])[0])
        if not isinstance(state, dict) or set(state) - {"since", "activity_cursors"}:
            raise ValueError("invalid monitor state")
        since = state.get("since")
        if since is not None:
            if not isinstance(since, str) or len(since) > 40:
                raise ValueError("invalid monitoring time")
            parsed_since = datetime.fromisoformat(since.replace("Z", "+00:00"))
            if parsed_since.tzinfo is None:
                raise ValueError("monitoring time needs offset")
        cursors = state.get("activity_cursors")
        if cursors is not None:
            if not isinstance(cursors, dict) or set(cursors) - {"calls", "attempts"}:
                raise ValueError("invalid monitoring cursors")
            for value in cursors.values():
                if not isinstance(value, dict) or set(value) != {"timestamp", "id"}:
                    raise ValueError("invalid monitoring cursor")
                if any(not isinstance(v, str) or len(v) > 128 for v in value.values()):
                    raise ValueError("invalid monitoring cursor value")
                parsed_cursor = datetime.fromisoformat(value["timestamp"].replace("Z", "+00:00"))
                if parsed_cursor.tzinfo is None:
                    raise ValueError("monitoring cursor needs offset")
    except (ValueError, TypeError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_monitoring_query"})
        return True
    result = operational_summary(
        owner.kernel.database.path, owner.kernel.subject_id, since=since, cursors=cursors
    )
    if owner.integrity is not None and owner.integrity.latest_report is not None:
        report = owner.integrity.latest_report
        result["integrity"] = {
            "status": report.status,
            "policy_mode": report.policy_mode,
            "completed_at": report.completed_at,
            "p0": len(report.p0),
            "p1": len(report.p1),
            "check_ids": sorted({finding.check_id for finding in report.findings})[:64],
        }
    self._json(HTTPStatus.OK, result)
    return True
