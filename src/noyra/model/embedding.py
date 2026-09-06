from __future__ import annotations

import ipaddress
import math
import os
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any
from urllib.parse import urljoin, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from noyra.core.errors import IntegrityError
from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    DEFAULT_MAX_RESPONSE_BYTES,
    HTTPResponseLimitError,
    PublicDNSHTTPTransport,
    SyncHTTPTimeoutHook,
    call_sync_http_with_deadline,
    read_bounded_sync_response,
    validate_response_headers,
)
from noyra.core.types import content_hash, strict_int, strict_json_loads

from .errors import EmbeddingProviderError

EMBEDDING_MAX_RESPONSE_BYTES = DEFAULT_MAX_RESPONSE_BYTES
EMBEDDING_MAX_HEADER_BYTES = DEFAULT_MAX_HEADER_BYTES


class _EmbeddingResponseTokenLimitError(ValueError):
    pass


def estimate_embedding_input_tokens(texts: Sequence[str]) -> int:
    """Conservative request reservation used by provider and durable budget checks."""
    return sum(max(1, len(text.encode("utf-8"))) + 4 for text in texts)


@dataclass(frozen=True)
class EmbeddingUsage:
    input_tokens: int

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError("embedding token usage cannot be negative")


@dataclass(frozen=True)
class EmbeddingProviderResponse:
    vectors: tuple[tuple[float, ...], ...]
    usage: EmbeddingUsage | None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class EmbeddingBudgetLimits:
    daily_calls: int = 1_000
    daily_tokens: int = 5_000_000
    daily_cost_microusd: int = 1_000_000

    def __post_init__(self) -> None:
        if min(self.daily_calls, self.daily_tokens, self.daily_cost_microusd) < 0:
            raise ValueError("embedding budget limits cannot be negative")


@dataclass(frozen=True)
class EmbeddingPricing:
    input_microusd_per_million: int = 20_000

    def __post_init__(self) -> None:
        if self.input_microusd_per_million < 0:
            raise ValueError("embedding pricing cannot be negative")

    def cost_microusd(self, input_tokens: int) -> int:
        if input_tokens < 0:
            raise ValueError("embedding token usage cannot be negative")
        numerator = input_tokens * self.input_microusd_per_million
        return (numerator + 999_999) // 1_000_000


@dataclass(frozen=True)
class EmbeddingCircuitPolicy:
    failure_threshold: int = 3
    cooldown_seconds: float = 60.0

    def __post_init__(self) -> None:
        if not 1 <= self.failure_threshold <= 20:
            raise ValueError("embedding circuit failure threshold is invalid")
        if not 1 <= self.cooldown_seconds <= 3_600:
            raise ValueError("embedding circuit cooldown is invalid")


class EmbeddingSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str
    model: str = Field(min_length=1, max_length=256)
    api_key: SecretStr
    dimensions: int | None = Field(default=None, ge=1, le=16_384)
    timeout_seconds: float = Field(default=30, gt=0, le=300)
    resource_id: str | None = Field(default=None, min_length=1, max_length=256)
    daily_call_limit: int = Field(default=1_000, ge=0)
    daily_token_limit: int = Field(default=5_000_000, ge=0)
    daily_cost_limit_microusd: int = Field(default=1_000_000, ge=0)
    input_cost_microusd_per_million: int = Field(default=20_000, ge=0)
    circuit_failure_threshold: int = Field(default=3, ge=1, le=20)
    circuit_cooldown_seconds: float = Field(default=60, ge=1, le=3_600)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        normalized = value.strip().rstrip("/") + "/"
        parsed = urlsplit(normalized)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("embedding API must use HTTPS")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("embedding API URL cannot contain credentials, query, or fragment")
        if parsed.hostname.casefold() == "localhost":
            raise ValueError("embedding API must use a public endpoint")
        try:
            literal = ipaddress.ip_address(parsed.hostname.split("%", 1)[0])
        except ValueError:
            return normalized
        if not literal.is_global:
            raise ValueError("embedding API must use a public endpoint")
        return normalized

    @property
    def stable_resource_id(self) -> str:
        return (
            self.resource_id
            or "env:" + content_hash({"base_url": self.base_url, "model": self.model})[:32]
        )

    def budget_limits(self) -> EmbeddingBudgetLimits:
        return EmbeddingBudgetLimits(
            self.daily_call_limit,
            self.daily_token_limit,
            self.daily_cost_limit_microusd,
        )

    def pricing(self) -> EmbeddingPricing:
        return EmbeddingPricing(self.input_cost_microusd_per_million)

    def circuit_policy(self) -> EmbeddingCircuitPolicy:
        return EmbeddingCircuitPolicy(
            self.circuit_failure_threshold,
            self.circuit_cooldown_seconds,
        )

    @classmethod
    def from_env(cls) -> EmbeddingSettings | None:
        base_url = os.getenv("NOYRA_EMBEDDING_BASE_URL", "").strip()
        api_key = os.getenv("NOYRA_EMBEDDING_API_KEY", "").strip()
        model = os.getenv("NOYRA_EMBEDDING_MODEL", "").strip()
        if not (base_url or api_key or model):
            return None
        if not (base_url and api_key and model):
            raise ValueError("embedding API base URL, model and key must be configured together")
        dimensions = os.getenv("NOYRA_EMBEDDING_DIMENSIONS", "").strip()
        return cls(
            base_url=base_url,
            model=model,
            api_key=SecretStr(api_key),
            dimensions=int(dimensions) if dimensions else None,
            timeout_seconds=float(os.getenv("NOYRA_EMBEDDING_TIMEOUT_SECONDS", "30")),
            daily_call_limit=int(os.getenv("NOYRA_DAILY_EMBEDDING_CALLS", "1000")),
            daily_token_limit=int(os.getenv("NOYRA_DAILY_EMBEDDING_TOKENS", "5000000")),
            daily_cost_limit_microusd=cls._usd_to_microusd(
                os.getenv("NOYRA_DAILY_EMBEDDING_COST_LIMIT", "1")
            ),
            input_cost_microusd_per_million=cls._usd_to_microusd(
                os.getenv("NOYRA_EMBEDDING_INPUT_USD_PER_MILLION", "0.02")
            ),
            circuit_failure_threshold=int(os.getenv("NOYRA_EMBEDDING_CIRCUIT_FAILURES", "3")),
            circuit_cooldown_seconds=float(
                os.getenv("NOYRA_EMBEDDING_CIRCUIT_COOLDOWN_SECONDS", "60")
            ),
        )

    @staticmethod
    def _usd_to_microusd(value: str) -> int:
        try:
            decimal = Decimal(value)
        except InvalidOperation as error:
            raise ValueError("embedding USD setting is invalid") from error
        if not decimal.is_finite() or decimal < 0:
            raise ValueError("embedding USD setting is invalid")
        return int((decimal * Decimal(1_000_000)).to_integral_value(rounding=ROUND_CEILING))


class OpenAIEmbeddingProvider:
    """Independent OpenAI-compatible embedding resource boundary."""

    def __init__(
        self,
        settings: EmbeddingSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        call_authorizer: Callable[[], None] | None = None,
    ):
        self.settings = settings
        self.transport = transport
        self.call_authorizer = call_authorizer

    @property
    def name(self) -> str:
        return f"openai-compatible:{self.settings.model}"

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(vector) for vector in self.embed_with_usage(texts).vectors]

    def embed_with_usage(self, texts: Sequence[str]) -> EmbeddingProviderResponse:
        if not texts or len(texts) > 128:
            raise ValueError("embedding batch size must be between one and 128")
        if any(not isinstance(text, str) or not text for text in texts):
            raise ValueError("embedding inputs must be nonempty text")
        if self.call_authorizer is not None:
            try:
                self.call_authorizer()
            except EmbeddingProviderError:
                raise
            except (IntegrityError, PermissionError) as error:
                raise EmbeddingProviderError(
                    "embedding_resource_inactive", usage_unknown=False
                ) from error
        payload: dict[str, object] = {
            "model": self.settings.model,
            "input": list(texts),
        }
        if self.settings.dimensions is not None:
            payload["dimensions"] = self.settings.dimensions

        timeout_hook = SyncHTTPTimeoutHook()

        def perform_request() -> tuple[bytes, str | None]:
            deadline = time.monotonic() + self.settings.timeout_seconds
            with (
                httpx.Client(
                    timeout=httpx.Timeout(self.settings.timeout_seconds),
                    follow_redirects=False,
                    trust_env=False,
                    transport=self.transport or PublicDNSHTTPTransport(),
                ) as client,
                client.stream(
                    "POST",
                    urljoin(self.settings.base_url, "embeddings"),
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.settings.api_key.get_secret_value()}",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                        "Content-Type": "application/json",
                        "User-Agent": "Noyra/0.1.0",
                    },
                    follow_redirects=False,
                ) as response,
            ):
                timeout_hook.register(response)
                try:
                    validate_response_headers(
                        response,
                        max_header_bytes=EMBEDDING_MAX_HEADER_BYTES,
                    )
                    response.raise_for_status()
                    body_bytes = read_bounded_sync_response(
                        response,
                        max_body_bytes=EMBEDDING_MAX_RESPONSE_BYTES,
                        max_header_bytes=EMBEDDING_MAX_HEADER_BYTES,
                        total_timeout_seconds=self.settings.timeout_seconds,
                        deadline=deadline,
                    )
                    request_id = response.headers.get("x-request-id") or response.headers.get(
                        "x-provider-request-id"
                    )
                finally:
                    timeout_hook.clear(response)
            return body_bytes, request_id

        try:
            body_bytes, request_id = call_sync_http_with_deadline(
                perform_request,
                timeout=self.settings.timeout_seconds,
                on_timeout=timeout_hook.cancel,
            )
        except httpx.ConnectError as error:
            raise EmbeddingProviderError("embedding_connect_failed", usage_unknown=False) from error
        except (TimeoutError, httpx.TimeoutException) as error:
            raise EmbeddingProviderError("embedding_timeout", usage_unknown=True) from error
        except HTTPResponseLimitError as error:
            raise EmbeddingProviderError(
                "embedding_response_too_large", usage_unknown=True
            ) from error
        except httpx.HTTPStatusError as error:
            raise EmbeddingProviderError(
                f"embedding_http_{error.response.status_code}", usage_unknown=True
            ) from error
        except httpx.TransportError as error:
            raise EmbeddingProviderError(
                "embedding_transport_failed", usage_unknown=True
            ) from error

        try:
            body = strict_json_loads(body_bytes)
            vectors = self._vectors(body, len(texts))
            usage = self._usage(body, estimate_embedding_input_tokens(texts))
        except _EmbeddingResponseTokenLimitError as error:
            raise EmbeddingProviderError(
                "embedding_response_token_limit", usage_unknown=True
            ) from error
        except (TypeError, ValueError, OverflowError) as error:
            raise EmbeddingProviderError(
                "embedding_response_invalid", usage_unknown=True
            ) from error
        return EmbeddingProviderResponse(vectors, usage, request_id)

    @staticmethod
    def _vectors(body: Any, expected_count: int) -> tuple[tuple[float, ...], ...]:
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list) or len(data) != expected_count:
            raise ValueError("embedding provider returned an invalid batch")
        indexed: dict[int, tuple[float, ...]] = {}
        dimensions: int | None = None
        for item in data:
            if not isinstance(item, dict):
                raise ValueError("embedding provider returned an invalid vector")
            index = strict_int(item.get("index"))
            vector = item.get("embedding")
            if index in indexed or not 0 <= index < expected_count or not isinstance(vector, list):
                raise ValueError("embedding provider returned an invalid vector")
            parsed = tuple(float(value) for value in vector)
            if not 1 <= len(parsed) <= 16_384 or any(not math.isfinite(value) for value in parsed):
                raise ValueError("embedding provider returned an invalid vector")
            dimensions = dimensions or len(parsed)
            if len(parsed) != dimensions:
                raise ValueError("embedding provider returned inconsistent dimensions")
            indexed[index] = parsed
        return tuple(indexed[index] for index in range(expected_count))

    @staticmethod
    def _usage(body: Any, max_input_tokens: int) -> EmbeddingUsage | None:
        usage = body.get("usage") if isinstance(body, dict) else None
        if usage is None:
            return None
        if not isinstance(usage, dict):
            raise ValueError("embedding provider returned invalid usage")
        raw_prompt_tokens = usage.get("prompt_tokens")
        raw_total_tokens = usage.get("total_tokens")
        if raw_prompt_tokens is None and raw_total_tokens is None:
            raise ValueError("embedding provider returned invalid usage")
        tokens = strict_int(
            raw_prompt_tokens if raw_prompt_tokens is not None else raw_total_tokens
        )
        if raw_total_tokens is not None and strict_int(raw_total_tokens) != tokens:
            raise ValueError("embedding provider returned inconsistent usage")
        if tokens < 0:
            raise ValueError("embedding provider returned invalid usage")
        if tokens > max_input_tokens:
            raise _EmbeddingResponseTokenLimitError(
                "embedding provider token usage exceeds the request bound"
            )
        return EmbeddingUsage(tokens)
