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
from collections import defaultdict, deque
from collections.abc import Callable, Mapping
from concurrent.futures import Future as ConcurrentFuture
from concurrent.futures import TimeoutError as FutureTimeoutError
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Concatenate, ParamSpec, TypeVar, cast

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
from noyra.core.credentials import CredentialError, read_secret_file
from noyra.core.database import Database
from noyra.core.deadline import remaining_seconds
from noyra.core.errors import (
    IntegrityError,
    InvalidTransitionError,
    NotFoundError,
    RuntimeOwnershipError,
)
from noyra.core.export_jobs import ExportControl, ExportJobManager
from noyra.core.integrity import (
    IntegrityAuditLimits,
    IntegrityAuditShutdown,
    IntegrityRuntimeController,
)
from noyra.core.locking import ProcessLock
from noyra.core.managed_data_root import managed_data_root
from noyra.core.operator_controls import (
    OperatorControlService,
)
from noyra.core.private_sources import WITHDRAWN_PARTICIPANT_INVITATION
from noyra.core.provider_health import ProviderHealthStore
from noyra.core.redaction import redact_secrets
from noyra.core.release_assurance import upgrade_channel
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
from noyra.http_routes.auth import _AdminSession as _AdminSession
from noyra.http_routes.auth import _ascii_token_equal as _ascii_token_equal
from noyra.http_routes.auth import (
    _verify_trusted_proxy_principal as _verify_trusted_proxy_principal,
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
from noyra.http_transport import handler_type
from noyra.interaction import (
    DeliveryDispatcher,
    InboundStore,
    InteractionStore,
    PublicPostStore,
    PublicProjection,
    TransportStore,
)
from noyra.interaction.conversations import ConversationStore
from noyra.interaction.erasure import ConversationErasure, ErasureReviewRequired
from noyra.interaction.public_archive import (
    PublicArchive,
)
from noyra.interaction.public_reading import public_article_html
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
from noyra.research import SearchProviderStore
from noyra.service_configuration import ServiceSettings as ServiceSettings
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


_InitParams = ParamSpec("_InitParams")
_InitInstance = TypeVar("_InitInstance")
_InitReturn = TypeVar("_InitReturn")


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
        def render_article(
            item: dict[str, Any],
            site_url: str,
            *,
            english: bool = False,
            theme: str = "system",
            return_view: str | None = None,
        ) -> bytes:
            # Resolve the original public renderer at call time for compatibility.
            return public_article_html(
                item, site_url, english=english, theme=theme, return_view=return_view
            )

        return handler_type(
            self, web_root=Path(__file__).with_name("web"), public_article_html=render_article
        )


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
