from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic import SecretStr

from noyra.core import Database, IdentityStore
from noyra.core.types import content_hash
from noyra.model import EmbeddingResourceInput, EmbeddingResourceStore


def test_embedding_resource_is_independent_and_secret_free_in_database() -> None:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        database = Database(root / "noyra.sqlite3")
        subject_id = "Noyra-embedding-resource"
        IdentityStore(database).ensure(subject_id, content_hash({"subject": subject_id}))
        store = EmbeddingResourceStore(database, root / "secrets")
        secret = "embedding-secret-that-must-not-enter-sqlite"
        record = store.configure(
            subject_id,
            EmbeddingResourceInput(
                label="semantic-provider",
                base_url="https://embedding.example/v1",
                model="embed-v1",
                api_key=SecretStr(secret),
                dimensions=768,
                daily_call_limit=7,
                daily_token_limit=12_345,
                daily_cost_limit_microusd=54_321,
                input_cost_microusd_per_million=9_876,
                circuit_failure_threshold=4,
                circuit_cooldown_seconds=45,
            ),
            actor="operator",
        )
        assert record.status == "active"
        assert record.daily_call_limit == 7
        settings = store.active_settings(subject_id)
        assert settings is not None
        assert settings.api_key.get_secret_value() == secret
        assert settings.stable_resource_id == record.config_id
        assert settings.budget_limits().daily_tokens == 12_345
        assert settings.budget_limits().daily_cost_microusd == 54_321
        assert settings.pricing().input_microusd_per_million == 9_876
        assert settings.circuit_policy().failure_threshold == 4
        assert settings.circuit_policy().cooldown_seconds == 45
        assert secret.encode() not in (root / "noyra.sqlite3").read_bytes()
        store.disable(
            record.config_id,
            reason="maintenance",
            actor="operator",
            subject_id=subject_id,
        )
        assert store.active_settings(subject_id) is None
        store.revoke(
            record.config_id,
            reason="retired",
            actor="operator",
            subject_id=subject_id,
        )
        assert not (root / "secrets" / f"{record.config_id}.key").exists()
