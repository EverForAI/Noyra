"""Post exports HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_export_jobs(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._create_export_job()
    return True
    return False


def admin_export_jobs_cancel(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._cancel_export_job()
    return True
    return False


def config_training_policy(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._update_training_policy()
    return True
    return False
