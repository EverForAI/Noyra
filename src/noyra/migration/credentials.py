"""Protected credential rebinding without copying secret material."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from noyra.core.types import content_hash, new_id

_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_REFERENCE = re.compile(r"(?:systemd|kms|secret):[A-Za-z0-9][A-Za-z0-9_.:/@-]{0,511}\Z")
_FILE_REFERENCE = re.compile(r"file:/[A-Za-z0-9_./@:+,-]{1,510}\Z")


def _valid_reference(value: str) -> bool:
    return _REFERENCE.fullmatch(value) is not None or (
        _FILE_REFERENCE.fullmatch(value) is not None
        and ".." not in value.removeprefix("file:").split("/")
    )


@dataclass(frozen=True)
class CredentialBindingPlan:
    references: dict[str, str]
    fingerprints: dict[str, str]
    binding_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "references": dict(self.references),
            "fingerprints": dict(self.fingerprints),
        }


@dataclass(frozen=True)
class CredentialBindingReceipt:
    binding_id: str
    status: str
    references: dict[str, str]
    fingerprints: dict[str, str]
    target_id: str | None = None
    manifest_digest: str | None = None
    target_identity: str | None = None
    availability_proof: str | None = None

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "status": self.status,
            "references": dict(self.references),
            "fingerprints": dict(self.fingerprints),
            "target_id": self.target_id,
            "manifest_digest": self.manifest_digest,
            "target_identity": self.target_identity,
            "availability_proof": self.availability_proof,
        }


class CredentialRebinder:
    @staticmethod
    def plan(
        source: Mapping[str, Any], target_references: Mapping[str, str]
    ) -> CredentialBindingPlan:
        if not isinstance(source, Mapping) or len(source) > 64:
            raise ValueError("credential source metadata is invalid")
        if any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in source):
            raise ValueError("credential source metadata is invalid")
        if not isinstance(target_references, Mapping) or len(target_references) > 64:
            raise ValueError("target credential references are required")
        references: dict[str, str] = {}
        for key, value in target_references.items():
            if (
                not isinstance(key, str)
                or not _KEY.fullmatch(key)
                or not isinstance(value, str)
                or len(value) > 512
                or not _valid_reference(value.strip())
            ):
                raise ValueError("credential reference is invalid")
            references[key] = value.strip()
        fingerprints: dict[str, str] = {}
        for key, value in source.items():
            try:
                fingerprints[key] = content_hash({"credential": key, "value": value})
            except (TypeError, ValueError, OverflowError) as error:
                raise ValueError("credential source metadata is invalid") from error
        return CredentialBindingPlan(
            references=references,
            fingerprints=fingerprints,
            binding_id=new_id("credential-binding"),
        )

    @staticmethod
    def apply(plan: CredentialBindingPlan) -> CredentialBindingReceipt:
        if not isinstance(plan, CredentialBindingPlan) or not plan.binding_id:
            raise ValueError("credential binding plan is invalid")
        if any(
            _KEY.fullmatch(key) is None or not _valid_reference(value)
            for key, value in plan.references.items()
        ):
            raise ValueError("credential binding plan contains an invalid reference")
        if any(
            _KEY.fullmatch(key) is None or not re.fullmatch(r"[0-9a-f]{64}", value)
            for key, value in plan.fingerprints.items()
        ):
            raise ValueError("credential binding plan contains an invalid fingerprint")
        return CredentialBindingReceipt(
            binding_id=plan.binding_id,
            status="rebind_required",
            references=dict(plan.references),
            fingerprints=dict(plan.fingerprints),
        )

    @staticmethod
    def verify(
        plan: CredentialBindingPlan,
        target: Mapping[str, Any],
        *,
        task_id: str,
        manifest_digest: str,
    ) -> CredentialBindingReceipt:
        """Accept only a target-side availability proof, never a secret value."""
        if not isinstance(target, Mapping) or target.get("status") != "verified":
            raise ValueError("target credential binding proof is required")
        target_id = target.get("target_id")
        identity = target.get("target_identity")
        proof = target.get("availability_proof")
        if (
            not isinstance(target_id, str)
            or not isinstance(identity, str)
            or not isinstance(proof, str)
            or not re.fullmatch(r"[0-9a-f]{64}", proof)
            or not isinstance(task_id, str)
            or not re.fullmatch(r"[0-9a-f]{64}", manifest_digest)
        ):
            raise ValueError("target credential binding proof is invalid")
        expected = content_hash(
            {
                "task_id": task_id,
                "manifest_digest": manifest_digest,
                "target_id": target_id,
                "target_identity": identity,
                "references": plan.references,
                "fingerprints": plan.fingerprints,
            }
        )
        if proof != expected:
            raise ValueError("target credential availability proof does not match")
        return CredentialBindingReceipt(
            binding_id=plan.binding_id,
            status="verified",
            references=dict(plan.references),
            fingerprints=dict(plan.fingerprints),
            target_id=target_id,
            manifest_digest=manifest_digest,
            target_identity=identity,
            availability_proof=proof,
        )
