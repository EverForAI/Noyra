from __future__ import annotations

import ipaddress
import os
from decimal import ROUND_CEILING, Decimal
from urllib.parse import urlparse

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    ValidationError,
    field_validator,
    model_validator,
)

from .errors import ConfigurationError
from .types import BudgetLimits, ModelPricing, RetryPolicy


class OpenAICompatibleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr
    timeout_seconds: float = Field(default=60, gt=0, le=600)
    connect_timeout_seconds: float = Field(default=10, gt=0, le=120)
    max_connections: int = Field(default=10, ge=1, le=100)
    max_response_bytes: int = Field(default=2_000_000, ge=1_024, le=20_000_000)
    trust_environment: bool = False
    allow_local_endpoint: bool = False

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("base_url must be an absolute HTTP URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url cannot contain credentials, query, or fragment")
        return normalized

    @model_validator(mode="after")
    def validate_endpoint_policy(self) -> OpenAICompatibleSettings:
        parsed = urlparse(self.base_url)
        hostname = parsed.hostname or ""
        local = hostname.casefold() == "localhost"
        try:
            address = ipaddress.ip_address(hostname.split("%", 1)[0])
        except ValueError:
            address = None
        if address is not None:
            local = address.is_loopback
            if not local and not address.is_global:
                raise ValueError("model endpoint must use a public address")
        if local:
            if not self.allow_local_endpoint:
                raise ValueError("local model endpoints require explicit opt-in")
            return self
        if parsed.scheme != "https":
            raise ValueError("remote model endpoints must use HTTPS")
        return self

    @classmethod
    def from_env(cls) -> OpenAICompatibleSettings:
        base_url = os.getenv("NOYRA_MODEL_BASE_URL", "")
        model = os.getenv("NOYRA_MODEL_NAME", "")
        api_key = os.getenv("NOYRA_MODEL_API_KEY", "")
        required = {"base_url": base_url, "model": model, "api_key": api_key}
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ConfigurationError("missing remote model settings: " + ", ".join(sorted(missing)))
        try:
            allow_local = os.getenv("NOYRA_ALLOW_LOCAL_MODEL_ENDPOINT", "false").strip().lower()
            if allow_local in {"1", "true", "yes", "on"}:
                allow_local_endpoint = True
            elif allow_local in {"0", "false", "no", "off"}:
                allow_local_endpoint = False
            else:
                raise ValueError("local model endpoint opt-in must be true or false")
            return cls(
                base_url=base_url,
                model=model,
                api_key=SecretStr(api_key),
                allow_local_endpoint=allow_local_endpoint,
            )
        except ValueError:
            raise ConfigurationError("invalid remote model settings") from None


class ModelRuntimeSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    daily_attempts: int = Field(ge=0)
    daily_input_tokens: int = Field(ge=0)
    daily_output_tokens: int = Field(ge=0)
    daily_cost_limit_usd: Decimal = Field(ge=0)
    input_usd_per_million: Decimal = Field(ge=0)
    output_usd_per_million: Decimal = Field(ge=0)
    max_attempts: int = Field(default=3, ge=1, le=10)
    retry_base_delay_seconds: float = Field(default=0.5, ge=0, le=60)
    retry_max_delay_seconds: float = Field(default=8, ge=0, le=300)

    @model_validator(mode="after")
    def validate_retry_delays(self) -> ModelRuntimeSettings:
        if self.retry_base_delay_seconds > self.retry_max_delay_seconds:
            raise ValueError("base retry delay cannot exceed maximum retry delay")
        return self

    @classmethod
    def from_env(cls) -> ModelRuntimeSettings:
        names = {
            "daily_attempts": "NOYRA_DAILY_MODEL_CALLS",
            "daily_input_tokens": "NOYRA_DAILY_INPUT_TOKENS",
            "daily_output_tokens": "NOYRA_DAILY_OUTPUT_TOKENS",
            "daily_cost_limit_usd": "NOYRA_DAILY_COST_LIMIT",
            "input_usd_per_million": "NOYRA_MODEL_INPUT_USD_PER_MILLION",
            "output_usd_per_million": "NOYRA_MODEL_OUTPUT_USD_PER_MILLION",
        }
        values = {field: os.getenv(variable, "") for field, variable in names.items()}
        missing = [names[field] for field, value in values.items() if not value]
        if missing:
            raise ConfigurationError("missing model budget settings: " + ", ".join(sorted(missing)))
        values.update(
            {
                "max_attempts": os.getenv("NOYRA_MODEL_MAX_ATTEMPTS", "3"),
                "retry_base_delay_seconds": os.getenv("NOYRA_MODEL_RETRY_BASE_SECONDS", "0.5"),
                "retry_max_delay_seconds": os.getenv("NOYRA_MODEL_RETRY_MAX_SECONDS", "8"),
            }
        )
        try:
            return cls.model_validate(values)
        except ValidationError as error:
            raise ConfigurationError("invalid model budget settings") from error

    def budget_limits(self) -> BudgetLimits:
        return BudgetLimits(
            daily_attempts=self.daily_attempts,
            daily_input_tokens=self.daily_input_tokens,
            daily_output_tokens=self.daily_output_tokens,
            daily_cost_microusd=self._usd_to_microusd(self.daily_cost_limit_usd),
        )

    def pricing(self) -> ModelPricing:
        return ModelPricing(
            input_microusd_per_million=self._usd_to_microusd(self.input_usd_per_million),
            output_microusd_per_million=self._usd_to_microusd(self.output_usd_per_million),
        )

    def retry_policy(self) -> RetryPolicy:
        return RetryPolicy(
            max_attempts=self.max_attempts,
            base_delay_seconds=self.retry_base_delay_seconds,
            max_delay_seconds=self.retry_max_delay_seconds,
        )

    @staticmethod
    def _usd_to_microusd(value: Decimal) -> int:
        return int((value * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))
