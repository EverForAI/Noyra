from __future__ import annotations

import os
import stat
from pathlib import Path

from noyra.core.actions import ActionLedger
from noyra.core.database import Database
from noyra.core.types import ActionRecord, new_id
from noyra.world import SafeWebReader, SourceRecord

from .store import CapabilityStore
from .types import ToolResult, WebToolResult

_O_NOFOLLOW = int(getattr(os, "O_NOFOLLOW", 0))


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
            self.capabilities.use(
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
            # Read max+1 bytes from the opened file.  A pre-read stat is only
            # advisory and cannot protect against a concurrently growing file.
            data = self._read_bounded_handle(subject_id, target)
            if len(data) > self.max_file_bytes:
                raise ValueError("authorized file exceeds the read limit")
            content = data.decode("utf-8")
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
            self.capabilities.use(
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
        temp_path = file_path.with_name(f".{file_path.name}.{new_id('tmp')}")
        parent_fd: int | None = None
        temp_name = temp_path.name
        try:
            self._revalidate_file_target(subject_id, "filesystem_write", target)
            self._revalidate_file_target(subject_id, "filesystem_write", str(file_path.parent))
            file_path.parent.mkdir(parents=True, exist_ok=True)
            # On POSIX, bind the write to an opened directory identity.  A
            # later rename or symlink replacement of the path cannot redirect
            # the temporary file or atomic replacement outside that directory.
            if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
                parent_flags = os.O_RDONLY | os.O_DIRECTORY
                parent_flags |= _O_NOFOLLOW
                parent_fd = os.open(file_path.parent, parent_flags)
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
            fd = (
                os.open(temp_name, flags, 0o600, dir_fd=parent_fd)
                if parent_fd is not None
                else os.open(temp_path, flags, 0o600)
            )
            with os.fdopen(fd, "wb") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
            self._revalidate_file_target(subject_id, "filesystem_write", target)
            if parent_fd is not None:
                os.replace(temp_name, file_path.name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            else:
                os.replace(temp_path, file_path)
            self._revalidate_file_target(subject_id, "filesystem_write", target)
        except Exception as error:
            try:
                if parent_fd is not None:
                    os.unlink(temp_name, dir_fd=parent_fd)
                else:
                    temp_path.unlink(missing_ok=True)
            except OSError:
                pass
            finished = self.actions.finish(
                action.action_id,
                "unknown",
                {"error": type(error).__name__},
                side_effect_summary="write completion is unknown",
                public_explanation="Authorized file write did not complete cleanly.",
                resource_summary=f"up to {len(encoded)} bytes",
            )
            return ToolResult(finished.action_id, finished.status, None, 0)
        finally:
            if parent_fd is not None:
                os.close(parent_fd)
        finished = self.actions.finish(
            action.action_id,
            "succeeded",
            {"bytes": len(encoded)},
            side_effect_summary="authorized file replaced atomically",
            public_explanation="Wrote an authorized local text file.",
            resource_summary=f"{len(encoded)} bytes",
        )
        return ToolResult(finished.action_id, finished.status, None, len(encoded))

    def _revalidate_file_target(self, subject_id: str, capability_type: str, target: str) -> None:
        """Recheck directory identity immediately before filesystem I/O.

        CapabilityStore performs the same resolved-root check during grant
        selection.  Repeating it at the I/O boundary closes the common
        authorization-then-junction replacement window and rejects symlinked
        paths.  The bounded handle read above additionally closes growth races.
        """
        path = Path(target)
        try:
            resolved = path.resolve(strict=False)
        except (OSError, RuntimeError, ValueError) as error:
            raise PermissionError("authorized filesystem path is unavailable") from error
        if os.path.normcase(str(resolved)) != os.path.normcase(str(path)):
            raise PermissionError("authorized filesystem path changed")
        # Do not call ``allows`` here: it intentionally enforces the grant's
        # rolling rate limit, and the authorization use was already recorded
        # before this I/O recheck.  Recheck only the active grant roots and
        # their integrity, without consuming a second use.
        grants = [
            grant
            for grant in self.capabilities.list(subject_id)
            if grant.capability_type == capability_type
            and grant.status == "active"
            and (capability_type != "filesystem_write" or grant.side_effect)
        ]
        if not any(
            Path(str(grant.scope["root"])).expanduser().resolve() == resolved
            or Path(str(grant.scope["root"])).expanduser().resolve() in resolved.parents
            for grant in grants
        ):
            raise PermissionError("authorized filesystem path changed")

    def _read_bounded_handle(self, subject_id: str, target: str) -> bytes:
        """Open only a no-follow descriptor and bind it to the authorized path."""
        self._revalidate_file_target(subject_id, "filesystem_read", target)
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= _O_NOFOLLOW
        path = Path(target)
        parent_fd: int | None = None
        if os.name != "nt" and hasattr(os, "O_DIRECTORY"):
            parent_flags = os.O_RDONLY | os.O_DIRECTORY
            parent_flags |= _O_NOFOLLOW
            parent_fd = os.open(path.parent, parent_flags)
        fd = (
            os.open(path.name, flags, dir_fd=parent_fd)
            if parent_fd is not None
            else os.open(target, flags)
        )
        try:
            self._revalidate_file_target(subject_id, "filesystem_read", target)
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode):
                raise PermissionError("authorized path is not a regular file")
            current = os.stat(target, follow_symlinks=False)
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise PermissionError("authorized filesystem path changed during open")
            with os.fdopen(fd, "rb") as handle:
                fd = -1
                return handle.read(self.max_file_bytes + 1)
        finally:
            if fd >= 0:
                os.close(fd)
            if parent_fd is not None:
                os.close(parent_fd)

    @staticmethod
    def _terminal_result(action: ActionRecord) -> ToolResult | None:
        status = action.status
        if status not in {"succeeded", "failed", "unknown", "cancelled"}:
            return None
        resource_cost = action.resource_cost
        bytes_written = int(resource_cost.get("bytes", 0)) if status == "succeeded" else 0
        return ToolResult(action.action_id, status, None, bytes_written)
