"""Post admin HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_upgrade(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._start_upgrade()
    return True
    return False


def admin_lifecycle_control(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._lifecycle_control()
    return True
    return False


def admin_actions_reconcile(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._reconcile_action()
    return True
    return False


def admin_retention_run(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._run_retention_batch()
    return True
    return False


def admin_retention_settings(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_retention()
    return True
    return False


def config_public_post_controls(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_public_post_controls()
    return True
    return False


def admin_public_publications_withdraw(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._public_publication()
    return True
    return False


def admin_conversations_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_conversation()
    return True
    return False
