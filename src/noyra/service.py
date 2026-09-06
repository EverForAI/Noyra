from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import multiprocessing
import os
import re
import secrets
import signal
import threading
import time
from collections import OrderedDict, defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, ClassVar, Concatenate, Literal, ParamSpec, TypeVar, cast
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from noyra.autonomy import AutonomyLoop, LoopConfig, TickResult
from noyra.capability import CapabilityGrant, CapabilityStore
from noyra.cognition import (
    CognitionCycle,
    CognitionSettings,
    ControlledSelfModification,
)
from noyra.core.admission import OperationInvalidated, bind_lease, current_lease
from noyra.core.archive import CloudArchiveCoordinator, S3ArchiveProvider, StorageQuota
from noyra.core.at_rest import AtRestConfig, AtRestError, AtRestGuard
from noyra.core.database import Database
from noyra.core.errors import (
    IntegrityError,
    InvalidTransitionError,
    NotFoundError,
    RuntimeOwnershipError,
    TrainingPolicyConflictError,
)
from noyra.core.export_jobs import ExportControl, ExportJobManager, ExportKind
from noyra.core.identity import validate_subject_id
from noyra.core.integrity import (
    IntegrityAuditLimits,
    IntegrityAuditShutdown,
    IntegrityRuntimeController,
)
from noyra.core.locking import ProcessLock
from noyra.core.operator_controls import (
    OperatorControlConflict,
    OperatorControlNotFound,
    OperatorControlService,
)
from noyra.core.redaction import redact_secrets
from noyra.core.runtime import SubjectKernel
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.storage import StorageLayout, TrainingStore
from noyra.core.storage_lifecycle import StorageLifecycleManager
from noyra.core.training_export import TrainingDatasetExporter, TrainingExportLimits
from noyra.core.types import content_hash, strict_json_loads, utc_now
from noyra.interaction import (
    ADAPTERS,
    REPLAY_WINDOW_SECONDS,
    DeliveryDispatcher,
    InboundAuthenticationError,
    InboundChallenge,
    InboundIgnored,
    InboundStore,
    InteractionStore,
    PublicPostCapacityError,
    PublicPostCaptchaError,
    PublicPostConflictError,
    PublicPostInput,
    PublicPostQueueFullError,
    PublicPostRateLimitError,
    PublicPostStore,
    PublicProjection,
    TransportInput,
    TransportStore,
    WeChatInboundAdapter,
)
from noyra.knowledge import (
    CommonKnowledgePeerInput,
    CommonKnowledgeStore,
    CommonKnowledgeSyncError,
)
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceGroupRecord,
    CognitiveResourceGroupUpdate,
    CognitiveResourceStore,
    EmbeddingResourceInput,
    EmbeddingResourceStore,
    EmbeddingSettings,
    ModelGateway,
    ModelLedger,
    ModelRuntimeSettings,
    OpenAICompatibleProvider,
    OpenAICompatibleSettings,
    RoutedModelGateway,
    resource_groups_from_env,
)
from noyra.model.errors import (
    BudgetExhaustedError,
    ModelCallStateError,
    ProviderCallError,
    StructuredOutputError,
)
from noyra.research import SearchProviderInput, SearchProviderStore
from noyra.service_contract import APIRouteContract, route_contracts
from noyra.sleep import FatigueTracker, SleepReflectionPlan, SleepRunRecord
from noyra.wallet import (
    ACQUISITION_RUN_STATES,
    WALLET_ACQUISITION_MAX_BATCH,
    WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH,
    WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE,
    WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
    WALLET_BALANCE_OBSERVATION_MAX_GROUPS,
    WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS,
    BountyInput,
    HTTPSWalletSigner,
    PaymentPolicyInput,
    RewardEvidenceDecisionInput,
    RewardIncidentResolutionInput,
    RewardPublicSubmissionInput,
    RewardWorkflowInput,
    SubmissionInput,
    WalletAcquisitionConflictError,
    WalletAcquisitionRunError,
    WalletAddressInput,
    WalletAssetInput,
    WalletBalanceAcquisitionLedger,
    WalletBalanceAcquisitionRunner,
    WalletEconomyStore,
    WalletExecutionError,
    WalletNetworkInput,
    WalletPaymentExecutionEngine,
    WalletRewardWorkflow,
    WalletSigner,
    WalletStore,
)
from noyra.wallet.types import canonical_timestamp
from noyra.world import SafeWebReader

LOGGER = logging.getLogger("noyra.service")
EX_CONFIG = 78


@dataclass(frozen=True)
class _WalletAutomationConfig:
    """Explicit, bounded defaults for autonomous help-task publication."""

    network_id: str
    asset_id: str
    reward_amount: str
    acceptance_criteria: tuple[str, ...]
    expiry_seconds: int
    max_submissions: int
    reward_slots: int
    auto_publish: bool


def _wallet_automation_from_env() -> _WalletAutomationConfig | None:
    raw_enabled = os.getenv("NOYRA_WALLET_AUTOMATION_ENABLED")
    if raw_enabled is None or raw_enabled.strip().lower() in {"", "0", "false", "no", "off"}:
        return None
    if raw_enabled.strip().lower() not in {"1", "true", "yes", "on"}:
        raise ValueError("NOYRA_WALLET_AUTOMATION_ENABLED must be true or false")

    network_id = os.getenv("NOYRA_WALLET_AUTOMATION_NETWORK_ID", "").strip()
    asset_id = os.getenv("NOYRA_WALLET_AUTOMATION_ASSET_ID", "").strip()
    reward_amount = os.getenv("NOYRA_WALLET_AUTOMATION_REWARD_AMOUNT", "").strip()
    criteria_raw = os.getenv("NOYRA_WALLET_AUTOMATION_ACCEPTANCE_CRITERIA", "")
    if not network_id or not asset_id or not reward_amount or not criteria_raw:
        raise ValueError(
            "wallet automation requires network, asset, reward amount, and acceptance criteria"
        )
    try:
        criteria_value = strict_json_loads(criteria_raw)
    except (TypeError, ValueError) as error:
        raise ValueError(
            "NOYRA_WALLET_AUTOMATION_ACCEPTANCE_CRITERIA must be a JSON array"
        ) from error
    if not isinstance(criteria_value, list) or not all(
        isinstance(item, str) for item in criteria_value
    ):
        raise ValueError("wallet automation acceptance criteria must be a string array")
    try:
        expiry_seconds = int(os.getenv("NOYRA_WALLET_AUTOMATION_EXPIRY_SECONDS", "86400"))
        max_submissions = int(os.getenv("NOYRA_WALLET_AUTOMATION_MAX_SUBMISSIONS", "1"))
        reward_slots = int(os.getenv("NOYRA_WALLET_AUTOMATION_REWARD_SLOTS", "1"))
    except ValueError as error:
        raise ValueError("wallet automation numeric settings are invalid") from error
    auto_publish_raw = os.getenv("NOYRA_WALLET_AUTOMATION_AUTO_PUBLISH", "false").strip().lower()
    if auto_publish_raw not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        raise ValueError("NOYRA_WALLET_AUTOMATION_AUTO_PUBLISH must be true or false")
    auto_publish = auto_publish_raw in {"1", "true", "yes", "on"}
    proposal = RewardWorkflowInput(
        assistance_request_id="automation-placeholder",
        acceptance_criteria=criteria_value,
        network_id=network_id,
        asset_id=asset_id,
        reward_amount=reward_amount,
        opens_at="2000-01-01T00:00:00+00:00",
        expires_at="2000-01-02T00:00:00+00:00",
        max_submissions=max_submissions,
        reward_slots=reward_slots,
        idempotency_key="automation-placeholder",
    )
    del proposal
    if not 300 <= expiry_seconds <= 2_592_000:
        raise ValueError("wallet automation expiry is invalid")
    return _WalletAutomationConfig(
        network_id=network_id,
        asset_id=asset_id,
        reward_amount=reward_amount,
        acceptance_criteria=tuple(criteria_value),
        expiry_seconds=expiry_seconds,
        max_submissions=max_submissions,
        reward_slots=reward_slots,
        auto_publish=auto_publish,
    )


DIAGNOSTICS_MAX_STATUS_BUCKETS = 64
DIAGNOSTICS_MAX_RESOURCE_PRESSURES = 64
DIAGNOSTICS_MAX_PENDING_INTERACTIONS = 50
DIAGNOSTICS_MAX_INTERACTION_CALLS = 20
DIAGNOSTICS_MAX_WAITING_TASKS = 20
# Wallet health is an operator-facing projection.  Keep the accepted shape
# explicit so a future store field (especially an identifier, URL, balance, or
# lease credential) cannot accidentally cross the HTTP boundary.
_WALLET_HEALTH_STATUSES = frozenset({"ok", "attention", "degraded"})
_WALLET_OBSERVATION_SOURCE = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_WALLET_OBSERVATION_FIELDS = frozenset(
    {
        "status",
        "evaluated_at",
        "stale_after_seconds",
        "observed_pairs",
        "fresh_pairs",
        "near_expiry_pairs",
        "stale_pairs",
        "never_pairs",
        "future_pairs",
        "anomalous_pairs",
        "max_age_seconds",
    }
)
_WALLET_BREAKDOWN_FIELDS = frozenset(
    {
        "group_by",
        "evaluated_at",
        "stale_after_seconds",
        "status",
        "groups",
        "total_groups",
        "has_more",
    }
)
_WALLET_BREAKDOWN_GROUP_FIELDS = frozenset(
    {
        "value",
        "active_pairs",
        "observed_pairs",
        "fresh_pairs",
        "near_expiry_pairs",
        "stale_pairs",
        "never_pairs",
        "future_pairs",
        "anomalous_pairs",
        "snapshot_count",
        "latest_observed_at",
        "max_age_seconds",
        "status",
    }
)
_WALLET_BREAKDOWN_COUNT_FIELDS = (
    "active_pairs",
    "observed_pairs",
    "fresh_pairs",
    "near_expiry_pairs",
    "stale_pairs",
    "never_pairs",
    "future_pairs",
    "anomalous_pairs",
    "snapshot_count",
)
_WALLET_OBSERVATION_COUNT_FIELDS = (
    "observed_pairs",
    "fresh_pairs",
    "near_expiry_pairs",
    "stale_pairs",
    "never_pairs",
    "future_pairs",
    "anomalous_pairs",
)
_WALLET_RESOURCE_FIELDS = frozenset({"active", "revoked", "total"})
_WALLET_RESOURCE_NAMES = ("networks", "assets", "addresses")
_WALLET_BALANCE_HISTORY_FIELDS = frozenset(
    {"snapshots", "latest_observed_at", "latest_created_at", "observation_health"}
)
_WALLET_SUMMARY_FIELDS = frozenset({"subject_id", "resources", "balance_history"})
_WALLET_ACQUISITION_FIELDS = frozenset(
    {
        "subject_id",
        "counts",
        "total",
        "active",
        "attention",
        "attempts",
        "expired_running",
        "next_attempt_at",
    }
)
_WALLET_ACQUISITION_FIELDS_WITH_STATUS = _WALLET_ACQUISITION_FIELDS | {"status"}
_WALLET_ACQUISITION_COUNT_FIELDS = frozenset(ACQUISITION_RUN_STATES)
_WALLET_MAX_INT = 9_223_372_036_854_775_807
_MODEL_RESOURCE_EXACT_FIELDS = (
    "priority",
    "weight",
    "daily_attempts",
    "daily_input_tokens",
    "daily_output_tokens",
    "daily_cost_microusd",
    "input_microusd_per_million",
    "output_microusd_per_million",
    "max_attempts",
)


def _cognitive_resource_payload(record: CognitiveResourceGroupRecord) -> dict[str, Any]:
    payload = dict(record.__dict__)
    payload["exact_budget"] = {
        field: str(getattr(record, field)) for field in _MODEL_RESOURCE_EXACT_FIELDS
    }
    return payload


def _wallet_unavailable() -> dict[str, str]:
    """Return the only degraded payload that may cross the wallet boundary."""

    return {"status": "degraded", "reason": "unavailable"}


def _wallet_exact_dict(value: object, fields: frozenset[str], label: str) -> dict[str, Any]:
    """Validate an aggregate mapping without retaining unapproved fields."""

    if type(value) is not dict or set(value) != fields:
        raise ValueError(f"{label} projection is invalid")
    return cast(dict[str, Any], value)


def _wallet_nonnegative_int(value: object, label: str) -> int:
    """Validate bounded integer counters (``bool`` is deliberately rejected)."""

    if type(value) is not int or not 0 <= value <= _WALLET_MAX_INT:
        raise ValueError(f"{label} is invalid")
    return value


def _wallet_timestamp(value: object, label: str, *, nullable: bool = False) -> str | None:
    """Accept only canonical, timezone-aware UTC-millisecond timestamps."""

    if value is None and nullable:
        return None
    if type(value) is not str or not value or len(value) > 64:
        raise ValueError(f"{label} is invalid")
    try:
        canonical = canonical_timestamp(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise ValueError(f"{label} is invalid") from error
    if canonical != value:
        raise ValueError(f"{label} is not canonical")
    return value


def _wallet_resource_counts(value: object, label: str) -> dict[str, int]:
    payload = _wallet_exact_dict(value, _WALLET_RESOURCE_FIELDS, label)
    active = _wallet_nonnegative_int(payload["active"], f"{label}.active")
    revoked = _wallet_nonnegative_int(payload["revoked"], f"{label}.revoked")
    total = _wallet_nonnegative_int(payload["total"], f"{label}.total")
    if total != active + revoked:
        raise ValueError(f"{label}.total is inconsistent")
    return {"active": active, "revoked": revoked, "total": total}


def _wallet_observation_health(value: object) -> dict[str, Any]:
    """Project and validate the complete Stage 4-A-7 observation contract."""

    payload = _wallet_exact_dict(value, _WALLET_OBSERVATION_FIELDS, "observation health")
    status = payload["status"]
    if type(status) is not str or status not in _WALLET_HEALTH_STATUSES:
        raise ValueError("observation health status is invalid")
    evaluated_at = _wallet_timestamp(payload["evaluated_at"], "observation evaluated_at")
    stale_after_seconds = _wallet_nonnegative_int(
        payload["stale_after_seconds"], "observation stale_after_seconds"
    )
    if stale_after_seconds > WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS:
        raise ValueError("observation stale_after_seconds exceeds the configured bound")

    counters = {
        field: _wallet_nonnegative_int(payload[field], f"observation {field}")
        for field in _WALLET_OBSERVATION_COUNT_FIELDS
    }
    if (
        counters["fresh_pairs"]
        + counters["near_expiry_pairs"]
        + counters["stale_pairs"]
        + counters["future_pairs"]
        != counters["observed_pairs"]
    ):
        raise ValueError("observation pair counters are inconsistent")
    if counters["anomalous_pairs"] > counters["observed_pairs"]:
        raise ValueError("observation anomaly count is inconsistent")
    max_age_seconds = payload["max_age_seconds"]
    if max_age_seconds is not None:
        max_age_seconds = _wallet_nonnegative_int(max_age_seconds, "observation max_age_seconds")
    issue_count = (
        counters["near_expiry_pairs"]
        + counters["stale_pairs"]
        + counters["never_pairs"]
        + counters["future_pairs"]
        + counters["anomalous_pairs"]
    )
    # An ``ok`` status must never mask a positive health signal.  We permit an
    # explicit attention/degraded status when counters are currently clear so
    # callers can represent an operator-held state without widening the shape.
    if status == "ok" and issue_count:
        raise ValueError("observation health status masks a health issue")
    return {
        "status": status,
        "evaluated_at": evaluated_at,
        "stale_after_seconds": stale_after_seconds,
        **counters,
        "max_age_seconds": max_age_seconds,
    }


def _wallet_observation_health_breakdown(value: object) -> dict[str, Any]:
    """Validate and redact the bounded network/source health breakdown."""

    payload = _wallet_exact_dict(value, _WALLET_BREAKDOWN_FIELDS, "observation breakdown")
    group_by = payload["group_by"]
    if type(group_by) is not str or group_by not in {"network", "source"}:
        raise ValueError("observation breakdown dimension is invalid")
    evaluated_at = _wallet_timestamp(payload["evaluated_at"], "breakdown evaluated_at")
    stale_after_seconds = _wallet_nonnegative_int(
        payload["stale_after_seconds"], "breakdown stale_after_seconds"
    )
    if stale_after_seconds > WALLET_BALANCE_OBSERVATION_MAX_WINDOW_SECONDS:
        raise ValueError("breakdown stale_after_seconds exceeds the configured bound")
    status = payload["status"]
    if type(status) is not str or status not in _WALLET_HEALTH_STATUSES:
        raise ValueError("observation breakdown status is invalid")
    groups = payload["groups"]
    if type(groups) is not list or len(groups) > WALLET_BALANCE_OBSERVATION_MAX_GROUPS:
        raise ValueError("observation breakdown groups are invalid")
    total_groups = _wallet_nonnegative_int(payload["total_groups"], "breakdown total_groups")
    if total_groups < len(groups) or total_groups > WALLET_BALANCE_OBSERVATION_MAX_GROUPS:
        raise ValueError("observation breakdown total_groups is inconsistent")
    has_more = payload["has_more"]
    if type(has_more) is not bool or has_more != (total_groups > len(groups)):
        raise ValueError("observation breakdown pagination is inconsistent")
    severity = {"ok": 0, "attention": 1, "degraded": 2}
    projected: list[dict[str, Any]] = []
    previous_value: str | None = None
    saw_null = False
    for item in groups:
        group = _wallet_exact_dict(
            item, _WALLET_BREAKDOWN_GROUP_FIELDS, "observation breakdown group"
        )
        value_item = group["value"]
        if group_by == "network":
            if type(value_item) is not str or not value_item:
                raise ValueError("observation breakdown network value is invalid")
        elif value_item is not None and (type(value_item) is not str or not value_item):
            raise ValueError("observation breakdown source value is invalid")
        if value_item is None:
            if saw_null:
                raise ValueError("observation breakdown groups are duplicated")
            saw_null = True
        elif saw_null:
            raise ValueError("observation breakdown null group must be last")
        elif previous_value is not None and value_item <= previous_value:
            raise ValueError("observation breakdown groups are not sorted")
        elif value_item is not None:
            if group_by == "source" and _WALLET_OBSERVATION_SOURCE.fullmatch(value_item) is None:
                raise ValueError("observation breakdown source value is invalid")
            previous_value = value_item
        counters = {
            field: _wallet_nonnegative_int(group[field], f"breakdown {field}")
            for field in _WALLET_BREAKDOWN_COUNT_FIELDS
        }
        if counters["observed_pairs"] + counters["never_pairs"] != counters["active_pairs"]:
            raise ValueError("observation breakdown pair counters are inconsistent")
        if (
            counters["fresh_pairs"]
            + counters["near_expiry_pairs"]
            + counters["stale_pairs"]
            + counters["future_pairs"]
            != counters["observed_pairs"]
        ):
            raise ValueError("observation breakdown health counters are inconsistent")
        if counters["anomalous_pairs"] > counters["observed_pairs"]:
            raise ValueError("observation breakdown anomaly count is inconsistent")
        latest = _wallet_timestamp(
            group["latest_observed_at"], "breakdown latest_observed_at", nullable=True
        )
        max_age = group["max_age_seconds"]
        if max_age is not None:
            max_age = _wallet_nonnegative_int(max_age, "breakdown max_age_seconds")
        if counters["snapshot_count"] == 0 and (latest is not None or max_age is not None):
            raise ValueError("empty observation breakdown group has metadata")
        if counters["snapshot_count"] > 0 and latest is None:
            raise ValueError("non-empty observation breakdown group is missing timestamp")
        group_status = group["status"]
        if type(group_status) is not str or group_status not in _WALLET_HEALTH_STATUSES:
            raise ValueError("observation breakdown group status is invalid")
        issue_count = (
            counters["near_expiry_pairs"]
            + counters["stale_pairs"]
            + counters["never_pairs"]
            + counters["future_pairs"]
            + counters["anomalous_pairs"]
        )
        if group_status == "ok" and issue_count:
            raise ValueError("observation breakdown group status masks a health issue")
        projected.append(
            {
                "value": value_item,
                **counters,
                "latest_observed_at": latest,
                "max_age_seconds": max_age,
                "status": group_status,
            }
        )
    computed_status = max(
        (group["status"] for group in projected), key=severity.__getitem__, default="ok"
    )
    if status == "ok" and computed_status != "ok":
        raise ValueError("observation breakdown status masks a health issue")
    return {
        "group_by": group_by,
        "evaluated_at": evaluated_at,
        "stale_after_seconds": stale_after_seconds,
        "status": status,
        "groups": projected,
        "total_groups": total_groups,
        "has_more": has_more,
    }


def _wallet_acquisition_projection(value: object, subject_id: str) -> dict[str, Any]:
    """Validate the acquisition summary and redact all non-contract fields."""

    if type(value) is not dict:
        raise ValueError("wallet acquisition projection is invalid")
    if value == _wallet_unavailable():
        return _wallet_unavailable()
    keys = set(value)
    has_status = "status" in keys
    expected_fields = (
        _WALLET_ACQUISITION_FIELDS_WITH_STATUS if has_status else _WALLET_ACQUISITION_FIELDS
    )
    if keys != expected_fields:
        raise ValueError("wallet acquisition projection fields are invalid")
    payload = cast(dict[str, Any], value)
    if payload["subject_id"] != subject_id or type(payload["subject_id"]) is not str:
        raise ValueError("wallet acquisition subject is invalid")
    counts_payload = _wallet_exact_dict(
        payload["counts"], _WALLET_ACQUISITION_COUNT_FIELDS, "wallet acquisition counts"
    )
    counts = {
        state: _wallet_nonnegative_int(counts_payload[state], f"wallet acquisition {state}")
        for state in sorted(_WALLET_ACQUISITION_COUNT_FIELDS)
    }
    total = _wallet_nonnegative_int(payload["total"], "wallet acquisition total")
    active = _wallet_nonnegative_int(payload["active"], "wallet acquisition active")
    attention = _wallet_nonnegative_int(payload["attention"], "wallet acquisition attention")
    attempts = _wallet_nonnegative_int(payload["attempts"], "wallet acquisition attempts")
    expired_running = _wallet_nonnegative_int(
        payload["expired_running"], "wallet acquisition expired_running"
    )
    if total != sum(counts.values()):
        raise ValueError("wallet acquisition total is inconsistent")
    if active != counts["queued"] + counts["running"] + counts["retry_wait"]:
        raise ValueError("wallet acquisition active count is inconsistent")
    if attention != counts["unknown"] + counts["failed"]:
        raise ValueError("wallet acquisition attention count is inconsistent")
    if expired_running > counts["running"]:
        raise ValueError("wallet acquisition expired_running count is inconsistent")
    next_attempt_at = _wallet_timestamp(
        payload["next_attempt_at"], "wallet acquisition next_attempt_at", nullable=True
    )
    computed_status = "attention" if attention or expired_running else "ok"
    if has_status:
        status = payload["status"]
        if type(status) is not str or status not in _WALLET_HEALTH_STATUSES:
            raise ValueError("wallet acquisition status is invalid")
        if status == "ok" and computed_status != "ok":
            raise ValueError("wallet acquisition status masks a health issue")
    else:
        status = computed_status
    return {
        "subject_id": subject_id,
        "status": status,
        "counts": counts,
        "total": total,
        "active": active,
        "attention": attention,
        "attempts": attempts,
        "expired_running": expired_running,
        "next_attempt_at": next_attempt_at,
    }


def _wallet_status_summary_projection(value: object, subject_id: str) -> dict[str, Any]:
    """Validate the WalletStore summary and return a safe, fixed-size copy."""

    payload = _wallet_exact_dict(value, _WALLET_SUMMARY_FIELDS, "wallet status")
    if type(payload["subject_id"]) is not str or payload["subject_id"] != subject_id:
        raise ValueError("wallet status subject is invalid")
    resources_payload = _wallet_exact_dict(
        payload["resources"], frozenset(_WALLET_RESOURCE_NAMES), "wallet resources"
    )
    resources = {
        name: _wallet_resource_counts(resources_payload[name], f"wallet resources.{name}")
        for name in _WALLET_RESOURCE_NAMES
    }
    history_payload = _wallet_exact_dict(
        payload["balance_history"], _WALLET_BALANCE_HISTORY_FIELDS, "wallet balance history"
    )
    snapshots = _wallet_nonnegative_int(history_payload["snapshots"], "wallet history snapshots")
    latest_observed_at = _wallet_timestamp(
        history_payload["latest_observed_at"], "wallet latest_observed_at", nullable=True
    )
    latest_created_at = _wallet_timestamp(
        history_payload["latest_created_at"], "wallet latest_created_at", nullable=True
    )
    observation = _wallet_observation_health(history_payload["observation_health"])
    observed_pairs = observation["observed_pairs"]
    if type(observed_pairs) is not int or observed_pairs > snapshots:
        raise ValueError("wallet history observation count is inconsistent")
    if snapshots == 0:
        if latest_observed_at is not None or latest_created_at is not None or observed_pairs != 0:
            raise ValueError("empty wallet history has non-empty observation metadata")
    elif latest_observed_at is None or latest_created_at is None:
        raise ValueError("non-empty wallet history is missing timestamp metadata")
    return {
        "subject_id": subject_id,
        "resources": resources,
        "balance_history": {
            "snapshots": snapshots,
            "latest_observed_at": latest_observed_at,
            "latest_created_at": latest_created_at,
            "observation_health": observation,
        },
    }


def _wallet_acquisition_run_payload(record: Any) -> dict[str, Any]:
    """Return operator-safe acquisition metadata without lease credentials."""
    payload = dict(record.__dict__)
    payload.pop("claim_token", None)
    payload.pop("lease_owner", None)
    return payload


def _wallet_acquisition_attempt_payload(record: Any) -> dict[str, Any]:
    """Return operator-safe attempt metadata without lease credentials."""
    payload = dict(record.__dict__)
    payload.pop("claim_token", None)
    payload.pop("lease_owner", None)
    return payload


_InitParams = ParamSpec("_InitParams")
_InitInstance = TypeVar("_InitInstance")
_InitReturn = TypeVar("_InitReturn")


@dataclass(frozen=True)
class _AdminSession:
    role: str
    actor: str
    csrf_token: str
    expires_at: float


def _release_startup_lock_on_failure(
    function: Callable[Concatenate[_InitInstance, _InitParams], _InitReturn],
) -> Callable[Concatenate[_InitInstance, _InitParams], _InitReturn]:
    """Make constructor failures release the lock synchronously."""

    def wrapped(
        instance: _InitInstance,
        /,
        *args: _InitParams.args,
        **kwargs: _InitParams.kwargs,
    ) -> _InitReturn:
        try:
            return function(instance, *args, **kwargs)
        except BaseException:
            lock = getattr(instance, "_startup_lock", None)
            if lock is not None:
                with suppress(Exception):
                    lock.release()
            raise

    return wrapped


class ServiceSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    data_dir: Path
    subject_id: str = Field(min_length=8, max_length=128)
    genesis_hash: str = Field(min_length=64, max_length=64)
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=8765, ge=0, le=65_535)
    admin_token: SecretStr | None = None
    read_token: SecretStr | None = None
    operator_token: SecretStr | None = None
    export_token: SecretStr | None = None
    break_glass_token: SecretStr | None = None
    allow_insecure_non_loopback: bool = False
    max_http_threads: int = Field(default=32, ge=4, le=512)
    request_rate_limit_per_minute: int = Field(default=120, ge=10, le=100_000)
    trusted_proxy_cidrs: tuple[str, ...] = ()
    public_post_rate_limit_per_hour: int = Field(default=10, ge=1, le=100_000)
    public_post_queue_cap: int = Field(default=1_000, ge=1, le=100_000)
    public_post_captcha_ttl_seconds: int = Field(default=300, ge=30, le=3_600)
    public_post_captcha_max_attempts: int = Field(default=5, ge=1, le=20)
    public_post_captcha_mode: Literal["letters", "digits", "alphanumeric"] = "alphanumeric"
    public_post_storage_cap_bytes: int = Field(default=250_000_000, ge=1_000_000, le=2_000_000_000)
    public_post_captcha_issue_limit_per_hour: int = Field(default=30, ge=1, le=100_000)
    public_post_captcha_global_rate_per_minute: int = Field(default=300, ge=1, le=100_000)
    active_interval_seconds: float = Field(default=30, ge=1, le=86_400)
    sleep_interval_seconds: float = Field(default=60, ge=1, le=86_400)
    deep_sleep_seconds: float = Field(default=21_600, ge=0, le=604_800)
    error_backoff_seconds: float = Field(default=30, ge=1, le=86_400)
    integrity_mode: Literal["off", "alert", "pause"] = "alert"
    integrity_interval_seconds: float = Field(default=300, ge=1, le=604_800)
    integrity_retry_seconds: float = Field(default=60, ge=1, le=86_400)
    integrity_startup_deadline_seconds: float = Field(default=10, ge=0.1, le=300)
    integrity_periodic_deadline_seconds: float = Field(default=30, ge=0.1, le=3_600)
    integrity_max_rows_per_check: int = Field(default=20_000, ge=100, le=1_000_000)
    integrity_max_bytes_per_check: int = Field(default=64_000_000, ge=1_000_000, le=1_000_000_000)
    integrity_max_value_bytes: int = Field(default=16_000_000, ge=100_000, le=256_000_000)
    integrity_max_files_per_check: int = Field(default=50_000, ge=100, le=1_000_000)
    request_timeout_seconds: float = Field(default=10, ge=1, le=300)
    health_cache_ttl_seconds: float = Field(default=5, ge=1, le=300)
    max_request_bytes: int = Field(default=100_000, ge=1_024, le=2_000_000)
    developer_log_export_enabled: bool = True
    subject_storage_quota_bytes: int = Field(default=2_000_000_000, ge=10_000_000)
    training_storage_quota_bytes: int = Field(default=5_000_000_000, ge=10_000_000)
    workspace_storage_quota_bytes: int = Field(default=20_000_000_000, ge=10_000_000)
    minimum_free_storage_bytes: int = Field(default=500_000_000, ge=10_000_000)
    event_payload_retention_days: int = Field(default=90, ge=1, le=3_650)
    at_rest_mode: Literal["development", "required"] = "development"
    volume_encryption_backend: Literal["auto", "attestation"] = "auto"
    volume_attestation_path: Path | None = None
    backup_keyring_path: Path | None = None
    training_record_enabled: bool | None = None
    training_export_enabled: bool | None = None
    training_include_private_psychology: bool | None = None
    training_include_conversations: bool | None = None
    training_include_model_io: bool | None = None
    training_include_external_actions: bool | None = None
    training_include_workspace: bool | None = None
    admin_session_ttl_seconds: int = Field(default=43_200, ge=300, le=604_800)
    admin_session_max_count: int = Field(default=256, ge=8, le=10_000)
    admin_session_cookie_secure: bool = False

    @field_validator("trusted_proxy_cidrs", mode="before")
    @classmethod
    def validate_trusted_proxy_cidrs(cls, value: object) -> tuple[str, ...]:
        if value is None or value == "":
            return ()
        if isinstance(value, str):
            values = tuple(item.strip() for item in value.split(",") if item.strip())
        elif isinstance(value, (tuple, list, set)):
            values = tuple(str(item).strip() for item in value if str(item).strip())
        else:
            raise ValueError("trusted proxy CIDRs must be a comma-separated list")
        normalized: list[str] = []
        for item in values:
            try:
                network = ipaddress.ip_network(item, strict=False)
            except ValueError as error:
                raise ValueError(f"trusted proxy CIDR is invalid: {item}") from error
            normalized.append(str(network))
        return tuple(dict.fromkeys(normalized))

    _TOKEN_PLACEHOLDER_MARKERS: ClassVar[tuple[str, ...]] = (
        "replace-with",
        "change-me",
        "changeme",
        "your-token",
        "example-token",
        "<token",
        "<replace",
    )

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("service host cannot be blank")
        return value

    @field_validator("subject_id")
    @classmethod
    def validate_subject_id(cls, value: str) -> str:
        return validate_subject_id(value)

    @field_validator("genesis_hash")
    @classmethod
    def validate_genesis_hash(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value.lower()):
            raise ValueError("genesis hash must be hexadecimal")
        return value.lower()

    @field_validator(
        "admin_token",
        "read_token",
        "operator_token",
        "export_token",
        "break_glass_token",
    )
    @classmethod
    def validate_admin_token(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < 32:
            raise ValueError("admin token must contain at least 32 characters")
        if value is not None:
            try:
                value.get_secret_value().encode("ascii")
            except UnicodeEncodeError as error:
                raise ValueError("admin token must contain ASCII characters only") from error
        return value

    @model_validator(mode="after")
    def validate_listener_security(self) -> ServiceSettings:
        loopback = {"127.0.0.1", "::1", "localhost"}
        if self.host.casefold() not in loopback and not self.allow_insecure_non_loopback:
            raise ValueError(
                "non-loopback HTTP listener requires NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true"
            )
        if self.integrity_max_value_bytes > self.integrity_max_bytes_per_check:
            raise ValueError("integrity max value bytes cannot exceed the per-check byte budget")
        if self.at_rest_mode == "required" and self.backup_keyring_path is None:
            raise ValueError("required at-rest mode needs NOYRA_BACKUP_KEYRING_PATH")
        if (
            self.at_rest_mode == "required"
            and self.volume_encryption_backend == "attestation"
            and self.volume_attestation_path is None
        ):
            raise ValueError("attested at-rest mode needs NOYRA_VOLUME_ATTESTATION_PATH")
        tokens = {
            name: secret.get_secret_value().strip()
            for name, secret in (
                ("admin_token", self.admin_token),
                ("read_token", self.read_token),
                ("operator_token", self.operator_token),
                ("export_token", self.export_token),
                ("break_glass_token", self.break_glass_token),
            )
            if secret is not None
        }
        for name, value in tokens.items():
            normalized = value.casefold()
            if not value or any(marker in normalized for marker in self._TOKEN_PLACEHOLDER_MARKERS):
                raise ValueError(f"{name} must be a non-placeholder bearer token")
        duplicates = len(tokens) != len(set(tokens.values()))
        if duplicates:
            raise ValueError("role bearer tokens must be distinct")
        if self.host.casefold() not in loopback and not tokens:
            raise ValueError("non-loopback HTTP listener requires at least one bearer token")
        return self

    @field_validator("developer_log_export_enabled", mode="before")
    @classmethod
    def validate_export_flag(cls, value: object) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError("developer log export flag must be true or false")

    @staticmethod
    def _environment_flag(name: str, default: str) -> bool:
        normalized = os.getenv(name, default).strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{name} must be true or false")

    @staticmethod
    def _optional_environment_flag(name: str) -> bool | None:
        value = os.getenv(name)
        if value is None:
            return None
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"{name} must be true or false")

    @classmethod
    def from_env(cls) -> ServiceSettings:
        subject_id = os.getenv("NOYRA_SUBJECT_ID", "Noyra-0001")
        genesis_hash = os.getenv("NOYRA_GENESIS_HASH") or content_hash(
            {"project": "Noyra", "subject_id": subject_id, "origin": "configured-runtime"}
        )
        token = os.getenv("NOYRA_ADMIN_TOKEN")
        read_token = os.getenv("NOYRA_READ_TOKEN")
        operator_token = os.getenv("NOYRA_OPERATOR_TOKEN")
        export_token = os.getenv("NOYRA_EXPORT_TOKEN")
        break_glass_token = os.getenv("NOYRA_BREAK_GLASS_TOKEN")
        return cls(
            data_dir=Path(os.getenv("NOYRA_DATA_DIR", ".runtime/data")),
            subject_id=subject_id,
            genesis_hash=genesis_hash,
            host=os.getenv("NOYRA_HOST", "127.0.0.1"),
            port=int(os.getenv("NOYRA_PORT", "8765")),
            admin_token=SecretStr(token) if token else None,
            read_token=SecretStr(read_token) if read_token else None,
            operator_token=SecretStr(operator_token) if operator_token else None,
            export_token=SecretStr(export_token) if export_token else None,
            break_glass_token=SecretStr(break_glass_token) if break_glass_token else None,
            allow_insecure_non_loopback=cls._environment_flag(
                "NOYRA_ALLOW_INSECURE_NON_LOOPBACK", "false"
            ),
            max_http_threads=int(os.getenv("NOYRA_MAX_HTTP_THREADS", "32")),
            request_rate_limit_per_minute=int(
                os.getenv("NOYRA_REQUEST_RATE_LIMIT_PER_MINUTE", "120")
            ),
            trusted_proxy_cidrs=tuple(
                item.strip()
                for item in os.getenv("NOYRA_TRUSTED_PROXY_CIDRS", "").split(",")
                if item.strip()
            ),
            public_post_rate_limit_per_hour=int(
                os.getenv("NOYRA_PUBLIC_POST_RATE_LIMIT_PER_HOUR", "10")
            ),
            public_post_queue_cap=int(os.getenv("NOYRA_PUBLIC_POST_QUEUE_CAP", "1000")),
            public_post_captcha_ttl_seconds=int(
                os.getenv("NOYRA_PUBLIC_POST_CAPTCHA_TTL_SECONDS", "300")
            ),
            public_post_captcha_max_attempts=int(
                os.getenv("NOYRA_PUBLIC_POST_CAPTCHA_MAX_ATTEMPTS", "5")
            ),
            public_post_captcha_mode=cast(
                Literal["letters", "digits", "alphanumeric"],
                os.getenv("NOYRA_PUBLIC_POST_CAPTCHA_MODE", "alphanumeric").strip().lower(),
            ),
            public_post_storage_cap_bytes=int(
                os.getenv("NOYRA_PUBLIC_POST_STORAGE_CAP_BYTES", "250000000")
            ),
            public_post_captcha_issue_limit_per_hour=int(
                os.getenv("NOYRA_PUBLIC_POST_CAPTCHA_ISSUE_LIMIT_PER_HOUR", "30")
            ),
            public_post_captcha_global_rate_per_minute=int(
                os.getenv("NOYRA_PUBLIC_POST_CAPTCHA_GLOBAL_RATE_PER_MINUTE", "300")
            ),
            active_interval_seconds=float(os.getenv("NOYRA_ACTIVE_INTERVAL_SECONDS", "30")),
            sleep_interval_seconds=float(os.getenv("NOYRA_SLEEP_INTERVAL_SECONDS", "60")),
            deep_sleep_seconds=float(os.getenv("NOYRA_DEEP_SLEEP_SECONDS", "21600")),
            error_backoff_seconds=float(os.getenv("NOYRA_ERROR_BACKOFF_SECONDS", "30")),
            integrity_mode=cast(
                Literal["off", "alert", "pause"],
                os.getenv("NOYRA_INTEGRITY_MODE", "alert").strip().lower(),
            ),
            integrity_interval_seconds=float(os.getenv("NOYRA_INTEGRITY_INTERVAL_SECONDS", "300")),
            integrity_retry_seconds=float(os.getenv("NOYRA_INTEGRITY_RETRY_SECONDS", "60")),
            integrity_startup_deadline_seconds=float(
                os.getenv("NOYRA_INTEGRITY_STARTUP_DEADLINE_SECONDS", "10")
            ),
            integrity_periodic_deadline_seconds=float(
                os.getenv("NOYRA_INTEGRITY_PERIODIC_DEADLINE_SECONDS", "30")
            ),
            integrity_max_rows_per_check=int(
                os.getenv("NOYRA_INTEGRITY_MAX_ROWS_PER_CHECK", "20000")
            ),
            integrity_max_bytes_per_check=int(
                os.getenv("NOYRA_INTEGRITY_MAX_BYTES_PER_CHECK", "64000000")
            ),
            integrity_max_value_bytes=int(os.getenv("NOYRA_INTEGRITY_MAX_VALUE_BYTES", "16000000")),
            integrity_max_files_per_check=int(
                os.getenv("NOYRA_INTEGRITY_MAX_FILES_PER_CHECK", "50000")
            ),
            request_timeout_seconds=float(os.getenv("NOYRA_REQUEST_TIMEOUT_SECONDS", "10")),
            health_cache_ttl_seconds=float(os.getenv("NOYRA_HEALTH_CACHE_TTL_SECONDS", "5")),
            max_request_bytes=int(os.getenv("NOYRA_MAX_REQUEST_BYTES", "100000")),
            developer_log_export_enabled=cls._environment_flag(
                "NOYRA_DEVELOPER_LOG_EXPORT_ENABLED", "true"
            ),
            subject_storage_quota_bytes=int(
                os.getenv("NOYRA_SUBJECT_STORAGE_QUOTA_BYTES", "2000000000")
            ),
            training_storage_quota_bytes=int(
                os.getenv("NOYRA_TRAINING_STORAGE_QUOTA_BYTES", "5000000000")
            ),
            workspace_storage_quota_bytes=int(
                os.getenv("NOYRA_WORKSPACE_STORAGE_QUOTA_BYTES", "20000000000")
            ),
            minimum_free_storage_bytes=int(
                os.getenv("NOYRA_MINIMUM_FREE_STORAGE_BYTES", "500000000")
            ),
            event_payload_retention_days=int(os.getenv("NOYRA_EVENT_PAYLOAD_RETENTION_DAYS", "90")),
            at_rest_mode=cast(
                Literal["development", "required"],
                os.getenv("NOYRA_AT_REST_MODE", "development").strip().lower(),
            ),
            volume_encryption_backend=cast(
                Literal["auto", "attestation"],
                os.getenv("NOYRA_VOLUME_ENCRYPTION_BACKEND", "auto").strip().lower(),
            ),
            volume_attestation_path=(
                Path(value)
                if (value := os.getenv("NOYRA_VOLUME_ATTESTATION_PATH", "").strip())
                else None
            ),
            backup_keyring_path=(
                Path(value)
                if (value := os.getenv("NOYRA_BACKUP_KEYRING_PATH", "").strip())
                else None
            ),
            training_record_enabled=cls._optional_environment_flag("NOYRA_TRAINING_RECORD_ENABLED"),
            training_export_enabled=cls._optional_environment_flag("NOYRA_TRAINING_EXPORT_ENABLED"),
            training_include_private_psychology=cls._optional_environment_flag(
                "NOYRA_TRAINING_INCLUDE_PRIVATE_PSYCHOLOGY"
            ),
            training_include_conversations=cls._optional_environment_flag(
                "NOYRA_TRAINING_INCLUDE_CONVERSATIONS"
            ),
            training_include_model_io=cls._optional_environment_flag(
                "NOYRA_TRAINING_INCLUDE_MODEL_IO"
            ),
            training_include_external_actions=cls._optional_environment_flag(
                "NOYRA_TRAINING_INCLUDE_EXTERNAL_ACTIONS"
            ),
            training_include_workspace=cls._optional_environment_flag(
                "NOYRA_TRAINING_INCLUDE_WORKSPACE"
            ),
            admin_session_ttl_seconds=int(os.getenv("NOYRA_ADMIN_SESSION_TTL_SECONDS", "43200")),
            admin_session_max_count=int(os.getenv("NOYRA_ADMIN_SESSION_MAX_COUNT", "256")),
            admin_session_cookie_secure=cls._environment_flag(
                "NOYRA_ADMIN_SESSION_COOKIE_SECURE", "false"
            ),
        )


class BoundedThreadingHTTPServer(ThreadingHTTPServer):
    """Threading server with a hard connection-worker ceiling."""

    def __init__(
        self,
        server_address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        max_workers: int,
    ):
        self._worker_slots = threading.BoundedSemaphore(max_workers)
        self._handler_condition = threading.Condition()
        self._active_handlers = 0
        self._accepting = True
        super().__init__(server_address, handler)

    @property
    def active_handlers(self) -> int:
        with self._handler_condition:
            return self._active_handlers

    def stop_accepting(self) -> None:
        with self._handler_condition:
            self._accepting = False
            self._handler_condition.notify_all()

    def wait_handlers(self, timeout: float | None = None) -> bool:
        with self._handler_condition:
            if timeout is None:
                while self._active_handlers:
                    self._handler_condition.wait()
                return True
            deadline = time.monotonic() + max(0.0, timeout)
            while self._active_handlers:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._handler_condition.wait(remaining)
            return True

    def process_request(self, request: Any, client_address: Any) -> None:
        with self._handler_condition:
            if not self._accepting:
                with suppress(OSError):
                    request.sendall(
                        b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                        b"Content-Length: 0\r\n\r\n"
                    )
                self.shutdown_request(request)
                return
        if not self._worker_slots.acquire(blocking=False):
            with suppress(OSError):
                request.sendall(
                    b"HTTP/1.1 503 Service Unavailable\r\nConnection: close\r\n"
                    b"Content-Length: 0\r\n\r\n"
                )
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self._worker_slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        with self._handler_condition:
            self._active_handlers += 1
        try:
            super().process_request_thread(request, client_address)
        finally:
            with self._handler_condition:
                self._active_handlers -= 1
                self._handler_condition.notify_all()
            self._worker_slots.release()


class NoyraHTTPServer:
    def __init__(
        self,
        kernel: SubjectKernel,
        settings: ServiceSettings,
        *,
        allow_common_knowledge_key_creation: bool = True,
        repair_secrets_on_init: bool = True,
        wallet_signer: WalletSigner | None = None,
        close_wallet_signer: bool = False,
    ):
        self.kernel = kernel
        self.settings = settings
        self.admission = kernel.admission
        self.quarantine_checker: Any = None
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self._async_handler_condition = threading.Condition()
        self._async_handler_operations: dict[ConcurrentFuture[Any], threading.Event] = {}
        self.integrity: IntegrityRuntimeController | None = None
        self.at_rest: AtRestGuard | None = None
        self.operator_controls: OperatorControlService | None = None
        self.projection = PublicProjection(kernel.database)
        self.capabilities = CapabilityStore(kernel.database)
        self.common_knowledge = CommonKnowledgeStore(
            kernel.database,
            kernel.subject_id,
            settings.data_dir / "secrets" / "common-knowledge",
            allow_key_creation=allow_common_knowledge_key_creation,
        )
        self.cloud_archive_status: dict[str, Any] = {
            "configured": False,
            "ready": False,
            "profile": None,
        }
        self.cloud_archive_provider: Any = None
        self.interactions = InteractionStore(kernel.database)
        self.inbound = InboundStore(kernel.database, self.interactions)
        self.public_posts = PublicPostStore(
            kernel.database,
            rate_limit_per_hour=settings.public_post_rate_limit_per_hour,
            queue_cap=settings.public_post_queue_cap,
            storage_cap_bytes=settings.public_post_storage_cap_bytes,
            minimum_free_bytes=settings.minimum_free_storage_bytes,
            captcha_issue_limit_per_hour=settings.public_post_captcha_issue_limit_per_hour,
            captcha_global_rate_per_minute=(settings.public_post_captcha_global_rate_per_minute),
        )
        self.transports = TransportStore(
            kernel.database,
            settings.data_dir / "secrets" / "transports",
            repair_on_init=repair_secrets_on_init,
        )
        self.deliveries = DeliveryDispatcher(kernel.database, self.transports)
        self.search_providers = SearchProviderStore(
            kernel.database,
            settings.data_dir / "secrets" / "search",
            repair_on_init=repair_secrets_on_init,
        )
        self.wallets = WalletStore(kernel.database)
        self.wallet_economy = WalletEconomyStore(kernel.database)
        self.wallet_acquisitions = WalletBalanceAcquisitionLedger(kernel.database)
        self.wallet_acquisition_runner = WalletBalanceAcquisitionRunner(self.wallet_acquisitions)
        # Signing is opt-in: production deployments inject an isolated signer;
        # the HTTP server remains fail-closed when none is configured.
        self.wallet_execution = (
            None
            if wallet_signer is None
            else WalletPaymentExecutionEngine(
                kernel.database, wallet_signer, economy=self.wallet_economy
            )
        )
        self.wallet_signer = wallet_signer
        self._close_wallet_signer = close_wallet_signer
        self.wallet_rewards = WalletRewardWorkflow(
            kernel.database,
            economy=self.wallet_economy,
            posts=self.public_posts,
            execution=self.wallet_execution,
        )
        self.cognitive_resources = CognitiveResourceStore(
            kernel.database,
            settings.data_dir / "secrets" / "models",
            repair_on_init=repair_secrets_on_init,
        )
        self.embedding_resources = EmbeddingResourceStore(
            kernel.database,
            settings.data_dir / "secrets" / "embedding",
            repair_on_init=repair_secrets_on_init,
        )
        self.cognition_gateway: RoutedModelGateway | None = None
        self._pending_cognitive_resource_proposals: tuple[CognitiveResourceGroupInput, ...] = ()
        self.runtime_exporter = RuntimeLogExporter(kernel.database)
        self.training = TrainingStore(kernel.database)
        export_budget = max(1_000_000, settings.subject_storage_quota_bytes // 2)
        shard_budget = min(25_000_000, max(2_000_000, export_budget // 8))
        self.training_exporter = TrainingDatasetExporter(
            kernel.database,
            workspace_root=settings.data_dir / "workspace",
            data_root=settings.data_dir,
            work_root=settings.data_dir / "exports" / "work",
            limits=TrainingExportLimits(
                max_work_bytes=min(750_000_000, export_budget),
                max_archive_bytes=min(1_000_000_000, export_budget),
                max_compatibility_bytes=min(200_000_000, export_budget),
                subject_quota_bytes=settings.subject_storage_quota_bytes,
                minimum_free_bytes=settings.minimum_free_storage_bytes,
                shard_bytes=shard_budget,
            ),
        )
        self.export_jobs = ExportJobManager(
            kernel.database,
            settings.data_dir / "exports" / "jobs",
            self._run_export,
        )
        # Export workers are started only after the startup integrity gate.  A
        # service constructor may already hold the process lock, but that does
        # not mean recovery and integrity admission have completed.
        self.self_modification = ControlledSelfModification(
            kernel.database, kernel.subject_id, CognitionSettings()
        )
        handler = self._handler_type()
        self._rate_lock = threading.Lock()
        self._request_times: dict[str, deque[float]] = defaultdict(deque)
        self._health_cache_lock = threading.Lock()
        self._health_cache_at = 0.0
        self._health_cache: tuple[HTTPStatus, dict[str, Any]] | None = None
        self._session_lock = threading.RLock()
        self._admin_sessions: OrderedDict[str, _AdminSession] = OrderedDict()
        self.server = BoundedThreadingHTTPServer(
            (settings.host, settings.port), handler, settings.max_http_threads
        )
        self.server.daemon_threads = False
        self.thread: threading.Thread | None = None
        self._closed = False
        self.defer_export_ownership = False

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.server.server_address[:2]
        return str(host), int(port)

    def _cached_health(self) -> tuple[HTTPStatus, dict[str, Any]] | None:
        """Return the bounded readiness projection while its TTL is valid."""
        with self._health_cache_lock:
            cached = self._health_cache
            if cached is not None and time.monotonic() - self._health_cache_at < (
                self.settings.health_cache_ttl_seconds
            ):
                return cached
        return None

    def _store_health(self, status: HTTPStatus, payload: dict[str, Any]) -> None:
        with self._health_cache_lock:
            self._health_cache_at = time.monotonic()
            self._health_cache = (status, payload)

    @staticmethod
    def api_route_contracts() -> tuple[APIRouteContract, ...]:
        """Return the normalized versioned route inventory used by contract gates."""
        return route_contracts()

    def start(self) -> None:
        if self._closed:
            raise RuntimeError("HTTP server is closed")
        if self.thread is not None:
            return
        if not self.kernel.process_lock.held:
            raise RuntimeError("HTTP server cannot start without subject ownership")
        if not self.defer_export_ownership:
            self._start_export_ownership()
        self.thread = threading.Thread(
            target=self.server.serve_forever,
            name="noyra-http",
            daemon=True,
        )
        self.thread.start()

    def close(self) -> None:
        if self._closed:
            return
        if self.thread is not None:
            self.server.stop_accepting()
            self.server.shutdown()
            self.thread.join()
            self.server.wait_handlers()
            self.thread = None
        else:
            self.server.wait_handlers()
        self.wait_async_handlers()
        self.server.server_close()
        self.wallet_acquisition_runner.close()
        self.export_jobs.close()
        if self._close_wallet_signer and isinstance(self.wallet_signer, HTTPSWalletSigner):
            self.wallet_signer.close()
        self._closed = True

    def begin_drain(self) -> None:
        self.server.stop_accepting()
        self.admission.begin_drain()

    def _register_async_handler(self, future: ConcurrentFuture[Any], done: threading.Event) -> None:
        with self._async_handler_condition:
            self._async_handler_operations[future] = done

        def complete(_future: ConcurrentFuture[Any]) -> None:
            with self._async_handler_condition:
                self._async_handler_operations.pop(_future, None)
                self._async_handler_condition.notify_all()

        future.add_done_callback(complete)

    def wait_async_handlers(self, timeout: float | None = None) -> bool:
        """Wait until every handler-submitted main-loop coroutine has exited."""
        with self._async_handler_condition:
            if timeout is None:
                while self._async_handler_operations:
                    self._async_handler_condition.wait()
                return True
            deadline = time.monotonic() + max(0.0, timeout)
            while self._async_handler_operations:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._async_handler_condition.wait(remaining)
            return True

    def run_async_from_handler(self, coroutine: Any) -> Any:
        loop = self.event_loop
        if loop is not None and loop.is_running():
            bridge: ConcurrentFuture[Any] = ConcurrentFuture()
            done = threading.Event()
            self._register_async_handler(bridge, done)
            task_holder: dict[str, asyncio.Task[Any]] = {}

            def spawn() -> None:
                try:
                    task = loop.create_task(coroutine)
                except BaseException as error:
                    if not bridge.done():
                        bridge.set_exception(error)
                    done.set()
                    return
                task_holder["task"] = task

                def finish(completed: asyncio.Task[Any]) -> None:
                    try:
                        if completed.cancelled():
                            if not bridge.done():
                                bridge.cancel()
                        else:
                            error = completed.exception()
                            if error is not None:
                                if not bridge.done():
                                    bridge.set_exception(error)
                            elif not bridge.done():
                                bridge.set_result(completed.result())
                    except BaseException as error:
                        if not bridge.done():
                            bridge.set_exception(error)
                    finally:
                        done.set()

                task.add_done_callback(finish)

            try:
                loop.call_soon_threadsafe(spawn)
            except RuntimeError:
                with suppress(Exception):
                    coroutine.close()
                with self._async_handler_condition:
                    self._async_handler_operations.pop(bridge, None)
                    self._async_handler_condition.notify_all()
                raise

            try:
                return bridge.result(timeout=self.settings.request_timeout_seconds)
            except FutureTimeoutError:
                # Cancel the actual asyncio task, not the bridge future.  A
                # concurrent Future is marked cancelled immediately, which
                # would let shutdown race past the still-running coroutine.
                def cancel_task() -> None:
                    task = task_holder.get("task")
                    if task is not None and not task.done():
                        task.cancel()

                with suppress(RuntimeError):
                    loop.call_soon_threadsafe(cancel_task)
                # Keep this handler registered until the task's done callback
                # fires.  If cancellation is ignored, retaining ownership is
                # intentional: shutdown must not release the subject lock.
                done.wait()
                raise
        return asyncio.run(coroutine)

    def _quarantined(self) -> bool:
        checker = self.quarantine_checker
        if checker is None:
            return self.admission.quarantined
        try:
            return bool(checker())
        except Exception:
            return True

    @staticmethod
    def _is_recovery_mutation(path: str) -> bool:
        """Identify the narrow set of writes allowed during quarantine."""
        return (
            (path.startswith("/api/admin/actions/") and path.endswith("/reconcile"))
            or (path.startswith("/api/admin/model-calls/") and path.endswith("/reconcile"))
            or (
                path.startswith("/api/deliveries/")
                and (path.endswith("/reconcile") or path.endswith("/lookup"))
            )
            or (
                path.startswith("/api/admin/wallet-acquisitions/")
                and (path.endswith("/retry") or path.endswith("/cancel"))
            )
            or path == "/api/admin/wallet-executions/recover"
        )

    def allow_mutation(self, path: str) -> bool:
        if self._closed or self.admission.closed:
            return False
        if self._quarantined():
            return self._is_recovery_mutation(path)
        if self.admission.accepting:
            return True
        # Manual pause closes cognition admission, but lifecycle controls must
        # remain available so an operator can resume or reset the runtime.
        return path in {
            "/api/admin/lifecycle/pause",
            "/api/admin/lifecycle/resume",
            "/api/admin/lifecycle/reset",
        } or self._is_recovery_mutation(path)

    def _run_export(
        self,
        subject_id: str,
        export_kind: str,
        target: Path,
        control: ExportControl | None = None,
    ) -> tuple[str, str]:
        if export_kind == "runtime":
            runtime_artifact = self.runtime_exporter.export_to_path(
                subject_id,
                actor="web-operator",
                target=target,
                control=control,
            )
            return runtime_artifact.filename, runtime_artifact.sha256
        if export_kind == "training":
            training_artifact = self.training_exporter.export_to_path(
                subject_id,
                actor="web-operator",
                target=target,
                control=control,
            )
            return training_artifact.filename, training_artifact.sha256
        raise ValueError("export kind is invalid")

    def _start_export_ownership(self) -> None:
        self.training_exporter.start_after_ownership()
        self.export_jobs.start_after_ownership(self.kernel.subject_id)

    def allow_request(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        try:
            address = ipaddress.ip_address(client_ip)
            if isinstance(address, ipaddress.IPv6Address):
                address = ipaddress.ip_network(f"{address}/64", strict=False).network_address
            bucket_key = str(address)
        except ValueError:
            bucket_key = client_ip[:128]
        with self._rate_lock:
            if bucket_key not in self._request_times and len(self._request_times) >= 10_000:
                self._request_times = defaultdict(
                    deque,
                    {
                        key: values
                        for key, values in self._request_times.items()
                        if values and values[-1] > cutoff
                    },
                )
                if len(self._request_times) >= 10_000:
                    bucket_key = "__overflow__"
            bucket = self._request_times[bucket_key]
            while bucket and bucket[0] <= cutoff:
                bucket.popleft()
            if len(bucket) >= self.settings.request_rate_limit_per_minute:
                return False
            bucket.append(now)
            return True

    def client_ip(self, peer: str, forwarded_for: str | None = None) -> str:
        """Resolve a rate-limit identity without trusting spoofable headers.

        X-Forwarded-For is considered only when the direct peer belongs to an
        explicitly configured trusted proxy network.  Walk the chain from the
        direct peer backwards and select the first address outside the trusted
        networks; this ignores attacker-supplied entries placed before a
        trusted proxy's appended chain.  Without that configuration, the
        socket peer remains authoritative.
        """
        try:
            peer_address = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        trusted = any(
            peer_address in ipaddress.ip_network(cidr, strict=False)
            for cidr in self.settings.trusted_proxy_cidrs
        )
        if not trusted or not forwarded_for:
            return str(peer_address)
        if len(forwarded_for) > 4_096:
            return str(peer_address)
        parts = forwarded_for.split(",")
        if len(parts) > 32:
            return str(peer_address)
        parsed_chain: list[Any] = []
        for candidate in (part.strip() for part in parts):
            try:
                address = ipaddress.ip_address(candidate)
                if getattr(address, "scope_id", None) is not None:
                    return str(peer_address)
                parsed_chain.append(address)
            except ValueError:
                return str(peer_address)
        for candidate in reversed(parsed_chain):
            if not any(
                candidate in ipaddress.ip_network(cidr, strict=False)
                for cidr in self.settings.trusted_proxy_cidrs
            ):
                return str(candidate)
        return str(peer_address)

    def create_admin_session(self, *, role: str, actor: str) -> tuple[str, _AdminSession]:
        if role not in {"operator", "admin", "break_glass"} or not actor.strip():
            raise ValueError("invalid admin session identity")
        now = time.time()
        session = _AdminSession(
            role=role,
            actor=actor,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=now + self.settings.admin_session_ttl_seconds,
        )
        session_id = secrets.token_urlsafe(32)
        with self._session_lock:
            self._purge_sessions_locked(now)
            while len(self._admin_sessions) >= self.settings.admin_session_max_count:
                self._admin_sessions.popitem(last=False)
            self._admin_sessions[session_id] = session
        return session_id, session

    def admin_session(self, session_id: str) -> _AdminSession | None:
        if not session_id:
            return None
        now = time.time()
        with self._session_lock:
            self._purge_sessions_locked(now)
            session = self._admin_sessions.get(session_id)
            return session if session is not None and session.expires_at > now else None

    def revoke_admin_session(self, session_id: str) -> None:
        with self._session_lock:
            self._admin_sessions.pop(session_id, None)

    def audit_admin_event(self, action: str, actor: str, payload: dict[str, Any]) -> None:
        """Append a redacted authentication/management audit event."""
        safe = redact_secrets(
            {key: value for key, value in payload.items() if key not in {"token", "csrf_token"}}
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "INSERT INTO audit_records("
                "audit_id, subject_id, action, actor, payload_json, occurred_at"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    f"audit_{secrets.token_hex(16)}",
                    self.kernel.subject_id,
                    action,
                    actor,
                    json.dumps(safe, sort_keys=True),
                    utc_now(),
                ),
            )

    def _purge_sessions_locked(self, now: float) -> None:
        for session_id, session in tuple(self._admin_sessions.items()):
            if session.expires_at <= now:
                self._admin_sessions.pop(session_id, None)

    def diagnostics(self) -> dict[str, Any]:
        """Return bounded operational diagnostics for the read-only panel."""
        delivery_counts: dict[str, int] = {}
        inbound_counts: dict[str, int] = {}
        capability_counts: dict[str, int] = {}
        knowledge_counts: dict[str, int] = {}
        pressure_rows: list[dict[str, Any]] = []
        pending_interaction_rows: list[dict[str, Any]] = []
        interaction_call_rows: list[dict[str, Any]] = []
        interaction_wait_rows: list[dict[str, Any]] = []
        with self.kernel.database.connection() as connection:
            loop = connection.execute(
                "SELECT circuit_status, consecutive_failures, next_retry_at, "
                "last_successful_tick_at, last_error_type FROM autonomy_loop_state "
                "WHERE subject_id = ?",
                (self.kernel.subject_id,),
            ).fetchone()
            unknown_actions = connection.execute(
                "SELECT COUNT(*) FROM actions WHERE subject_id = ? AND status = 'unknown'",
                (self.kernel.subject_id,),
            ).fetchone()[0]
            unknown_calls = connection.execute(
                "SELECT COUNT(*) FROM model_calls WHERE subject_id = ? AND status = 'unknown'",
                (self.kernel.subject_id,),
            ).fetchone()[0]
            for row in connection.execute(
                "SELECT COALESCE(r.outcome, d.status) AS status, COUNT(*) AS count "
                "FROM interaction_deliveries d "
                "LEFT JOIN interaction_delivery_reconciliations r ON r.delivery_id = d.delivery_id "
                "AND r.outcome IN ('delivered', 'failed', 'cancelled') "
                "WHERE d.subject_id = ? GROUP BY COALESCE(r.outcome, d.status)",
                (self.kernel.subject_id,),
            ):
                if len(delivery_counts) >= DIAGNOSTICS_MAX_STATUS_BUCKETS:
                    break
                delivery_counts[str(row["status"])] = int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM interaction_inbound_events "
                "WHERE subject_id = ? GROUP BY status",
                (self.kernel.subject_id,),
            ):
                if len(inbound_counts) >= DIAGNOSTICS_MAX_STATUS_BUCKETS:
                    break
                inbound_counts[str(row["status"])] = int(row["count"])
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM capability_grants "
                "WHERE subject_id = ? GROUP BY status",
                (self.kernel.subject_id,),
            ):
                if len(capability_counts) >= DIAGNOSTICS_MAX_STATUS_BUCKETS:
                    break
                capability_counts[str(row["status"])] = int(row["count"])
            for row in connection.execute(
                "SELECT pool, pressure, updated_at FROM resource_pool_pressures "
                "WHERE subject_id = ? ORDER BY pool LIMIT ?",
                (self.kernel.subject_id, DIAGNOSTICS_MAX_RESOURCE_PRESSURES),
            ):
                pressure_rows.append(dict(row))
            for row in connection.execute(
                "SELECT status, COUNT(*) AS count FROM common_knowledge_imports "
                "WHERE subject_id = ? GROUP BY status",
                (self.kernel.subject_id,),
            ):
                if len(knowledge_counts) >= DIAGNOSTICS_MAX_STATUS_BUCKETS:
                    break
                knowledge_counts[str(row["status"])] = int(row["count"])
            storage = connection.execute(
                "SELECT payload_json FROM events WHERE subject_id = ? "
                "AND event_type = 'storage_maintenance' ORDER BY occurred_at DESC LIMIT 1",
                (self.kernel.subject_id,),
            ).fetchone()
            pending_interaction_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT interaction_id, status, created_at, decided_at "
                    "FROM interactions WHERE subject_id = ? AND direction = 'incoming' "
                    "AND status IN ('offered', 'deferred') "
                    "ORDER BY created_at, interaction_id LIMIT ?",
                    (self.kernel.subject_id, DIAGNOSTICS_MAX_PENDING_INTERACTIONS),
                )
            ]
            interaction_call_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT purpose, status, error_code, created_at, completed_at "
                    "FROM model_calls WHERE subject_id = ? "
                    "AND purpose LIKE 'interaction_cognition:%' "
                    "ORDER BY created_at DESC, call_id DESC LIMIT ?",
                    (self.kernel.subject_id, DIAGNOSTICS_MAX_INTERACTION_CALLS),
                )
            ]
            interaction_wait_rows = [
                dict(row)
                for row in connection.execute(
                    "SELECT pool, reason_code, retry_count, next_retry_at, updated_at "
                    "FROM waiting_cognitive_tasks WHERE subject_id = ? "
                    "AND purpose LIKE 'interaction_cognition:%' AND status = 'waiting' "
                    "ORDER BY updated_at DESC, task_id DESC LIMIT ?",
                    (self.kernel.subject_id, DIAGNOSTICS_MAX_WAITING_TASKS),
                )
            ]
        storage_payload: dict[str, Any] | None = None
        if storage is not None:
            try:
                parsed = json.loads(str(storage["payload_json"]))
                if isinstance(parsed, dict):
                    storage_payload = {
                        key: parsed[key]
                        for key in ("warnings", "over_quota", "free_bytes", "cognition_allowed")
                        if key in parsed
                    }
            except (TypeError, ValueError):
                storage_payload = None
        operator_health = (
            {"status": "unavailable"}
            if self.operator_controls is None
            else self.operator_controls.health()
        )
        wallet_state = self.wallet_health()
        wallet_acquisition = wallet_state.get("acquisition")
        if not isinstance(wallet_acquisition, dict):
            wallet_acquisition = {"status": "degraded", "reason": "unavailable"}
        cognition_diagnostics: dict[str, Any] = {
            "enabled": getattr(self, "cognition", None) is not None,
            "pending_interactions": pending_interaction_rows,
            "recent_interaction_model_calls": interaction_call_rows,
            "waiting_interaction_tasks": interaction_wait_rows,
        }
        cognition_gateway = getattr(self, "cognition_gateway", None)
        if cognition_gateway is not None:
            try:
                cognition_diagnostics["model_pools"] = cognition_gateway.pool_status()
            except Exception as error:
                cognition_diagnostics["model_pools"] = {
                    "status": "unavailable",
                    "error_type": type(error).__name__,
                }
        return {
            "loop": None if loop is None else dict(loop),
            "unknown": {"actions": int(unknown_actions), "model_calls": int(unknown_calls)},
            "deliveries": delivery_counts,
            "inbound": inbound_counts,
            "capabilities": capability_counts,
            "resource_pressures": pressure_rows,
            "common_knowledge_imports": knowledge_counts,
            "storage": storage_payload,
            "integrity": None if self.integrity is None else self.integrity.summary(),
            "operator_health": operator_health,
            "archive_key": operator_health.get("archive_key"),
            "migration": operator_health.get("migration"),
            "wal": operator_health.get("wal"),
            "storage_health": operator_health.get("storage"),
            "export_health": operator_health.get("export"),
            "wallet": wallet_state,
            "wallet_acquisition": wallet_acquisition,
            "wallet_execution": self.wallet_execution_health(),
            "cognition": cognition_diagnostics,
        }

    def public_post_controls(self) -> dict[str, Any]:
        controls = self.public_posts.controls(
            self.kernel.subject_id,
            defaults={
                "rate_limit_per_hour": self.settings.public_post_rate_limit_per_hour,
                "queue_cap": self.settings.public_post_queue_cap,
                "captcha_ttl_seconds": self.settings.public_post_captcha_ttl_seconds,
                "captcha_max_attempts": self.settings.public_post_captcha_max_attempts,
                "captcha_mode": self.settings.public_post_captcha_mode,
                "storage_cap_bytes": self.settings.public_post_storage_cap_bytes,
                "captcha_issue_limit_per_hour": (
                    self.settings.public_post_captcha_issue_limit_per_hour
                ),
                "captcha_global_rate_per_minute": (
                    self.settings.public_post_captcha_global_rate_per_minute
                ),
            },
        )
        return {
            **controls,
            "usage": self.public_posts.usage(
                self.kernel.subject_id,
                storage_cap_bytes=controls["storage_cap_bytes"],
            ),
        }

    def wallet_execution_health(self) -> dict[str, Any]:
        """Return bounded execution counters without signer or transaction data."""
        if self.wallet_execution is None:
            return {"status": "unavailable", "configured": False}
        try:
            with self.kernel.database.read_transaction() as connection:
                status_rows = connection.execute(
                    "SELECT status, COUNT(*) AS count FROM wallet_payment_executions "
                    "WHERE subject_id=? GROUP BY status",
                    (self.kernel.subject_id,),
                ).fetchall()
                attempts = connection.execute(
                    "SELECT COUNT(*) FROM wallet_payment_execution_attempts WHERE subject_id=?",
                    (self.kernel.subject_id,),
                ).fetchone()[0]
            counts = {str(row["status"]): int(row["count"]) for row in status_rows}
            return {
                "status": "attention" if counts.get("unknown", 0) else "ok",
                "configured": True,
                "executions": counts,
                "attempts": int(attempts),
            }
        except Exception:
            return {"status": "degraded", "configured": True, "reason": "unavailable"}

    def wallet_acquisition_health(self) -> dict[str, Any]:
        """Return a constant-size, credential-free acquisition projection."""
        try:
            summary = self.wallet_acquisitions.status_summary(self.kernel.subject_id)
            if type(summary) is not dict:
                raise ValueError("wallet acquisition summary is invalid")
            # The ledger's durable summary predates the explicit status field;
            # the projection helper computes it from bounded counters and then
            # drops every field outside the public contract.
            return _wallet_acquisition_projection(summary, self.kernel.subject_id)
        except Exception:
            # A diagnostics endpoint must remain usable when this optional
            # subsystem is unavailable, but it must never fail open or expose
            # the original exception/payload.
            return _wallet_unavailable()

    def wallet_observation_health_breakdown(
        self,
        *,
        group_by: str = "network",
        network_id: str | None = None,
        source: str | None = None,
        limit: int = WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
    ) -> dict[str, Any]:
        """Return the fixed, credential-free wallet health breakdown."""

        try:
            projection = self.wallets.observation_health_breakdown(
                self.kernel.subject_id,
                group_by=group_by,
                network_id=network_id,
                source=source,
                limit=limit,
            )
            return _wallet_observation_health_breakdown(projection)
        except Exception:
            return _wallet_unavailable()

    def wallet_health(self) -> dict[str, Any]:
        """Return bounded wallet state/history diagnostics without full history reads."""

        try:
            summary = _wallet_status_summary_projection(
                self.wallets.status_summary(self.kernel.subject_id), self.kernel.subject_id
            )
        except Exception:
            # Missing or malformed observation health is an unavailable wallet
            # projection, rather than an implicit healthy/empty state.
            return _wallet_unavailable()

        try:
            acquisition = _wallet_acquisition_projection(
                self.wallet_acquisition_health(), self.kernel.subject_id
            )
        except Exception:
            acquisition = _wallet_unavailable()
        summary["acquisition"] = acquisition

        severity = {"ok": 0, "attention": 1, "degraded": 2}
        observation_status = summary["balance_history"]["observation_health"]["status"]
        acquisition_status = acquisition["status"]
        summary["status"] = max((observation_status, acquisition_status), key=severity.__getitem__)
        return summary

    def _handler_type(self) -> type[BaseHTTPRequestHandler]:
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "NoyraHTTP/0.1.0"

            def setup(self) -> None:
                super().setup()
                self.connection.settimeout(owner.settings.request_timeout_seconds)

            def do_GET(self) -> None:
                if not owner.allow_request(
                    owner.client_ip(
                        str(self.client_address[0]), self.headers.get("X-Forwarded-For")
                    )
                ):
                    self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
                    return
                if self.path.startswith("/api/v1/"):
                    normalized_path = self.path.replace("/api/v1/", "/api/", 1)
                    if normalized_path.startswith("/api/webhooks/"):
                        self.path = normalized_path.replace("/api/webhooks/", "/webhooks/", 1)
                if self.path.startswith("/webhooks/wechat/"):
                    self._wechat_webhook_challenge()
                    return
                parsed = urlsplit(self.path)
                if parsed.path == "/admin":
                    self._asset("admin.html", "text/html; charset=utf-8")
                    return
                if parsed.path == "/admin.js":
                    self._asset("admin.js", "text/javascript; charset=utf-8")
                    return
                if parsed.path == "/admin.css":
                    self._asset("admin.css", "text/css; charset=utf-8")
                    return
                if parsed.path == "/admin/session":
                    self._admin_session_status()
                    return
                if parsed.path == "/":
                    self._asset("index.html", "text/html; charset=utf-8")
                    return
                if parsed.path == "/app.js":
                    self._asset("app.js", "text/javascript; charset=utf-8")
                    return
                if parsed.path == "/styles.css":
                    self._asset("styles.css", "text/css; charset=utf-8")
                    return
                if parsed.path == "/favicon.ico":
                    self.send_response(HTTPStatus.NO_CONTENT)
                    self._headers("image/x-icon", 0)
                    self.end_headers()
                    return
                if parsed.path == "/health/live":
                    self._json(HTTPStatus.OK, {"status": "ok", "service": "noyra"})
                    return
                if parsed.path == "/health/ready":
                    try:
                        lifecycle = owner.kernel.lifecycle.current().state
                        at_rest = None if owner.at_rest is None else owner.at_rest.health()
                        at_rest_ready = not bool(
                            at_rest is not None
                            and at_rest.get("enforced")
                            and not at_rest.get("ready")
                        )
                        quarantine = owner.quarantine_checker
                        integrity_ready = not bool(quarantine()) if callable(quarantine) else True
                        cloud_ready = not (
                            owner.cloud_archive_status["configured"]
                            and not owner.cloud_archive_status["ready"]
                        )
                        ready = at_rest_ready and integrity_ready and cloud_ready
                        payload = {
                            "status": "ok" if ready else "degraded",
                            "service": "noyra",
                            "lifecycle": lifecycle,
                            "at_rest": at_rest,
                            "cloud_archive": owner.cloud_archive_status,
                        }
                        self._json(
                            HTTPStatus.OK if ready else HTTPStatus.SERVICE_UNAVAILABLE,
                            payload,
                        )
                    except Exception:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"status": "degraded", "service": "noyra", "reason": "unavailable"},
                        )
                    return
                if parsed.path == "/health":
                    cached_health = owner._cached_health()
                    if cached_health is not None:
                        cached_status, cached_payload = cached_health
                        self._json(cached_status, cached_payload)
                        return
                if parsed.path.startswith("/api/v1/"):
                    self.path = self.path.replace("/api/v1/", "/api/", 1)
                    parsed = urlsplit(self.path)
                if parsed.path in {
                    "/api/config/wallet-balance-history",
                    "/api/v1/config/wallet-balance-history",
                    "/api/config/wallet-observation-health",
                    "/api/v1/config/wallet-observation-health",
                }:
                    # Preserve blank values so this route can distinguish an
                    # omitted parameter from an explicitly invalid one.
                    query = parse_qs(parsed.query, keep_blank_values=True)
                else:
                    query = parse_qs(parsed.query)
                limit = self._limit(query)
                if (
                    parsed.path != "/health"
                    and parsed.path.startswith("/api/")
                    and not self._at_rest_available()
                ):
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "at_rest_boundary_unavailable"},
                    )
                    return
                if parsed.path == "/health":
                    provider = owner.cloud_archive_provider
                    if isinstance(provider, S3ArchiveProvider):
                        readiness = provider.readiness()
                        owner.cloud_archive_status.update(
                            {
                                key: readiness[key]
                                for key in ("ready", "last_checked_at", "error", "provider_id")
                            }
                        )
                    state = owner.projection.state(owner.kernel.subject_id)
                    with owner.kernel.database.connection() as connection:
                        loop_state = connection.execute(
                            "SELECT circuit_status, consecutive_failures, last_tick_started_at, "
                            "last_successful_tick_at, last_error_type FROM autonomy_loop_state "
                            "WHERE subject_id = ?",
                            (owner.kernel.subject_id,),
                        ).fetchone()
                    integrity = None if owner.integrity is None else owner.integrity.summary()
                    integrity_degraded = (
                        integrity is not None
                        and integrity["policy_mode"] != "off"
                        and integrity["status"] in {"degraded", "corrupt", "incomplete"}
                    )
                    secret_cleanup_domains = {
                        "transport": owner.transports.secret_cleanup.health(
                            owner.kernel.subject_id, "transport"
                        ),
                        "search": owner.search_providers.secret_cleanup.health(
                            owner.kernel.subject_id, "search"
                        ),
                        "cognitive": owner.cognitive_resources.secret_cleanup.health(
                            owner.kernel.subject_id, "cognitive"
                        ),
                        "embedding": owner.embedding_resources.cleanup_health(
                            owner.kernel.subject_id
                        ),
                    }
                    secret_cleanup_pending = sum(
                        cast(int, domain["pending"]) for domain in secret_cleanup_domains.values()
                    )
                    secret_cleanup = {
                        "status": "degraded" if secret_cleanup_pending else "ok",
                        "pending": secret_cleanup_pending,
                        "domains": secret_cleanup_domains,
                    }
                    embedding_secret_cleanup = secret_cleanup_domains["embedding"]
                    at_rest = None if owner.at_rest is None else owner.at_rest.health()
                    at_rest_degraded = bool(
                        at_rest is not None and at_rest["enforced"] and not at_rest["ready"]
                    )
                    operator_health = (
                        {"status": "unavailable"}
                        if owner.operator_controls is None
                        else owner.operator_controls.health()
                    )
                    operator_health_degraded = operator_health.get("status") == "degraded"
                    wallet_acquisition_health = owner.wallet_acquisition_health()
                    wallet_acquisition_degraded = (
                        wallet_acquisition_health.get("status") == "degraded"
                    )
                    degraded = (
                        (loop_state is not None and loop_state["circuit_status"] == "open")
                        or integrity_degraded
                        or at_rest_degraded
                        or operator_health_degraded
                        or wallet_acquisition_degraded
                        or (
                            owner.cloud_archive_status["configured"]
                            and not owner.cloud_archive_status["ready"]
                        )
                        or secret_cleanup_pending != 0
                    )
                    health_status = HTTPStatus.SERVICE_UNAVAILABLE if degraded else HTTPStatus.OK
                    health_payload = {
                        "status": "degraded" if degraded else "ok",
                        "lifecycle": state["lifecycle"],
                        "loop": None if loop_state is None else dict(loop_state),
                        "integrity": integrity,
                        "at_rest": at_rest,
                        "cloud_archive": owner.cloud_archive_status,
                        "secret_cleanup": secret_cleanup,
                        "embedding_secret_cleanup": embedding_secret_cleanup,
                        "operator_health": operator_health,
                        "wallet_acquisition": wallet_acquisition_health,
                        "health": operator_health,
                    }
                    owner._store_health(health_status, health_payload)
                    self._json(health_status, health_payload)
                elif parsed.path == "/api/state":
                    state = owner.projection.state(owner.kernel.subject_id)
                    self._json(HTTPStatus.OK, state)
                elif parsed.path == "/api/state/details":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    state = owner.projection.private_state(owner.kernel.subject_id)
                    if owner.cognition_gateway is not None:
                        state["cognitive_resources"] = owner.cognition_gateway.pool_status()
                    self._json(HTTPStatus.OK, state)
                elif parsed.path == "/api/diary":
                    self._json(
                        HTTPStatus.OK, owner.projection.diary(owner.kernel.subject_id, limit=limit)
                    )
                elif parsed.path == "/api/behavior":
                    self._json(
                        HTTPStatus.OK,
                        owner.projection.behavior_logs(owner.kernel.subject_id, limit=limit),
                    )
                elif parsed.path == "/api/goals":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.projection.goals_view(owner.kernel.subject_id, limit=limit),
                    )
                elif parsed.path == "/api/projects":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.projection.projects_view(owner.kernel.subject_id, limit=limit),
                    )
                elif parsed.path == "/api/outcomes":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.projection.outcomes_view(owner.kernel.subject_id, limit=limit),
                    )
                elif parsed.path == "/api/runtime-logs":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    if "offset" in query:
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "offset_pagination_unsupported"},
                        )
                        return
                    try:
                        page = owner.projection.runtime_logs(
                            owner.kernel.subject_id,
                            limit=limit,
                            cursor=self._cursor(query),
                        )
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
                        return
                    self._json(HTTPStatus.OK, page)
                elif parsed.path == "/api/diagnostics":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(HTTPStatus.OK, owner.diagnostics())
                elif parsed.path == "/api/admin/health":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    if owner.operator_controls is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "operator_controls_unavailable"},
                        )
                        return
                    self._json(HTTPStatus.OK, owner.operator_controls.health())
                elif parsed.path == "/api/admin/recoverable-work":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    if owner.operator_controls is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "operator_controls_unavailable"},
                        )
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.operator_controls.recoverable_work(limit=limit),
                    )
                elif parsed.path == "/api/admin/lifecycle":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        {"lifecycle": owner.kernel.lifecycle.current().__dict__},
                    )
                elif parsed.path == "/api/interactions":
                    self._json(
                        HTTPStatus.OK,
                        owner.projection.interactions_view(owner.kernel.subject_id, limit=limit),
                    )
                elif parsed.path == "/api/public-posts":
                    try:
                        posts = owner.projection.public_posts_view(
                            owner.kernel.subject_id, limit=limit
                        )
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, posts)
                elif parsed.path == "/api/admin/public-posts":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        status_filter = query.get("status", [None])[0]
                        cursor = query.get("cursor", [None])[0]
                        posts = [
                            record.__dict__
                            for record in owner.public_posts.admin(
                                owner.kernel.subject_id,
                                limit=limit,
                                status=status_filter,
                                cursor=cursor,
                            )
                        ]
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_query"})
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, posts)
                elif parsed.path == "/api/admin/public-post-controls":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        controls = owner.public_post_controls()
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, controls)
                elif parsed.path == "/api/common-knowledge/public-key":
                    self._json(
                        HTTPStatus.OK,
                        {
                            "key_id": owner.common_knowledge.key_id,
                            "public_key": owner.common_knowledge.public_key,
                        },
                    )
                elif parsed.path == "/api/common-knowledge/discovery":
                    self._json(HTTPStatus.OK, owner.common_knowledge.discovery_document())
                elif parsed.path == "/api/common-knowledge/feed":
                    raw_cursor = query.get("cursor", ["0"])[0]
                    if not raw_cursor.isdigit():
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
                        return
                    try:
                        document = owner.common_knowledge.feed_document(
                            cursor=int(raw_cursor),
                            limit=limit,
                        )
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_cursor"})
                        return
                    self._json(HTTPStatus.OK, document)
                elif parsed.path == "/api/common-knowledge/public":
                    self._json(
                        HTTPStatus.OK,
                        [
                            owner.common_knowledge.export_package(record.package_id)
                            for record in owner.common_knowledge.published(limit=limit)
                        ],
                    )
                elif parsed.path == "/api/mailbox":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    records = owner.interactions.list_channel(
                        owner.kernel.subject_id,
                        "web",
                        limit=limit,
                    )
                    self._json(
                        HTTPStatus.OK,
                        [
                            {
                                "interaction_id": record.interaction_id,
                                "direction": record.direction,
                                "kind": record.kind,
                                "channel": record.channel,
                                "counterparty": record.counterparty,
                                "content": record.content,
                                "related_interaction_id": record.related_interaction_id,
                                "status": record.status,
                                "created_at": record.created_at,
                                "decided_at": record.decided_at,
                            }
                            for record in records
                        ],
                    )
                elif parsed.path == "/api/bounties":
                    try:
                        rows = owner.wallet_economy.list_bounties(
                            owner.kernel.subject_id, status="published", limit=limit
                        )
                        self._json(
                            HTTPStatus.OK,
                            [
                                {
                                    "bounty_id": r.bounty_id,
                                    "title": r.title,
                                    "description": r.description,
                                    "acceptance_criteria": list(r.acceptance_criteria),
                                    "network_id": r.network_id,
                                    "asset_id": r.asset_id,
                                    "reward_amount": r.reward_amount,
                                    "opens_at": r.opens_at,
                                    "expires_at": r.expires_at,
                                    "max_submissions": r.max_submissions,
                                    "reward_slots": r.reward_slots,
                                    "status": r.status,
                                }
                                for r in rows
                            ],
                        )
                    except Exception:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_economy_unavailable"},
                            retry_after=60,
                        )
                elif parsed.path.startswith("/api/bounties/") and parsed.path.endswith(
                    "/submissions"
                ):
                    self._json(HTTPStatus.METHOD_NOT_ALLOWED, {"error": "method_not_allowed"})
                elif parsed.path.startswith("/api/bounties/"):
                    bounty_id = parsed.path.removeprefix("/api/bounties/")
                    if not bounty_id or "/" in bounty_id:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty"})
                        return
                    try:
                        record = owner.wallet_economy.get_bounty(bounty_id, owner.kernel.subject_id)
                        if record.status != "published":
                            self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
                            return
                        self._json(
                            HTTPStatus.OK,
                            {
                                "bounty_id": record.bounty_id,
                                "title": record.title,
                                "description": record.description,
                                "acceptance_criteria": list(record.acceptance_criteria),
                                "network_id": record.network_id,
                                "asset_id": record.asset_id,
                                "reward_amount": record.reward_amount,
                                "opens_at": record.opens_at,
                                "expires_at": record.expires_at,
                                "max_submissions": record.max_submissions,
                                "reward_slots": record.reward_slots,
                                "status": record.status,
                            },
                        )
                    except NotFoundError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
                    except Exception:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_economy_unavailable"},
                            retry_after=60,
                        )
                elif parsed.path == "/api/admin/wallet-bounties":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        self._json(
                            HTTPStatus.OK,
                            [
                                r.__dict__
                                for r in owner.wallet_economy.list_bounties(
                                    owner.kernel.subject_id, limit=limit
                                )
                            ],
                        )
                    except Exception:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_economy_unavailable"},
                            retry_after=60,
                        )
                elif parsed.path == "/api/admin/wallet-submissions":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    with owner.kernel.database.connection() as c:
                        submission_rows = c.execute(
                            "SELECT * FROM wallet_bounty_submissions "
                            "WHERE subject_id=? ORDER BY created_at DESC LIMIT ?",
                            (owner.kernel.subject_id, limit),
                        ).fetchall()
                    self._json(
                        HTTPStatus.OK,
                        [
                            {
                                "submission_id": row["submission_id"],
                                "bounty_id": row["bounty_id"],
                                "subject_id": row["subject_id"],
                                "counterparty": row["counterparty"],
                                "content": row["content"],
                                "evidence": strict_json_loads(row["evidence_json"]),
                                "recipient_address": row["recipient_address"],
                                "status": row["status"],
                                "decision_reason": row["decision_reason"],
                                "created_at": row["created_at"],
                                "decided_at": row["decided_at"],
                            }
                            for row in submission_rows
                        ],
                    )
                elif parsed.path == "/api/admin/wallet-orders":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            r.__dict__
                            for r in owner.wallet_economy.list_orders(
                                owner.kernel.subject_id, limit=limit
                            )
                        ],
                    )
                elif parsed.path == "/api/admin/wallet-rewards":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            r.__dict__
                            for r in owner.wallet_rewards.list_workflows(
                                owner.kernel.subject_id, limit=limit
                            )
                        ],
                    )
                elif parsed.path == "/api/admin/wallet-reward-incidents":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            r.__dict__
                            for r in owner.wallet_rewards.incidents(
                                owner.kernel.subject_id, limit=limit
                            )
                        ],
                    )
                elif parsed.path == "/api/admin/wallet-executions":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    engine = owner.wallet_execution
                    if engine is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_execution_unavailable"},
                            retry_after=60,
                        )
                        return
                    try:
                        self._json(
                            HTTPStatus.OK,
                            [
                                record.__dict__
                                for record in engine.list_executions(
                                    owner.kernel.subject_id, limit=limit
                                )
                            ],
                        )
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_execution_integrity_unavailable"},
                            retry_after=60,
                        )
                    except (ValueError, TypeError):
                        self._json(
                            HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_query"}
                        )
                elif parsed.path.startswith("/api/admin/wallet-executions/"):
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    engine = owner.wallet_execution
                    if engine is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_execution_unavailable"},
                            retry_after=60,
                        )
                        return
                    parts = parsed.path.removeprefix("/api/admin/wallet-executions/").split("/")
                    if len(parts) == 1 and parts[0]:
                        try:
                            execution_record = engine.get_execution(
                                parts[0], owner.kernel.subject_id
                            )
                        except NotFoundError:
                            self._json(
                                HTTPStatus.NOT_FOUND, {"error": "wallet_execution_not_found"}
                            )
                            return
                        self._json(HTTPStatus.OK, execution_record.__dict__)
                        return
                    if len(parts) == 2 and parts[0] and parts[1] == "attempts":
                        try:
                            attempt_records = engine.list_attempts(
                                parts[0], owner.kernel.subject_id, limit=limit
                            )
                        except NotFoundError:
                            self._json(
                                HTTPStatus.NOT_FOUND, {"error": "wallet_execution_not_found"}
                            )
                            return
                        except (ValueError, TypeError):
                            self._json(
                                HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_query"}
                            )
                            return
                        self._json(HTTPStatus.OK, [record.__dict__ for record in attempt_records])
                        return
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_path"})
                elif parsed.path == "/api/admin/wallet-policy":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.wallet_economy.get_policy(owner.kernel.subject_id).__dict__,
                    )
                elif parsed.path == "/api/admin/wallet-ledger":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            b.__dict__
                            for b in owner.wallet_economy.ledger_balances(owner.kernel.subject_id)
                        ],
                    )
                elif parsed.path == "/api/config/search-providers":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            {
                                "config_id": record.config_id,
                                "provider_type": record.provider_type,
                                "label": record.label,
                                "key_fingerprint": record.key_fingerprint[:12],
                                "rate_limit_per_hour": record.rate_limit_per_hour,
                                "status": record.status,
                                "created_at": record.created_at,
                                "revoked_at": record.revoked_at,
                            }
                            for record in owner.search_providers.list(owner.kernel.subject_id)
                        ],
                    )
                elif parsed.path == "/api/config/wallet-networks":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        wallet_networks = owner.wallets.list_networks(
                            owner.kernel.subject_id, limit=limit
                        )
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_networks])
                elif parsed.path == "/api/config/wallet-assets":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        wallet_assets = owner.wallets.list_assets(
                            owner.kernel.subject_id,
                            network_id=query.get("network_id", [None])[0],
                            limit=limit,
                        )
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_asset"})
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_assets])
                elif parsed.path == "/api/config/wallet-addresses":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        wallet_addresses = owner.wallets.list_addresses(
                            owner.kernel.subject_id,
                            network_id=query.get("network_id", [None])[0],
                            limit=limit,
                        )
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_address"})
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_addresses])
                elif parsed.path == "/api/config/wallet-balances":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        wallet_balances = owner.wallets.latest_balances(
                            owner.kernel.subject_id,
                            network_id=query.get("network_id", [None])[0],
                            asset_id=query.get("asset_id", [None])[0],
                            address_id=query.get("address_id", [None])[0],
                            limit=limit,
                        )
                    except ValueError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_balance"})
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, [record.__dict__ for record in wallet_balances])
                elif parsed.path == "/api/config/wallet-observation-health":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        (
                            breakdown_group_by,
                            breakdown_limit,
                            breakdown_network_id,
                            breakdown_source,
                        ) = self._wallet_observation_health_query(query)
                        projection = owner.wallets.observation_health_breakdown(
                            owner.kernel.subject_id,
                            group_by=breakdown_group_by,
                            network_id=breakdown_network_id,
                            source=breakdown_source,
                            limit=breakdown_limit,
                        )
                        payload = _wallet_observation_health_breakdown(projection)
                    except NotFoundError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_network_not_found"})
                        return
                    except ValueError:
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_wallet_observation_health_query"},
                        )
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, payload)
                elif parsed.path == "/api/config/wallet-balance-history":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        (
                            history_limit,
                            history_cursor,
                            network_id,
                            asset_id,
                            address_id,
                        ) = self._wallet_balance_history_query(query)
                        history_page = owner.wallets.balance_history_page(
                            owner.kernel.subject_id,
                            network_id=network_id,
                            asset_id=asset_id,
                            address_id=address_id,
                            limit=history_limit,
                            cursor=history_cursor,
                        )
                    except ValueError:
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_wallet_balance_history_query"},
                        )
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(
                        HTTPStatus.OK,
                        {
                            "items": [record.__dict__ for record in history_page.items],
                            "next_cursor": history_page.next_cursor,
                            "has_more": history_page.has_more,
                        },
                    )
                elif parsed.path == "/api/admin/wallet-acquisitions":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        statuses = self._wallet_acquisition_statuses(query)
                        wallet_acquisition_records = owner.wallet_acquisitions.list(
                            owner.kernel.subject_id,
                            limit=limit,
                            statuses=statuses,
                        )
                    except (ValueError, TypeError):
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_wallet_acquisition_query"},
                        )
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            _wallet_acquisition_run_payload(record)
                            for record in wallet_acquisition_records
                        ],
                    )
                elif parsed.path == "/api/admin/wallet-acquisition-budget":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    network_id = query.get("network_id", [None])[0]
                    if network_id is None:
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "wallet_acquisition_network_required"},
                        )
                        return
                    try:
                        owner.wallets.get_network(
                            network_id,
                            subject_id=owner.kernel.subject_id,
                        )
                        budget = owner.wallet_acquisitions.budget_status(
                            owner.kernel.subject_id,
                            network_id,
                        )
                    except NotFoundError:
                        self._json(
                            HTTPStatus.NOT_FOUND,
                            {"error": "wallet_network_not_found"},
                        )
                        return
                    except (ValueError, TypeError):
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_wallet_acquisition_network"},
                        )
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(HTTPStatus.OK, budget.__dict__)
                elif parsed.path.startswith("/api/admin/wallet-acquisitions/"):
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    suffix = parsed.path.removeprefix("/api/admin/wallet-acquisitions/")
                    parts = suffix.strip("/").split("/") if suffix.strip("/") else []
                    if len(parts) == 2 and parts[1] == "attempts" and parts[0]:
                        try:
                            attempts = owner.wallet_acquisitions.attempts(
                                parts[0], subject_id=owner.kernel.subject_id
                            )
                        except ValueError:
                            self._json(
                                HTTPStatus.BAD_REQUEST,
                                {"error": "invalid_wallet_acquisition_id"},
                            )
                            return
                        except NotFoundError:
                            self._json(
                                HTTPStatus.NOT_FOUND,
                                {"error": "wallet_acquisition_not_found"},
                            )
                            return
                        except IntegrityError:
                            self._json(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": "wallet_integrity_unavailable"},
                                retry_after=60,
                            )
                            return
                        self._json(
                            HTTPStatus.OK,
                            [_wallet_acquisition_attempt_payload(attempt) for attempt in attempts],
                        )
                    elif len(parts) == 1 and parts[0]:
                        try:
                            acquisition_record = owner.wallet_acquisitions.get(
                                parts[0], subject_id=owner.kernel.subject_id
                            )
                        except ValueError:
                            self._json(
                                HTTPStatus.BAD_REQUEST,
                                {"error": "invalid_wallet_acquisition_id"},
                            )
                            return
                        except NotFoundError:
                            self._json(
                                HTTPStatus.NOT_FOUND,
                                {"error": "wallet_acquisition_not_found"},
                            )
                            return
                        except IntegrityError:
                            self._json(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": "wallet_integrity_unavailable"},
                                retry_after=60,
                            )
                            return
                        self._json(
                            HTTPStatus.OK, _wallet_acquisition_run_payload(acquisition_record)
                        )
                    else:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                elif parsed.path == "/api/config/inbound-bindings":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.inbound.list_bindings(owner.kernel.subject_id, limit=limit),
                    )
                elif parsed.path.startswith("/api/config/model-resources/"):
                    # Treat the whole resource namespace as protected before
                    # inspecting path shape.  This keeps malformed URLs from
                    # becoming an unauthenticated oracle while still making
                    # every request terminate with a concrete response.
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    parts = parsed.path.removeprefix("/api/config/model-resources/").split("/")
                    if len(parts) == 2 and parts[1] == "keys" and parts[0]:
                        try:
                            key_rows = owner.cognitive_resources.keys(
                                parts[0], subject_id=owner.kernel.subject_id
                            )
                        except NotFoundError:
                            self._json(
                                HTTPStatus.NOT_FOUND,
                                {"error": "model_resource_not_found"},
                            )
                            return
                        except IntegrityError:
                            self._json(
                                HTTPStatus.SERVICE_UNAVAILABLE,
                                {"error": "model_resource_integrity_unavailable"},
                                retry_after=60,
                            )
                            return
                        self._json(
                            HTTPStatus.OK,
                            [
                                {
                                    "key_id": row.key_id,
                                    "group_id": row.group_id,
                                    "key_fingerprint": row.key_fingerprint[:12],
                                    "status": row.status,
                                    "selection_count": row.selection_count,
                                    "consecutive_failures": row.consecutive_failures,
                                    "cooldown_until": row.cooldown_until,
                                    "last_selected_at": row.last_selected_at,
                                    "last_success_at": row.last_success_at,
                                    "last_failure_at": row.last_failure_at,
                                    "created_at": row.created_at,
                                }
                                for row in key_rows
                            ],
                        )
                        return
                    # This prefix branch must terminate every unmatched path.
                    # Without an explicit response, malformed resource URLs
                    # (for example ``/{id}/keys/extra``) fall out of the
                    # ``elif`` chain with no HTTP response and leave clients
                    # waiting until their request timeout.
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                elif parsed.path == "/api/config/model-resources":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    try:
                        model_resource_records = owner.cognitive_resources.list(
                            owner.kernel.subject_id
                        )
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "model_resource_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    self._json(
                        HTTPStatus.OK,
                        [_cognitive_resource_payload(record) for record in model_resource_records],
                    )
                elif parsed.path == "/api/config/embedding-resources":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            {
                                **record.__dict__,
                                "key_fingerprint": record.key_fingerprint[:12],
                            }
                            for record in owner.embedding_resources.list(owner.kernel.subject_id)
                        ],
                    )
                elif parsed.path == "/api/config/training-policy":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        owner.training.policy(owner.kernel.subject_id).__dict__,
                    )
                elif parsed.path == "/api/config/common-knowledge":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        {
                            "published": [
                                {
                                    **owner.common_knowledge.package_metadata(record.package_id),
                                    "import_status": "published",
                                    "can_revoke": True,
                                }
                                for record in owner.common_knowledge.published(limit=limit)
                            ],
                            "review_queue": owner.common_knowledge.review_queue(limit=limit),
                            "peers": [record.__dict__ for record in owner.common_knowledge.peers()],
                        },
                    )
                elif parsed.path == "/api/config/transports":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            record.__dict__
                            for record in owner.transports.list(owner.kernel.subject_id)
                        ],
                    )
                elif parsed.path == "/api/config/capabilities":
                    if not self._authorized():
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            {
                                **record.__dict__,
                                "effective_status": (
                                    "blocked_legacy_approval"
                                    if record.requires_approval
                                    else record.status
                                ),
                            }
                            for record in owner.capabilities.list(owner.kernel.subject_id)
                        ],
                    )
                elif parsed.path == "/api/deliveries":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    with owner.kernel.database.connection() as connection:
                        delivery_rows = connection.execute(
                            "SELECT delivery_id FROM interaction_deliveries WHERE subject_id = ? "
                            "ORDER BY updated_at DESC LIMIT ?",
                            (owner.kernel.subject_id, limit),
                        ).fetchmany(limit)
                    self._json(
                        HTTPStatus.OK,
                        [
                            owner.deliveries.get_delivery(str(row["delivery_id"])).__dict__
                            for row in delivery_rows
                        ],
                    )
                elif parsed.path.startswith("/api/deliveries/") and parsed.path.endswith(
                    "/reconciliations"
                ):
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    delivery_id = (
                        parsed.path.removeprefix("/api/deliveries/")
                        .removesuffix("/reconciliations")
                        .strip("/")
                    )
                    try:
                        reconciliation_records = owner.deliveries.reconciliation_history(
                            delivery_id,
                            subject_id=owner.kernel.subject_id,
                        )
                    except NotFoundError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "delivery_not_found"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [record.__dict__ for record in reconciliation_records],
                    )
                elif parsed.path == "/api/self-modification":
                    if not self._authorized("read"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    mod_records: list[dict[str, Any]] = []
                    with owner.kernel.database.connection() as connection:
                        modification_rows = connection.execute(
                            "SELECT proposal_id FROM self_modification_proposals "
                            "WHERE subject_id = ? ORDER BY updated_at DESC LIMIT ?",
                            (owner.kernel.subject_id, limit),
                        ).fetchmany(limit)
                    mod_records = [
                        owner.self_modification.get(str(row["proposal_id"])).__dict__
                        for row in modification_rows
                    ]
                    self._json(HTTPStatus.OK, mod_records)
                elif parsed.path == "/api/admin/export-jobs":
                    if not self._authorized("export"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    self._json(
                        HTTPStatus.OK,
                        [
                            job.__dict__
                            for job in owner.export_jobs.list(
                                owner.kernel.subject_id,
                                limit=limit,
                            )
                        ],
                    )
                elif parsed.path.startswith("/api/admin/export-jobs/"):
                    if not self._authorized("export"):
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    suffix = parsed.path.removeprefix("/api/admin/export-jobs/")
                    if suffix.endswith("/download"):
                        job_id = suffix.removesuffix("/download").strip("/")
                        try:
                            job, path = owner.export_jobs.download_path(
                                job_id,
                                subject_id=owner.kernel.subject_id,
                            )
                        except NotFoundError:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
                            return
                        except FileNotFoundError:
                            self._json(
                                HTTPStatus.GONE,
                                {"error": "export_artifact_missing"},
                            )
                            return
                        except RuntimeError:
                            self._json(
                                HTTPStatus.CONFLICT,
                                {"error": "export_not_ready"},
                            )
                            return
                        self._send_file(
                            path,
                            filename=job.filename or f"{job_id}.zip",
                            digest=job.sha256 or "",
                        )
                    else:
                        try:
                            job = owner.export_jobs.get(
                                suffix.strip("/"),
                                subject_id=owner.kernel.subject_id,
                            )
                        except NotFoundError:
                            self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
                            return
                        self._json(HTTPStatus.OK, job.__dict__)
                elif parsed.path == "/api/admin/runtime-export":
                    # Keep the historical GET endpoint as a compatibility
                    # shim, but never run the export on this request worker.
                    if not owner.allow_mutation(parsed.path):
                        self.close_connection = True
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "integrity_quarantine"},
                        )
                        return
                    self._enqueue_export_job("runtime")
                elif parsed.path == "/api/admin/training-export":
                    # See runtime-export above.  Clients should poll
                    # /api/admin/export-jobs/{job_id} and download the staged
                    # artifact once it is complete.
                    if not owner.allow_mutation(parsed.path):
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "integrity_quarantine"},
                        )
                        return
                    self._enqueue_export_job("training")
                else:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})

            def do_POST(self) -> None:
                if not owner.allow_request(
                    owner.client_ip(
                        str(self.client_address[0]), self.headers.get("X-Forwarded-For")
                    )
                ):
                    self._discard_small_request_body()
                    self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "rate_limited"})
                    return
                # Route admission and dispatch must never include a query
                # string. Keep original target for webhook query signatures.
                self._request_target = self.path
                request_path = urlsplit(self.path).path
                if request_path == "/admin/session":
                    self._create_admin_session()
                    return
                if request_path == "/admin/session/logout":
                    self._logout_admin_session()
                    return
                if request_path.startswith("/api/v1/"):
                    normalized_path = request_path.replace("/api/v1/", "/api/", 1)
                    if normalized_path.startswith("/api/webhooks/"):
                        self.path = normalized_path.replace("/api/webhooks/", "/webhooks/", 1)
                    else:
                        self.path = normalized_path
                else:
                    self.path = request_path
                if self.path.startswith("/webhooks/"):
                    self._receive_inbound_webhook()
                    return
                if not self._at_rest_available():
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "at_rest_boundary_unavailable"},
                    )
                    return
                if not owner.allow_mutation(self.path):
                    self.close_connection = True
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "integrity_quarantine"},
                    )
                    return
                # Lifecycle controls intentionally invalidate/reopen the gate
                # as part of their own transaction.  They must not run under
                # a lease that they invalidate themselves.
                if self.path in {
                    "/api/admin/lifecycle/pause",
                    "/api/admin/lifecycle/resume",
                    "/api/admin/lifecycle/reset",
                }:
                    try:
                        with owner.admission.lifecycle_control_scope():
                            if not owner.allow_mutation(self.path):
                                raise OperationInvalidated(
                                    "lifecycle control crossed integrity quarantine"
                                )
                            self._dispatch_post()
                    except OperationInvalidated:
                        self.close_connection = True
                        self._discard_small_request_body()
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "integrity_quarantine"},
                        )
                    return
                # Only explicit recovery endpoints may bypass a paused or
                # quarantined admission.  Deriving this from the current
                # gate state would let a race after ``allow_mutation`` turn
                # an ordinary POST into an unfenced write.
                allow_recovery = owner._is_recovery_mutation(self.path)
                try:
                    lease = owner.admission.begin(
                        "http_mutation",
                        allow_quarantine=allow_recovery,
                    )
                except (OperationInvalidated, RuntimeOwnershipError):
                    self.close_connection = True
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "runtime_admission_unavailable"},
                    )
                    return
                try:
                    with bind_lease(lease):
                        self._dispatch_post()
                except OperationInvalidated:
                    self.close_connection = True
                    with suppress(Exception):
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "runtime_epoch_invalidated"},
                        )
                finally:
                    owner.admission.finish(lease)

            def _dispatch_post(self) -> None:
                if self.path in {
                    "/api/admin/lifecycle/pause",
                    "/api/admin/lifecycle/resume",
                    "/api/admin/lifecycle/reset",
                }:
                    self._lifecycle_control()
                    return
                if self.path.startswith("/api/admin/actions/") and self.path.endswith("/reconcile"):
                    self._reconcile_action()
                    return
                if self.path.startswith("/api/admin/model-calls/") and self.path.endswith(
                    "/reconcile"
                ):
                    self._reconcile_model_call()
                    return
                if self.path == "/api/admin/export-jobs":
                    self._create_export_job()
                    return
                if self.path.startswith("/api/admin/export-jobs/") and self.path.endswith(
                    "/cancel"
                ):
                    self._cancel_export_job()
                    return
                if self.path == "/api/admin/wallet-bounties":
                    self._create_wallet_bounty()
                    return
                if self.path == "/api/admin/wallet-rewards":
                    self._create_wallet_reward()
                    return
                if self.path == "/api/admin/wallet-rewards/execute-ready":
                    self._execute_wallet_rewards()
                    return
                if self.path.startswith(
                    "/api/admin/wallet-reward-incidents/"
                ) and self.path.endswith("/resolve"):
                    self._resolve_wallet_reward_incident()
                    return
                if self.path.startswith(
                    "/api/admin/wallet-reward-submissions/"
                ) and self.path.endswith("/decide"):
                    self._decide_wallet_reward_submission()
                    return
                if self.path.startswith("/api/admin/wallet-rewards/"):
                    self._wallet_reward_operation()
                    return
                if self.path.startswith("/api/wallet-rewards/") and self.path.endswith(
                    "/submissions"
                ):
                    self._submit_wallet_reward()
                    return
                if self.path.startswith("/api/admin/wallet-bounties/"):
                    self._wallet_bounty_operation()
                    return
                if self.path.startswith("/api/bounties/") and self.path.endswith("/submissions"):
                    self._submit_wallet_bounty()
                    return
                if self.path.startswith("/api/admin/wallet-submissions/"):
                    self._wallet_submission_operation()
                    return
                if self.path == "/api/admin/wallet-executions/recover":
                    self._recover_wallet_executions()
                    return
                if self.path.startswith("/api/admin/wallet-executions/"):
                    self._wallet_execution_operation()
                    return
                if self.path.startswith("/api/admin/wallet-orders/"):
                    self._wallet_order_operation()
                    return
                if self.path == "/api/admin/wallet-policy":
                    self._update_wallet_policy()
                    return
                if self.path == "/api/config/wallet-networks":
                    self._configure_wallet_network()
                    return
                if self.path == "/api/config/wallet-assets":
                    self._configure_wallet_asset()
                    return
                if self.path == "/api/config/wallet-addresses":
                    self._configure_wallet_address()
                    return
                if self.path == "/api/admin/wallet-acquisitions":
                    self._enqueue_wallet_acquisition()
                    return
                if self.path == "/api/admin/wallet-acquisitions/run":
                    self._run_wallet_acquisitions()
                    return
                if self.path.startswith("/api/admin/wallet-acquisitions/") and self.path.endswith(
                    "/retry"
                ):
                    self._retry_wallet_acquisition()
                    return
                if self.path.startswith("/api/admin/wallet-acquisitions/") and self.path.endswith(
                    "/cancel"
                ):
                    self._cancel_wallet_acquisition()
                    return
                if self.path.startswith("/api/config/wallet-networks/") and self.path.endswith(
                    "/revoke"
                ):
                    self._revoke_wallet_resource("network")
                    return
                if self.path.startswith("/api/config/wallet-assets/") and self.path.endswith(
                    "/revoke"
                ):
                    self._revoke_wallet_resource("asset")
                    return
                if self.path.startswith("/api/config/wallet-addresses/") and self.path.endswith(
                    "/revoke"
                ):
                    self._revoke_wallet_resource("address")
                    return
                if self.path == "/api/config/search-providers":
                    self._configure_search_provider()
                    return
                if self.path == "/api/config/inbound-bindings":
                    self._configure_inbound_binding()
                    return
                if self.path.startswith("/api/config/inbound-bindings/"):
                    self._change_inbound_binding()
                    return
                if self.path.startswith("/api/config/search-providers/") and self.path.endswith(
                    "/revoke"
                ):
                    self._revoke_search_provider()
                    return
                if self.path == "/api/config/model-resources":
                    self._configure_model_resource()
                    return
                if self.path == "/api/config/embedding-resources":
                    self._configure_embedding_resource()
                    return
                if self.path.startswith("/api/config/model-resources/") and self.path.endswith(
                    "/test"
                ):
                    self._test_model_resource()
                    return
                if self.path.startswith("/api/config/model-resources/"):
                    key_parts = self.path.removeprefix("/api/config/model-resources/").split("/")
                    if len(key_parts) == 4 and key_parts[1] == "keys" and key_parts[3] == "revoke":
                        self._revoke_model_resource_key()
                        return
                if self.path.startswith("/api/config/model-resources/") and self.path.endswith(
                    "/keys"
                ):
                    self._add_model_resource_keys()
                    return
                if self.path.startswith("/api/config/model-resources/") and self.path.endswith(
                    "/update"
                ):
                    self._update_model_resource()
                    return
                if self.path.startswith("/api/config/model-resources/"):
                    self._change_model_resource_status()
                    return
                if self.path.startswith("/api/config/embedding-resources/"):
                    self._change_embedding_resource_status()
                    return
                if self.path == "/api/config/training-policy":
                    self._update_training_policy()
                    return
                if self.path == "/api/config/transports":
                    self._configure_transport()
                    return
                if self.path == "/api/config/public-post-controls":
                    self._configure_public_post_controls()
                    return
                if self.path.startswith("/api/config/transports/"):
                    self._change_transport_status()
                    return
                if self.path.startswith("/api/deliveries/"):
                    self._delivery_operation()
                    return
                if self.path == "/api/config/capabilities":
                    self._grant_capability()
                    return
                if self.path.startswith("/api/config/capabilities/") and self.path.endswith(
                    "/revoke"
                ):
                    self._revoke_capability()
                    return
                if self.path == "/api/config/common-knowledge/trust":
                    self._trust_common_knowledge_key()
                    return
                if self.path == "/api/config/common-knowledge/import":
                    self._import_common_knowledge()
                    return
                if self.path == "/api/config/common-knowledge/peers":
                    self._register_common_knowledge_peer()
                    return
                if self.path.startswith(
                    "/api/config/common-knowledge/peers/"
                ) and self.path.endswith("/sync"):
                    self._sync_common_knowledge_peer()
                    return
                if self.path.startswith("/api/config/common-knowledge/") and self.path.endswith(
                    "/revoke"
                ):
                    self._revoke_common_knowledge()
                    return
                if self.path == "/api/public-posts/captcha":
                    self._issue_public_post_captcha()
                    return
                if self.path == "/api/public-posts":
                    if self.headers.get_content_type() != "application/json":
                        self._discard_small_request_body()
                        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                        return
                    payload = self._request_json()
                    if payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_public_post"},
                        )
                        return
                    try:
                        controls = owner.public_post_controls()
                        idempotency_key = payload.get("idempotency_key")
                        if idempotency_key is not None and not isinstance(idempotency_key, str):
                            raise TypeError("idempotency_key must be text")
                        proposal = PublicPostInput.model_validate(
                            {
                                key: value
                                for key, value in payload.items()
                                if key not in {"idempotency_key", "captcha_id", "captcha_answer"}
                            }
                        )
                        post_record = owner.public_posts.create(
                            owner.kernel.subject_id,
                            proposal,
                            idempotency_key=idempotency_key,
                            client_ip=owner.client_ip(
                                str(self.client_address[0]), self.headers.get("X-Forwarded-For")
                            ),
                            captcha_id=payload.get("captcha_id"),
                            captcha_answer=payload.get("captcha_answer"),
                            rate_limit_per_hour=controls["rate_limit_per_hour"],
                            queue_cap=controls["queue_cap"],
                            storage_cap_bytes=controls["storage_cap_bytes"],
                        )
                    except PublicPostCaptchaError:
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_captcha"})
                        return
                    except PublicPostRateLimitError:
                        self._json(
                            HTTPStatus.TOO_MANY_REQUESTS,
                            {"error": "public_post_rate_limited"},
                            retry_after=3600,
                        )
                        return
                    except PublicPostQueueFullError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_queue_full"},
                            retry_after=60,
                        )
                        return
                    except PublicPostCapacityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_capacity_full"},
                            retry_after=60,
                        )
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    except (ValueError, TypeError):
                        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post"})
                        return
                    self._json(
                        HTTPStatus.CREATED,
                        {"post_id": post_record.post_id, "status": post_record.status},
                    )
                    return
                if self.path.startswith("/api/admin/public-posts/") and self.path.rsplit("/", 1)[
                    -1
                ] in {"publish", "reject", "archive"}:
                    if not self._authorized():
                        self._discard_small_request_body()
                        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                        return
                    if self.headers.get_content_type() != "application/json":
                        self._discard_small_request_body()
                        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                        return
                    payload = self._request_json()
                    if payload is None:
                        return
                    if not isinstance(payload, dict):
                        self._json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "invalid_public_post_moderation"},
                        )
                        return
                    post_id, operation = self.path.removeprefix("/api/admin/public-posts/").rsplit(
                        "/", 1
                    )
                    try:
                        reason = payload.get("reason")
                        expected_status = payload.get("expected_status")
                        idempotency_key = payload.get("idempotency_key")
                        if (
                            not isinstance(reason, str)
                            or (
                                expected_status is not None and not isinstance(expected_status, str)
                            )
                            or (
                                idempotency_key is not None and not isinstance(idempotency_key, str)
                            )
                        ):
                            raise TypeError("moderation reason must be text")
                        actor = self._actor()
                        record = owner.public_posts.moderate(
                            post_id,
                            subject_id=owner.kernel.subject_id,
                            status={
                                "publish": "published",
                                "reject": "rejected",
                                "archive": "archived",
                            }[operation],
                            actor=actor,
                            reason=reason,
                            expected_status=expected_status,
                            idempotency_key=idempotency_key,
                        )
                        try:
                            owner.audit_admin_event(
                                "public_post_moderated",
                                actor,
                                {
                                    "post_id": post_id,
                                    "operation": operation,
                                    "reason": payload.get("reason"),
                                    "status": record.status,
                                },
                            )
                        except Exception:
                            LOGGER.exception("public post moderation audit failed")
                    except NotFoundError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "public_post_not_found"})
                        return
                    except PublicPostConflictError:
                        self._json(
                            HTTPStatus.CONFLICT,
                            {"error": "public_post_moderation_conflict"},
                        )
                        return
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "public_post_integrity_unavailable"},
                            retry_after=60,
                        )
                        return
                    except (ValueError, TypeError):
                        self._json(
                            HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_moderation"}
                        )
                        return
                    self._json(HTTPStatus.OK, record.__dict__)
                    return
                if self.path != "/api/interactions":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    channel = payload.get("channel", "web")
                    counterparty = payload.get("counterparty", "web-user")
                    content = payload.get("content")
                    idempotency_key = payload.get("idempotency_key")
                    if not isinstance(channel, str) or not isinstance(counterparty, str):
                        raise TypeError("interaction metadata must be text")
                    if channel != "web" or counterparty != "web-user":
                        raise ValueError("web mailbox identity cannot be overridden")
                    if not isinstance(content, str):
                        raise TypeError("interaction content must be text")
                    if idempotency_key is not None and not isinstance(idempotency_key, str):
                        raise TypeError("idempotency key must be text")
                    interaction_record = owner.interactions.receive(
                        owner.kernel.subject_id,
                        channel,
                        counterparty,
                        content,
                        idempotency_key=idempotency_key,
                    )
                except (ValueError, TypeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_interaction"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "interaction_id": interaction_record.interaction_id,
                        "status": interaction_record.status,
                    },
                )

            def _lifecycle_control(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                controls = owner.operator_controls
                if controls is None:
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "operator_controls_unavailable"},
                    )
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if set(payload) != {"reason"} or not isinstance(payload.get("reason"), str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_lifecycle_control"},
                    )
                    return
                operation = self.path.rsplit("/", 1)[-1]
                try:
                    mutation = {
                        "pause": controls.pause,
                        "resume": controls.resume,
                        "reset": controls.reset,
                    }[operation](
                        actor=self._actor(),
                        reason=str(payload["reason"]),
                    )
                except OperatorControlConflict as error:
                    self._json(self._operator_error_status(error), {"error": error.code})
                    return
                self._json(HTTPStatus.OK, mutation.public())

            def _reconcile_action(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                controls = owner.operator_controls
                if controls is None:
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "operator_controls_unavailable"},
                    )
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                allowed = {"reason", "outcome", "status", "result", "evidence"}
                if not set(payload) <= allowed or not isinstance(payload.get("reason"), str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_action_reconciliation"},
                    )
                    return
                outcome = payload.get("outcome", payload.get("status"))
                if (
                    "outcome" in payload
                    and "status" in payload
                    and payload["outcome"] != payload["status"]
                ):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_action_reconciliation"},
                    )
                    return
                result = payload.get("result", payload.get("evidence", {}))
                action_id = self.path.removeprefix("/api/admin/actions/").removesuffix("/reconcile")
                if not action_id or "/" in action_id or not isinstance(outcome, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_action_reconciliation"},
                    )
                    return
                try:
                    record = controls.reconcile_action(
                        action_id,
                        actor=self._actor(),
                        reason=str(payload["reason"]),
                        outcome=outcome,
                        result=result,
                    )
                except OperatorControlNotFound:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "action_not_found"})
                    return
                except OperatorControlConflict as error:
                    self._json(self._operator_error_status(error), {"error": error.code})
                    return
                self._json(HTTPStatus.OK, record)

            def _reconcile_model_call(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                controls = owner.operator_controls
                if controls is None:
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "operator_controls_unavailable"},
                    )
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                allowed = {"reason", "outcome", "status", "response", "evidence"}
                outcome = payload.get("outcome", payload.get("status"))
                response = payload.get("response", payload.get("evidence"))
                if (
                    not set(payload) <= allowed
                    or not isinstance(payload.get("reason"), str)
                    or not isinstance(outcome, str)
                    or (
                        "outcome" in payload
                        and "status" in payload
                        and payload["outcome"] != payload["status"]
                    )
                ):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_model_call_reconciliation"},
                    )
                    return
                call_id = self.path.removeprefix("/api/admin/model-calls/").removesuffix(
                    "/reconcile"
                )
                if not call_id or "/" in call_id:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_model_call_reconciliation"},
                    )
                    return
                try:
                    record = controls.reconcile_model_call(
                        call_id,
                        actor=self._actor(),
                        reason=str(payload["reason"]),
                        outcome=outcome,
                        response=response,
                    )
                except OperatorControlNotFound:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "model_call_not_found"})
                    return
                except OperatorControlConflict as error:
                    self._json(self._operator_error_status(error), {"error": error.code})
                    return
                self._json(HTTPStatus.OK, record)

            def _create_wallet_bounty(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                try:
                    record = owner.wallet_economy.create_bounty(
                        owner.kernel.subject_id,
                        BountyInput.model_validate(payload),
                        actor=self._actor(),
                    )
                    self._json(HTTPStatus.CREATED, asdict(record))
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reference_not_found"})
                except Exception as error:
                    self._json(
                        HTTPStatus.CONFLICT
                        if "idempotency" in str(error)
                        else HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_bounty"},
                    )

            def _create_wallet_reward(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                if set(payload) - {
                    "assistance_request_id",
                    "acceptance_criteria",
                    "network_id",
                    "asset_id",
                    "reward_amount",
                    "opens_at",
                    "expires_at",
                    "max_submissions",
                    "reward_slots",
                    "idempotency_key",
                }:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward"})
                    return
                try:
                    record = owner.wallet_rewards.create(
                        owner.kernel.subject_id,
                        RewardWorkflowInput.model_validate(payload),
                        actor=self._actor(),
                    )
                    self._json(HTTPStatus.CREATED, record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_reference_not_found"})
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_reward_integrity_unavailable"},
                        retry_after=60,
                    )
                except Exception as error:
                    self._json(
                        HTTPStatus.CONFLICT
                        if "idempotency" in str(error)
                        else HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_reward"},
                    )

            def _wallet_reward_operation(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                parts = self.path.removeprefix("/api/admin/wallet-rewards/").split("/")
                if len(parts) != 2 or not all(parts):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_operation"})
                    return
                resource_id, operation = parts
                payload = self._request_json()
                if payload is None:
                    return
                if operation == "publish" and payload:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_operation"})
                    return
                if operation == "resume" and set(payload) != {"reason"}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_operation"})
                    return
                try:
                    if operation == "publish":
                        record: Any = owner.wallet_rewards.publish(
                            resource_id, owner.kernel.subject_id, actor=self._actor()
                        )
                    elif operation == "resume":
                        reason = payload.get("reason")
                        if not isinstance(reason, str):
                            raise ValueError("resume reason is invalid")
                        record = owner.wallet_rewards.resume(
                            resource_id,
                            owner.kernel.subject_id,
                            actor=self._actor(),
                            reason=reason,
                        )
                    else:
                        raise ValueError("unknown reward operation")
                    self._json(HTTPStatus.OK, record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_not_found"})
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_reward_integrity_unavailable"},
                        retry_after=60,
                    )
                except Exception:
                    self._json(HTTPStatus.CONFLICT, {"error": "wallet_reward_transition_conflict"})

            def _decide_wallet_reward_submission(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                submission_id = self.path.removeprefix(
                    "/api/admin/wallet-reward-submissions/"
                ).removesuffix("/decide")
                if not submission_id or "/" in submission_id:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_reward_submission"},
                    )
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.wallet_rewards.decide(
                        submission_id,
                        owner.kernel.subject_id,
                        RewardEvidenceDecisionInput.model_validate(payload),
                        actor=self._actor(),
                    )
                    self._json(HTTPStatus.OK, asdict(record))
                except NotFoundError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "wallet_reward_submission_not_found"},
                    )
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_reward_integrity_unavailable"},
                        retry_after=60,
                    )
                except Exception as error:
                    self._json(
                        HTTPStatus.CONFLICT
                        if "idempotency" in str(error)
                        else HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_reward_decision"},
                    )

            def _resolve_wallet_reward_incident(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                incident_id = self.path.removeprefix(
                    "/api/admin/wallet-reward-incidents/"
                ).removesuffix("/resolve")
                if not incident_id or "/" in incident_id:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_incident"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if set(payload) != {"resolution", "idempotency_key"}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_incident"})
                    return
                try:
                    record = owner.wallet_rewards.resolve_incident(
                        incident_id,
                        owner.kernel.subject_id,
                        RewardIncidentResolutionInput.model_validate(payload),
                        actor=self._actor(),
                    )
                    self._json(HTTPStatus.OK, record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_incident_not_found"})
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_reward_integrity_unavailable"},
                        retry_after=60,
                    )
                except Exception as error:
                    self._json(
                        (
                            HTTPStatus.CONFLICT
                            if "idempotency" in str(error)
                            else HTTPStatus.BAD_REQUEST
                        ),
                        {"error": "invalid_wallet_reward_incident_resolution"},
                    )

            def _submit_wallet_reward(self) -> None:
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                workflow_id = self.path.removeprefix("/api/wallet-rewards/").removesuffix(
                    "/submissions"
                )
                if not workflow_id or "/" in workflow_id:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward"})
                    return
                try:
                    record = owner.wallet_rewards.submit_public(
                        workflow_id,
                        owner.kernel.subject_id,
                        RewardPublicSubmissionInput.model_validate(payload),
                        client_ip=str(self.client_address[0]),
                    )
                    self._json(HTTPStatus.CREATED, asdict(record))
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_reward_not_found"})
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_reward_integrity_unavailable"},
                        retry_after=60,
                    )
                except Exception as error:
                    self._json(
                        HTTPStatus.CONFLICT
                        if "idempotency" in str(error)
                        else HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_reward_submission"},
                    )

            def _execute_wallet_rewards(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                if set(payload) - {"limit"}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_execution"})
                    return
                limit = payload.get("limit", 20)
                if type(limit) is not int or not 1 <= limit <= 100:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_reward_execution"})
                    return
                try:
                    records = owner.wallet_rewards.execute_ready(
                        owner.kernel.subject_id, actor=self._actor(), limit=limit
                    )
                    self._json(HTTPStatus.OK, [asdict(record) for record in records])
                except WalletExecutionError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_execution_unavailable"},
                        retry_after=60,
                    )
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_reward_integrity_unavailable"},
                        retry_after=60,
                    )

            def _wallet_bounty_operation(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                parts = self.path.removeprefix("/api/admin/wallet-bounties/").split("/")
                if len(parts) != 2 or not all(parts):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty_operation"})
                    return
                bid, op = parts
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = {
                        "publish": owner.wallet_economy.publish_bounty,
                        "close": owner.wallet_economy.close_bounty,
                        "cancel": owner.wallet_economy.cancel_bounty,
                    }[op](bid, owner.kernel.subject_id, actor=self._actor())
                    self._json(HTTPStatus.OK, record.__dict__)
                except KeyError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty_operation"})
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
                except Exception:
                    self._json(HTTPStatus.CONFLICT, {"error": "wallet_bounty_transition_conflict"})

            def _submit_wallet_bounty(self) -> None:
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                bid = self.path.removeprefix("/api/bounties/").removesuffix("/submissions")
                if not bid or "/" in bid:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_bounty"})
                    return
                try:
                    record = owner.wallet_economy.submit(
                        bid,
                        owner.kernel.subject_id,
                        SubmissionInput.model_validate(payload),
                        client_ip=str(self.client_address[0]),
                    )
                    self._json(HTTPStatus.CREATED, record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_bounty_not_found"})
                except Exception as error:
                    self._json(
                        HTTPStatus.CONFLICT
                        if "idempotency" in str(error)
                        else HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_submission"},
                    )

            def _wallet_submission_operation(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                parts = self.path.removeprefix("/api/admin/wallet-submissions/").split("/")
                if len(parts) != 2:
                    self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_submission_operation"}
                    )
                    return
                sid, op = parts
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    if op == "accept":
                        record = owner.wallet_economy.decide_submission(
                            sid,
                            owner.kernel.subject_id,
                            accepted=True,
                            reason=str(payload.get("reason", "accepted")),
                            actor=self._actor(),
                        )
                    elif op == "reject":
                        record = owner.wallet_economy.decide_submission(
                            sid,
                            owner.kernel.subject_id,
                            accepted=False,
                            reason=str(payload.get("reason", "rejected")),
                            actor=self._actor(),
                        )
                    elif op == "withdraw":
                        record = owner.wallet_economy.withdraw_submission(
                            sid, owner.kernel.subject_id
                        )
                    else:
                        raise ValueError
                    self._json(HTTPStatus.OK, record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_submission_not_found"})
                except Exception:
                    self._json(
                        HTTPStatus.CONFLICT, {"error": "wallet_submission_transition_conflict"}
                    )

            def _wallet_order_operation(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_order_operation"})
                    return
                parts = self.path.removeprefix("/api/admin/wallet-orders/").split("/")
                if len(parts) != 2 or not all(parts):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_order_operation"})
                    return
                order_id, operation = parts
                execution_record: Any
                if operation in {"execute", "retry", "refund"}:
                    engine = owner.wallet_execution
                    if engine is None:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_execution_unavailable"},
                            retry_after=60,
                        )
                        return
                    try:
                        actor = self._actor()
                        if operation == "execute":
                            allowed = {"gas_limit", "max_fee_per_gas", "nonce"}
                            if set(payload) - allowed:
                                raise ValueError("invalid wallet execute payload")
                            kwargs: dict[str, Any] = {}
                            for key in allowed:
                                if key in payload:
                                    value = payload[key]
                                    if key in {"gas_limit", "nonce"} and (type(value) is not int):
                                        raise ValueError("wallet execution quantity is invalid")
                                    if key == "max_fee_per_gas" and not isinstance(value, str):
                                        raise ValueError("wallet execution fee is invalid")
                                    kwargs[key] = value
                            execution_record = engine.execute_order(
                                order_id, owner.kernel.subject_id, actor=actor, **kwargs
                            )
                        elif operation == "retry":
                            if set(payload) != {"reason"} or not isinstance(
                                payload.get("reason"), str
                            ):
                                raise ValueError("wallet retry reason is required")
                            execution_record = engine.retry_unknown(
                                order_id,
                                owner.kernel.subject_id,
                                actor=actor,
                                reason=payload["reason"],
                            )
                        else:
                            if set(payload) != {"reason"} or not isinstance(
                                payload.get("reason"), str
                            ):
                                raise ValueError("wallet refund reason is required")
                            execution_record = cast(
                                Any,
                                engine.refund(
                                    order_id,
                                    owner.kernel.subject_id,
                                    actor=actor,
                                    reason=payload["reason"],
                                ),
                            )
                        self._json(HTTPStatus.OK, execution_record.__dict__)
                    except NotFoundError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_order_not_found"})
                    except IntegrityError:
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "wallet_execution_integrity_unavailable"},
                            retry_after=60,
                        )
                    except InvalidTransitionError:
                        self._json(
                            HTTPStatus.CONFLICT, {"error": "wallet_order_transition_conflict"}
                        )
                    except (WalletExecutionError, ValueError, TypeError):
                        self._json(
                            HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_order_execution"}
                        )
                    return
                try:
                    if operation == "confirm":
                        order_record = owner.wallet_economy.confirm_order(
                            order_id, owner.kernel.subject_id, actor=self._actor()
                        )
                    elif operation == "reject":
                        order_record = owner.wallet_economy.reject_order(
                            order_id,
                            owner.kernel.subject_id,
                            actor=self._actor(),
                            reason=str(payload.get("reason", "rejected")),
                        )
                    elif operation == "cancel":
                        order_record = owner.wallet_economy.cancel_order(
                            order_id,
                            owner.kernel.subject_id,
                            actor=self._actor(),
                            reason=str(payload.get("reason", "cancelled")),
                        )
                    else:
                        raise ValueError
                    self._json(HTTPStatus.OK, order_record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_order_not_found"})
                except Exception:
                    self._json(HTTPStatus.CONFLICT, {"error": "wallet_order_transition_conflict"})

            def _wallet_execution_operation(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                engine = owner.wallet_execution
                if engine is None:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_execution_unavailable"},
                        retry_after=60,
                    )
                    return
                parts = self.path.removeprefix("/api/admin/wallet-executions/").split("/")
                if len(parts) != 2 or not parts[0] or parts[1] != "receipt":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_path"})
                    return
                if payload:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_receipt_request"})
                    return
                try:
                    execution_record = engine.poll_receipt(
                        parts[0], owner.kernel.subject_id, actor=self._actor()
                    )
                    self._json(HTTPStatus.OK, execution_record.__dict__)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_execution_not_found"})
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_execution_integrity_unavailable"},
                        retry_after=60,
                    )
                except InvalidTransitionError:
                    self._json(
                        HTTPStatus.CONFLICT, {"error": "wallet_execution_transition_conflict"}
                    )
                except (WalletExecutionError, ValueError, TypeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_receipt_request"})

            def _recover_wallet_executions(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                if set(payload) - {"limit"}:
                    self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_recovery"}
                    )
                    return
                engine = owner.wallet_execution
                if engine is None:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_execution_unavailable"},
                        retry_after=60,
                    )
                    return
                limit_value = payload.get("limit", 100)
                if type(limit_value) is not int:
                    self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_recovery"}
                    )
                    return
                try:
                    records = engine.recover_inflight(
                        owner.kernel.subject_id, actor=self._actor(), limit=limit_value
                    )
                    self._json(HTTPStatus.OK, [record.__dict__ for record in records])
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_execution_integrity_unavailable"},
                        retry_after=60,
                    )
                except (ValueError, TypeError):
                    self._json(
                        HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_execution_recovery"}
                    )

            def _update_wallet_policy(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                if not isinstance(payload, dict) or "expected_version" not in payload:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_policy"})
                    return
                try:
                    expected = int(payload.pop("expected_version"))
                    record = owner.wallet_economy.update_policy(
                        owner.kernel.subject_id,
                        PaymentPolicyInput.model_validate(payload),
                        expected_version=expected,
                        actor=self._actor(),
                    )
                    self._json(HTTPStatus.OK, record.__dict__)
                except Exception as error:
                    self._json(
                        HTTPStatus.CONFLICT if "version" in str(error) else HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_policy"},
                    )

            def _configure_wallet_network(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.wallets.register_network(
                        owner.kernel.subject_id,
                        WalletNetworkInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_network"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "network_id": record.network_id,
                        "label": record.label,
                        "chain_family": record.chain_family,
                        "chain_id": record.chain_id,
                        "status": record.status,
                    },
                )

            def _configure_wallet_asset(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.wallets.register_asset(
                        owner.kernel.subject_id,
                        WalletAssetInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_network_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_asset"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "asset_id": record.asset_id,
                        "network_id": record.network_id,
                        "asset_type": record.asset_type,
                        "symbol": record.symbol,
                        "status": record.status,
                    },
                )

            def _configure_wallet_address(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.wallets.register_address(
                        owner.kernel.subject_id,
                        WalletAddressInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_network_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wallet_address"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "address_id": record.address_id,
                        "network_id": record.network_id,
                        "label": record.label,
                        "address": record.address,
                        "status": record.status,
                    },
                )

            def _revoke_wallet_resource(self, resource_type: str) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                prefix, identifier_field = {
                    "network": ("/api/config/wallet-networks/", "network_id"),
                    "asset": ("/api/config/wallet-assets/", "asset_id"),
                    "address": ("/api/config/wallet-addresses/", "address_id"),
                }[resource_type]
                resource_id = self.path.removeprefix(prefix).removesuffix("/revoke")
                reason = payload.get("reason")
                if (
                    set(payload) != {"reason"}
                    or not isinstance(reason, str)
                    or not resource_id
                    or "/" in resource_id
                ):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"invalid_wallet_{resource_type}"},
                    )
                    return
                try:
                    if resource_type == "network":
                        resource_status = owner.wallets.revoke_network(
                            resource_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        ).status
                    elif resource_type == "asset":
                        resource_status = owner.wallets.revoke_asset(
                            resource_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        ).status
                    else:
                        resource_status = owner.wallets.revoke_address(
                            resource_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        ).status
                except NotFoundError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": f"wallet_{resource_type}_not_found"},
                    )
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": f"invalid_wallet_{resource_type}"},
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    {identifier_field: resource_id, "status": resource_status},
                )

            @staticmethod
            def _wallet_observation_health_query(
                query: dict[str, list[str]],
            ) -> tuple[str, int, str | None, str | None]:
                allowed = {"group_by", "limit", "network_id", "source"}
                if set(query) - allowed:
                    raise ValueError("wallet observation health query is invalid")

                def one(name: str) -> str | None:
                    values = query.get(name)
                    if values is None:
                        return None
                    if len(values) != 1:
                        raise ValueError("wallet observation health query is invalid")
                    return values[0]

                raw_group_value = one("group_by")
                raw_group = "network" if raw_group_value is None else raw_group_value
                if raw_group not in {"network", "source"}:
                    raise ValueError("wallet observation health group is invalid")
                raw_limit = one("limit")
                if raw_limit is None:
                    bounded_limit = WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT
                elif (
                    not raw_limit.isascii()
                    or not raw_limit.isdecimal()
                    or not 1 <= int(raw_limit) <= WALLET_BALANCE_OBSERVATION_MAX_GROUPS
                ):
                    raise ValueError("wallet observation health limit is invalid")
                else:
                    bounded_limit = int(raw_limit)
                network_id = one("network_id")
                source = one("source")
                if network_id == "" or source == "":
                    raise ValueError("wallet observation health filter is invalid")
                return raw_group, bounded_limit, network_id, source

            @staticmethod
            def _wallet_balance_history_query(
                query: dict[str, list[str]],
            ) -> tuple[int, str | None, str | None, str | None, str | None]:
                allowed = {"limit", "cursor", "network_id", "asset_id", "address_id"}
                if set(query) - allowed:
                    raise ValueError("wallet balance history query is invalid")

                def one(name: str) -> str | None:
                    values = query.get(name)
                    if values is None:
                        return None
                    if len(values) != 1:
                        raise ValueError("wallet balance history query is invalid")
                    return values[0]

                raw_limit = one("limit")
                if raw_limit is None:
                    limit = 100
                elif (
                    not raw_limit.isascii()
                    or not raw_limit.isdecimal()
                    or not 1 <= int(raw_limit) <= WALLET_BALANCE_HISTORY_MAX_PAGE_SIZE
                ):
                    raise ValueError("wallet balance history limit is invalid")
                else:
                    limit = int(raw_limit)
                cursor = one("cursor")
                if cursor is not None and (
                    not cursor or len(cursor) > WALLET_BALANCE_HISTORY_MAX_CURSOR_LENGTH
                ):
                    raise ValueError("wallet balance history cursor is invalid")
                return limit, cursor, one("network_id"), one("asset_id"), one("address_id")

            @staticmethod
            def _wallet_acquisition_id(path: str, operation: str) -> str | None:
                prefix = "/api/admin/wallet-acquisitions/"
                if not path.startswith(prefix) or not path.endswith(f"/{operation}"):
                    return None
                run_id = path.removeprefix(prefix).removesuffix(f"/{operation}")
                if not run_id or "/" in run_id:
                    return None
                return run_id

            @staticmethod
            def _wallet_acquisition_statuses(
                query: dict[str, list[str]],
            ) -> tuple[str, ...] | None:
                values = query.get("status")
                if not values:
                    return None
                raw = values[0]
                statuses = tuple(value.strip() for value in raw.split(","))
                if not statuses or any(
                    not status or status not in ACQUISITION_RUN_STATES for status in statuses
                ):
                    raise ValueError("invalid wallet acquisition status filter")
                if len(set(statuses)) != len(statuses):
                    raise ValueError("duplicate wallet acquisition status filter")
                return statuses

            def _wallet_payload(self) -> dict[str, Any] | None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return None
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return None
                return self._request_json()

            def _enqueue_wallet_acquisition(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                required = {"asset_id", "address_id"}
                optional = {"idempotency_key", "max_attempts", "not_before"}
                if set(payload) - required - optional or not required <= set(payload):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition"},
                    )
                    return
                asset_id = payload.get("asset_id")
                address_id = payload.get("address_id")
                if not isinstance(asset_id, str) or not isinstance(address_id, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition"},
                    )
                    return
                try:
                    record = owner.wallet_acquisitions.enqueue(
                        owner.kernel.subject_id,
                        asset_id=asset_id,
                        address_id=address_id,
                        actor=self._actor(),
                        idempotency_key=payload.get("idempotency_key"),
                        max_attempts=payload.get("max_attempts", 5),
                        not_before=payload.get("not_before"),
                    )
                except NotFoundError:
                    self._json(
                        HTTPStatus.NOT_FOUND,
                        {"error": "wallet_acquisition_target_not_found"},
                    )
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except WalletAcquisitionConflictError:
                    self._json(
                        HTTPStatus.CONFLICT,
                        {"error": "wallet_acquisition_conflict"},
                    )
                    return
                except ValueError:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition"},
                    )
                    return
                except (TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition"},
                    )
                    return
                self._json(HTTPStatus.ACCEPTED, _wallet_acquisition_run_payload(record))

            def _run_wallet_acquisitions(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                if set(payload) - {"limit"}:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_run"},
                    )
                    return
                limit = payload.get("limit", 1)
                if isinstance(limit, bool) or not isinstance(limit, int):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_run"},
                    )
                    return
                try:
                    records = owner.wallet_acquisition_runner.run_due(
                        owner.kernel.subject_id,
                        actor=self._actor(),
                        limit=limit,
                    )
                except WalletAcquisitionRunError as error:
                    self._json(HTTPStatus.CONFLICT, {"error": error.code})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_run"},
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    {
                        "processed": len(records),
                        "runs": [_wallet_acquisition_run_payload(record) for record in records],
                    },
                )

            def _retry_wallet_acquisition(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                run_id = self._wallet_acquisition_id(self.path, "retry")
                if run_id is None or set(payload) - {"reason", "not_before"}:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_retry"},
                    )
                    return
                reason = payload.get("reason")
                if not isinstance(reason, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_retry"},
                    )
                    return
                try:
                    record = owner.wallet_acquisitions.retry_unknown(
                        run_id,
                        subject_id=owner.kernel.subject_id,
                        actor=self._actor(),
                        reason=reason,
                        not_before=payload.get("not_before"),
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_acquisition_not_found"})
                    return
                except WalletAcquisitionConflictError:
                    self._json(HTTPStatus.CONFLICT, {"error": "wallet_acquisition_conflict"})
                    return
                except WalletAcquisitionRunError as error:
                    status = (
                        HTTPStatus.BAD_REQUEST
                        if error.code.startswith("wallet_acquisition_invalid_")
                        else HTTPStatus.CONFLICT
                    )
                    self._json(status, {"error": error.code})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_retry"},
                    )
                    return
                self._json(HTTPStatus.OK, _wallet_acquisition_run_payload(record))

            def _cancel_wallet_acquisition(self) -> None:
                payload = self._wallet_payload()
                if payload is None:
                    return
                run_id = self._wallet_acquisition_id(self.path, "cancel")
                if run_id is None or set(payload) != {"reason"}:
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_cancel"},
                    )
                    return
                reason = payload.get("reason")
                if not isinstance(reason, str):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_cancel"},
                    )
                    return
                try:
                    record = owner.wallet_acquisitions.cancel(
                        run_id,
                        subject_id=owner.kernel.subject_id,
                        actor=self._actor(),
                        reason=reason,
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "wallet_acquisition_not_found"})
                    return
                except WalletAcquisitionRunError as error:
                    self._json(HTTPStatus.CONFLICT, {"error": error.code})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "wallet_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_wallet_acquisition_cancel"},
                    )
                    return
                self._json(HTTPStatus.OK, _wallet_acquisition_run_payload(record))

            def _configure_search_provider(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    proposal = SearchProviderInput.model_validate(payload)
                    record = owner.search_providers.configure(
                        owner.kernel.subject_id,
                        proposal,
                        actor=self._actor(),
                    )
                except (ValueError, TypeError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "config_id": record.config_id,
                        "provider_type": record.provider_type,
                        "label": record.label,
                        "status": record.status,
                    },
                )

            def _configure_transport(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.transports.configure(
                        owner.kernel.subject_id,
                        TransportInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_transport"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "transport_id": record.transport_id,
                        "channel": record.channel,
                        "label": record.label,
                        "status": record.status,
                    },
                )

            def _configure_public_post_controls(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                fields = (
                    "rate_limit_per_hour",
                    "queue_cap",
                    "captcha_ttl_seconds",
                    "captcha_max_attempts",
                    "storage_cap_bytes",
                    "captcha_issue_limit_per_hour",
                    "captcha_global_rate_per_minute",
                )
                if any(type(payload.get(field)) is not int for field in fields) or not isinstance(
                    payload.get("captcha_mode"), str
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_controls"})
                    return
                try:
                    controls = owner.public_posts.configure_controls(
                        owner.kernel.subject_id,
                        rate_limit_per_hour=payload["rate_limit_per_hour"],
                        queue_cap=payload["queue_cap"],
                        captcha_ttl_seconds=payload["captcha_ttl_seconds"],
                        captcha_max_attempts=payload["captcha_max_attempts"],
                        captcha_mode=payload["captcha_mode"],
                        storage_cap_bytes=payload["storage_cap_bytes"],
                        captcha_issue_limit_per_hour=payload["captcha_issue_limit_per_hour"],
                        captcha_global_rate_per_minute=payload["captcha_global_rate_per_minute"],
                        actor=self._actor(),
                    )
                    controls = owner.public_post_controls()
                    owner.audit_admin_event(
                        "public_post_controls_updated",
                        self._actor(),
                        {
                            key: controls[key]
                            for key in (
                                "rate_limit_per_hour",
                                "queue_cap",
                                "captcha_ttl_seconds",
                                "captcha_max_attempts",
                                "captcha_mode",
                                "storage_cap_bytes",
                                "captcha_issue_limit_per_hour",
                                "captcha_global_rate_per_minute",
                            )
                        },
                    )
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_post_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_public_post_controls"})
                    return
                self._json(HTTPStatus.OK, controls)

            def _issue_public_post_captcha(self) -> None:
                """Issue a stateful challenge only to a same-origin JSON POST."""
                fetch_site = self.headers.get("Sec-Fetch-Site", "").strip().casefold()
                origin = self.headers.get("Origin", "").strip()
                host = self.headers.get("Host", "").strip().casefold()
                if fetch_site == "cross-site" or (
                    origin
                    and (
                        urlsplit(origin).scheme not in {"http", "https"}
                        or urlsplit(origin).netloc.casefold() != host
                    )
                ):
                    self._discard_small_request_body()
                    self._json(HTTPStatus.FORBIDDEN, {"error": "cross_site_request_forbidden"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict) or payload:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_captcha_request"})
                    return
                try:
                    controls = owner.public_post_controls()
                    challenge = owner.public_posts.issue_captcha(
                        owner.kernel.subject_id,
                        owner.client_ip(
                            str(self.client_address[0]), self.headers.get("X-Forwarded-For")
                        ),
                        ttl_seconds=controls["captcha_ttl_seconds"],
                        max_attempts=controls["captcha_max_attempts"],
                        mode=controls["captcha_mode"],
                        issue_limit_per_hour=controls["captcha_issue_limit_per_hour"],
                        global_rate_per_minute=controls["captcha_global_rate_per_minute"],
                        queue_cap=controls["queue_cap"],
                        storage_cap_bytes=controls["storage_cap_bytes"],
                    )
                except PublicPostRateLimitError:
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "captcha_rate_limited"},
                        retry_after=60,
                    )
                    return
                except PublicPostQueueFullError:
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "public_post_queue_full"},
                        retry_after=60,
                    )
                    return
                except PublicPostCapacityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_post_capacity_full"},
                        retry_after=60,
                    )
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_post_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_captcha_request"})
                    return
                self._json(HTTPStatus.OK, challenge)

            def _change_transport_status(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                parts = self.path.removeprefix("/api/config/transports/").split("/")
                reason = payload.get("reason")
                if len(parts) != 2 or not isinstance(reason, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_transport"})
                    return
                transport_id, operation = parts
                try:
                    if operation == "enable":
                        record = owner.transports.enable(
                            transport_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        )
                    elif operation == "disable":
                        record = owner.transports.disable(
                            transport_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        )
                    elif operation == "revoke":
                        record = owner.transports.revoke(
                            transport_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        )
                    else:
                        raise ValueError("unsupported transport operation")
                except (ValueError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_transport"})
                    return
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "transport_not_found"})
                    return
                self._json(
                    HTTPStatus.OK,
                    {"transport_id": record.transport_id, "status": record.status},
                )

            def _delivery_operation(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                parts = self.path.removeprefix("/api/deliveries/").strip("/").split("/")
                reason = payload.get("reason")
                if len(parts) != 2 or not isinstance(reason, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_delivery_operation"})
                    return
                delivery_id, operation = parts
                try:
                    if operation == "lookup":
                        record = owner.run_async_from_handler(
                            owner.deliveries.lookup_unknown(
                                delivery_id,
                                subject_id=owner.kernel.subject_id,
                                actor=self._actor(),
                                reason=reason,
                            )
                        )
                    elif operation == "reconcile":
                        outcome = payload.get("status")
                        provider_message_id = payload.get("provider_message_id")
                        evidence = payload.get("evidence", {})
                        if outcome not in {"delivered", "failed", "cancelled"}:
                            raise ValueError("invalid delivery reconciliation status")
                        if provider_message_id is not None and not isinstance(
                            provider_message_id, str
                        ):
                            raise TypeError("provider message id must be text")
                        if not isinstance(evidence, dict):
                            raise TypeError("delivery reconciliation evidence must be an object")
                        record = owner.deliveries.reconcile_unknown(
                            delivery_id,
                            cast(Literal["delivered", "failed", "cancelled"], outcome),
                            subject_id=owner.kernel.subject_id,
                            actor=self._actor(),
                            reason=reason,
                            provider_message_id=provider_message_id,
                            evidence=evidence,
                        )
                    else:
                        raise ValueError("unsupported delivery operation")
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "delivery_not_found"})
                    return
                except InvalidTransitionError:
                    self._json(
                        HTTPStatus.CONFLICT,
                        {"error": "delivery_reconciliation_conflict"},
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_delivery_operation"},
                    )
                    return
                self._json(HTTPStatus.OK, record.__dict__)

            def _grant_capability(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.capabilities.grant(
                        owner.kernel.subject_id,
                        CapabilityGrant.model_validate(payload),
                        actor=self._actor(),
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
                    return
                self._json(HTTPStatus.CREATED, record.__dict__)

            def _revoke_capability(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
                    return
                reason = payload.get("reason")
                grant_id = self.path.removeprefix("/api/config/capabilities/").removesuffix(
                    "/revoke"
                )
                if not isinstance(reason, str) or not reason.strip():
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
                    return
                try:
                    record = owner.capabilities.revoke(
                        grant_id,
                        reason=reason,
                        actor=self._actor(),
                        subject_id=owner.kernel.subject_id,
                    )
                except (ValueError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_capability"})
                    return
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "capability_not_found"})
                    return
                self._json(HTTPStatus.OK, record.__dict__)

            def _trust_common_knowledge_key(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                public_key = payload.get("public_key")
                label = payload.get("label")
                if not isinstance(public_key, str) or not isinstance(label, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_trusted_key"})
                    return
                try:
                    key_id = owner.common_knowledge.trust_key(
                        public_key,
                        label=label,
                        actor=self._actor(),
                    )
                except (ValueError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_trusted_key"})
                    return
                self._json(HTTPStatus.CREATED, {"key_id": key_id})

            def _import_common_knowledge(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.common_knowledge.import_package(payload)
                except PermissionError:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "publisher_not_trusted"})
                    return
                except (ValueError, TypeError, IntegrityError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_package"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {"package_id": record.package_id, "status": "quarantined"},
                )

            def _register_common_knowledge_peer(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.common_knowledge.register_peer(
                        CommonKnowledgePeerInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_peer"})
                    return
                self._json(HTTPStatus.CREATED, record.__dict__)

            def _sync_common_knowledge_peer(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                self._discard_small_request_body()
                peer_id = self.path.removeprefix(
                    "/api/config/common-knowledge/peers/"
                ).removesuffix("/sync")
                if not peer_id or "/" in peer_id:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_peer"})
                    return
                try:
                    result = owner.common_knowledge.sync_peer(peer_id)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "knowledge_peer_not_found"})
                    return
                except CommonKnowledgeSyncError as error:
                    self._json(HTTPStatus.BAD_GATEWAY, {"error": error.code})
                    return
                except PermissionError:
                    self._json(HTTPStatus.CONFLICT, {"error": "knowledge_peer_disabled"})
                    return
                self._json(HTTPStatus.OK, result.__dict__)

            def _revoke_common_knowledge(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                reason = payload.get("reason")
                package_id = self.path.removeprefix("/api/config/common-knowledge/").removesuffix(
                    "/revoke"
                )
                if not isinstance(reason, str) or not package_id or "/" in package_id:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_knowledge_revoke"})
                    return
                try:
                    owner.common_knowledge.revoke(
                        package_id,
                        reason=reason,
                        actor=self._actor(),
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "knowledge_package_not_found"})
                    return
                except PermissionError:
                    self._json(HTTPStatus.FORBIDDEN, {"error": "knowledge_revoke_forbidden"})
                    return
                except ValueError:
                    self._json(HTTPStatus.CONFLICT, {"error": "knowledge_already_revoked"})
                    return
                self._json(HTTPStatus.OK, {"package_id": package_id, "status": "revoked"})

            def _configure_model_resource(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                try:
                    record = owner.cognitive_resources.configure(
                        owner.kernel.subject_id,
                        CognitiveResourceGroupInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except ValueError as error:
                    if str(error) == "cognitive resource label already exists in this pool":
                        self._json(
                            HTTPStatus.CONFLICT,
                            {"error": "model_resource_label_exists"},
                        )
                        return
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "model_resource_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "group_id": record.group_id,
                        "pool": record.pool,
                        "label": record.label,
                        "status": record.status,
                    },
                )

            def _configure_embedding_resource(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    record = owner.embedding_resources.configure(
                        owner.kernel.subject_id,
                        EmbeddingResourceInput.model_validate(payload),
                        actor=self._actor(),
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_embedding_resource"})
                    return
                self._json(
                    HTTPStatus.CREATED,
                    {
                        "config_id": record.config_id,
                        "label": record.label,
                        "model": record.model,
                        "status": record.status,
                    },
                )

            def _test_model_resource(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
                    return
                reason = payload.get("reason")
                group_id = self.path.removeprefix("/api/config/model-resources/").removesuffix(
                    "/test"
                )
                if (
                    set(payload) != {"reason"}
                    or not group_id
                    or "/" in group_id
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
                    return
                gateway = owner.cognition_gateway
                if gateway is None:
                    self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "cognition_unavailable"})
                    return
                try:
                    lease = owner.kernel.admission.begin("operator_model_test")
                except OperationInvalidated:
                    self._json(HTTPStatus.CONFLICT, {"error": "runtime_unavailable"})
                    return
                try:
                    with bind_lease(lease):
                        result = asyncio.run(
                            gateway.test_resource(group_id, subject_id=owner.kernel.subject_id)
                        )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "model_resource_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except BudgetExhaustedError:
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "model_test_budget_exhausted"},
                    )
                    return
                except (ProviderCallError, StructuredOutputError) as error:
                    self._json(
                        HTTPStatus.BAD_GATEWAY,
                        {"error": getattr(error, "code", "model_test_failed")},
                    )
                    return
                except (ModelCallStateError, ValueError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
                    return
                finally:
                    owner.kernel.admission.finish(lease)
                self._json(HTTPStatus.OK, result)

            def _change_embedding_resource_status(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                reason = payload.get("reason")
                suffix = self.path.removeprefix("/api/config/embedding-resources/")
                config_id, operation = suffix.rsplit("/", 1) if "/" in suffix else ("", "")
                if (
                    not isinstance(reason, str)
                    or not config_id
                    or operation
                    not in {
                        "enable",
                        "disable",
                        "revoke",
                    }
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_embedding_resource"})
                    return
                try:
                    method = {
                        "enable": owner.embedding_resources.enable,
                        "disable": owner.embedding_resources.disable,
                        "revoke": owner.embedding_resources.revoke,
                    }[operation]
                    record = method(
                        config_id,
                        reason=reason,
                        actor=self._actor(),
                        subject_id=owner.kernel.subject_id,
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "embedding_resource_not_found"})
                    return
                except (ValueError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_embedding_resource"})
                    return
                self._json(HTTPStatus.OK, record.__dict__)

            def _change_model_resource_status(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                parts = self.path.removeprefix("/api/config/model-resources/").split("/")
                reason = payload.get("reason")
                if (
                    len(parts) != 2
                    or not parts[0]
                    or not parts[1]
                    or set(payload) != {"reason"}
                    or not isinstance(reason, str)
                    or not reason.strip()
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                group_id, operation = parts
                try:
                    if operation == "enable":
                        record = owner.cognitive_resources.enable(
                            group_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        )
                    elif operation == "disable":
                        record = owner.cognitive_resources.disable(
                            group_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        )
                    elif operation == "revoke":
                        record = owner.cognitive_resources.revoke(
                            group_id,
                            reason=reason,
                            actor=self._actor(),
                            subject_id=owner.kernel.subject_id,
                        )
                    else:
                        raise ValueError("unsupported model resource operation")
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "model_resource_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    {"group_id": record.group_id, "status": record.status},
                )

            def _update_model_resource(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                # `_request_json` currently returns a mapping, but keep this
                # guard at the handler boundary so a future decoder change
                # cannot turn malformed input into an AttributeError/500.
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_update"})
                    return
                prefix = "/api/config/model-resources/"
                suffix = self.path.removeprefix(prefix)
                if not suffix.endswith("/update"):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_update"})
                    return
                group_id = suffix.removesuffix("/update")
                reason = payload.get("reason")
                update_payload = {key: value for key, value in payload.items() if key != "reason"}
                if (
                    not group_id
                    or "/" in group_id
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or set(update_payload)
                    - {
                        "priority",
                        "weight",
                        "daily_attempts",
                        "daily_input_tokens",
                        "daily_output_tokens",
                        "daily_cost_limit_usd",
                        "input_usd_per_million",
                        "output_usd_per_million",
                        "max_attempts",
                    }
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_update"})
                    return
                try:
                    proposal = CognitiveResourceGroupUpdate.model_validate(update_payload)
                    record = owner.cognitive_resources.update(
                        group_id,
                        proposal,
                        reason=reason,
                        actor=self._actor(),
                        subject_id=owner.kernel.subject_id,
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "model_resource_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_model_resource_update"},
                    )
                    return
                self._json(HTTPStatus.OK, _cognitive_resource_payload(record))

            def _add_model_resource_keys(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                group_id = self.path.removeprefix("/api/config/model-resources/").removesuffix(
                    "/keys"
                )
                raw_keys = payload.get("api_keys")
                try:
                    if set(payload) != {"api_keys"}:
                        raise TypeError("unexpected model resource key fields")
                    if not isinstance(raw_keys, list) or not all(
                        isinstance(value, str) for value in raw_keys
                    ):
                        raise TypeError("api_keys must be a list of strings")
                    record = owner.cognitive_resources.add_keys(
                        group_id,
                        tuple(SecretStr(value) for value in raw_keys),
                        actor=self._actor(),
                        subject_id=owner.kernel.subject_id,
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
                    return
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "model_resource_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                self._json(
                    HTTPStatus.OK,
                    {"group_id": record.group_id, "key_count": record.key_count},
                )

            def _revoke_model_resource_key(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_key"})
                    return
                parts = self.path.removeprefix("/api/config/model-resources/").split("/")
                reason = payload.get("reason")
                if (
                    len(parts) != 4
                    or parts[1] != "keys"
                    or parts[3] != "revoke"
                    or not parts[0]
                    or not parts[2]
                    or not isinstance(reason, str)
                    or not reason.strip()
                    or set(payload) != {"reason"}
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_key"})
                    return
                try:
                    key = next(
                        (
                            item
                            for item in owner.cognitive_resources.keys(
                                parts[0], subject_id=owner.kernel.subject_id
                            )
                            if item.key_id == parts[2]
                        ),
                        None,
                    )
                    if key is None:
                        raise NotFoundError("model resource key not found")
                    record = owner.cognitive_resources.revoke_key(
                        key.key_id,
                        reason=reason,
                        actor=self._actor(),
                        subject_id=owner.kernel.subject_id,
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_key_not_found"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "model_resource_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_key"})
                    return
                self._json(
                    HTTPStatus.OK,
                    {"key_id": record.key_id, "group_id": record.group_id, "status": record.status},
                )

            def _revoke_search_provider(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                config_id = self.path.removeprefix("/api/config/search-providers/").removesuffix(
                    "/revoke"
                )
                reason = payload.get("reason")
                try:
                    if not isinstance(reason, str):
                        raise TypeError("reason must be text")
                    record = owner.search_providers.revoke(
                        config_id,
                        reason=reason,
                        actor=self._actor(),
                        subject_id=owner.kernel.subject_id,
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_search_provider"})
                    return
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "search_provider_not_found"})
                    return
                self._json(HTTPStatus.OK, {"config_id": record.config_id, "status": record.status})

            def _authorized(self, required_role: str = "operator") -> bool:
                allowed: dict[str, tuple[SecretStr | None, ...]] = {
                    "read": (
                        owner.settings.read_token,
                        owner.settings.operator_token,
                        owner.settings.export_token,
                        owner.settings.break_glass_token,
                        owner.settings.admin_token,
                    ),
                    "operator": (
                        owner.settings.operator_token,
                        owner.settings.break_glass_token,
                        owner.settings.admin_token,
                    ),
                    "export": (
                        owner.settings.export_token,
                        owner.settings.break_glass_token,
                        owner.settings.admin_token,
                    ),
                    "break_glass": (owner.settings.break_glass_token,),
                }
                if required_role not in allowed:
                    return False
                supplied = self.headers.get("Authorization", "")
                bearer_authorized = any(
                    secret is not None
                    and hmac.compare_digest(
                        supplied,
                        f"Bearer {secret.get_secret_value()}",
                    )
                    for secret in allowed[required_role]
                )
                if bearer_authorized:
                    return True
                _session_id, session = self._session_from_cookie()
                if session is None or required_role not in {"read", "operator"}:
                    return False
                if required_role == "operator" and session.role not in {
                    "operator",
                    "admin",
                    "break_glass",
                }:
                    return False
                if self.command == "POST" and self.path not in {
                    "/admin/session",
                    "/admin/session/logout",
                }:
                    return hmac.compare_digest(
                        self.headers.get("X-CSRF-Token", ""), session.csrf_token
                    )
                return True

            def _actor(self) -> str:
                supplied = self.headers.get("Authorization", "")
                for role, secret in (
                    ("admin", owner.settings.admin_token),
                    ("operator", owner.settings.operator_token),
                    ("break_glass", owner.settings.break_glass_token),
                ):
                    if secret is not None and hmac.compare_digest(
                        supplied, f"Bearer {secret.get_secret_value()}"
                    ):
                        return f"web-{role}"
                _session_id, session = self._session_from_cookie()
                return session.actor if session is not None else "web-operator"

            def _session_from_cookie(self) -> tuple[str, _AdminSession | None]:
                cookie = SimpleCookie()
                try:
                    cookie.load(self.headers.get("Cookie", ""))
                except Exception:
                    return "", None
                session_id = cookie.get("noyra_admin_session")
                value = "" if session_id is None else session_id.value
                return value, owner.admin_session(value)

            def _create_admin_session(self) -> None:
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                token = payload.get("token")
                role = None
                for candidate, secret in (
                    ("admin", owner.settings.admin_token),
                    ("operator", owner.settings.operator_token),
                    ("break_glass", owner.settings.break_glass_token),
                ):
                    if (
                        isinstance(token, str)
                        and secret is not None
                        and hmac.compare_digest(token, secret.get_secret_value())
                    ):
                        role = candidate
                        break
                if role is None:
                    owner.audit_admin_event(
                        "admin_login_failed", "web-anonymous", {"path": "/admin/session"}
                    )
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if role == "break_glass":
                    owner.audit_admin_event(
                        "admin_break_glass_rejected_session", "web-break_glass", {}
                    )
                    self._json(HTTPStatus.FORBIDDEN, {"error": "break_glass_session_forbidden"})
                    return
                session_id, session = owner.create_admin_session(role=role, actor=f"web-{role}")
                owner.audit_admin_event("admin_login_succeeded", session.actor, {"role": role})
                body = json.dumps(
                    {"authenticated": True, "role": session.role, "csrf_token": session.csrf_token},
                    separators=(",", ":"),
                ).encode()
                cookie = (
                    f"noyra_admin_session={session_id}; Path=/; HttpOnly; SameSite=Strict; "
                    f"Max-Age={owner.settings.admin_session_ttl_seconds}"
                    + ("; Secure" if owner.settings.admin_session_cookie_secure else "")
                )
                self.send_response(HTTPStatus.OK)
                self.send_header("Set-Cookie", cookie)
                self._headers("application/json; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)

            def _admin_session_status(self) -> None:
                _session_id, session = self._session_from_cookie()
                if session is None:
                    self._json(HTTPStatus.OK, {"authenticated": False})
                    return
                self._json(
                    HTTPStatus.OK,
                    {
                        "authenticated": True,
                        "role": session.role,
                        "csrf_token": session.csrf_token,
                    },
                )

            def _logout_admin_session(self) -> None:
                session_id, session = self._session_from_cookie()
                if session is not None and not hmac.compare_digest(
                    self.headers.get("X-CSRF-Token", ""), session.csrf_token
                ):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "csrf_required"})
                    return
                owner.revoke_admin_session(session_id)
                if session is not None:
                    owner.audit_admin_event("admin_logout", session.actor, {"role": session.role})
                self.send_response(HTTPStatus.NO_CONTENT)
                self.send_header(
                    "Set-Cookie",
                    "noyra_admin_session=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0",
                )
                self._headers("application/json; charset=utf-8", 0)
                self.end_headers()

            @staticmethod
            def _operator_error_status(error: OperatorControlConflict) -> HTTPStatus:
                if error.code.startswith(("invalid_", "operator_")):
                    return HTTPStatus.BAD_REQUEST
                if error.code == "at_rest_boundary_unavailable":
                    return HTTPStatus.SERVICE_UNAVAILABLE
                return HTTPStatus.CONFLICT

            @staticmethod
            def _at_rest_available() -> bool:
                if owner.at_rest is None or not owner.at_rest.required:
                    return True
                try:
                    owner.at_rest.require_ready()
                except AtRestError:
                    return False
                return True

            def _update_training_policy(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                allowed = {
                    "record_enabled",
                    "export_enabled",
                    "include_private_psychology",
                    "include_conversations",
                    "include_model_io",
                    "include_external_actions",
                    "include_workspace",
                }
                expected_version = payload.get("expected_version")
                if (
                    isinstance(expected_version, bool)
                    or not isinstance(expected_version, int)
                    or expected_version < 1
                ):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "expected_training_policy_version_required"},
                    )
                    return
                changes = {key: payload[key] for key in allowed if key in payload}
                try:
                    record = owner.training.update_policy(
                        owner.kernel.subject_id,
                        actor="operator",
                        reason="management_api_update",
                        expected_version=expected_version,
                        **changes,
                    )
                except TrainingPolicyConflictError:
                    self._json(
                        HTTPStatus.CONFLICT,
                        {
                            "error": "training_policy_version_conflict",
                            "current": owner.training.policy(owner.kernel.subject_id).__dict__,
                        },
                    )
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_training_policy"})
                    return
                self._json(HTTPStatus.OK, record.__dict__)

            def _create_export_job(self) -> None:
                if not self._authorized("export"):
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                export_kind = payload.get("kind")
                if not isinstance(export_kind, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_export_kind"})
                    return
                if export_kind not in {"runtime", "training"}:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_export_kind"})
                    return
                self._enqueue_export_job(export_kind)

            def _enqueue_export_job(self, export_kind: str) -> None:
                """Create an export job without doing export work inline.

                This is shared by the explicit POST job API and the legacy
                synchronous GET routes.  Keeping all policy/queue checks in
                one path prevents the compatibility shims from bypassing
                authentication or training consent.
                """
                if current_lease() is None:
                    try:
                        lease = owner.admission.begin("http_export")
                    except (OperationInvalidated, RuntimeOwnershipError):
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "runtime_admission_unavailable"},
                        )
                        return
                    try:
                        with bind_lease(lease):
                            self._enqueue_export_job(export_kind)
                    finally:
                        owner.admission.finish(lease)
                    return
                if not self._authorized("export"):
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if export_kind == "runtime" and not owner.settings.developer_log_export_enabled:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                if export_kind == "training":
                    try:
                        if not owner.training.policy(owner.kernel.subject_id).export_enabled:
                            self._json(
                                HTTPStatus.FORBIDDEN,
                                {"error": "training_export_disabled"},
                            )
                            return
                    except NotFoundError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "subject_not_found"})
                        return
                try:
                    job = owner.export_jobs.create(
                        owner.kernel.subject_id,
                        cast(ExportKind, export_kind),
                    )
                except ValueError:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_export_kind"})
                    return
                except RuntimeError:
                    self._json(HTTPStatus.TOO_MANY_REQUESTS, {"error": "export_queue_full"})
                    return
                self._json(HTTPStatus.ACCEPTED, job.__dict__)

            def _cancel_export_job(self) -> None:
                if not self._authorized("export"):
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                job_id = self.path.removeprefix("/api/admin/export-jobs/").removesuffix("/cancel")
                if not job_id or "/" in job_id:
                    self._discard_small_request_body()
                    self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
                    return
                self._discard_small_request_body()
                try:
                    job = owner.export_jobs.cancel(job_id, owner.kernel.subject_id)
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "export_job_not_found"})
                    return
                except RuntimeError:
                    self._json(HTTPStatus.CONFLICT, {"error": "export_not_cancellable"})
                    return
                self._json(HTTPStatus.OK, job.__dict__)

            def _request_json(self) -> dict[str, Any] | None:
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdigit():
                    self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
                    return None
                length = int(raw_length)
                if length > owner.settings.max_request_bytes:
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
                    return None
                try:
                    payload = json.loads(self.rfile.read(length))
                except (UnicodeDecodeError, ValueError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                    return None
                if not isinstance(payload, dict):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_json"})
                    return None
                return payload

            def _receive_inbound_webhook(self) -> None:
                """Decode one configured native channel callback.

                Provider callbacks are authenticated by their transport secret,
                bounded before parsing, and accepted only after an explicit
                account binding.  The provider payload is never forwarded as a
                command or copied into an interaction beyond its text envelope.
                """
                if not self._at_rest_available() or not owner.allow_mutation(self.path):
                    self.close_connection = True
                    self._discard_small_request_body()
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "integrity_quarantine"},
                    )
                    return
                parts = self.path.split("/", 3)
                if len(parts) != 4 or parts[1] != "webhooks":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.NOT_FOUND, {"error": "not_found"})
                    return
                channel = parts[2]
                transport_id = parts[3]
                if channel not in ADAPTERS or not transport_id or len(transport_id) > 128:
                    self._discard_small_request_body()
                    self._json(HTTPStatus.NOT_FOUND, {"error": "inbound_route_not_found"})
                    return
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdigit():
                    self._json(HTTPStatus.LENGTH_REQUIRED, {"error": "length_required"})
                    return
                length = int(raw_length)
                if length > min(owner.settings.max_request_bytes, 1_048_576):
                    self._json(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, {"error": "too_large"})
                    return
                body = self.rfile.read(length)
                try:
                    transport = owner.transports.get(
                        transport_id, subject_id=owner.kernel.subject_id
                    )
                    if transport.channel != channel or transport.status != "active":
                        raise PermissionError("inbound transport is unavailable")
                    secret_payload = owner.transports.secret(
                        transport_id, subject_id=owner.kernel.subject_id
                    )
                    credentials = secret_payload.get("credentials")
                    if not isinstance(credentials, dict):
                        raise PermissionError("inbound transport secret is invalid")
                    transport_settings = owner.transports.settings(
                        transport_id, subject_id=owner.kernel.subject_id
                    )
                    if channel == "qq":
                        # Official QQ HTTP callbacks derive Ed25519 from
                        # AppSecret.  Legacy HMAC is opt-in and separate.
                        secret_value = str(credentials.get("app_secret") or "")
                        signing_key = str(credentials.get("webhook_secret") or "") or None
                    elif channel == "feishu":
                        secret_value = str(
                            credentials.get("verification_token")
                            or credentials.get("webhook_secret")
                            or ""
                        )
                        signing_key = None
                    else:
                        secret_value = next(
                            (
                                str(credentials[key])
                                for key in (
                                    "webhook_secret",
                                    "verification_token",
                                    "secret",
                                    "signing_secret",
                                    "token",
                                )
                                if credentials.get(key)
                            ),
                            "",
                        )
                        signing_key = None
                    callback_headers = {str(key): str(value) for key, value in self.headers.items()}
                    if channel == "wechat":
                        # WeChat signs callbacks with query parameters rather
                        # than HTTP headers. Normalize them for the adapter.
                        request_target = getattr(self, "_request_target", self.path)
                        callback_query = parse_qs(urlsplit(request_target).query)
                        for query_name, header_name in (
                            ("signature", "X-Wechat-Signature"),
                            ("msg_signature", "X-Wechat-MsgSignature"),
                            ("timestamp", "X-Wechat-Timestamp"),
                            ("nonce", "X-Wechat-Nonce"),
                            ("encrypt_type", "X-Wechat-EncryptType"),
                        ):
                            values = callback_query.get(query_name, [])
                            if len(values) == 1:
                                callback_headers[header_name] = values[0]
                    adapter = ADAPTERS[channel]()
                    if channel == "wechat":
                        envelope = adapter.parse(
                            body,
                            callback_headers,
                            transport_id=transport_id,
                            secret=secret_value,
                            encryption_key=(
                                str(credentials.get("encoding_aes_key"))
                                if credentials.get("encoding_aes_key")
                                else None
                            ),
                            signing_key=signing_key,
                            account_id=(
                                str(credentials.get("app_id"))
                                if credentials.get("app_id")
                                else None
                            ),
                        )
                    elif channel == "feishu":
                        envelope = adapter.parse(
                            body,
                            callback_headers,
                            transport_id=transport_id,
                            secret=secret_value,
                            encryption_key=(
                                str(credentials.get("encrypt_key"))
                                if credentials.get("encrypt_key")
                                else None
                            ),
                            signing_key=signing_key,
                        )
                    else:
                        envelope = adapter.parse(
                            body,
                            callback_headers,
                            transport_id=transport_id,
                            secret=secret_value,
                            signing_key=signing_key,
                        )
                    if isinstance(envelope, InboundChallenge):
                        self._json(
                            HTTPStatus.OK,
                            envelope.response or {"challenge": envelope.challenge},
                        )
                        return
                    if isinstance(envelope, InboundIgnored):
                        self._inbound_ack(
                            channel,
                            {"status": "ignored", "reason": envelope.reason},
                        )
                        return
                    configured_account = transport_settings.get("external_account_id")
                    if not configured_account:
                        configured_account = transport_settings.get("account_id")
                    if not configured_account and channel in {"qq", "feishu"}:
                        configured_account = credentials.get("app_id")
                    if (
                        isinstance(configured_account, (str, int))
                        and str(configured_account).strip()
                        and channel in {"qq", "feishu"}
                    ):
                        envelope = envelope.model_copy(
                            update={"external_account_id": str(configured_account).strip()}
                        )
                    accepted = owner.inbound.ingest(envelope)
                    try:
                        owner.wallet_rewards.submit_inbound(
                            accepted,
                            external_counterparty=envelope.external_sender_id,
                        )
                    except ValueError as error:
                        if "not a reward submission envelope" not in str(error):
                            raise
                except (PermissionError, InboundAuthenticationError):
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "inbound_unauthorized"})
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "inbound_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, NotFoundError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_event"})
                    return
                try:
                    owner.audit_admin_event(
                        "inbound_message_received",
                        f"transport:{channel}",
                        {
                            "event_id": accepted.event_id,
                            "interaction_id": accepted.interaction_id,
                            "channel": channel,
                            "duplicate": accepted.duplicate,
                            "scheduling_priority": accepted.scheduling_priority,
                        },
                    )
                except Exception:
                    # Durable ingestion is already complete.  A provider must
                    # receive a success response or it will redeliver forever.
                    LOGGER.exception("inbound acceptance audit failed")
                self._inbound_ack(
                    channel,
                    {
                        "event_id": accepted.event_id,
                        "interaction_id": accepted.interaction_id,
                        "duplicate": accepted.duplicate,
                        "status": "accepted",
                    },
                )

            def _wechat_webhook_challenge(self) -> None:
                if not self._at_rest_available() or not owner.allow_mutation(self.path):
                    self.close_connection = True
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "integrity_quarantine"},
                        retry_after=60,
                    )
                    return
                parsed = urlsplit(self.path)
                transport_id = parsed.path.removeprefix("/webhooks/wechat/").strip("/")
                query = parse_qs(parsed.query)
                if not transport_id or any(
                    len(query.get(key, [])) != 1 for key in ("timestamp", "nonce", "echostr")
                ):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wechat_challenge"})
                    return
                try:
                    transport = owner.transports.get(
                        transport_id, subject_id=owner.kernel.subject_id
                    )
                    secret_payload = owner.transports.secret(
                        transport_id, subject_id=owner.kernel.subject_id
                    )
                    credentials = secret_payload.get("credentials")
                    secret = (
                        ""
                        if not isinstance(credentials, dict)
                        else next(
                            (
                                str(credentials[key])
                                for key in ("webhook_secret", "secret", "token")
                                if credentials.get(key)
                            ),
                            "",
                        )
                    )
                    if transport.channel != "wechat" or transport.status != "active" or not secret:
                        raise ValueError("wechat transport is unavailable")
                    encrypt_type = query.get("encrypt_type", [""])[0].casefold()
                    encrypted_mode = encrypt_type == "aes" or "msg_signature" in query
                    signature_name = "msg_signature" if encrypted_mode else "signature"
                    if len(query.get(signature_name, [])) != 1:
                        raise ValueError("wechat challenge signature is missing")
                    signature = query[signature_name][0]
                    timestamp = query["timestamp"][0]
                    nonce = query["nonce"][0]
                    try:
                        timestamp_value = int(timestamp)
                    except ValueError as error:
                        raise ValueError("wechat timestamp is invalid") from error
                    if abs(time.time() - timestamp_value) > REPLAY_WINDOW_SECONDS:
                        raise ValueError("wechat signature is stale")
                    echostr = query["echostr"][0]
                    signature_message = (
                        (secret, timestamp, nonce, echostr)
                        if encrypted_mode
                        else (
                            secret,
                            timestamp,
                            nonce,
                        )
                    )
                    expected = hashlib.sha1("".join(sorted(signature_message)).encode()).hexdigest()
                    if not hmac.compare_digest(signature, expected):
                        raise ValueError("wechat signature is invalid")
                    if not echostr:
                        raise ValueError("wechat challenge is missing")
                    if encrypted_mode:
                        encoding_key = (
                            credentials.get("encoding_aes_key")
                            if isinstance(credentials, dict)
                            else None
                        )
                        if not isinstance(encoding_key, str) or not encoding_key:
                            raise ValueError("wechat encrypted challenge is not configured")
                        account_id = (
                            credentials.get("app_id") if isinstance(credentials, dict) else None
                        )
                        if not isinstance(account_id, str) or not account_id.strip():
                            raise ValueError("wechat encrypted challenge account is not configured")
                        body = WeChatInboundAdapter._decrypt_xml(
                            echostr,
                            encoding_key,
                            expected_account_id=account_id,
                        )
                    else:
                        body = echostr.encode()
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "inbound_integrity_unavailable"},
                        retry_after=60,
                    )
                    return
                except (ValueError, NotFoundError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_wechat_challenge"})
                    return
                self.send_response(HTTPStatus.OK)
                self._headers("text/plain; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)

            def _configure_inbound_binding(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                try:
                    transport_id = payload.get("transport_id")
                    account = payload.get("external_account_id")
                    sender = payload.get("external_sender_id")
                    role = payload.get("role", "participant")
                    label = payload.get("label", "")
                    if (
                        not isinstance(transport_id, str)
                        or not isinstance(account, str)
                        or not isinstance(sender, str)
                        or not isinstance(role, str)
                        or not isinstance(label, str)
                    ):
                        raise TypeError("binding fields must be text")
                    if role not in {"creator", "participant"}:
                        raise ValueError("binding role is invalid")
                    binding_id = owner.inbound.bind(
                        owner.kernel.subject_id,
                        transport_id,
                        external_account_id=account,
                        external_sender_id=sender,
                        role=cast(Literal["creator", "participant"], role),
                        label=label,
                    )
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
                    return
                self._json(HTTPStatus.CREATED, {"binding_id": binding_id, "status": "active"})

            def _change_inbound_binding(self) -> None:
                if not self._authorized():
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
                    return
                if self.headers.get_content_type() != "application/json":
                    self._discard_small_request_body()
                    self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
                    return
                payload = self._request_json()
                if payload is None:
                    return
                parts = (
                    self.path.removeprefix("/api/config/inbound-bindings/").strip("/").split("/")
                )
                reason = payload.get("reason")
                if len(parts) != 2 or not isinstance(reason, str):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
                    return
                binding_id, operation = parts
                status = {"enable": "active", "disable": "disabled", "revoke": "revoked"}.get(
                    operation
                )
                if status is None:
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
                    return
                try:
                    result = owner.inbound.change_binding(
                        binding_id,
                        owner.kernel.subject_id,
                        status=cast(Literal["active", "disabled", "revoked"], status),
                        actor=self._actor(),
                        reason=reason,
                    )
                except NotFoundError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "inbound_binding_not_found"})
                    return
                except (ValueError, TypeError, PermissionError):
                    self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_inbound_binding"})
                    return
                self._json(HTTPStatus.OK, result)

            def _discard_small_request_body(self) -> None:
                raw_length = self.headers.get("Content-Length", "")
                if not raw_length.isdigit():
                    return
                length = int(raw_length)
                if 0 < length <= owner.settings.max_request_bytes:
                    self.rfile.read(length)

            def _asset(self, filename: str, content_type: str) -> None:
                path = Path(__file__).with_name("web") / filename
                try:
                    body = path.read_bytes()
                except OSError:
                    self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
                    return
                self.send_response(HTTPStatus.OK)
                self._headers(content_type, len(body))
                self.end_headers()
                self.wfile.write(body)

            def _send_file(self, path: Path, *, filename: str, digest: str) -> None:
                length = path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("X-Archive-SHA256", digest)
                self._headers("application/zip", length)
                self.end_headers()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        self.wfile.write(chunk)

            def _inbound_ack(self, channel: str, payload: Mapping[str, Any]) -> None:
                """Return the platform-native acknowledgement after durable handling."""
                if channel == "qq":
                    self._json(HTTPStatus.OK, {"op": 12, "d": 0})
                    return
                if channel == "wechat":
                    body = b"success"
                    self.send_response(HTTPStatus.OK)
                    self._headers("text/plain; charset=utf-8", len(body))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                self._json(HTTPStatus.OK, payload)

            def _json(
                self, status: HTTPStatus, payload: Any, *, retry_after: int | None = None
            ) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                self.send_response(status)
                if status == HTTPStatus.TOO_MANY_REQUESTS:
                    self.send_header("Retry-After", str(60 if retry_after is None else retry_after))
                elif status == HTTPStatus.SERVICE_UNAVAILABLE:
                    self.send_header("Retry-After", str(5 if retry_after is None else retry_after))
                self._headers("application/json; charset=utf-8", len(body))
                self.end_headers()
                self.wfile.write(body)

            def _headers(self, content_type: str, length: int) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; base-uri 'none'; object-src 'none'; "
                    "frame-ancestors 'none'; "
                    "script-src 'self'; style-src 'self'; connect-src 'self'; form-action 'none'",
                )

            @staticmethod
            def _limit(query: dict[str, list[str]]) -> int:
                try:
                    return max(1, min(int(query.get("limit", ["100"])[0]), 1_000))
                except ValueError:
                    return 100

            @staticmethod
            def _cursor(query: dict[str, list[str]]) -> str | None:
                values = query.get("cursor")
                if not values:
                    return None
                value = values[0]
                if not value or len(value) > 8_192:
                    raise ValueError("runtime log cursor is invalid")
                return value

            def log_message(self, format: str, *args: Any) -> None:
                LOGGER.info("http %s", format % args)

        return Handler


class _UnownedHTTPFacade:
    """No-write facade returned while another process owns the subject."""

    def close(self) -> None:
        return


class _UnavailableCloudArchiveCoordinator:
    """Read-only placeholder while an existing subject fails integrity boot."""

    provider: Any | None = None

    def tick(self, **_: Any) -> dict[str, int]:
        return {"uploaded": 0, "failed": 0, "dead": 0, "garbage_collected": 0}


def _cloud_archive_subject_state_available(database: Database, subject_id: str) -> bool:
    """Return whether cloud archive storage can be bound to this subject."""

    with database.connection() as connection:
        row = connection.execute(
            "SELECT 1 FROM subject_identity i "
            "JOIN subject_storage_keys k ON k.subject_id = i.subject_id "
            "WHERE i.subject_id = ?",
            (subject_id,),
        ).fetchone()
    return row is not None


class NoyraService:
    @_release_startup_lock_on_failure
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        wallet_signer: WalletSigner | None = None,
        close_wallet_signer: bool = False,
    ):
        self.settings = settings
        database_path = (settings.data_dir / "noyra.sqlite3").resolve()
        self._startup_lock = ProcessLock(f"{database_path}.lock")
        try:
            self._startup_lock.acquire()
        except RuntimeOwnershipError:
            # Preserve a read-only preflight object for callers that need to
            # inspect diagnostics, while making boot the explicit ownership
            # boundary.  No at-rest/layout/HTTP constructor runs on this path.
            self._unowned_preflight = True
            self.kernel = SubjectKernel(
                database_path,
                settings.subject_id,
                settings.genesis_hash,
                allow_subject_creation=False,
                process_lock=self._startup_lock,
                defer_preflight=True,
            )
            self.http = cast(Any, _UnownedHTTPFacade())
            self._construction_complete = True
            return
        self._unowned_preflight = False
        self._construction_complete = False
        self._wallet_automation_config: _WalletAutomationConfig | None = None
        self.at_rest = AtRestGuard(
            AtRestConfig(
                data_root=settings.data_dir,
                mode=settings.at_rest_mode,
                volume_backend=settings.volume_encryption_backend,
                attestation_path=settings.volume_attestation_path,
                backup_keyring_path=settings.backup_keyring_path,
            )
        )
        self.at_rest.prepare()
        self.storage_layout = StorageLayout.create(settings.data_dir)
        self._pending_cognitive_resource_proposals: tuple[CognitiveResourceGroupInput, ...] = ()
        # Keep the historical database path for compatibility with existing
        # installations; new workspace categories are still isolated by layout.
        database_preexisting = database_path.is_file() and database_path.stat().st_size > 0
        common_key_directory = settings.data_dir / "secrets" / "common-knowledge"
        initialize_common_key_after_integrity = (
            database_preexisting and not common_key_directory.exists()
        )
        self.kernel = SubjectKernel(
            database_path,
            settings.subject_id,
            settings.genesis_hash,
            allow_subject_creation=not database_preexisting,
            process_lock=self._startup_lock,
        )
        subject_storage_binding_available = not database_preexisting or (
            _cloud_archive_subject_state_available(
                self.kernel.database,
                self.kernel.subject_id,
            )
        )
        (settings.data_dir / "secrets").mkdir(parents=True, exist_ok=True)
        self.at_rest.post_initialize()
        self.storage_lifecycle = StorageLifecycleManager(
            self.kernel.database,
            settings.subject_id,
            self.storage_layout,
            StorageQuota(
                subject_bytes=settings.subject_storage_quota_bytes,
                training_bytes=settings.training_storage_quota_bytes,
                workspace_bytes=settings.workspace_storage_quota_bytes,
            ),
            minimum_free_bytes=settings.minimum_free_storage_bytes,
            event_payload_retention_days=settings.event_payload_retention_days,
            initialize_archives=subject_storage_binding_available,
        )
        self.http = NoyraHTTPServer(
            self.kernel,
            settings,
            allow_common_knowledge_key_creation=not database_preexisting,
            repair_secrets_on_init=False,
            wallet_signer=wallet_signer,
            close_wallet_signer=close_wallet_signer,
        )
        self.http.at_rest = self.at_rest
        self._initialize_common_key_after_integrity = initialize_common_key_after_integrity
        self.cognition: CognitionCycle | None = None
        self.cognitive_resources = self.http.cognitive_resources
        self.cognition_gateway: RoutedModelGateway | None = None
        self.cloud_archives: CloudArchiveCoordinator | _UnavailableCloudArchiveCoordinator
        if not subject_storage_binding_available:
            # Missing identity/storage-key rows are startup-integrity findings,
            # not permission to recreate subject filesystem ownership.  Keep
            # cloud archive work disabled until the integrity gate reports the
            # damaged database and establishes a safe pause.
            self.cloud_archives = _UnavailableCloudArchiveCoordinator()
        else:
            self.cloud_archives = CloudArchiveCoordinator(
                self.kernel.database,
                self.kernel.subject_id,
                self.storage_layout.training_raw,
                local_archive_root=(
                    self.storage_lifecycle.event_archive.root
                    if self.storage_lifecycle.event_archive is not None
                    else self.storage_layout.subject / "cold"
                ),
            )
        self._integrity_stop = threading.Event()
        self.integrity = IntegrityRuntimeController(
            self.kernel,
            settings.data_dir,
            policy_mode=settings.integrity_mode,
            interval_seconds=settings.integrity_interval_seconds,
            retry_seconds=settings.integrity_retry_seconds,
            startup_deadline_seconds=settings.integrity_startup_deadline_seconds,
            periodic_deadline_seconds=settings.integrity_periodic_deadline_seconds,
            limits=IntegrityAuditLimits(
                max_rows_per_check=settings.integrity_max_rows_per_check,
                max_bytes_per_check=settings.integrity_max_bytes_per_check,
                max_value_bytes=settings.integrity_max_value_bytes,
                max_files_per_check=settings.integrity_max_files_per_check,
            ),
            checkpoint=self._integrity_checkpoint,
        )
        self.http.integrity = self.integrity
        self.http.quarantine_checker = self._http_quarantine_active
        self.http.operator_controls = OperatorControlService(
            self.kernel,
            integrity=self.integrity,
            at_rest=self.at_rest,
            storage=self.storage_lifecycle,
            export_jobs=self.http.export_jobs,
            cloud_archive_status=self.http.cloud_archive_status,
        )
        self.loop = AutonomyLoop(
            self.kernel,
            config=LoopConfig(
                active_interval_seconds=settings.active_interval_seconds,
                sleep_interval_seconds=settings.sleep_interval_seconds,
                deep_sleep_seconds=settings.deep_sleep_seconds,
                error_backoff_seconds=settings.error_backoff_seconds,
            ),
            active_hook=self._active_tick,
            reflection_hook=self._sleep_reflection,
            pre_tick_hook=self._integrity_pre_tick,
            next_wakeup_hook=self.integrity.seconds_until_due,
        )
        self._event_loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._integrity_worker: asyncio.Task[Any] | None = None
        self._thread_workers: set[asyncio.Task[Any]] = set()
        self._boot_recovery_pending = False
        self._construction_complete = True

    def _http_quarantine_active(self) -> bool:
        if self._boot_recovery_pending:
            return True
        try:
            summary = self.integrity.summary()
        except Exception:
            return True
        return bool(
            self.integrity.policy_mode == "pause"
            and (summary.get("pause_pending") or summary.get("p0") or summary.get("p1"))
        )

    def __del__(self) -> None:
        lock = getattr(self, "_startup_lock", None)
        if lock is not None:
            with suppress(Exception):
                lock.release()

    @classmethod
    def from_env(cls, *, wallet_signer: WalletSigner | None = None) -> NoyraService:
        owns_wallet_signer = False
        if wallet_signer is None:
            signer_endpoint = os.getenv("NOYRA_WALLET_SIGNER_ENDPOINT", "").strip()
            if signer_endpoint:
                signer_id = os.getenv("NOYRA_WALLET_SIGNER_ID", "").strip()
                if not signer_id:
                    raise ValueError(
                        "NOYRA_WALLET_SIGNER_ID is required when a wallet signer endpoint "
                        "is configured"
                    )
                raw_timeout = os.getenv("NOYRA_WALLET_SIGNER_TIMEOUT_SECONDS", "15")
                try:
                    signer_timeout = float(raw_timeout)
                except ValueError as error:
                    raise ValueError(
                        "NOYRA_WALLET_SIGNER_TIMEOUT_SECONDS must be numeric"
                    ) from error
                wallet_signer = HTTPSWalletSigner(
                    signer_endpoint,
                    signer_id=signer_id,
                    timeout_seconds=signer_timeout,
                    bearer_token=os.getenv("NOYRA_WALLET_SIGNER_BEARER_TOKEN"),
                )
                owns_wallet_signer = True
        try:
            service = cls(
                ServiceSettings.from_env(),
                wallet_signer=wallet_signer,
                close_wallet_signer=owns_wallet_signer,
            )
        except Exception:
            if owns_wallet_signer and isinstance(wallet_signer, HTTPSWalletSigner):
                wallet_signer.close()
            raise
        if service._unowned_preflight:
            if owns_wallet_signer and isinstance(wallet_signer, HTTPSWalletSigner):
                wallet_signer.close()
            return service
        try:
            service._wallet_automation_config = _wallet_automation_from_env()
            if (
                service._wallet_automation_config is not None
                and service.http.wallet_execution is None
            ):
                raise ValueError(
                    "wallet automation requires an explicitly configured isolated signer"
                )
            cognition_settings = CognitionSettings.from_env()
            cloud_configured = bool(os.getenv("NOYRA_ARCHIVE_S3_BUCKET", "").strip())
            service.http.cloud_archive_status.update(
                {
                    "configured": cloud_configured,
                    "ready": False,
                    "profile": os.getenv("NOYRA_INSTALL_PROFILE", "unknown").strip() or "unknown",
                }
            )
            if cloud_configured:
                try:
                    provider = S3ArchiveProvider.from_env()
                except Exception as error:
                    raise RuntimeError(
                        "S3 archive is configured but the cloud install profile is unavailable"
                    ) from error
                service.cloud_archives.provider = provider
                service.http.cloud_archive_provider = provider
                if isinstance(provider, S3ArchiveProvider):
                    readiness = provider.readiness(force=True)
                    service.http.cloud_archive_status.update(
                        {
                            key: readiness[key]
                            for key in ("ready", "last_checked_at", "error", "provider_id")
                        }
                    )
                else:
                    # Test/plugin providers do not expose the production S3
                    # readiness contract; their factory remains the authority.
                    service.http.cloud_archive_status["ready"] = True
            if not cognition_settings.enabled:
                return service
            configured_groups = resource_groups_from_env("economy") + resource_groups_from_env(
                "deep"
            )
            service._pending_cognitive_resource_proposals = configured_groups
            legacy_gateway = None
            if not configured_groups and os.getenv("NOYRA_MODEL_BASE_URL"):
                provider_settings = OpenAICompatibleSettings.from_env()
                runtime_settings = ModelRuntimeSettings.from_env()
                legacy_gateway = ModelGateway(
                    OpenAICompatibleProvider(provider_settings),
                    ModelLedger(service.kernel.database),
                    model=provider_settings.model,
                    limits=runtime_settings.budget_limits(),
                    pricing=runtime_settings.pricing(),
                    retry_policy=runtime_settings.retry_policy(),
                    resource_pool="deep",
                    capture_model_io=bool(service.settings.training_include_model_io),
                    capture_model_io_getter=service._capture_model_io_enabled,
                    enforce_training_policy=True,
                )
            gateway = RoutedModelGateway(
                service.kernel.database,
                service.kernel.subject_id,
                service.cognitive_resources,
                legacy_gateway=legacy_gateway,
                capture_model_io=bool(service.settings.training_include_model_io),
                capture_model_io_getter=service._capture_model_io_enabled,
                enforce_training_policy=True,
            )
            service.cognition_gateway = gateway
            service.http.cognition_gateway = gateway
            embedding_call_authorizer = None
            # ``active_settings`` uses ``None`` to represent an absent managed
            # resource.  Integrity failures (for example a missing or
            # fingerprint-mismatched secret) must abort startup instead of
            # silently falling back to an unmanaged environment credential.
            managed_embedding_settings = service.http.embedding_resources.active_settings(
                service.kernel.subject_id
            )
            if managed_embedding_settings is None:
                embedding_settings = EmbeddingSettings.from_env()
            else:
                embedding_settings = managed_embedding_settings
                embedding_call_authorizer = service.http.embedding_resources.call_authorizer(
                    service.kernel.subject_id,
                    managed_embedding_settings,
                )
            service.cognition = CognitionCycle(
                service.kernel,
                gateway,
                cognition_settings,
                reader=SafeWebReader(),
                embedding_settings=embedding_settings,
                embedding_call_authorizer=embedding_call_authorizer,
                common_knowledge=service.http.common_knowledge,
            )
            return service
        except Exception:
            service.close()
            raise

    def _capture_model_io_enabled(self) -> bool:
        """Read the live consent policy; fail closed if it cannot be read."""
        try:
            policy = self.http.training.policy(self.kernel.subject_id)
            if policy.policy_version == 1 and self.settings.training_include_model_io is not None:
                return bool(self.settings.training_include_model_io)
            return bool(policy.include_model_io)
        except Exception:
            return False

    def _sync_cognitive_resources(self, proposals: tuple[CognitiveResourceGroupInput, ...]) -> None:
        existing = {
            (record.pool, record.label)
            for record in self.cognitive_resources.list(self.kernel.subject_id)
        }
        for proposal in proposals:
            if (proposal.pool, proposal.label) in existing:
                continue
            self.cognitive_resources.configure(
                self.kernel.subject_id,
                proposal,
                actor="environment-operator",
            )

    def boot(self) -> None:
        if self._unowned_preflight:
            raise RuntimeOwnershipError("another process owns runtime lock")
        acquired = False
        try:
            self.at_rest.require_ready()
            acquired = self.kernel.acquire_ownership()
            self._boot_recovery_pending = True
            self.kernel.admission.quarantine()
            self.http.defer_export_ownership = True
            integrity = self.integrity.run_startup()
            if integrity is not None and integrity.action.get("result") == "pause_failed":
                raise IntegrityError("integrity policy could not establish a safe pause")
            if (
                integrity is not None
                and self.integrity.policy_mode == "pause"
                and integrity.findings
            ):
                self.kernel.admission.quarantine()
                return
            self._complete_boot_after_integrity()
        except Exception:
            if acquired or self.kernel.process_lock.held:
                with suppress(Exception):
                    self.kernel.admission.begin_drain()
                    self.kernel.admission.wait_for_drain()
                self.kernel.close()
            raise

    def close(self) -> None:
        """Release all owned resources after a partial or completed startup."""
        with suppress(Exception):
            self.kernel.admission.begin_drain()
        with suppress(Exception):
            self.http.close()
        # Do not release the process lock while a fenced operation still has
        # access to the subject database.  ``close`` is also used by partial
        # startup/error paths where the async ``run`` finalizer is not present.
        with suppress(Exception):
            self.kernel.admission.wait_for_drain()
        with suppress(Exception):
            self.kernel.close()

    def _complete_boot_after_integrity(self) -> None:
        if not self._boot_recovery_pending:
            return
        state = self.kernel.recover_after_integrity()
        # A prior process may have stopped while an external RPC read was in
        # flight.  Preserve the ambiguous outcome as ``unknown`` and require
        # an explicit operator retry; never replay it automatically.
        while True:
            batch = self.http.wallet_acquisitions.recover_interrupted(
                self.kernel.subject_id,
                limit=WALLET_ACQUISITION_MAX_BATCH,
            )
            if batch < WALLET_ACQUISITION_MAX_BATCH:
                break
        while True:
            recovered = WalletPaymentExecutionEngine.recover_database_inflight(
                self.kernel.database,
                self.kernel.subject_id,
                actor="system-recovery",
                limit=1000,
                economy=self.http.wallet_economy,
            )
            if len(recovered) < 1000:
                break
        # Provider writes may have completed just before a previous process
        # stopped.  Replay those bounded staging manifests while this process
        # owns the subject and before normal runtime admission opens.
        self.storage_lifecycle.reconcile_archives(limit=16)
        if self._initialize_common_key_after_integrity:
            self.http.common_knowledge.ensure_signing_key()
            self._initialize_common_key_after_integrity = False
        self.http.transports.secret_cleanup.repair(
            self.kernel.subject_id,
            "transport",
            self.http.transports.secret_dir,
        )
        # Service construction deliberately defers secret repair until after
        # the startup integrity gate.  Complete the schema-50 endpoint binding
        # here as part of the same pre-admission recovery boundary; otherwise
        # production starts would leave legacy origin-only rows indefinitely.
        self.http.transports._upgrade_legacy_endpoints(self.kernel.subject_id)
        self.http.search_providers.secret_cleanup.repair(
            self.kernel.subject_id,
            "search",
            self.http.search_providers.secret_dir,
        )
        self.http.cognitive_resources.secret_cleanup.repair(
            self.kernel.subject_id,
            "cognitive",
            self.http.cognitive_resources.secret_dir,
        )
        self.http.embedding_resources.secret_cleanup.repair(
            self.kernel.subject_id,
            "embedding",
            self.http.embedding_resources.secret_dir,
        )
        if self._pending_cognitive_resource_proposals:
            self._sync_cognitive_resources(self._pending_cognitive_resource_proposals)
            self._pending_cognitive_resource_proposals = ()
        self.http._start_export_ownership()
        self.http.defer_export_ownership = False
        if state.state == "booting":
            self.kernel.orient()
            self.kernel.activate()
        try:
            self.kernel.snapshot_store.latest(self.kernel.subject_id)
        except NotFoundError:
            lifecycle = self.kernel.lifecycle.current()
            self.kernel.checkpoint(
                {
                    "kind": "service_boot_checkpoint",
                    "subject_id": self.kernel.subject_id,
                    "lifecycle": lifecycle.state,
                    "lifecycle_version": lifecycle.version,
                },
                reason="establish restart continuity checkpoint",
            )
        self._boot_recovery_pending = False
        current = self.kernel.lifecycle.current()
        # A restart can legitimately land in a sleep/paused lifecycle.  The
        # startup integrity policy decides whether cognition initialization is
        # allowed (pause-mode findings return before this method), while the
        # admission gate itself must only open for an active lifecycle.  Keep
        # policy synchronization and the cognition bootstrap compatible with
        # the historical alert-mode behavior without reopening paused work.
        if current.state == "active":
            self.kernel.admission.open(epoch=current.version)
        self._sync_training_policy()
        if self.cognition is not None:
            self.cognition.bootstrap()

    def _sync_training_policy(self) -> None:
        policy = self.http.training.policy(self.kernel.subject_id)
        # Environment values are bootstrap defaults only.  Once the durable
        # policy has been changed (version > 1), a restart must not recreate
        # consent from an .env template.
        if policy.policy_version > 1:
            return
        configured = {
            "record_enabled": self.settings.training_record_enabled,
            "export_enabled": self.settings.training_export_enabled,
            "include_private_psychology": self.settings.training_include_private_psychology,
            "include_conversations": self.settings.training_include_conversations,
            "include_model_io": self.settings.training_include_model_io,
            "include_external_actions": self.settings.training_include_external_actions,
            "include_workspace": self.settings.training_include_workspace,
        }
        changes = {
            key: value
            for key, value in configured.items()
            if value is not None and getattr(policy, key) != value
        }
        if changes:
            self.http.training.update_policy(
                self.kernel.subject_id,
                actor="environment-configuration",
                reason="service_settings_sync",
                expected_version=policy.policy_version,
                **changes,
            )

    async def run(self) -> None:
        if self._unowned_preflight:
            raise RuntimeOwnershipError("another process owns runtime lock")
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        self._integrity_stop.clear()
        self._event_loop = loop
        self.http.event_loop = loop
        self._stop_event = stop_event
        try:
            self.boot()
            self.http.start()
            for signum in (signal.SIGINT, signal.SIGTERM):
                with suppress(NotImplementedError, RuntimeError):
                    loop.add_signal_handler(signum, self.request_shutdown)
            await self.loop.run_forever(stop_event)
        finally:
            self.kernel.admission.begin_drain()
            self.http.server.stop_accepting()
            self._integrity_stop.set()
            if self._integrity_worker is not None:
                await self._drain_integrity_worker(self._integrity_worker)
            # ``http.close`` waits for every request handler.  Run that
            # blocking join outside the service event loop so a handler that
            # is synchronously waiting on ``run_coroutine_threadsafe`` can
            # finish its coroutine while shutdown drains it.
            await self._tracked_to_thread(self.http.close)
            await self._drain_thread_workers()
            if self.cognition is not None:
                await self.cognition.aclose()
            elif self.cognition_gateway is not None:
                await self.cognition_gateway.aclose()
            await self.http.deliveries.aclose()
            # Every operation lease must be finished before the subject lock
            # is released.  Run the blocking condition wait in a tracked
            # worker so shutdown cannot block the service event loop.
            await self._tracked_to_thread(self.kernel.admission.wait_for_drain)
            self._stop_event = None
            self._event_loop = None
            self.http.event_loop = None
            self.kernel.close()

    def request_shutdown(self) -> None:
        self.kernel.admission.begin_drain()
        self.http.server.stop_accepting()
        self._integrity_stop.set()
        if self._event_loop is not None and self._stop_event is not None:
            self._event_loop.call_soon_threadsafe(self._stop_event.set)

    async def _integrity_pre_tick(self) -> TickResult | None:
        worker = asyncio.create_task(
            asyncio.to_thread(self.integrity.run_periodic_if_due),
            name="noyra-integrity-watchdog",
        )
        self._integrity_worker = worker
        try:
            report = await asyncio.shield(worker)
        except IntegrityAuditShutdown:
            report = None
        except asyncio.CancelledError:
            self._integrity_stop.set()
            await self._drain_integrity_worker(worker)
            raise
        finally:
            if self._integrity_worker is worker and worker.done():
                self._integrity_worker = None
        if self._integrity_stop.is_set():
            lifecycle = self.kernel.lifecycle.current().state
            return TickResult(
                lifecycle,
                "shutdown_pending",
                None,
                self.settings.sleep_interval_seconds,
            )
        summary = self.integrity.summary()
        if self._boot_recovery_pending:
            if self.integrity.policy_mode == "pause" and (summary["p0"] or summary["p1"]):
                self.kernel.admission.quarantine()
                lifecycle = self.kernel.lifecycle.current().state
                return TickResult(
                    lifecycle,
                    "integrity_startup_quarantine",
                    None,
                    self.settings.sleep_interval_seconds,
                )
            try:
                self._complete_boot_after_integrity()
            except Exception:
                self.request_shutdown()
                return TickResult(
                    self.kernel.lifecycle.current().state,
                    "integrity_boot_recovery_failed",
                    None,
                    self.settings.error_backoff_seconds,
                )
        if report is None:
            if self.integrity.policy_mode == "pause" and summary["pause_pending"]:
                self.kernel.admission.quarantine()
                return TickResult(
                    self.kernel.lifecycle.current().state,
                    "integrity_quarantine",
                    None,
                    self.settings.sleep_interval_seconds,
                )
            return None
        action = report.action.get("result")
        lifecycle = self.kernel.lifecycle.current().state
        if action in {"paused", "already_paused"} and lifecycle == "paused":
            self.kernel.admission.quarantine()
            return TickResult(
                lifecycle,
                "integrity_safe_pause",
                None,
                self.settings.sleep_interval_seconds,
            )
        if action == "pause_failed":
            self.request_shutdown()
            return TickResult(
                lifecycle,
                "integrity_pause_failed",
                None,
                self.settings.error_backoff_seconds,
            )
        blocking = self.integrity.summary()
        if self.integrity.policy_mode == "pause" and (blocking["p0"] or blocking["p1"]):
            self.kernel.admission.quarantine()
            return TickResult(
                lifecycle,
                "integrity_safe_pause" if lifecycle == "paused" else "integrity_quarantine",
                None,
                self.settings.sleep_interval_seconds,
            )
        current = self.kernel.lifecycle.current()
        if (
            not self._boot_recovery_pending
            and current.state == "active"
            and not self.kernel.admission.closed
            and not self._http_quarantine_active()
        ):
            self.kernel.admission.open(epoch=current.version)
        return None

    def _integrity_checkpoint(self) -> None:
        if self._integrity_stop.is_set():
            raise IntegrityAuditShutdown("integrity audit stopped during service shutdown")

    async def _drain_integrity_worker(self, worker: asyncio.Task[Any]) -> None:
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            try:
                worker.result()
            except IntegrityAuditShutdown:
                pass
            except Exception as error:
                LOGGER.warning(
                    "integrity worker failed while joining shutdown: %s",
                    type(error).__name__,
                )
        if self._integrity_worker is worker:
            self._integrity_worker = None

    async def _tracked_to_thread(self, function: Any, /, *args: Any, **kwargs: Any) -> Any:
        worker = asyncio.create_task(
            asyncio.to_thread(function, *args, **kwargs),
            name=f"noyra-worker-{getattr(function, '__name__', 'thread')}",
        )
        self._thread_workers.add(worker)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            raise
        finally:
            if worker.done():
                self._thread_workers.discard(worker)

    async def _drain_thread_workers(self) -> None:
        while self._thread_workers:
            workers = tuple(self._thread_workers)
            for worker in workers:
                while not worker.done():
                    try:
                        await asyncio.shield(worker)
                    except asyncio.CancelledError:
                        continue
                    except Exception:
                        break
                self._thread_workers.discard(worker)

    async def _active_tick(self) -> str | None:
        if self._integrity_stop.is_set():
            return "shutdown_pending"
        if not hasattr(self, "kernel"):
            storage = await self._tracked_to_thread(
                self.storage_lifecycle.maintain,
                defer_pressure_decision=True,
            )
            try:
                await self._tracked_to_thread(
                    self.cloud_archives.tick,
                    garbage_collect_local=True,
                    allow_staging=storage.write_amplification_allowed,
                )
            except Exception as error:
                LOGGER.warning("cloud archive tick deferred: %s", type(error).__name__)
            storage = await self._tracked_to_thread(self.storage_lifecycle.reassess, storage)
            return "storage_pressure" if not storage.cognition_allowed else None
        try:
            lease = self.kernel.admission.begin("active_tick")
        except OperationInvalidated:
            return "shutdown_pending"
        try:
            at_rest = getattr(self, "at_rest", None)
            if at_rest is not None and at_rest.required:
                try:
                    at_rest.require_ready()
                except AtRestError:
                    return "at_rest_boundary_unavailable"
            with bind_lease(lease):
                storage = await self._tracked_to_thread(
                    self.storage_lifecycle.maintain,
                    defer_pressure_decision=True,
                    checkpoint=lease.assert_current,
                )
            lease.assert_current()
            try:
                with bind_lease(lease):
                    await self._tracked_to_thread(
                        self.cloud_archives.tick,
                        garbage_collect_local=True,
                        allow_staging=storage.write_amplification_allowed,
                        checkpoint=lease.assert_current,
                    )
                lease.assert_current()
            except OperationInvalidated:
                return "shutdown_pending"
            except Exception as error:
                LOGGER.warning("cloud archive tick deferred: %s", type(error).__name__)
            with bind_lease(lease):
                storage = await self._tracked_to_thread(
                    self.storage_lifecycle.reassess,
                    storage,
                    checkpoint=lease.assert_current,
                )
            lease.assert_current()
            if not storage.cognition_allowed:
                return "storage_pressure"
            try:
                with bind_lease(lease):
                    await self._tracked_to_thread(
                        self.http.common_knowledge.sync_due,
                        checkpoint=lease.assert_current,
                    )
                lease.assert_current()
            except OperationInvalidated:
                return "shutdown_pending"
            except Exception as error:
                LOGGER.warning("common knowledge sync deferred: %s", type(error).__name__)
            with bind_lease(lease):
                await self._tracked_to_thread(
                    self.http.wallet_economy.maintain_expired_bounties,
                    self.kernel.subject_id,
                    limit=100,
                )
            lease.assert_current()
            if self.http.wallet_execution is not None:
                with bind_lease(lease):
                    await self._tracked_to_thread(
                        self._advance_autonomous_reward_workflows,
                        self.kernel.subject_id,
                        limit=4,
                    )
                    await self._tracked_to_thread(
                        self.http.wallet_rewards.execute_ready,
                        self.kernel.subject_id,
                        actor="autonomy",
                        limit=20,
                    )
                lease.assert_current()
            if self.cognition is None:
                with bind_lease(lease):
                    await self.http.deliveries.deliver_pending(self.kernel.subject_id)
                lease.assert_current()
                return None
            with bind_lease(lease):
                await self.http.deliveries.deliver_pending(self.kernel.subject_id)
            lease.assert_current()
            if self.cognition_gateway is not None:
                pool_status = self.cognition_gateway.pool_status()
                with self.kernel.admission.commit_scope(lease):
                    FatigueTracker(self.kernel.database).set_pool_pressures(
                        self.kernel.subject_id,
                        {
                            pool: float(status.get("pressure", 0.0))
                            for pool, status in pool_status.items()
                        },
                    )
            with bind_lease(lease):
                result = await self.cognition.run_once()
            lease.assert_current()
            return result
        except OperationInvalidated:
            return "shutdown_pending"
        finally:
            self.kernel.admission.finish(lease)

    def _advance_autonomous_reward_workflows(self, subject_id: str, *, limit: int = 4) -> int:
        """Create and, when explicitly enabled, publish bounded help bounties.

        Assistance requests do not contain payment terms.  Terms therefore
        come only from the operator-owned environment policy, never from a
        model response or public request.  Moderation remains pending unless
        the separate auto-publish switch is enabled, and every created
        workflow remains subject to the normal policy, budget, and signer
        checks before any payment can leave the process.
        """
        config = self._wallet_automation_config
        if config is None:
            return 0
        if type(limit) is not int or not 1 <= limit <= 16:
            raise ValueError("invalid autonomous wallet workflow limit")
        with self.kernel.database.read_transaction() as connection:
            requests = connection.execute(
                "SELECT r.request_id FROM autonomous_project_assistance_requests r "
                "WHERE r.subject_id=? AND r.status='open' AND NOT EXISTS ("
                "SELECT 1 FROM wallet_reward_workflows w "
                "WHERE w.assistance_request_id=r.request_id) "
                "ORDER BY r.created_at,r.request_id LIMIT ?",
                (subject_id, limit),
            ).fetchall()
        created = 0
        for request in requests:
            now = datetime.now(UTC)
            proposal = RewardWorkflowInput(
                assistance_request_id=str(request["request_id"]),
                acceptance_criteria=list(config.acceptance_criteria),
                network_id=config.network_id,
                asset_id=config.asset_id,
                reward_amount=config.reward_amount,
                opens_at=canonical_timestamp(now.isoformat()),
                expires_at=canonical_timestamp(
                    (now + timedelta(seconds=config.expiry_seconds)).isoformat()
                ),
                max_submissions=config.max_submissions,
                reward_slots=config.reward_slots,
                idempotency_key=f"autonomous-help:{request['request_id']}",
            )
            try:
                workflow = self.http.wallet_rewards.create(subject_id, proposal, actor="autonomy")
                if config.auto_publish:
                    self.http.public_posts.moderate(
                        workflow.post_id,
                        subject_id=subject_id,
                        status="published",
                        actor="autonomy",
                        reason="operator-enabled autonomous wallet publication",
                        expected_status="pending_review",
                        idempotency_key=f"autonomous-moderation:{workflow.workflow_id}",
                    )
                    self.http.wallet_rewards.publish(
                        workflow.workflow_id, subject_id, actor="autonomy"
                    )
                created += 1
            except (IntegrityError, InvalidTransitionError, ValueError) as error:
                LOGGER.warning(
                    "autonomous wallet workflow deferred for %s: %s",
                    request["request_id"],
                    type(error).__name__,
                )
        return created

    async def _sleep_reflection(self, run: SleepRunRecord) -> SleepReflectionPlan:
        if self.cognition is not None:
            return await self.cognition.reflect_sleep(run)
        with self.kernel.database.connection() as connection:
            event_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM events WHERE subject_id = ? AND occurred_at >= ?",
                    (self.kernel.subject_id, run.started_at),
                ).fetchone()[0]
            )
            pending_interactions = int(
                connection.execute(
                    "SELECT COUNT(*) FROM interactions WHERE subject_id = ? "
                    "AND status IN ('offered', 'deferred')",
                    (self.kernel.subject_id,),
                ).fetchone()[0]
            )
        return SleepReflectionPlan(
            summary=(
                f"Automatic bounded reflection for {run.sleep_id}: reviewed {event_count} "
                f"events and retained {pending_interactions} pending communication invitations. "
                "No unvalidated memory, belief, goal, or personality revision was committed."
            ),
            facts=(
                f"Events recorded since sleep began: {event_count}.",
                f"Pending communication invitations: {pending_interactions}.",
            ),
        )


def main() -> None:
    multiprocessing.freeze_support()
    parser = argparse.ArgumentParser(prog="noyra-service")
    parser.add_argument("--log-level", default=os.getenv("NOYRA_LOG_LEVEL", "INFO"))
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, str(args.log_level).upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        service = NoyraService.from_env()
    except (ValueError, RuntimeError) as error:
        # Configuration/dependency profile failures are not transient service
        # faults.  Supervisors use EX_CONFIG to stop restart storms while
        # retaining a clear journal error for operators.
        LOGGER.error("service configuration failed: %s", error)
        raise SystemExit(EX_CONFIG) from error
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
