"""Increment objects use the reviewed encrypted boundary in a distinct namespace."""

from __future__ import annotations

from .workflow_export_files import WorkflowArtifactStore
from .workflow_increment_contract import INCREMENT


class WorkflowIncrementStore(WorkflowArtifactStore):
    publication_pattern = INCREMENT
    storage_directory = "workflow-increments"
