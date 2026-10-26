from __future__ import annotations

import argparse
import asyncio
import hashlib
import hmac
import html
import io
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
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, ClassVar, Concatenate, Literal, ParamSpec, TypeVar, cast
from urllib.parse import parse_qs, quote, unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from noyra.autonomy import AutonomyLoop, LoopConfig, TickResult
from noyra.capability import CapabilityStore
from noyra.cognition import (
    CognitionCycle,
    CognitionSettings,
    ControlledSelfModification,
)
from noyra.core.admission import OperationInvalidated, bind_lease
from noyra.core.archive import CloudArchiveCoordinator, S3ArchiveProvider, StorageQuota
from noyra.core.at_rest import AtRestConfig, AtRestError, AtRestGuard
from noyra.core.credentials import CredentialError, read_env_secret, read_secret_file
from noyra.core.database import Database
from noyra.core.deadline import operation_deadline, remaining_seconds
from noyra.core.errors import (
    IntegrityError,
    InvalidTransitionError,
    NotFoundError,
    RuntimeOwnershipError,
)
from noyra.core.export_jobs import ExportControl, ExportJobManager
from noyra.core.identity import validate_subject_id
from noyra.core.integrity import (
    IntegrityAuditLimits,
    IntegrityAuditShutdown,
    IntegrityRuntimeController,
)
from noyra.core.locking import ProcessLock
from noyra.core.managed_data_root import managed_data_root
from noyra.core.operator_controls import (
    OperatorControlConflict,
    OperatorControlService,
)
from noyra.core.private_sources import WITHDRAWN_PARTICIPANT_INVITATION
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.redaction import redact_secrets
from noyra.core.release_assurance import upgrade_channel
from noyra.core.request_io import DeadlineRequestReader
from noyra.core.retention import RetentionManager, RetentionSettings
from noyra.core.runtime import SubjectKernel
from noyra.core.runtime_export import RuntimeLogExporter
from noyra.core.runtime_observation import RuntimeObserver
from noyra.core.storage import StorageLayout, TrainingStore
from noyra.core.storage_lifecycle import StorageLifecycleManager
from noyra.core.storage_mode import (
    StorageLayout as StorageContract,
)
from noyra.core.storage_mode import (
    StorageMode,
    StorageUnavailableError,
    detect_storage_layout,
    resolve_storage_profile,
)
from noyra.core.training_export import TrainingDatasetExporter, TrainingExportLimits
from noyra.core.types import content_hash, strict_json_loads, utc_now
from noyra.core.upgrade import (
    UPGRADE_CURRENT_RELEASE_PATH,
    UPGRADE_REQUEST_PATH,
    UPGRADE_RUNNER_TRIGGER_PATH,
    UPGRADE_SOURCE_PATH,
    UPGRADE_STATUS_PATH,
    UpgradeManager,
)
from noyra.http_access_logging import http_log_fields
from noyra.http_routes import (
    cognition,
    exports,
    get,
    interaction,
    knowledge,
    operator,
    post,
    public_posts,
    search,
    wallet_automation,
    wallet_payments,
    wallet_resources,
    wallet_rewards,
)
from noyra.http_routes.validation import (
    _MODEL_RESOURCE_EXACT_FIELDS as _MODEL_RESOURCE_EXACT_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_ACQUISITION_COUNT_FIELDS as _WALLET_ACQUISITION_COUNT_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_ACQUISITION_FIELDS as _WALLET_ACQUISITION_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_ACQUISITION_FIELDS_WITH_STATUS as _WALLET_ACQUISITION_FIELDS_WITH_STATUS,
)
from noyra.http_routes.validation import (
    _WALLET_BALANCE_HISTORY_FIELDS as _WALLET_BALANCE_HISTORY_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_BREAKDOWN_COUNT_FIELDS as _WALLET_BREAKDOWN_COUNT_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_BREAKDOWN_FIELDS as _WALLET_BREAKDOWN_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_BREAKDOWN_GROUP_FIELDS as _WALLET_BREAKDOWN_GROUP_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_HEALTH_STATUSES as _WALLET_HEALTH_STATUSES,
)
from noyra.http_routes.validation import (
    _WALLET_MAX_INT as _WALLET_MAX_INT,
)
from noyra.http_routes.validation import (
    _WALLET_OBSERVATION_COUNT_FIELDS as _WALLET_OBSERVATION_COUNT_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_OBSERVATION_FIELDS as _WALLET_OBSERVATION_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_OBSERVATION_SOURCE as _WALLET_OBSERVATION_SOURCE,
)
from noyra.http_routes.validation import (
    _WALLET_RESOURCE_FIELDS as _WALLET_RESOURCE_FIELDS,
)
from noyra.http_routes.validation import (
    _WALLET_RESOURCE_NAMES as _WALLET_RESOURCE_NAMES,
)
from noyra.http_routes.validation import (
    _WALLET_SUMMARY_FIELDS as _WALLET_SUMMARY_FIELDS,
)
from noyra.http_routes.validation import (
    _cognitive_resource_payload as _cognitive_resource_payload,
)
from noyra.http_routes.validation import (
    _wallet_acquisition_attempt_payload as _wallet_acquisition_attempt_payload,
)
from noyra.http_routes.validation import (
    _wallet_acquisition_projection as _wallet_acquisition_projection,
)
from noyra.http_routes.validation import (
    _wallet_acquisition_run_payload as _wallet_acquisition_run_payload,
)
from noyra.http_routes.validation import (
    _wallet_exact_dict as _wallet_exact_dict,
)
from noyra.http_routes.validation import (
    _wallet_nonnegative_int as _wallet_nonnegative_int,
)
from noyra.http_routes.validation import (
    _wallet_observation_health as _wallet_observation_health,
)
from noyra.http_routes.validation import (
    _wallet_observation_health_breakdown as _wallet_observation_health_breakdown,
)
from noyra.http_routes.validation import (
    _wallet_resource_counts as _wallet_resource_counts,
)
from noyra.http_routes.validation import (
    _wallet_status_summary_projection as _wallet_status_summary_projection,
)
from noyra.http_routes.validation import (
    _wallet_timestamp as _wallet_timestamp,
)
from noyra.http_routes.validation import (
    _wallet_unavailable as _wallet_unavailable,
)
from noyra.interaction import (
    ADAPTERS,
    REPLAY_WINDOW_SECONDS,
    DeliveryDispatcher,
    InboundAuthenticationError,
    InboundChallenge,
    InboundIdentityObserved,
    InboundIgnored,
    InboundStore,
    InteractionStore,
    PublicPostStore,
    PublicProjection,
    TransportStore,
    WeChatInboundAdapter,
)
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired
from noyra.interaction.public_archive import (
    ARCHIVE_CONTRACT,
    PublicArchive,
    PublicQueryBudgetExceeded,
)
from noyra.interaction.public_contract import (
    PUBLIC_CACHE_CONTROL,
    PUBLIC_CONTRACT_ID,
    PUBLIC_MAX_RESPONSE_BYTES,
)
from noyra.interaction.public_reading import PUBLIC_VIEWS, public_article_html, public_item_path
from noyra.knowledge import (
    CommonKnowledgeStore,
)
from noyra.migration import (
    CutoverCoordinator,
    HTTPMigrationExecutor,
    MigrationExecutionError,
    MigrationManager,
    MigrationProposalStore,
    MigrationStore,
    RecoveryCoordinator,
    SQLiteArtifactProvider,
    TargetRegistry,
)
from noyra.migration.discovery import MigrationDiscovery, TargetObservationProvider
from noyra.migration.runtime import MigrationRuntime
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceStore,
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
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.errors import (
    EmbeddingShutdownIncompleteError,
)
from noyra.model.reasoning import (
    ReasoningEffort,
)
from noyra.research import SearchProviderStore
from noyra.service_contract import APIRouteContract, route_contracts
from noyra.sleep import FatigueTracker, SleepReflectionPlan, SleepRunRecord
from noyra.wallet import (
    WALLET_ACQUISITION_MAX_BATCH,
    WALLET_BALANCE_OBSERVATION_DEFAULT_GROUP_LIMIT,
    RewardWorkflowInput,
    WalletBalanceAcquisitionLedger,
    WalletBalanceAcquisitionRunner,
    WalletEconomyStore,
    WalletPaymentExecutionEngine,
    WalletRewardWorkflow,
    WalletSigner,
    WalletStore,
)
from noyra.wallet.config import configured_wallet_signer_from_env
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
    if os.getenv("NOYRA_PROFILE", "development").strip().lower() == "production":
        release_gate = os.getenv("NOYRA_WALLET_AUTOMATION_RELEASE_GATE", "").strip().lower()
        if release_gate != "external_verified":
            raise ValueError("production wallet automation requires an external gate approval")

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


def _ascii_token_equal(supplied: object, expected: str) -> bool:
    """Reject malformed credentials without changing valid token equivalence."""
    return (
        isinstance(supplied, str)
        and supplied.isascii()
        and expected.isascii()
        and hmac.compare_digest(supplied, expected)
    )


def _verify_trusted_proxy_principal(
    principal: object,
    timestamp: object,
    signature: object,
    peer_address: object,
    *,
    trusted_proxy_cidrs: tuple[str, ...],
    hmac_key: SecretStr | None,
    method: str,
    path: str,
    now: int | None = None,
) -> str | None:
    if (
        hmac_key is None
        or not isinstance(principal, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@/-]{0,127}", principal)
        or not isinstance(timestamp, str)
        or re.fullmatch(r"[0-9]{1,12}", timestamp) is None
        or not isinstance(signature, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", signature) is None
        or not isinstance(path, str)
        or not path.startswith("/")
        or len(path) > 4_096
    ):
        return None
    try:
        peer = ipaddress.ip_address(str(peer_address))
        timestamp_value = int(timestamp)
    except ValueError:
        return None
    if not any(peer in ipaddress.ip_network(cidr, strict=False) for cidr in trusted_proxy_cidrs):
        return None
    if abs((int(time.time()) if now is None else now) - timestamp_value) > 60:
        return None
    message = "\n".join(
        (
            "noyra-authenticated-principal/v1",
            principal,
            timestamp,
            method.upper(),
            path,
        )
    ).encode("utf-8")
    expected = hmac.new(
        hmac_key.get_secret_value().encode("utf-8"), message, hashlib.sha256
    ).hexdigest()
    return principal if hmac.compare_digest(signature.casefold(), expected) else None


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
    backup_root: Path = Path("/var/backups/noyra")
    subject_id: str = Field(min_length=3, max_length=128)
    genesis_hash: str = Field(min_length=64, max_length=64)
    profile: Literal["development", "test", "production"] = "development"
    deployment_profile: Literal["host", "container_internal"] = "host"
    storage_profile: Literal["runtime_only", "recovery_capable"] | None = None
    host: str = Field(default="127.0.0.1", min_length=1, max_length=255)
    port: int = Field(default=8765, ge=0, le=65_535)
    admin_token: SecretStr | None = None
    read_token: SecretStr | None = None
    operator_token: SecretStr | None = None
    operator_token_file: Path | None = None
    export_token: SecretStr | None = None
    break_glass_token: SecretStr | None = None
    allow_insecure_non_loopback: bool = False
    max_http_threads: int = Field(default=32, ge=4, le=512)
    request_rate_limit_per_minute: int = Field(default=120, ge=10, le=100_000)
    admin_login_rate_limit_per_minute: int = Field(default=5, ge=1, le=1_000)
    trusted_proxy_cidrs: tuple[str, ...] = ()
    public_post_rate_limit_per_hour: int = Field(default=10, ge=1, le=100_000)
    public_post_queue_cap: int = Field(default=1_000, ge=1, le=100_000)
    public_post_captcha_ttl_seconds: int = Field(default=300, ge=30, le=3_600)
    public_post_captcha_max_attempts: int = Field(default=5, ge=1, le=20)
    public_post_captcha_mode: Literal["letters", "digits", "alphanumeric"] = "digits"
    public_post_storage_cap_bytes: int = Field(default=250_000_000, ge=1_000, le=2_000_000_000)
    public_post_captcha_issue_limit_per_hour: int = Field(default=30, ge=1, le=100_000)
    public_post_captcha_global_rate_per_minute: int = Field(default=300, ge=1, le=100_000)
    public_hash_key_file: Path | None = None
    public_site_url: str | None = None
    public_sitemap_keyring_path: Path | None = None
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
    migration_enabled: bool = False
    migration_approval_mode: Literal[
        "disabled",
        "manual",
        "self_attested",
        "dual_review",
        "policy_auto",
        "emergency_recovery",
    ] = "disabled"
    migration_rejection_cooldown_days: int = Field(default=7, ge=0, le=365)
    migration_proposal_expiry_seconds: int = Field(default=86400, ge=300, le=604800)
    migration_wallet_mode: Literal[
        "external_signer_rebind", "local_wallet_transfer", "disabled"
    ] = "external_signer_rebind"
    migration_local_wallet_transfer_enabled: bool = False
    migration_require_independent_review: bool = False
    migration_proxy_principal_hmac_key: SecretStr | None = None

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

    @field_validator("public_site_url", mode="before")
    @classmethod
    def validate_public_site_url(cls, value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError("public site URL must be a string")
        normalized = value.strip()
        if not normalized:
            return None
        if any(character.isspace() or ord(character) < 0x21 for character in normalized):
            raise ValueError("public site URL contains whitespace")
        try:
            parsed = urlsplit(normalized)
            hostname = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ValueError("public site URL is invalid") from error
        if (
            parsed.scheme.lower() != "https"
            or not hostname
            or port == 0
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("public site URL must be an HTTPS origin")
        try:
            ascii_hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise ValueError("public site URL host is invalid") from error
        authority_host = f"[{ascii_hostname}]" if ":" in ascii_hostname else ascii_hostname
        authority = authority_host if port is None else f"{authority_host}:{port}"
        return f"https://{authority}"

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("service host cannot be blank")
        return value

    @field_validator("operator_token_file", mode="before")
    @classmethod
    def validate_operator_token_file(cls, value: object) -> Path | None:
        if value is None or value == "":
            return None
        path = Path(str(value)).expanduser()
        if not path.is_absolute():
            raise ValueError("operator token file must be an absolute path")
        return path

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

    @field_validator("migration_proxy_principal_hmac_key")
    @classmethod
    def validate_migration_proxy_principal_hmac_key(
        cls, value: SecretStr | None
    ) -> SecretStr | None:
        if value is not None and len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("migration proxy principal HMAC key must contain at least 32 bytes")
        return value

    @model_validator(mode="after")
    def validate_listener_security(self) -> ServiceSettings:
        if self.storage_profile is None:
            object.__setattr__(
                self,
                "storage_profile",
                resolve_storage_profile(self.profile, self.storage_profile),
            )
        loopback = {"127.0.0.1", "::1", "localhost"}
        if self.profile == "production":
            # Production is always terminated by a trusted HTTPS proxy while
            # the runtime remains on loopback.  The explicit insecure escape
            # hatch is intentionally unavailable in this profile.
            if self.host.casefold() not in loopback and (
                self.deployment_profile != "container_internal"
            ):
                raise ValueError(
                    "production profile requires a loopback HTTP listener unless "
                    "deployment_profile is container_internal"
                )
            if self.deployment_profile == "container_internal" and not (
                self.allow_insecure_non_loopback
            ):
                raise ValueError(
                    "container_internal deployment requires NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true"
                )
            # A production operator must not be able to downgrade the session
            # cookie through a stale environment file or template value.
            object.__setattr__(self, "admin_session_cookie_secure", True)
            if self.at_rest_mode != "required":
                raise ValueError("production profile requires at-rest protection")
            if self.integrity_mode == "off":
                raise ValueError("production profile requires integrity monitoring")
            if self.developer_log_export_enabled:
                # The field default is intentionally development-friendly for
                # direct construction in tests and libraries.  Production
                # still fails closed by forcing the unsafe option off rather
                # than accepting a stale environment value.
                object.__setattr__(self, "developer_log_export_enabled", False)
        if self.host.casefold() not in loopback and not self.allow_insecure_non_loopback:
            raise ValueError(
                "non-loopback HTTP listener requires NOYRA_ALLOW_INSECURE_NON_LOOPBACK=true"
            )
        if self.integrity_max_value_bytes > self.integrity_max_bytes_per_check:
            raise ValueError("integrity max value bytes cannot exceed the per-check byte budget")
        if self.migration_require_independent_review and (
            self.migration_proxy_principal_hmac_key is None
        ):
            raise ValueError("strict migration review requires a signed proxy principal HMAC key")
        if self.migration_require_independent_review and not self.trusted_proxy_cidrs:
            raise ValueError("strict migration review requires trusted proxy CIDRs")
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
    def _read_operator_token_file(path: Path | None) -> str | None:
        if path is None:
            return None
        try:
            return read_secret_file(path, label="operator token file", single_line=True)
        except (CredentialError, OSError, UnicodeError, ValueError) as error:
            raise ValueError("operator token file is unreadable") from error

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
        managed_root = managed_data_root()
        profile = os.getenv("NOYRA_PROFILE", "development").strip().lower()
        subject_id = os.getenv("NOYRA_SUBJECT_ID", "Noyra-0001")
        configured_genesis_hash = os.getenv("NOYRA_GENESIS_HASH", "").strip()
        if profile == "production" and not configured_genesis_hash:
            raise ValueError("production requires an explicit NOYRA_GENESIS_HASH")
        genesis_hash = configured_genesis_hash or content_hash(
            {"project": "Noyra", "subject_id": subject_id, "origin": "configured-runtime"}
        )
        token = os.getenv("NOYRA_ADMIN_TOKEN")
        read_token = os.getenv("NOYRA_READ_TOKEN")
        operator_token_file_value = os.getenv("NOYRA_OPERATOR_TOKEN_FILE")
        operator_token_file = (
            cls.validate_operator_token_file(operator_token_file_value)
            if operator_token_file_value
            else None
        )
        try:
            operator_token = read_env_secret(
                value_var="NOYRA_OPERATOR_TOKEN",
                file_var="NOYRA_OPERATOR_TOKEN_FILE",
                credential_var="NOYRA_OPERATOR_TOKEN_CREDENTIAL",
                label="operator token",
                allow_inline=profile != "production",
            )
        except CredentialError as error:
            raise ValueError(str(error)) from error
        try:
            migration_proxy_principal_hmac_key = read_env_secret(
                value_var="NOYRA_MIGRATION_PROXY_PRINCIPAL_HMAC_KEY",
                file_var="NOYRA_MIGRATION_PROXY_PRINCIPAL_HMAC_KEY_FILE",
                credential_var="NOYRA_MIGRATION_PROXY_PRINCIPAL_HMAC_KEY_CREDENTIAL",
                label="migration proxy principal HMAC key",
                allow_inline=profile != "production",
            )
        except CredentialError as error:
            raise ValueError(str(error)) from error
        export_token = os.getenv("NOYRA_EXPORT_TOKEN")
        break_glass_token = os.getenv("NOYRA_BREAK_GLASS_TOKEN")
        return cls(
            data_dir=managed_root or Path(os.getenv("NOYRA_DATA_DIR", ".runtime/data")),
            subject_id=subject_id,
            genesis_hash=genesis_hash,
            profile=cast(Literal["development", "test", "production"], profile),
            deployment_profile=cast(
                Literal["host", "container_internal"],
                os.getenv("NOYRA_DEPLOYMENT_PROFILE", "host").strip().lower(),
            ),
            storage_profile=cast(
                Literal["runtime_only", "recovery_capable"] | None,
                (os.getenv("NOYRA_STORAGE_PROFILE", "").strip().lower() or None),
            ),
            host=os.getenv("NOYRA_HOST", "127.0.0.1"),
            port=int(os.getenv("NOYRA_PORT", "8765")),
            admin_token=SecretStr(token) if token else None,
            read_token=SecretStr(read_token) if read_token else None,
            operator_token=SecretStr(operator_token) if operator_token else None,
            operator_token_file=operator_token_file,
            export_token=SecretStr(export_token) if export_token else None,
            break_glass_token=SecretStr(break_glass_token) if break_glass_token else None,
            allow_insecure_non_loopback=cls._environment_flag(
                "NOYRA_ALLOW_INSECURE_NON_LOOPBACK", "false"
            ),
            max_http_threads=int(os.getenv("NOYRA_MAX_HTTP_THREADS", "32")),
            request_rate_limit_per_minute=int(
                os.getenv("NOYRA_REQUEST_RATE_LIMIT_PER_MINUTE", "120")
            ),
            admin_login_rate_limit_per_minute=int(
                os.getenv("NOYRA_ADMIN_LOGIN_RATE_LIMIT_PER_MINUTE", "5")
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
                os.getenv("NOYRA_PUBLIC_POST_CAPTCHA_MODE", "digits").strip().lower(),
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
            public_hash_key_file=(
                Path(value)
                if (value := os.getenv("NOYRA_PUBLIC_HASH_KEY_FILE", "").strip())
                else None
            ),
            public_site_url=os.getenv("NOYRA_PUBLIC_SITE_URL"),
            public_sitemap_keyring_path=(
                Path(os.environ["NOYRA_PUBLIC_SITEMAP_KEYRING_PATH"])
                if os.getenv("NOYRA_PUBLIC_SITEMAP_KEYRING_PATH")
                else None
            ),
            backup_root=Path(os.getenv("NOYRA_BACKUP_ROOT", "/var/backups/noyra")),
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
                "NOYRA_DEVELOPER_LOG_EXPORT_ENABLED",
                "false" if profile == "production" else "true",
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
                "NOYRA_ADMIN_SESSION_COOKIE_SECURE",
                "true" if profile == "production" else "false",
            ),
            migration_enabled=cls._environment_flag("NOYRA_MIGRATION_ENABLED", "false"),
            migration_approval_mode=cast(
                Literal[
                    "disabled",
                    "manual",
                    "self_attested",
                    "dual_review",
                    "policy_auto",
                    "emergency_recovery",
                ],
                os.getenv("NOYRA_MIGRATION_APPROVAL_MODE", "disabled").strip().lower(),
            ),
            migration_rejection_cooldown_days=int(
                os.getenv("NOYRA_MIGRATION_REJECTION_COOLDOWN_DAYS", "7")
            ),
            migration_proposal_expiry_seconds=int(
                os.getenv("NOYRA_MIGRATION_PROPOSAL_EXPIRY_SECONDS", "86400")
            ),
            migration_wallet_mode=cast(
                Literal["external_signer_rebind", "local_wallet_transfer", "disabled"],
                os.getenv("NOYRA_MIGRATION_WALLET_MODE", "external_signer_rebind").strip().lower(),
            ),
            migration_local_wallet_transfer_enabled=cls._environment_flag(
                "NOYRA_MIGRATION_LOCAL_WALLET_TRANSFER_ENABLED", "false"
            ),
            migration_require_independent_review=cls._environment_flag(
                "NOYRA_MIGRATION_REQUIRE_INDEPENDENT_REVIEW", "false"
            ),
            migration_proxy_principal_hmac_key=(
                SecretStr(migration_proxy_principal_hmac_key)
                if migration_proxy_principal_hmac_key
                else None
            ),
        )


def _load_public_post_hash_key(settings: ServiceSettings) -> bytes | None:
    path = settings.public_hash_key_file or (
        settings.data_dir / "secrets" / "public-post-ip-hash.key"
    )
    if not path.exists():
        if settings.profile == "production":
            raise ValueError("production requires a persistent public anti-abuse hash key")
        return None
    try:
        key = read_secret_file(path, label="public anti-abuse hash key").encode("utf-8")
    except CredentialError as error:
        raise ValueError("public anti-abuse hash key is unavailable or unsafe") from error
    if len(key) < 32:
        raise ValueError("public anti-abuse hash key must contain at least 32 bytes")
    return key


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
    storage_contract: StorageContract

    def __init__(
        self,
        kernel: SubjectKernel,
        settings: ServiceSettings,
        *,
        allow_common_knowledge_key_creation: bool = True,
        repair_secrets_on_init: bool = True,
        wallet_signer: WalletSigner | None = None,
        close_wallet_signer: bool = False,
        upgrade_manager: UpgradeManager | None = None,
    ):
        public_hash_key = _load_public_post_hash_key(settings)
        self.kernel = kernel
        self.settings = settings
        self.upgrade_manager = upgrade_manager or UpgradeManager(
            source_path=Path(os.getenv("NOYRA_UPGRADE_SOURCE", str(UPGRADE_SOURCE_PATH))),
            current_release_path=Path(
                os.getenv("NOYRA_UPGRADE_CURRENT_RELEASE", str(UPGRADE_CURRENT_RELEASE_PATH))
            ),
            status_path=Path(os.getenv("NOYRA_UPGRADE_STATUS", str(UPGRADE_STATUS_PATH))),
            request_path=Path(os.getenv("NOYRA_UPGRADE_REQUEST_PATH", str(UPGRADE_REQUEST_PATH))),
            runner_trigger_path=Path(
                os.getenv("NOYRA_UPGRADE_TRIGGER_PATH", str(UPGRADE_RUNNER_TRIGGER_PATH))
            ),
            github_owner=os.getenv("NOYRA_UPGRADE_GITHUB_OWNER", "EverForAI"),
            github_repo=os.getenv("NOYRA_UPGRADE_GITHUB_REPO", "Noyra"),
            channel=upgrade_channel(),
        )
        self.admission = kernel.admission
        self.quarantine_checker: Any = None
        self.event_loop: asyncio.AbstractEventLoop | None = None
        self._async_handler_condition = threading.Condition()
        self._async_handler_operations: dict[ConcurrentFuture[Any], threading.Event] = {}
        self.integrity: IntegrityRuntimeController | None = None
        self.at_rest: AtRestGuard | None = None
        self.operator_controls: OperatorControlService | None = None
        self.projection = PublicProjection(kernel.database)
        from noyra.interaction.sitemap_tokens import SitemapTokens

        sitemap_tokens = (
            SitemapTokens.from_file(settings.public_sitemap_keyring_path)
            if settings.public_sitemap_keyring_path is not None
            else SitemapTokens()
        )
        self.public_archive = PublicArchive(kernel.database, sitemap_tokens=sitemap_tokens)
        self.provider_health = ProviderHealthStore(kernel.database)
        self.retention = RetentionManager(
            kernel.database,
            RetentionSettings.from_env(),
            minimum_free_bytes=settings.minimum_free_storage_bytes,
            backup_root=settings.backup_root,
        )
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
        self.conversations = ConversationStore(kernel.database)
        self.inbound = InboundStore(kernel.database, self.interactions)
        self.public_posts = PublicPostStore(
            kernel.database,
            rate_limit_per_hour=settings.public_post_rate_limit_per_hour,
            queue_cap=settings.public_post_queue_cap,
            storage_cap_bytes=settings.public_post_storage_cap_bytes,
            minimum_free_bytes=settings.minimum_free_storage_bytes,
            captcha_issue_limit_per_hour=settings.public_post_captcha_issue_limit_per_hour,
            captcha_global_rate_per_minute=(settings.public_post_captcha_global_rate_per_minute),
            ip_hash_key=public_hash_key,
            allow_ephemeral_ip_hash_key=settings.profile != "production",
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
        self.wallet_economy = WalletEconomyStore(
            kernel.database, production=settings.profile == "production"
        )
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
        self.embedding_runtime_status: Callable[[], dict[str, Any]] = lambda: {
            "status": "unavailable",
            "restart_required": True,
        }
        self.cognition_is_enabled: Callable[[], bool] = lambda: False
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
        self.migration_store = MigrationStore(
            kernel.database, production=settings.profile == "production"
        )
        self.migration_targets = TargetRegistry(kernel.database, self.migration_store)
        self.migration_proposals = MigrationProposalStore(kernel.database)
        self.migration_manager = MigrationManager(
            kernel.database,
            self.migration_store,
            require_principal_identity=settings.migration_require_independent_review,
        )
        database_path = (settings.data_dir / "noyra.sqlite3").resolve()
        self.migration_http_executor = HTTPMigrationExecutor(
            target_resolver=self._migration_http_target,
            token_resolver=self._migration_http_token,
            artifact_resolver=SQLiteArtifactProvider(
                database_path,
                settings.data_dir / "migration" / "source" / "outgoing",
            ),
            source_fence=self._migration_http_source_fence,
            source_unfence=self._migration_http_source_unfence,
        )
        self.migration_cutover = CutoverCoordinator(
            kernel.database,
            production=settings.profile == "production",
            admission=kernel.admission,
            executor=self.migration_http_executor,
        )
        self.migration_recovery = RecoveryCoordinator(kernel.database)
        observation_provider = TargetObservationProvider(
            lambda target_id: self._migration_http_target(
                SimpleNamespace(target_id=target_id, subject_id=kernel.subject_id)
            ),
            lambda target_id: self._migration_http_token(SimpleNamespace(target_id=target_id)),
            self.migration_http_executor.transport,
        )
        self.migration_runtime = MigrationRuntime(
            self,
            MigrationDiscovery(kernel.database, observation_provider),
            gateway=lambda: self.cognition_gateway,
            to_thread=asyncio.to_thread,
        )
        self._migration_worker: threading.Thread | None = None
        self._migration_worker_lock = threading.Lock()
        handler = self._handler_type()
        self._rate_lock = threading.Lock()
        self._request_times: dict[str, deque[float]] = defaultdict(deque)
        self._storage_observation_lock = threading.Lock()
        self._storage_observation_at = 0.0
        self._storage_observation: tuple[StorageContract, str] | None = None
        self.runtime_observer = RuntimeObserver(
            self.kernel.database.path, self.settings.data_dir, self.kernel.subject_id
        )
        self._health_cache_lock = threading.Lock()
        self._health_cache_at = 0.0
        self._health_cache: tuple[HTTPStatus, dict[str, Any]] | None = None
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

    def start_migration(self, task_id: str) -> None:
        task = self.migration_manager.get_task(task_id)
        if task.subject_id != self.kernel.subject_id or task.status != "approved":
            raise ValueError("migration task is not approved")
        with self._migration_worker_lock:
            if self._closed or (self._migration_worker and self._migration_worker.is_alive()):
                raise ValueError("migration execution is already active or draining")

            def execute() -> None:
                if not self.migration_runtime._lock.acquire(blocking=False):
                    return
                try:
                    self.migration_runtime.status = "executing"
                    self.migration_runtime.execute(task_id)
                    self.migration_runtime.status = "committed"
                except Exception as error:
                    self.migration_runtime.status = "reconciliation_required"
                    LOGGER.warning("migration execution deferred: %s", type(error).__name__)
                finally:
                    self.migration_runtime._lock.release()

            self._migration_worker = threading.Thread(
                target=execute,
                name="noyra-migration",
                daemon=False,
            )
            self._migration_worker.start()

    def _migration_http_target(self, task: Any) -> Mapping[str, Any]:
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM migration_targets WHERE target_id=? AND subject_id=?",
                (task.target_id, task.subject_id),
            ).fetchone()
        if row is None:
            raise MigrationExecutionError("migration_target_not_found")
        self.migration_targets._assert_row_integrity(row)
        if row["status"] != "active":
            raise MigrationExecutionError("migration_target_not_active")
        if not row["recipient_public_key"] or not row["recipient_key_fingerprint"]:
            raise MigrationExecutionError("migration_target_recipient_key_unavailable")
        return {
            "target_id": row["target_id"],
            "endpoint": row["endpoint"],
            "public_key": row["public_key"],
            "recipient_public_key": row["recipient_public_key"],
            "recipient_key_fingerprint": row["recipient_key_fingerprint"],
            "enrollment_generation": int(row["enrollment_generation"]),
            "encrypted_volume": bool(row["encrypted_volume"]),
            "release_sha": row["release_sha"],
        }

    def _migration_http_token(self, task: Any) -> str:
        token_root = Path(
            os.getenv(
                "NOYRA_MIGRATION_TARGET_TOKEN_DIR",
                str(self.settings.data_dir / "secrets" / "migration-targets"),
            )
        )
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,128}", task.target_id):
            raise MigrationExecutionError("migration_target_id_invalid")
        try:
            return read_secret_file(
                token_root / f"{task.target_id}.token",
                label="migration target token",
            )
        except CredentialError as error:
            raise MigrationExecutionError("migration_target_token_unavailable") from error

    def _migration_http_source_fence(self, task: Any, source_epoch: str) -> str:
        try:
            self.kernel.admission.assert_migration_fenced()
        except OperationInvalidated as error:
            raise MigrationExecutionError("runtime_migration_fence_missing") from error
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT e.epoch_id,e.epoch_number,e.status FROM migration_epochs e "
                "JOIN migration_tasks t ON t.target_epoch_id=e.epoch_id "
                "WHERE t.task_id=? AND t.subject_id=? AND e.status='active' "
                "AND t.source_epoch=?",
                (task.task_id, task.subject_id, source_epoch),
            ).fetchone()
        if row is None:
            raise MigrationExecutionError("source_epoch_not_fenced")
        return content_hash(
            {
                "task_id": task.task_id,
                "source_epoch": source_epoch,
                "epoch_id": row["epoch_id"],
                "epoch_number": int(row["epoch_number"]),
                "status": row["status"],
            }
        )

    def _migration_http_source_unfence(self, task: Any, source_epoch: str) -> None:
        # The durable EpochLease remains active until the operator explicitly
        # rolls the task back; keeping it active is the fail-closed recovery
        # state when a remote target fails after source fencing.
        del task, source_epoch

    def _current_storage(self) -> tuple[StorageContract, str]:
        """Observe current identity with bounded subprocesses and a short shared TTL."""
        if not self._storage_observation_lock.acquire(timeout=0.25):
            raise StorageUnavailableError("storage_probe_busy")
        try:
            ttl = min(self.settings.health_cache_ttl_seconds, 5.0)
            if (
                self._storage_observation is not None
                and time.monotonic() - self._storage_observation_at < ttl
            ):
                return self._storage_observation
            try:
                layout = detect_storage_layout(self.settings.data_dir, self.settings.backup_root)
            except (StorageUnavailableError, OSError):
                layout = StorageContract(
                    data_root=self.settings.data_dir,
                    backup_root=self.settings.backup_root,
                    upgrade_root=self.settings.data_dir / "upgrade",
                    mode=StorageMode.UNAVAILABLE,
                    data_device=None,
                    backup_device=None,
                    writable=False,
                    reason_code="storage_probe_unavailable",
                    backup_reason="storage_probe_unavailable",
                )
            observation = (layout, utc_now())
            self.storage_contract = layout
            self._storage_observation = observation
            self._storage_observation_at = time.monotonic()
            return observation
        finally:
            self._storage_observation_lock.release()

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
        if self.settings.public_sitemap_keyring_path is None:
            from noyra.interaction.sitemap_tokens import SitemapTokens

            self.public_archive.sitemap_tokens = SitemapTokens.managed(
                self.settings.data_dir / "secrets"
            )
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
        close_transport = getattr(self.migration_http_executor.transport, "close", None)
        if callable(close_transport):
            close_transport()
        if self._migration_worker is not None:
            # Do not abandon a worker retaining database ownership. Transport
            # cancellation/deadlines bound network waits; local snapshot work
            # must still drain before releasing the process lock.
            self._migration_worker.join()
        self.server.server_close()
        self.wallet_acquisition_runner.close()
        self.export_jobs.close()
        if self._close_wallet_signer and self.wallet_signer is not None:
            close = getattr(self.wallet_signer, "close", None)
            if callable(close):
                close()
        if self.at_rest is not None:
            close_guard = getattr(self.at_rest, "close", None)
            if callable(close_guard):
                close_guard()
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
        try:
            remaining = remaining_seconds(self.settings.request_timeout_seconds)
        except TimeoutError:
            coroutine.close()
            raise
        loop = self.event_loop
        if loop is not None and loop.is_running():
            bridge: ConcurrentFuture[Any] = ConcurrentFuture()
            done = threading.Event()
            self._register_async_handler(bridge, done)
            task_holder: dict[str, asyncio.Task[Any]] = {}

            async def start_if_current() -> Any:
                try:
                    remaining_seconds(remaining)
                except TimeoutError:
                    coroutine.close()
                    raise
                return await coroutine

            def spawn() -> None:
                wrapper = start_if_current()
                try:
                    task = loop.create_task(wrapper)
                except BaseException as error:
                    wrapper.close()
                    coroutine.close()
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
                        with suppress(Exception):
                            coroutine.close()
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
                return bridge.result(timeout=remaining_seconds(remaining))
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

        async def bounded() -> Any:
            try:
                async with asyncio.timeout(remaining_seconds(remaining)):
                    return await coroutine
            finally:
                coroutine.close()

        return asyncio.run(bounded())

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

    @staticmethod
    def _is_migration_control(path: str) -> bool:
        return path.startswith("/api/admin/migration/tasks/") and (
            path.endswith("/cutover") or path.endswith("/rollback")
        )

    def allow_mutation(self, path: str) -> bool:
        if self._closed or self.admission.closed:
            return False
        if self._quarantined():
            return self._is_recovery_mutation(path)
        if self._is_migration_control(path):
            return True
        if self.admission.accepting:
            return True
        # Manual pause closes cognition admission, but lifecycle controls must
        # remain available so an operator can resume or reset the runtime.
        return path in {
            "/api/admin/lifecycle/pause",
            "/api/admin/lifecycle/resume",
            "/api/admin/lifecycle/reset",
            "/api/admin/lifecycle/wake",
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

    @staticmethod
    def _rate_limit_bucket_key(client_ip: str) -> str:
        try:
            address = ipaddress.ip_address(client_ip)
            if isinstance(address, ipaddress.IPv6Address):
                address = ipaddress.ip_network(f"{address}/64", strict=False).network_address
            return str(address)
        except ValueError:
            return client_ip[:128]

    def allow_request(self, client_ip: str) -> bool:
        now = time.monotonic()
        cutoff = now - 60
        bucket_key = self._rate_limit_bucket_key(client_ip)
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

    def allow_admin_login(self, client_ip: str) -> bool:
        """Apply the failure budget through a durable SQLite bucket.

        Login requests may be handled by several workers or across a restart.
        The cleanup, count and reservation are one transaction so every worker
        observes the same budget.
        """
        now = time.time()
        cutoff = now - 60.0
        client_key_hash = self._admin_client_key_hash(client_ip)
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "DELETE FROM admin_login_rate_events WHERE subject_id=? AND occurred_at <= ?",
                (self.kernel.subject_id, cutoff),
            )
            count = connection.execute(
                "SELECT COUNT(*) FROM admin_login_rate_events "
                "WHERE subject_id=? AND client_key_hash=? AND occurred_at > ?",
                (self.kernel.subject_id, client_key_hash, cutoff),
            ).fetchone()[0]
            if int(count) >= self.settings.admin_login_rate_limit_per_minute:
                return False
            connection.execute(
                "INSERT INTO admin_login_rate_events("
                "event_id, subject_id, client_key_hash, occurred_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    f"admin_rate_{secrets.token_hex(16)}",
                    self.kernel.subject_id,
                    client_key_hash,
                    now,
                ),
            )
            return True

    def clear_admin_login_failures(self, client_ip: str) -> None:
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "DELETE FROM admin_login_rate_events WHERE subject_id=? AND client_key_hash=?",
                (self.kernel.subject_id, self._admin_client_key_hash(client_ip)),
            )

    @staticmethod
    def _admin_client_key_hash(client_ip: str) -> str:
        return hashlib.sha256(f"noyra-admin-client:{client_ip}".encode()).hexdigest()

    @staticmethod
    def _admin_session_hash(session_id: str) -> str:
        return hashlib.sha256(f"noyra-admin-session:{session_id}".encode()).hexdigest()

    def _admin_session_csrf(self, session_id: str, role: str) -> str:
        secret_value = {
            "admin": self.settings.admin_token,
            "operator": self.settings.operator_token,
            "break_glass": self.settings.break_glass_token,
        }.get(role)
        secret = None if secret_value is None else secret_value.get_secret_value()
        if secret is None:
            raise RuntimeError("admin session signing secret is unavailable")
        return hmac.new(
            secret.encode("ascii"),
            f"{self.kernel.subject_id}:{session_id}".encode(),
            hashlib.sha256,
        ).hexdigest()

    def _admin_session_state_hash(
        self,
        *,
        session_hash: str,
        role: str,
        actor: str,
        expires_at: float,
        created_at: float,
    ) -> str:
        return content_hash(
            {
                "session_hash": session_hash,
                "subject_id": self.kernel.subject_id,
                "role": role,
                "actor": actor,
                "expires_at": expires_at,
                "created_at": created_at,
            }
        )

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
        session_id = secrets.token_urlsafe(32)
        session_hash = self._admin_session_hash(session_id)
        expires_at = now + self.settings.admin_session_ttl_seconds
        session = _AdminSession(
            role=role,
            actor=actor,
            csrf_token=self._admin_session_csrf(session_id, role),
            expires_at=expires_at,
        )
        state_hash = self._admin_session_state_hash(
            session_hash=session_hash,
            role=role,
            actor=actor,
            expires_at=expires_at,
            created_at=now,
        )
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "DELETE FROM admin_sessions WHERE subject_id=? AND "
                "(revoked_at IS NOT NULL OR expires_at <= ?)",
                (self.kernel.subject_id, now),
            )
            oldest = connection.execute(
                "SELECT session_hash FROM admin_sessions WHERE subject_id=? "
                "AND revoked_at IS NULL AND expires_at > ? "
                "ORDER BY created_at ASC LIMIT 1 OFFSET ?",
                (self.kernel.subject_id, now, self.settings.admin_session_max_count - 1),
            ).fetchone()
            if oldest is not None:
                # A wall-clock correction must not prevent an existing session's revocation.
                connection.execute(
                    "UPDATE admin_sessions SET revoked_at=MAX(?, created_at) WHERE session_hash=?",
                    (now, oldest["session_hash"]),
                )
            connection.execute(
                "INSERT INTO admin_sessions("
                "session_hash, subject_id, role, actor, expires_at, created_at, "
                "revoked_at, state_hash) VALUES (?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    session_hash,
                    self.kernel.subject_id,
                    role,
                    actor,
                    expires_at,
                    now,
                    state_hash,
                ),
            )
        return session_id, session

    def admin_session(self, session_id: str) -> _AdminSession | None:
        if not session_id:
            return None
        now = time.time()
        session_hash = self._admin_session_hash(session_id)
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT role, actor, expires_at, created_at, revoked_at, state_hash "
                "FROM admin_sessions WHERE session_hash=? AND subject_id=?",
                (session_hash, self.kernel.subject_id),
            ).fetchone()
        if row is None or row["revoked_at"] is not None:
            return None
        expires_at = float(row["expires_at"])
        created_at = float(row["created_at"])
        if expires_at <= now:
            self.revoke_admin_session(session_id)
            return None
        expected_hash = self._admin_session_state_hash(
            session_hash=session_hash,
            role=str(row["role"]),
            actor=str(row["actor"]),
            expires_at=expires_at,
            created_at=created_at,
        )
        if not hmac.compare_digest(str(row["state_hash"]), expected_hash):
            return None
        return _AdminSession(
            role=str(row["role"]),
            actor=str(row["actor"]),
            csrf_token=self._admin_session_csrf(session_id, str(row["role"])),
            expires_at=expires_at,
        )

    def revoke_admin_session(self, session_id: str) -> None:
        """Revoke durably even if the wall clock moved behind session creation."""
        if not session_id:
            return
        with self.kernel.database.transaction() as connection:
            connection.execute(
                "UPDATE admin_sessions SET revoked_at=MAX(?, created_at) WHERE session_hash=? "
                "AND subject_id=? AND revoked_at IS NULL",
                (time.time(), self._admin_session_hash(session_id), self.kernel.subject_id),
            )

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
        # Preserve sub-second precision so events written earlier in this same
        # second are included in the aggregation window.
        observed_at = datetime.now(UTC)
        window_start = observed_at - timedelta(days=7)
        model_observation: dict[str, Any] = {}
        search_observation: dict[str, Any] = {
            "runs": 0,
            "by_method": {method: {} for method in ("api", "model", "browser", "wait")},
        }
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
                    "FROM interactions i WHERE subject_id = ? AND direction = 'incoming' "
                    "AND status IN ('offered', 'deferred') AND NOT "
                    + WITHDRAWN_PARTICIPANT_INVITATION
                    + " ORDER BY created_at, interaction_id LIMIT ?",
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
            usage = connection.execute(
                """SELECT COUNT(DISTINCT call.call_id) AS calls,
                    COUNT(DISTINCT CASE WHEN call.status='succeeded' THEN call.call_id END)
                        AS succeeded_calls,
                    COUNT(DISTINCT CASE WHEN call.status='failed' THEN call.call_id END)
                        AS failed_calls,
                    COUNT(DISTINCT CASE WHEN call.status='unknown' THEN call.call_id END)
                        AS unknown_calls,
                    COUNT(attempt.attempt_id) AS attempts,
                    SUM(CASE WHEN attempt.status='succeeded' THEN 1 ELSE 0 END) AS succeeded,
                    SUM(CASE WHEN attempt.status='failed' THEN 1 ELSE 0 END) AS failed,
                    SUM(CASE WHEN attempt.status='unknown' THEN 1 ELSE 0 END) AS unknown,
                    SUM(COALESCE(attempt.input_tokens, 0)) AS input_tokens,
                    SUM(COALESCE(attempt.output_tokens, 0)) AS output_tokens,
                    SUM(CASE WHEN attempt.cost_microusd IS NULL
                             THEN attempt.reserved_cost_microusd
                             ELSE attempt.cost_microusd END) AS cost_microusd,
                    SUM(CASE WHEN attempt.attempt_id IS NOT NULL
                                  AND (attempt.cost_microusd IS NULL OR call.usage_estimated=1)
                             THEN 1 ELSE 0 END) AS estimated_cost_attempts
                FROM model_calls AS call
                LEFT JOIN model_attempts AS attempt
                    ON attempt.call_id=call.call_id AND attempt.subject_id=call.subject_id
                WHERE call.subject_id=?
                    AND call.created_at>=? AND call.created_at<=?""",
                (
                    self.kernel.subject_id,
                    window_start.isoformat(),
                    observed_at.isoformat(),
                ),
            ).fetchone()
            model_observation = {
                "calls": int(usage["calls"] or 0),
                "succeeded_calls": int(usage["succeeded_calls"] or 0),
                "failed_calls": int(usage["failed_calls"] or 0),
                "unknown_calls": int(usage["unknown_calls"] or 0),
                "attempts": int(usage["attempts"] or 0),
                "succeeded_attempts": int(usage["succeeded"] or 0),
                "failed_attempts": int(usage["failed"] or 0),
                "unknown_attempts": int(usage["unknown"] or 0),
                "input_tokens": int(usage["input_tokens"] or 0),
                "output_tokens": int(usage["output_tokens"] or 0),
                "cost_microusd": str(int(usage["cost_microusd"] or 0)),
                "estimated_cost_attempts": int(usage["estimated_cost_attempts"] or 0),
            }
            for row in connection.execute(
                """SELECT final_method, status, COUNT(*) AS count
                FROM research_search_runs WHERE subject_id=? AND created_at>=? AND created_at<=?
                GROUP BY final_method, status ORDER BY final_method, status LIMIT 20""",
                (
                    self.kernel.subject_id,
                    window_start.isoformat(),
                    observed_at.isoformat(),
                ),
            ):
                method = str(row["final_method"])
                status = str(row["status"])
                if method in search_observation["by_method"]:
                    search_observation["by_method"][method][status] = int(row["count"])
                    search_observation["runs"] += int(row["count"])
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
            "enabled": self.cognition_is_enabled(),
            "embedding_runtime": self.embedding_runtime_status(),
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
            "model_observability": {
                "window_days": 7,
                "since": window_start.isoformat(),
                "sampled_at": observed_at.isoformat(),
                **model_observation,
                "search": search_observation,
            },
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
                self.rfile = io.BufferedReader(
                    DeadlineRequestReader(
                        self.rfile, self.connection, owner.settings.request_timeout_seconds
                    )
                )

            def handle_one_request(self) -> None:
                self._log_request_parsed = False
                with operation_deadline(owner.settings.request_timeout_seconds):
                    super().handle_one_request()

            def parse_request(self) -> bool:
                self._log_request_parsed = super().parse_request()
                return self._log_request_parsed

            def do_GET(self) -> None:
                return get.dispatch(self, owner)

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
                    "/api/admin/lifecycle/wake",
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
                # Migration cutover and rollback are privileged controls that
                # must remain usable while the durable migration epoch fences
                # ordinary runtime admission.  The coordinator still requires
                # the process ownership check and performs its own drain;
                # this scope does not grant a general HTTP bypass.
                migration_control = owner._is_migration_control(self.path)
                if migration_control:
                    # CutoverCoordinator owns the control scope so the
                    # authenticated HTTP route cannot accidentally nest a
                    # second scope around its durable epoch transition.
                    try:
                        self._dispatch_post()
                    except (OperationInvalidated, RuntimeOwnershipError):
                        self.close_connection = True
                        self._discard_small_request_body()
                        self._json(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            {"error": "migration_control_unavailable"},
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

            def do_PUT(self) -> None:
                """Route authenticated JSON updates through the same admission boundary."""
                self.command = "PUT"
                self.do_POST()

            def _dispatch_post(self) -> None:
                return post.dispatch(self, owner)

            def _configure_conversation(self) -> None:
                return interaction.configure_conversation(self, owner)

            def _lifecycle_control(self) -> None:
                return operator.lifecycle_control(self, owner)

            def _run_retention_batch(self) -> None:
                return operator.run_retention_batch(self, owner)

            def _configure_retention(self) -> None:
                return operator.configure_retention(self, owner)

            def _reconcile_action(self) -> None:
                return operator.reconcile_action(self, owner)

            def _reconcile_model_call(self) -> None:
                return operator.reconcile_model_call(self, owner)

            def _create_wallet_bounty(self) -> None:
                return wallet_rewards.create_wallet_bounty(self, owner)

            def _create_wallet_reward(self) -> None:
                return wallet_rewards.create_wallet_reward(self, owner)

            def _wallet_reward_operation(self) -> None:
                return wallet_rewards.wallet_reward_operation(self, owner)

            def _decide_wallet_reward_submission(self) -> None:
                return wallet_rewards.decide_wallet_reward_submission(self, owner)

            def _resolve_wallet_reward_incident(self) -> None:
                return wallet_rewards.resolve_wallet_reward_incident(self, owner)

            def _submit_wallet_reward(self) -> None:
                return wallet_rewards.submit_wallet_reward(self, owner)

            def _execute_wallet_rewards(self) -> None:
                return wallet_rewards.execute_wallet_rewards(self, owner)

            def _wallet_bounty_operation(self) -> None:
                return wallet_rewards.wallet_bounty_operation(self, owner)

            def _submit_wallet_bounty(self) -> None:
                return wallet_rewards.submit_wallet_bounty(self, owner)

            def _wallet_submission_operation(self) -> None:
                return wallet_rewards.wallet_submission_operation(self, owner)

            def _wallet_order_operation(self) -> None:
                return wallet_payments.wallet_order_operation(self, owner)

            def _wallet_execution_operation(self) -> None:
                return wallet_payments.wallet_execution_operation(self, owner)

            def _recover_wallet_executions(self) -> None:
                return wallet_payments.recover_wallet_executions(self, owner)

            def _update_wallet_policy(self) -> None:
                return wallet_payments.update_wallet_policy(self, owner)

            def _wallet_automation_projection(self) -> dict[str, Any]:
                return wallet_automation.wallet_automation_projection(self, owner)

            def _update_wallet_automation(self, payload: object | None = None) -> None:
                return wallet_automation.update_wallet_automation(self, owner, payload)

            def _set_wallet_automation_pause(self, paused: bool) -> None:
                return wallet_automation.set_wallet_automation_pause(self, owner, paused)

            def _configure_wallet_network(self) -> None:
                return wallet_resources.configure_wallet_network(self, owner)

            def _configure_wallet_asset(self) -> None:
                return wallet_resources.configure_wallet_asset(self, owner)

            def _configure_wallet_address(self) -> None:
                return wallet_resources.configure_wallet_address(self, owner)

            def _revoke_wallet_resource(self, resource_type: str) -> None:
                return wallet_resources.revoke_wallet_resource(self, owner, resource_type)

            @staticmethod
            def _wallet_observation_health_query(
                query: dict[str, list[str]],
            ) -> tuple[str, int, str | None, str | None]:
                return wallet_resources.wallet_observation_health_query(owner, query)

            @staticmethod
            def _wallet_balance_history_query(
                query: dict[str, list[str]],
            ) -> tuple[int, str | None, str | None, str | None, str | None]:
                return wallet_resources.wallet_balance_history_query(owner, query)

            @staticmethod
            def _wallet_acquisition_id(path: str, operation: str) -> str | None:
                return wallet_resources.wallet_acquisition_id(owner, path, operation)

            @staticmethod
            def _wallet_acquisition_statuses(
                query: dict[str, list[str]],
            ) -> tuple[str, ...] | None:
                return wallet_resources.wallet_acquisition_statuses(owner, query)

            def _wallet_payload(self) -> dict[str, Any] | None:
                return wallet_payments.wallet_payload(self, owner)

            def _enqueue_wallet_acquisition(self) -> None:
                return wallet_resources.enqueue_wallet_acquisition(self, owner)

            def _run_wallet_acquisitions(self) -> None:
                return wallet_resources.run_wallet_acquisitions(self, owner)

            def _retry_wallet_acquisition(self) -> None:
                return wallet_resources.retry_wallet_acquisition(self, owner)

            def _cancel_wallet_acquisition(self) -> None:
                return wallet_resources.cancel_wallet_acquisition(self, owner)

            def _configure_search_provider(self) -> None:
                return search.configure_search_provider(self, owner)

            def _configure_search_routing(self) -> None:
                return search.configure_search_routing(self, owner)

            def _set_search_provider_routing(self) -> None:
                return search.set_search_provider_routing(self, owner)

            def _set_search_provider_enabled(self) -> None:
                return search.handle_set_search_provider_enabled(self, owner)

            def _test_unstored_search_provider(self) -> None:
                return search.test_unstored_search_provider(self, owner)

            def _configure_transport(self) -> None:
                return interaction.configure_transport(self, owner)

            def _public_publication(self) -> None:
                return public_posts.public_publication(self, owner)

            def _configure_public_post_controls(self) -> None:
                return public_posts.configure_public_post_controls(self, owner)

            def _issue_public_post_captcha(self) -> None:
                return public_posts.issue_public_post_captcha(self, owner)

            def _change_transport_status(self) -> None:
                return interaction.change_transport_status(self, owner)

            def _delivery_operation(self) -> None:
                return interaction.delivery_operation(self, owner)

            def _grant_capability(self) -> None:
                return operator.grant_capability(self, owner)

            def _revoke_capability(self) -> None:
                return operator.revoke_capability(self, owner)

            def _trust_common_knowledge_key(self) -> None:
                return knowledge.trust_common_knowledge_key(self, owner)

            def _import_common_knowledge(self) -> None:
                return knowledge.import_common_knowledge(self, owner)

            def _register_common_knowledge_peer(self) -> None:
                return knowledge.register_common_knowledge_peer(self, owner)

            def _sync_common_knowledge_peer(self) -> None:
                return knowledge.sync_common_knowledge_peer(self, owner)

            def _revoke_common_knowledge(self) -> None:
                return knowledge.revoke_common_knowledge(self, owner)

            def _configure_model_resource(self) -> None:
                return cognition.configure_model_resource(self, owner)

            def _draft_model_credentials(
                self, payload: object, *, require_model: bool
            ) -> tuple[str, str, SecretStr, ReasoningEffort | None]:
                return cognition.draft_model_credentials(
                    self, owner, payload, require_model=require_model
                )

            def _test_unstored_model_resource(self) -> None:
                return cognition.test_unstored_model_resource(self, owner)

            def _discover_unstored_models(self) -> None:
                return cognition.discover_unstored_models(self, owner)

            def _configure_embedding_resource(self) -> None:
                return cognition.configure_embedding_resource(self, owner)

            def _test_model_resource(self) -> None:
                return cognition.test_model_resource(self, owner)

            def _change_embedding_resource_status(self) -> None:
                return cognition.change_embedding_resource_status(self, owner)

            def _change_model_resource_status(self) -> None:
                return cognition.change_model_resource_status(self, owner)

            def _update_model_resource(self) -> None:
                return cognition.update_model_resource(self, owner)

            def _add_model_resource_keys(self) -> None:
                return cognition.add_model_resource_keys(self, owner)

            def _revoke_model_resource_key(self) -> None:
                return cognition.revoke_model_resource_key(self, owner)

            def _revoke_search_provider(self) -> None:
                return search.revoke_search_provider(self, owner)

            def _start_upgrade(self) -> None:
                return operator.start_upgrade(self, owner)

            def _health_projection(self, payload: dict[str, Any]) -> dict[str, Any]:
                # Apply the access boundary on every read, including cache hits.
                if self._authorized("operator"):
                    return payload
                return {"status": payload["status"], "service": "noyra"}

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
                    and _ascii_token_equal(
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
                if self.command in {"POST", "PUT"} and self.path not in {
                    "/admin/session",
                    "/admin/session/logout",
                }:
                    return _ascii_token_equal(
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
                    if secret is not None and _ascii_token_equal(
                        supplied, f"Bearer {secret.get_secret_value()}"
                    ):
                        return f"web-{role}"
                _session_id, session = self._session_from_cookie()
                return session.actor if session is not None else "web-operator"

            def _approval_identity(self) -> tuple[str, str | None, str | None]:
                """Return an audit label and a non-secret review binding."""
                supplied = self.headers.get("Authorization", "")
                for role, secret in (
                    ("admin", owner.settings.admin_token),
                    ("operator", owner.settings.operator_token),
                    ("break_glass", owner.settings.break_glass_token),
                ):
                    if secret is not None and _ascii_token_equal(
                        supplied, f"Bearer {secret.get_secret_value()}"
                    ):
                        binding = hashlib.sha256(
                            f"noyra-admin-principal:{owner.kernel.subject_id}:{role}".encode()
                        ).hexdigest()
                        actor = f"web-{role}"
                        if owner.settings.migration_require_independent_review:
                            return actor, None, self._trusted_reviewer_principal()
                        return actor, binding, None
                session_id, session = self._session_from_cookie()
                if session is None:
                    return self._actor(), None, self._trusted_reviewer_principal()
                if owner.settings.migration_require_independent_review:
                    return session.actor, None, self._trusted_reviewer_principal()
                return session.actor, owner._admin_session_hash(session_id), None

            def _trusted_reviewer_principal(self) -> str | None:
                principal = self.headers.get("X-Noyra-Authenticated-Principal", "").strip()
                return _verify_trusted_proxy_principal(
                    principal,
                    self.headers.get("X-Noyra-Principal-Timestamp", ""),
                    self.headers.get("X-Noyra-Principal-Signature", ""),
                    self.client_address[0],
                    trusted_proxy_cidrs=owner.settings.trusted_proxy_cidrs,
                    hmac_key=owner.settings.migration_proxy_principal_hmac_key,
                    method=self.command,
                    path=self.path,
                )

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
                client_ip = owner.client_ip(
                    str(self.client_address[0]), self.headers.get("X-Forwarded-For")
                )
                if not owner.allow_admin_login(client_ip):
                    self._discard_small_request_body()
                    owner.audit_admin_event(
                        "admin_login_rate_limited",
                        "web-anonymous",
                        {"path": "/admin/session"},
                    )
                    self._json(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        {"error": "admin_login_rate_limited"},
                        retry_after=60,
                    )
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
                        and _ascii_token_equal(token, secret.get_secret_value())
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
                    owner.clear_admin_login_failures(client_ip)
                    owner.audit_admin_event(
                        "admin_break_glass_rejected_session", "web-break_glass", {}
                    )
                    self._json(HTTPStatus.FORBIDDEN, {"error": "break_glass_session_forbidden"})
                    return
                owner.clear_admin_login_failures(client_ip)
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
                if session is not None and not _ascii_token_equal(
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
                    check = getattr(
                        owner.at_rest, "require_request_ready", owner.at_rest.require_ready
                    )
                    check()
                except AtRestError:
                    return False
                return True

            def _update_training_policy(self) -> None:
                return exports.update_training_policy(self, owner)

            def _create_export_job(self) -> None:
                return exports.create_export_job(self, owner)

            def _enqueue_export_job(self, export_kind: str) -> None:
                return exports.enqueue_export_job(self, owner, export_kind)

            def _cancel_export_job(self) -> None:
                return exports.cancel_export_job(self, owner)

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
                    if isinstance(accepted, InboundIdentityObserved):
                        self._inbound_ack(channel, {"status": "identity_observed"})
                        return
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
                return interaction.configure_inbound_binding(self, owner)

            def _identity_challenge(self) -> None:
                return interaction.identity_challenge(self, owner)

            def _change_inbound_binding(self) -> None:
                return interaction.change_inbound_binding(self, owner)

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
                cache_control = "no-store"
                if filename == "index.html":
                    hero_path = Path(__file__).with_name("web") / "assets" / "public-hero.webp"
                    try:
                        hero_body = hero_path.read_bytes()
                    except OSError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
                        return
                    hero_version = hashlib.sha256(hero_body).hexdigest()[:16]
                    mobile_hero_path = hero_path.with_name("public-hero-mobile.webp")
                    try:
                        mobile_hero_body = mobile_hero_path.read_bytes()
                    except OSError:
                        self._json(HTTPStatus.NOT_FOUND, {"error": "asset_not_found"})
                        return
                    mobile_hero_version = hashlib.sha256(mobile_hero_body).hexdigest()[:16]
                    body = body.replace(b"__PUBLIC_HERO_VERSION__", hero_version.encode("ascii"))
                    body = body.replace(
                        b"__PUBLIC_HERO_MOBILE_VERSION__", mobile_hero_version.encode("ascii")
                    )
                    site_url = owner.settings.public_site_url
                    metadata = [
                        '<meta property="og:type" content="website">',
                        '<meta property="og:locale" content="zh_CN">',
                        '<meta property="og:title" content="Noyra \u00b7 '
                        '\u516c\u5f00\u6863\u6848">',
                        '<meta property="og:description" '
                        'content="Noyra '
                        "\u7684\u516c\u5f00\u6863\u6848\uff1a\u8bb0\u5f55\u4e00\u4e2a"
                        "\u6301\u7eed\u8fd0\u884c\u7684"
                        "\u4eba\u5de5\u4e3b\u4f53"
                        '\u5982\u4f55\u89c2\u5bdf\u3001\u5224\u65ad\u4e0e\u53cd\u601d">',
                    ]
                    if site_url:
                        escaped_url = html.escape(site_url + "/", quote=True)
                        image_url = html.escape(f"{site_url}/assets/public-social.png", quote=True)
                        metadata.extend(
                            (
                                f'<link rel="canonical" href="{escaped_url}">',
                                f'<meta property="og:url" content="{escaped_url}">',
                                f'<meta property="og:image" content="{image_url}">',
                                '<meta property="og:image:width" content="1200">',
                                '<meta property="og:image:height" content="630">',
                                '<meta property="og:image:alt" content="Noyra '
                                '公开档案的治愈科技视觉">',
                                f'<meta name="twitter:image" content="{image_url}">',
                            )
                        )
                    body = body.replace(
                        b"<!-- NOYRA_SEO_METADATA -->", "\n  ".join(metadata).encode("utf-8")
                    )
                    cache_control = "no-cache"
                elif filename in {"app.js", "participant.js", "styles.css"}:
                    cache_control = "public, max-age=300, must-revalidate"
                elif filename in {
                    "assets/public-hero.webp",
                    "assets/public-hero-mobile.webp",
                }:
                    versions = parse_qs(urlsplit(self.path).query).get("v", [])
                    expected_version = hashlib.sha256(body).hexdigest()[:16]
                    cache_control = (
                        "public, max-age=31536000, immutable"
                        if len(versions) == 1 and hmac.compare_digest(versions[0], expected_version)
                        else "no-cache"
                    )
                elif filename == "assets/public-social.png":
                    cache_control = "public, max-age=31536000, immutable"
                self.send_response(HTTPStatus.OK)
                self._headers(content_type, len(body), cache_control=cache_control)
                self.end_headers()
                self.wfile.write(body)

            def _public_article(self, parsed: Any) -> None:
                parts = parsed.path.split("/")
                if len(parts) != 4 or parts[2] not in PUBLIC_VIEWS or not parts[3]:
                    self._text(
                        HTTPStatus.NOT_FOUND,
                        "text/plain; charset=utf-8",
                        b"Not found\n",
                        cache_control="no-store",
                    )
                    return
                kind = parts[2]
                try:
                    identifier = unquote(parts[3], errors="strict")
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    if (
                        set(params) - {"lang", "theme", "return"}
                        or any(len(values) != 1 for values in params.values())
                        or params.get("lang", ["zh"]) not in [["zh"], ["en"]]
                        or params.get("theme", ["system"]) not in [["system"], ["light"], ["dark"]]
                        or params.get("return", [PUBLIC_VIEWS[kind]])[0]
                        not in {*PUBLIC_VIEWS.values(), "overview", "timeline"}
                    ):
                        raise ValueError("invalid reading language")
                    page = owner.public_archive.page(
                        owner.kernel.subject_id, kind=kind, limit=1, identifier=identifier
                    )
                except (ValueError, UnicodeError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_public_query"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                except PublicQueryBudgetExceeded:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_query_budget_exceeded"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_content_unavailable"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                if not page["items"]:
                    self._text(
                        HTTPStatus.NOT_FOUND,
                        "text/plain; charset=utf-8",
                        b"Not found\n",
                        cache_control="no-store",
                    )
                    return
                language = params.get("lang", ["zh"])[0]
                try:
                    body = public_article_html(
                        page["items"][0],
                        owner.settings.public_site_url or "",
                        english=language == "en",
                        theme=params.get("theme", ["system"])[0],
                        return_view=params.get("return", [None])[0],
                    )
                except (KeyError, TypeError, ValueError):
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_content_unavailable"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                if len(body) > PUBLIC_MAX_RESPONSE_BYTES:
                    self._json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "public_response_too_large"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                self.send_response(HTTPStatus.OK)
                self._headers("text/html; charset=utf-8", len(body), cache_control="no-store")
                self.end_headers()
                self.wfile.write(body)

            def _robots_txt(self) -> None:
                lines = [
                    "User-agent: *",
                    "Disallow: /admin",
                    "Disallow: /admin.html",
                    "Disallow: /api/",
                    "Disallow: /health/",
                ]
                if owner.settings.public_site_url:
                    lines.append(f"Sitemap: {owner.settings.public_site_url}/sitemap.xml")
                body = ("\n".join(lines) + "\n").encode("utf-8")
                self._text(HTTPStatus.OK, "text/plain; charset=utf-8", body)

            def _sitemap_xml(self, parsed: Any) -> None:
                site_url = owner.settings.public_site_url
                if not site_url:
                    self._text(
                        HTTPStatus.NOT_FOUND,
                        "text/plain; charset=utf-8",
                        b"Configure NOYRA_PUBLIC_SITE_URL to publish a sitemap.\n",
                        cache_control="no-store",
                    )
                    return
                try:
                    params = parse_qs(parsed.query, keep_blank_values=True)
                    if set(params) - {"part"} or (params and len(params["part"]) != 1):
                        raise ValueError("invalid sitemap query")
                    if params:
                        if params["part"] == ["home"]:
                            items = []
                        else:
                            items = owner.public_archive.sitemap_partition(
                                owner.kernel.subject_id, params["part"][0]
                            )
                        entries = "".join(
                            "<url><loc>"
                            f"{html.escape(site_url + public_item_path(item['kind'], item['id']))}"
                            "</loc>"
                            f"<lastmod>{html.escape(item['updated_at'])}</lastmod></url>\n"
                            for item in items
                        )
                        if params["part"] == ["home"]:
                            entries = f"<url><loc>{html.escape(site_url + '/')}</loc></url>\n"
                        root_tag = "urlset"
                    else:
                        partitions = owner.public_archive.sitemap_partitions(
                            owner.kernel.subject_id
                        )
                        entries = "".join(
                            "<sitemap><loc>"
                            f"{html.escape(site_url + '/sitemap.xml?part=' + quote(part, safe=''))}"
                            "</loc></sitemap>\n"
                            for part in ["home", *partitions]
                        )
                        root_tag = "sitemapindex"
                except (ValueError, UnicodeError):
                    self._json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "invalid_public_query"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                except PublicQueryBudgetExceeded:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_query_budget_exceeded"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                except IntegrityError:
                    self._json(
                        HTTPStatus.SERVICE_UNAVAILABLE,
                        {"error": "public_content_unavailable"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                body = (
                    '<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<{root_tag} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
                    f"{entries}"
                    f"</{root_tag}>\n"
                ).encode()
                if len(body) > PUBLIC_MAX_RESPONSE_BYTES:
                    self._json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "public_response_too_large"},
                        public_contract=ARCHIVE_CONTRACT,
                    )
                    return
                self._text(
                    HTTPStatus.OK,
                    "application/xml; charset=utf-8",
                    body,
                    cache_control="no-store",
                )

            def _text(
                self,
                status: HTTPStatus,
                content_type: str,
                body: bytes,
                *,
                cache_control: str = "public, max-age=300",
            ) -> None:
                self.send_response(status)
                self._headers(content_type, len(body), cache_control=cache_control)
                self.end_headers()
                self.wfile.write(body)

            def _send_file(
                self, path: Path, *, filename: str, digest: str, export_job_id: str | None = None
            ) -> None:
                length = path.stat().st_size
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
                self.send_header("X-Archive-SHA256", digest)
                self._headers("application/zip", length)
                self.end_headers()
                with path.open("rb") as stream:
                    while chunk := stream.read(1024 * 1024):
                        if export_job_id is not None:
                            try:
                                owner.export_jobs.download_path(
                                    export_job_id, subject_id=owner.kernel.subject_id
                                )
                            except (RuntimeError, FileNotFoundError):
                                self.close_connection = True
                                return
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
                self,
                status: HTTPStatus,
                payload: Any,
                *,
                retry_after: int | None = None,
                public_contract: bool | str = False,
            ) -> None:
                body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
                if public_contract and len(body) > PUBLIC_MAX_RESPONSE_BYTES:
                    status = HTTPStatus.REQUEST_ENTITY_TOO_LARGE
                    body = json.dumps(
                        {
                            "error": "public_response_too_large",
                            "contract": public_contract
                            if isinstance(public_contract, str)
                            else PUBLIC_CONTRACT_ID,
                        },
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode()
                try:
                    self.send_response(status)
                    if status == HTTPStatus.TOO_MANY_REQUESTS:
                        self.send_header(
                            "Retry-After", str(60 if retry_after is None else retry_after)
                        )
                    elif status == HTTPStatus.SERVICE_UNAVAILABLE:
                        self.send_header(
                            "Retry-After", str(5 if retry_after is None else retry_after)
                        )
                    self._headers(
                        "application/json; charset=utf-8",
                        len(body),
                        cache_control=PUBLIC_CACHE_CONTROL if public_contract else "no-store",
                        public_contract=public_contract,
                    )
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    # Clients routinely cancel health polling or large admin
                    # responses.  The request is already gone; avoid a noisy
                    # traceback while allowing the worker to be reused.
                    self.close_connection = True
                    return

            def _headers(
                self,
                content_type: str,
                length: int,
                *,
                cache_control: str = "no-store",
                public_contract: bool | str = False,
            ) -> None:
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(length))
                self.send_header("Cache-Control", cache_control)
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
                self.send_header("Cross-Origin-Opener-Policy", "same-origin")
                self.send_header("Cross-Origin-Resource-Policy", "same-origin")
                self.send_header("X-Permitted-Cross-Domain-Policies", "none")
                if public_contract:
                    self.send_header(
                        "X-Noyra-Public-Contract",
                        public_contract if isinstance(public_contract, str) else PUBLIC_CONTRACT_ID,
                    )
                if owner.settings.public_site_url or owner.settings.admin_session_cookie_secure:
                    self.send_header(
                        "Strict-Transport-Security",
                        "max-age=31536000; includeSubDomains",
                    )
                self.send_header(
                    "Content-Security-Policy",
                    "default-src 'self'; img-src 'self' data:; media-src 'self' data:; "
                    "base-uri 'none'; object-src 'none'; "
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

            def _log_http_event(self, event: str, code: object = "-") -> None:
                method, route, status = http_log_fields(
                    getattr(self, "command", None),
                    getattr(self, "path", "")
                    if getattr(self, "_log_request_parsed", False)
                    else "",
                    code,
                )
                LOGGER.info(
                    "http event=%s method=%s route=%s status=%s", event, method, route, status
                )

            def log_request(self, code: int | str = "-", size: int | str = "-") -> None:
                # Never delegate to the stdlib request-line formatter. Neither
                # the original target nor response/header/body data enter logs.
                self._log_http_event("request", code)

            def log_error(self, format: str, *args: Any) -> None:
                if format == "code %d, message %s" and args:
                    self._log_http_event("response_error", args[0])
                elif format == "Request timed out: %r":
                    self._log_http_event("timeout")
                else:
                    self._log_http_event("protocol_error")

            def log_message(self, format: str, *args: Any) -> None:
                # Unknown/future stdlib diagnostics may contain request data or
                # exception text. Keep a fixed event, never interpolate them.
                self._log_http_event("diagnostic")

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
        managed_data_root(configured=settings.data_dir)
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
                migration_fence_root=settings.data_dir / "migration",
            )
            self.http = cast(Any, _UnownedHTTPFacade())
            self._construction_complete = True
            return
        self._unowned_preflight = False
        self._construction_complete = False
        self._pending_capability_profiles: object | None = None
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
        self.storage_contract = detect_storage_layout(settings.data_dir, settings.backup_root)
        if (
            self.storage_contract.mode is StorageMode.UNAVAILABLE
            or not self.storage_contract.writable
        ):
            raise StorageUnavailableError(
                self.storage_contract.reason_code or "storage_unavailable"
            )
        if (
            settings.storage_profile == "recovery_capable"
            and not self.storage_contract.backup_ready
        ):
            raise StorageUnavailableError(
                self.storage_contract.backup_reason_code or "backup_storage_unavailable"
            )
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
            migration_fence_root=settings.data_dir / "migration",
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
        self.http.storage_contract = self.storage_contract
        self._initialize_common_key_after_integrity = initialize_common_key_after_integrity
        self.cognition: CognitionCycle | None = None
        self.http.cognition_is_enabled = lambda: self.cognition is not None
        self.http.embedding_runtime_status = self._embedding_runtime_status
        self.http.migration_runtime.to_thread = self._tracked_to_thread
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
        self._last_retention_run_at: datetime | None = None
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
            wallet_signer = configured_wallet_signer_from_env()
            owns_wallet_signer = wallet_signer is not None
        try:
            service = cls(
                ServiceSettings.from_env(),
                wallet_signer=wallet_signer,
                close_wallet_signer=owns_wallet_signer,
            )
        except Exception:
            if owns_wallet_signer and wallet_signer is not None:
                close = getattr(wallet_signer, "close", None)
                if callable(close):
                    close()
            raise
        if service._unowned_preflight:
            if owns_wallet_signer and wallet_signer is not None:
                close = getattr(wallet_signer, "close", None)
                if callable(close):
                    close()
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
            service._pending_capability_profiles = service._read_environment_capabilities()
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
            embedding_observed_dimension_recorder = None
            if (
                managed_embedding_settings is not None
                and managed_embedding_settings.dimensions is None
            ):

                def record_embedding_dimensions(dimensions: int) -> None:
                    service.http.embedding_resources.record_observed_dimensions(
                        service.kernel.subject_id,
                        managed_embedding_settings.resource_id or "",
                        dimensions,
                    )

                embedding_observed_dimension_recorder = record_embedding_dimensions
            service.cognition = CognitionCycle(
                service.kernel,
                gateway,
                cognition_settings,
                reader=SafeWebReader(),
                embedding_settings=embedding_settings,
                embedding_call_authorizer=embedding_call_authorizer,
                embedding_observed_dimension_recorder=embedding_observed_dimension_recorder,
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

    def _embedding_runtime_status(self) -> dict[str, Any]:
        """Report stored selection separately from the immutable running cycle."""
        with self.kernel.database.connection() as connection:
            row = connection.execute(
                "SELECT config_id, label, model FROM embedding_resources "
                "WHERE subject_id=? AND status='active' "
                "ORDER BY updated_at DESC, config_id DESC LIMIT 1",
                (self.kernel.subject_id,),
            ).fetchone()
        desired_id = str(row["config_id"]) if row is not None else None
        effective_id = None
        effective_name = None
        if self.cognition is not None and self.cognition.embedding_gateway is not None:
            effective_id = self.cognition.embedding_gateway.resource_id
            effective_name = self.cognition.embedding_gateway.name
        matches_managed = desired_id is not None and desired_id == effective_id
        matches_environment = desired_id is None and bool(
            effective_id and effective_id.startswith("env:")
        )
        effective = matches_managed or matches_environment
        unconfigured = desired_id is None and effective_id is None
        restart_required = not (effective or unconfigured)
        cognition_disabled = self.cognition is None
        return {
            "status": (
                "cognition_disabled"
                if cognition_disabled
                else "applied"
                if effective
                else "not_configured"
                if unconfigured
                else "restart_required"
            ),
            "restart_required": restart_required and not cognition_disabled,
            "desired_resource_id": desired_id,
            "desired_label": None if row is None else str(row["label"]),
            "desired_model": None if row is None else str(row["model"]),
            "effective_resource_id": effective_id,
            "effective_provider": effective_name,
            "effective": effective,
        }

    def _sync_cognitive_resources(self, proposals: tuple[CognitiveResourceGroupInput, ...]) -> None:
        for proposal in proposals:
            current = self.cognitive_resources.find(
                self.kernel.subject_id, proposal.pool, proposal.label
            )
            if current is not None and current.status != "active":
                LOGGER.error(
                    "environment_model_resource_%s: pool=%s label=%s",
                    current.status,
                    proposal.pool,
                    proposal.label,
                )
                continue
            if proposal.reasoning_effort is not None:
                capability = CapabilityProfileStore(self.kernel.database).get(
                    self.kernel.subject_id,
                    proposal.provider_type,
                    proposal.base_url,
                    proposal.model,
                )
                if (
                    capability is None
                    or capability.status != "known"
                    or proposal.reasoning_effort not in capability.tested_efforts
                    or proposal.capability_revision is None
                    or capability.revision < proposal.capability_revision
                    or (
                        capability.revision == proposal.capability_revision
                        and capability.profile_digest != proposal.capability_digest
                    )
                    # Existing, unchanged durable groups may use newer online
                    # probes. New provisioning must match the operator's pin.
                    or (current is None and capability.revision != proposal.capability_revision)
                ):
                    raise RuntimeError("environment_model_capability_profile_required")
            if current is not None:
                expected_values = CognitiveResourceStore._values(
                    self.kernel.subject_id, proposal, current.updated_at
                )
                proposed_keys = {
                    content_hash({"api_key": key.get_secret_value()}) for key in proposal.api_keys
                }
                current_keys, revoked = self.cognitive_resources.environment_key_status(
                    current.group_id, subject_id=self.kernel.subject_id, proposed=proposed_keys
                )
                if revoked:
                    raise RuntimeError("environment_model_resource_key_revoked")
                expected_pricing_status = (
                    "priced"
                    if expected_values[12] or expected_values[13]
                    else proposal.pricing_status or "unknown"
                )
                matches = (
                    (
                        current.pool,
                        current.label,
                        current.provider_type,
                        current.base_url,
                        current.model,
                        current.priority,
                        current.weight,
                        current.daily_attempts,
                        current.daily_input_tokens,
                        current.daily_output_tokens,
                        current.daily_cost_microusd,
                        current.input_microusd_per_million,
                        current.output_microusd_per_million,
                        current.max_attempts,
                        current.reasoning_effort,
                    )
                    == expected_values[1:]
                    and current_keys == proposed_keys
                    and current.limit_enabled == proposal.limit_enabled
                    and current.pricing_status == expected_pricing_status
                )
                if not matches:
                    raise RuntimeError("environment_model_resource_conflict")
                continue
            self.cognitive_resources.configure(
                self.kernel.subject_id,
                proposal,
                actor="environment-operator",
            )

    def _read_environment_capabilities(self) -> object | None:
        path_value = os.getenv("NOYRA_MODEL_CAPABILITY_PROFILES_FILE", "").strip()
        if not path_value:
            return None
        try:
            raw = read_secret_file(
                path_value,
                label="model capability profile file",
                single_line=False,
                max_bytes=1_000_000,
            )
            return cast(object, json.loads(raw))
        except (CredentialError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise RuntimeError("model_capability_profile_import_failed") from error

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
        # A timed-out embedding worker may outlive its operation lease. Do not
        # suppress this failure or let synchronous error cleanup release its lock.
        cognition = getattr(self, "cognition", None)
        if (
            cognition is not None
            and cognition.embedding_gateway is not None
            and not cognition.embedding_gateway.close(wait=True)
        ):
            raise EmbeddingShutdownIncompleteError()
        with suppress(Exception):
            self.kernel.close()

    def _complete_boot_after_integrity(self) -> None:
        if not self._boot_recovery_pending:
            return
        # Replay accepted privacy intents before exports or cognition can read them.
        erasure = ConversationErasure(self.kernel.database)
        while results := erasure.resume(self.kernel.subject_id):
            if any(result["status"] != "completed" for result in results):
                raise ErasureReviewRequired("private_deletion_boot_replay_requires_review")
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
        if self._pending_capability_profiles is not None:
            CapabilityProfileStore(self.kernel.database).import_profiles(
                self.kernel.subject_id, self._pending_capability_profiles, bootstrap=True
            )
            self._pending_capability_profiles = None
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
        # admission gate must remain closed for work while allowing lifecycle
        # controls after a clean audit. Keep policy synchronization and the
        # cognition bootstrap compatible with historical alert-mode behavior.
        if current.state == "active":
            self.kernel.admission.open(epoch=current.version)
        else:
            self.kernel.admission.clear_quarantine()
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
            # Retention is a bounded, deletion-only recovery action.  It must
            # still run when cognition is blocked by storage pressure; placing
            # it after the pressure decision made a freelist-heavy database
            # unable to clean its own reclaimable aggregates.
            if hasattr(storage, "write_amplification_allowed"):
                await self._tracked_to_thread(
                    self._run_retention_if_due,
                    allow_compaction=storage.write_amplification_allowed,
                )
            else:
                await self._tracked_to_thread(self._run_retention_if_due)
            return "storage_pressure" if not storage.cognition_allowed else None
        migration_runtime = getattr(self.http, "migration_runtime", None)
        if migration_runtime is not None:
            try:
                result = await migration_runtime.tick()
                if result:
                    return str(result)
            except Exception as error:
                LOGGER.warning("migration assessment deferred: %s", type(error).__name__)
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
            with bind_lease(lease):
                if hasattr(storage, "write_amplification_allowed"):
                    await self._tracked_to_thread(
                        self._run_retention_if_due,
                        allow_compaction=storage.write_amplification_allowed,
                    )
                else:
                    await self._tracked_to_thread(self._run_retention_if_due)
            lease.assert_current()
            if not storage.cognition_allowed:
                return "storage_pressure"
            with bind_lease(lease):
                evidence_status = await self._tracked_to_thread(
                    self.http.retention.evidence_capacity, self.kernel.subject_id
                )
            lease.assert_current()
            evidence_exhausted = evidence_status["blocked"]
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
                    if not evidence_exhausted:
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
            with bind_lease(lease):
                await self.http.deliveries.deliver_pending(self.kernel.subject_id)
            lease.assert_current()
            if evidence_exhausted:
                return "evidence_capacity_reached"
            if self.cognition is None:
                return None
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

    def _run_retention_if_due(self, *, allow_compaction: bool = True) -> None:
        """Prune derived aggregates at a low frequency without blocking cognition."""
        http = getattr(self, "http", None)
        manager = getattr(http, "retention", None)
        if manager is None:
            return
        now = datetime.now(UTC)
        interval = timedelta(
            seconds=manager.effective_settings(self.kernel.subject_id).interval_seconds
        )
        if self._last_retention_run_at is not None and now - self._last_retention_run_at < interval:
            return
        self._last_retention_run_at = now
        try:
            result = manager.run_batch(
                self.kernel.subject_id, now=now, allow_compaction=allow_compaction
            )
            LOGGER.info(
                "retention batch completed: deleted=%s sqlite_space=%s",
                result.get("deleted_by_table", {}),
                result.get("sqlite_space", {}),
            )
        except Exception as error:
            LOGGER.warning("retention batch deferred: %s", type(error).__name__)

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
                    "SELECT COUNT(*) FROM interactions i WHERE subject_id = ? "
                    "AND status IN ('offered', 'deferred') AND NOT "
                    + WITHDRAWN_PARTICIPANT_INVITATION,
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
    from noyra.core.runtime_logging import install_log_safety

    install_log_safety()
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
