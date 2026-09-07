from __future__ import annotations

import os
from pathlib import Path

from noyra.core.actions import ActionLedger
from noyra.core.database import Database
from noyra.core.types import ActionRecord
from noyra.world import SafeWebReader, SourceRecord

from .filesystem import normalized_root, read_bounded, write_atomic
from .store import CapabilityStore
from .types import ToolResult, WebToolResult


class ToolRunner:
    """Small audited filesystem tool surface; no arbitrary command execution."""

    def __init__(self, database: Database, *, max_file_bytes: int = 2_000_000):
        if not 1_024 <= max_file_bytes <= 100_000_000:
            raise ValueError("tool file limit must be 1KB-100MB")
        self.database = database
        self.actions = ActionLedger(database)
        self.capabilities = CapabilityStore(database)
        self.max_file_bytes = max_file_bytes

    def read_text(
        self,
        subject_id: str,
        path: Path | str,
        *,
        # Deprecated compatibility input; capability grants do not use it.
        approval_id: str | None = None,
        project_id: str | None = None,
        phase_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        target = str(Path(path).expanduser().resolve())
        action = self.actions.prepare(
            subject_id,
            "filesystem_read",
            "authorized_filesystem",
            target,
            {"path": target},
            project_id=project_id,
            phase_id=phase_id,
            side_effect=False,
            idempotency_key=idempotency_key,
        )
        terminal = self._terminal_result(action)
        if terminal is not None:
            return terminal
        try:
            grant = self.capabilities.use(
                subject_id,
                "filesystem_read",
                target,
                side_effect=False,
                approval_id=approval_id,
                action_id=action.action_id,
            )
        except Exception:
            if action.status == "prepared":
                self.actions.cancel(action.action_id, "capability authorization failed")
            raise
        action = self.actions.start(action.action_id)
        try:
            root = self._revalidate_file_target(
                subject_id, "filesystem_read", target, grant.grant_id
            )
            # The authorized directory is held by a no-reparse handle for the
            # complete operation; this closes rename/junction replacement races.
            data = read_bounded(
                Path(target),
                root,
                self.max_file_bytes,
                before_read=lambda: self._revalidate_file_target(
                    subject_id, "filesystem_read", target, grant.grant_id
                ),
            )
            if len(data) > self.max_file_bytes:
                raise ValueError("authorized file exceeds the read limit")
            content = data.decode("utf-8")
            self._revalidate_file_target(subject_id, "filesystem_read", target, grant.grant_id)
        except Exception as error:
            finished = self.actions.finish(
                action.action_id,
                "failed",
                {"error": type(error).__name__},
                public_explanation="Authorized file read failed.",
                resource_summary="no model tokens",
            )
            return ToolResult(finished.action_id, finished.status, None, 0)
        finished = self.actions.finish(
            action.action_id,
            "succeeded",
            {"bytes": len(content.encode("utf-8"))},
            public_explanation="Read an authorized local text file.",
            resource_summary=f"{len(content.encode('utf-8'))} bytes",
        )
        return ToolResult(finished.action_id, finished.status, content, 0)

    async def fetch_public(
        self,
        subject_id: str,
        source: SourceRecord,
        reader: SafeWebReader,
        *,
        # Deprecated compatibility input; capability grants do not use it.
        approval_id: str | None = None,
        project_id: str | None = None,
        phase_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        result = await self.fetch_document(
            subject_id,
            source,
            reader,
            approval_id=approval_id,
            project_id=project_id,
            phase_id=phase_id,
            idempotency_key=idempotency_key,
        )
        return ToolResult(
            result.action_id,
            result.status,
            result.document.content if result.document is not None else None,
            0,
        )

    async def fetch_document(
        self,
        subject_id: str,
        source: SourceRecord,
        reader: SafeWebReader,
        *,
        # Deprecated compatibility input; capability grants do not use it.
        approval_id: str | None = None,
        idempotency_key: str | None = None,
        goal_id: str | None = None,
        project_id: str | None = None,
        phase_id: str | None = None,
        strategy_id: str | None = None,
        expected_outcome: str = "",
        public_goal_reference: str | None = None,
    ) -> WebToolResult:
        action = self.actions.prepare(
            subject_id,
            "web_read",
            "safe_web_reader",
            source.url,
            {"source_id": source.source_id, "url": source.url},
            goal_id=goal_id,
            project_id=project_id,
            phase_id=phase_id,
            strategy_id=strategy_id,
            expected_outcome=expected_outcome,
            side_effect=False,
            idempotency_key=idempotency_key,
        )
        terminal = self._terminal_result(action)
        if terminal is not None:
            return WebToolResult(terminal.action_id, terminal.status, None)
        try:
            self.capabilities.use(
                subject_id,
                "web_read",
                source.url,
                side_effect=False,
                approval_id=approval_id,
                action_id=action.action_id,
            )
        except Exception:
            if action.status == "prepared":
                self.actions.cancel(action.action_id, "capability authorization failed")
            raise
        action = self.actions.start(action.action_id)
        try:
            document = await reader.fetch(source)
        except Exception as error:
            finished = self.actions.finish(
                action.action_id,
                "failed",
                {"error": type(error).__name__},
                public_target=source.url,
                public_goal_reference=public_goal_reference,
                public_explanation="Authorized public source read failed.",
                resource_summary="network read failed",
            )
            return WebToolResult(finished.action_id, finished.status, None)
        byte_count = len(document.content.encode("utf-8"))
        finished = self.actions.finish(
            action.action_id,
            "succeeded",
            {"source_id": source.source_id, "content_hash": document.content_hash},
            public_goal_reference=public_goal_reference,
            public_target=source.url,
            public_explanation="Read an authorized public source.",
            resource_summary=f"{byte_count} extracted bytes",
        )
        return WebToolResult(finished.action_id, finished.status, document)

    def write_text(
        self,
        subject_id: str,
        path: Path | str,
        content: str,
        *,
        # Deprecated compatibility input; capability grants do not use it.
        approval_id: str | None = None,
        project_id: str | None = None,
        phase_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> ToolResult:
        encoded = content.encode("utf-8")
        if len(encoded) > self.max_file_bytes:
            raise ValueError("authorized file exceeds the write limit")
        target = str(Path(path).expanduser().resolve())
        action = self.actions.prepare(
            subject_id,
            "filesystem_write",
            "authorized_filesystem",
            target,
            {"path": target, "content": content},
            side_effect=True,
            project_id=project_id,
            phase_id=phase_id,
            resource_cost={"bytes": len(encoded)},
            idempotency_key=idempotency_key,
        )
        terminal = self._terminal_result(action)
        if terminal is not None:
            return terminal
        try:
            grant = self.capabilities.use(
                subject_id,
                "filesystem_write",
                target,
                side_effect=True,
                approval_id=approval_id,
                action_id=action.action_id,
            )
        except Exception:
            if action.status == "prepared":
                self.actions.cancel(action.action_id, "capability authorization failed")
            raise
        action = self.actions.start(action.action_id)
        file_path = Path(target)
        try:
            root = self._revalidate_file_target(
                subject_id, "filesystem_write", target, grant.grant_id
            )
            self._revalidate_file_target(
                subject_id, "filesystem_write", str(file_path.parent), grant.grant_id
            )
            write_atomic(
                Path(target),
                root,
                encoded,
                before_publish=lambda: self._revalidate_file_target(
                    subject_id, "filesystem_write", target, grant.grant_id
                ),
            )
            self._revalidate_file_target(subject_id, "filesystem_write", target, grant.grant_id)
        except Exception as error:
            finished = self.actions.finish(
                action.action_id,
                "unknown",
                {"error": type(error).__name__},
                side_effect_summary="write completion is unknown",
                public_explanation="Authorized file write did not complete cleanly.",
                resource_summary=f"up to {len(encoded)} bytes",
            )
            return ToolResult(finished.action_id, finished.status, None, 0)
        finished = self.actions.finish(
            action.action_id,
            "succeeded",
            {"bytes": len(encoded)},
            side_effect_summary="authorized file replaced atomically",
            public_explanation="Wrote an authorized local text file.",
            resource_summary=f"{len(encoded)} bytes",
        )
        return ToolResult(finished.action_id, finished.status, None, len(encoded))

    def _revalidate_file_target(
        self, subject_id: str, capability_type: str, target: str, grant_id: str
    ) -> Path:
        """Recheck grants; only handle-relative I/O provides race protection."""
        path = Path(target)
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise PermissionError("authorized filesystem path is unavailable") from error
        if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
            raise PermissionError("authorized filesystem path changed")
        grant = self.capabilities.revalidate_use(
            grant_id,
            subject_id,
            capability_type,
            target,
            side_effect=capability_type == "filesystem_write",
        )
        return normalized_root(str(grant.scope["root"]))

    @staticmethod
    def _terminal_result(action: ActionRecord) -> ToolResult | None:
        status = action.status
        if status not in {"succeeded", "failed", "unknown", "cancelled"}:
            return None
        resource_cost = action.resource_cost
        bytes_written = int(resource_cost.get("bytes", 0)) if status == "succeeded" else 0
        return ToolResult(action.action_id, status, None, bytes_written)
