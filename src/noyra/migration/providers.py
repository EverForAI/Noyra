"""Provider adapter boundary; the default provider only lists registered targets."""

from __future__ import annotations

from dataclasses import dataclass

from .policy import MigrationPolicy


@dataclass(frozen=True)
class TargetCandidate:
    target_id: str
    region: str | None
    provider: str | None
    release_sha: str
    encrypted_volume: bool
    free_bytes: int
    trust_level: int
    status: str = "active"


class RegisteredTargetProvider:
    def __init__(self, targets: list[TargetCandidate]):
        self.targets = tuple(targets)

    def list_candidates(self, policy: MigrationPolicy) -> list[TargetCandidate]:
        if not policy.enabled:
            return []
        return [
            target
            for target in self.targets
            if target.status == "active"
            and (not policy.allowed_target_ids or target.target_id in policy.allowed_target_ids)
        ]

    def provision(self, candidate: TargetCandidate, policy: MigrationPolicy) -> None:
        raise RuntimeError("automatic provider provisioning is disabled; register a target first")
