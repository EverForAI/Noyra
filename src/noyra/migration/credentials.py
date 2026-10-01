"""Protected credential rebinding without copying secret material."""

# ruff: noqa: E501

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class CredentialBindingPlan:
    references: dict[str, str]
    fingerprints: dict[str, str]


class CredentialRebinder:
    @staticmethod
    def plan(source: Mapping[str, Any], target_references: Mapping[str, str]) -> CredentialBindingPlan:
        if not isinstance(target_references, Mapping):
            raise ValueError("target credential references are required")
        references = {str(key): str(value) for key, value in target_references.items()}
        if any(not key or not value or len(value) > 512 for key, value in references.items()):
            raise ValueError("credential reference is invalid")
        return CredentialBindingPlan(references=references, fingerprints={})

    @staticmethod
    def apply(plan: CredentialBindingPlan) -> dict[str, Any]:
        return {"status": "rebind_required", "references": dict(plan.references), "fingerprints": dict(plan.fingerprints)}
