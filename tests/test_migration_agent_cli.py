from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def _module():
    path = Path(__file__).parents[1] / "scripts" / "noyra-migration-agent.py"
    spec = importlib.util.spec_from_file_location("noyra_migration_agent_cli", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_identity_and_dispatch_keep_secret_out_of_manifest(tmp_path: Path) -> None:
    module = _module()
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes_raw()
    identity = tmp_path / "identity.json"
    identity.write_text(
        json.dumps(
            {
                "target_id": "target-1",
                "key_fingerprint": hashlib.sha256(public).hexdigest(),
                "generation": 1,
                "public_key": base64.urlsafe_b64encode(public).decode(),
                "private_key": base64.urlsafe_b64encode(private.private_bytes_raw()).decode(),
            }
        )
    )
    agent = module.load_agent(identity, tmp_path / "data")
    receipt = module.dispatch(
        agent, "receive", {"artifact_id": "artifact-1", "byte_size": 1, "schema_version": 1}
    )
    assert receipt["status"] == "received"
    report = module.dispatch(agent, "restore", receipt)
    assert report["status"] == "restored"
