from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from noyra.cognition import ProjectExecutionError, ProjectWorkspace
from noyra.cognition.execution import (
    ProjectExecutionValidationError,
    _PrototypePublication,
    _validate_prototype_file,
    _windows_workspace_api,
)


def _workspace(tmp_path: Path) -> ProjectWorkspace:
    return ProjectWorkspace(
        tmp_path / "workspace", "subject_00000000000000000000000000000013", quota_bytes=1_000_000
    )


def _missing_tree(workspace: ProjectWorkspace, project_id: str, relative_path: str) -> None:
    with pytest.raises(ProjectExecutionError):
        workspace.files(project_id, relative_path)


def test_manifest_is_published_only_after_the_complete_generation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    project_id = "project-atomic"
    publication = _PrototypePublication(
        workspace,
        project_id,
        "phase-build",
        "execution-atomic",
    )
    original_write = workspace.write
    observed_commit_point: list[bool] = []

    def observed_write(project: str, relative_path: str, payload: bytes) -> Path:
        if relative_path == publication.manifest_path:
            generation = workspace.files(project, publication.generation_root)
            assert generation == {
                f"{publication.generation_root}/app.js": len(b"const answer = 42;"),
                f"{publication.generation_root}/index.html": len(b"<main>Ready</main>"),
            }
            with pytest.raises(ProjectExecutionError):
                workspace.read(project, publication.manifest_path)
            observed_commit_point.append(True)
        return original_write(project, relative_path, payload)

    monkeypatch.setattr(workspace, "write", observed_write)
    with publication:
        publication.write_file("index.html", b"<main>Ready</main>")
        publication.write_file("app.js", b"const answer = 42;")
        evidence, manifest, digest = publication.publish(
            summary="A complete bounded prototype generation.",
            validation_claims=("HTML and JavaScript are syntactically bounded.",),
            validator=_validate_prototype_file,
        )
        publication.commit()

    assert observed_commit_point == [True]
    assert manifest == workspace.path(project_id, publication.manifest_path)
    payload = workspace.read(project_id, publication.manifest_path)
    assert json.loads(payload) == evidence
    assert digest == hashlib.sha256(payload).hexdigest()
    _missing_tree(workspace, project_id, publication.staging_root)


def test_validation_failure_and_mid_publish_failure_leave_no_live_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    project_id = "project-failure"
    invalid = _PrototypePublication(
        workspace,
        project_id,
        "phase-invalid",
        "execution-invalid",
    )
    with pytest.raises(ProjectExecutionValidationError), invalid:
        invalid.write_file("index.html", b"<h1>TODO</h1>")
        invalid.publish(
            summary="Invalid placeholder generation.",
            validation_claims=("Model-only claim.",),
            validator=_validate_prototype_file,
        )

    assert workspace.files(project_id) == {}
    _missing_tree(workspace, project_id, invalid.staging_root)
    _missing_tree(workspace, project_id, invalid.generation_root)

    interrupted = _PrototypePublication(
        workspace,
        project_id,
        "phase-interrupted",
        "execution-interrupted",
    )
    original_write = workspace.write

    def fail_manifest(project: str, relative_path: str, payload: bytes) -> Path:
        if relative_path == interrupted.manifest_path:
            raise OSError("injected manifest publication failure")
        return original_write(project, relative_path, payload)

    monkeypatch.setattr(workspace, "write", fail_manifest)
    with pytest.raises(OSError, match="injected manifest"), interrupted:
        interrupted.write_file("index.html", b"<main>Complete</main>")
        interrupted.publish(
            summary="A generation interrupted at its commit point.",
            validation_claims=("HTML parsed before publication.",),
            validator=_validate_prototype_file,
        )

    assert workspace.files(project_id) == {}
    _missing_tree(workspace, project_id, interrupted.staging_root)
    _missing_tree(workspace, project_id, interrupted.generation_root)


def test_partial_file_write_and_restart_orphans_are_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    project_id = "project-recovery"

    def fail_after_prefix(descriptor: int, payload: bytes) -> None:
        os.write(descriptor, payload[:3])
        raise OSError("injected partial write")

    monkeypatch.setattr(ProjectWorkspace, "_write_descriptor", staticmethod(fail_after_prefix))
    with pytest.raises(OSError, match="partial write"):
        workspace.write(project_id, "notes/result.txt", b"complete-result")
    assert workspace.files(project_id) == {}

    monkeypatch.undo()
    publication = _PrototypePublication(
        workspace,
        project_id,
        "phase-recovery",
        "execution-recovery",
    )
    workspace.write(project_id, f"{publication.staging_root}/partial.txt", b"partial")
    workspace.write(project_id, f"{publication.generation_root}/partial.txt", b"partial")
    workspace.write(project_id, publication.manifest_path, b"{}")

    with publication:
        assert workspace.files(project_id, publication.staging_root) == {}
        _missing_tree(workspace, project_id, publication.generation_root)
        with pytest.raises(ProjectExecutionError):
            workspace.read(project_id, publication.manifest_path)

    assert workspace.files(project_id) == {}


def test_later_generation_cannot_overwrite_prior_evidence(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project_id = "project-history"
    first = _PrototypePublication(
        workspace,
        project_id,
        "phase-history",
        "execution-first",
    )
    with first:
        first.write_file("index.html", b"<main>First generation</main>")
        first_evidence, first_manifest, first_hash = first.publish(
            summary="The first immutable generation.",
            validation_claims=("The first HTML generation parsed.",),
            validator=_validate_prototype_file,
        )
        first.commit()
    first_payload = workspace.read(project_id, first.manifest_path)

    second = _PrototypePublication(
        workspace,
        project_id,
        "phase-history",
        "execution-second",
    )
    with second:
        second.write_file("app.js", b"const generation = 2;")
        second.publish(
            summary="The second immutable generation.",
            validation_claims=("The second JavaScript generation parsed.",),
            validator=_validate_prototype_file,
        )
        second.commit()

    assert workspace.read(project_id, first.manifest_path) == first_payload
    assert hashlib.sha256(first_payload).hexdigest() == first_hash
    assert json.loads(first_payload) == first_evidence
    assert first_manifest != workspace.path(project_id, second.manifest_path)
    assert workspace.files(project_id, first.generation_root) == {
        f"{first.generation_root}/index.html": len(b"<main>First generation</main>")
    }


@pytest.mark.skipif(os.name != "nt", reason="Windows reparse-point contract")
def test_windows_junction_and_directory_replacement_cannot_escape(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = _workspace(tmp_path)
    project_id = "project-windows-race"
    project_root = workspace.project_root(project_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    junction = project_root / "phases"
    subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(junction), str(outside)],
        check=True,
        capture_output=True,
    )
    try:
        with pytest.raises(ProjectExecutionError, match="reparse"):
            workspace.write(project_id, "phases/escaped.txt", b"must-not-escape")
        assert list(outside.iterdir()) == []
        (outside / "secret.txt").write_bytes(b"outside-secret")
        with pytest.raises(ProjectExecutionError, match="reparse"):
            workspace.read(project_id, "phases/secret.txt")
        (outside / "secret.txt").unlink()
    finally:
        os.rmdir(junction)

    workspace.ensure_directory(project_id, "phases/safe")
    safe = project_root / "phases" / "safe"
    displaced = project_root / "phases" / "safe-displaced"
    api = _windows_workspace_api()
    original_open = api.open_relative
    swapped = False

    def racing_open(
        parent: int,
        name: str,
        *,
        directory: bool | None,
        create: bool = False,
        write: bool = False,
        delete: bool = False,
    ) -> int:
        nonlocal swapped
        if not swapped and directory is False and create:
            safe.rename(displaced)
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(safe), str(outside)],
                check=True,
                capture_output=True,
            )
            swapped = True
        return original_open(
            parent,
            name,
            directory=directory,
            create=create,
            write=write,
            delete=delete,
        )

    monkeypatch.setattr(api, "open_relative", racing_open)
    try:
        with pytest.raises(ProjectExecutionError, match="replaced"):
            workspace.write(project_id, "phases/safe/result.txt", b"handle-relative")
        assert list(outside.iterdir()) == []
        assert list(displaced.iterdir()) == []
    finally:
        if safe.exists():
            os.rmdir(safe)
        displaced.rename(safe)


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink contract")
def test_posix_symlink_cannot_escape(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path)
    project_id = "project-posix-race"
    project_root = workspace.project_root(project_id)
    outside = tmp_path / "outside"
    outside.mkdir()
    (project_root / "phases").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ProjectExecutionError):
        workspace.write(project_id, "phases/escaped.txt", b"must-not-escape")
    assert list(outside.iterdir()) == []
    (outside / "secret.txt").write_bytes(b"outside-secret")
    with pytest.raises(ProjectExecutionError):
        workspace.read(project_id, "phases/secret.txt")
