"""Target-side migration agent primitives with bounded, secret-free manifests."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from noyra.core.types import canonical_json, content_hash


@dataclass(frozen=True)
class EnrollmentReceipt:
    target_id: str
    key_fingerprint: str
    generation: int


@dataclass(frozen=True)
class ReceiveReceipt:
    artifact_id: str
    manifest_digest: str
    byte_size: int
    status: str


class MigrationAgent:
    """Pure protocol boundary; privileged restore is supplied by the runner."""

    def __init__(self, *, target_id: str, key_fingerprint: str, generation: int = 1):
        if (
            not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", target_id)
            or not re.fullmatch(r"[0-9a-f]{64}", key_fingerprint)
            or type(generation) is not int
            or generation < 1
        ):
            raise ValueError("agent identity is invalid")
        self.target_id = target_id
        self.key_fingerprint = key_fingerprint
        self.generation = generation

    def enroll(self) -> EnrollmentReceipt:
        return EnrollmentReceipt(self.target_id, self.key_fingerprint, self.generation)

    def receive(self, manifest: Mapping[str, Any]) -> ReceiveReceipt:
        if not isinstance(manifest, Mapping) or len(manifest) > 32:
            raise ValueError("migration manifest is invalid")
        forbidden = {"secret", "token", "password", "private_key", "api_key", "bearer"}
        if any(
            self._contains_forbidden_key(key, value, forbidden)
            for key, value in manifest.items()
        ):
            raise ValueError("migration manifest contains a forbidden secret field")
        encoded = canonical_json(dict(manifest)).encode()
        if len(encoded) > 1_000_000:
            raise ValueError("migration manifest is too large")
        artifact_id = str(manifest.get("artifact_id", ""))
        byte_size = manifest.get("byte_size")
        if not artifact_id or type(byte_size) is not int or byte_size < 0:
            raise ValueError("migration artifact metadata is invalid")
        return ReceiveReceipt(artifact_id, content_hash(dict(manifest)), byte_size, "received")

    def validate(self, receipt: ReceiveReceipt, *, expected_digest: str) -> dict[str, Any]:
        if receipt.manifest_digest != expected_digest:
            raise ValueError("migration manifest digest mismatch")
        return {"target_id": self.target_id, "generation": self.generation, "status": "healthy"}

    @classmethod
    def _contains_forbidden_key(cls, key: Any, value: Any, forbidden: set[str]) -> bool:
        if not isinstance(key, str):
            return True
        if key.casefold() in forbidden:
            return True
        if isinstance(value, Mapping):
            return any(
                cls._contains_forbidden_key(child_key, child_value, forbidden)
                for child_key, child_value in value.items()
            )
        if isinstance(value, (list, tuple)):
            return any(cls._contains_forbidden_key("", item, forbidden) for item in value)
        return False
