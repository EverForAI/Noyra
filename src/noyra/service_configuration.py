"""Service configuration and validation; no runtime construction or network IO."""

from __future__ import annotations

import ipaddress
import os
from pathlib import Path
from typing import ClassVar, Literal, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from noyra.core.credentials import CredentialError, read_env_secret, read_secret_file
from noyra.core.identity import validate_subject_id
from noyra.core.managed_data_root import managed_data_root
from noyra.core.storage_mode import resolve_storage_profile
from noyra.core.types import content_hash


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
