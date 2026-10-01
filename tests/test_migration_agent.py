from __future__ import annotations

import pytest

from noyra.migration.agent import MigrationAgent


def test_agent_accepts_bounded_secret_free_manifest() -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    receipt = agent.receive({"artifact_id": "artifact-1", "byte_size": 12, "schema_version": 73})
    assert agent.validate(receipt, expected_digest=receipt.manifest_digest)["status"] == "healthy"


def test_agent_rejects_secret_fields() -> None:
    agent = MigrationAgent(target_id="target-1", key_fingerprint="a" * 64)
    with pytest.raises(ValueError, match="secret"):
        agent.receive({"artifact_id": "artifact-1", "byte_size": 1, "api_key": "secret"})
