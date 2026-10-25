from __future__ import annotations


class ModelGatewayError(Exception):
    """Base class for model-boundary failures safe to expose to the supervisor."""


class ConfigurationError(ModelGatewayError):
    """Remote model configuration is missing or unsafe."""


class BudgetExhaustedError(ModelGatewayError):
    """A hard daily model-resource limit would be exceeded."""


class EmbeddingBudgetExhaustedError(BudgetExhaustedError):
    """A hard daily embedding-resource limit would be exceeded."""


class EmbeddingCircuitOpenError(ModelGatewayError):
    """The embedding provider circuit is open and recall must use local fallback."""


class EmbeddingProviderError(ModelGatewayError):
    """Sanitized embedding-provider failure with explicit usage ambiguity."""

    def __init__(self, code: str, *, usage_unknown: bool) -> None:
        super().__init__(code)
        self.code = code
        self.usage_unknown = usage_unknown


class EmbeddingShutdownIncompleteError(ModelGatewayError):
    """Embedding work is still live; callers must retain runtime ownership."""

    def __init__(self) -> None:
        super().__init__("embedding_shutdown_incomplete")
        self.code = "embedding_shutdown_incomplete"


class ModelCallConflictError(ModelGatewayError):
    """An idempotency key conflicts with another logical model call."""


class ModelCallStateError(ModelGatewayError):
    """A model call cannot advance from its current durable state."""


class UnknownRetryResolutionEvidenceRequired(ModelCallStateError):
    """Closing an ambiguous retry requires evidence and explicit risk acceptance."""


class ModelResourceInactiveError(ModelCallStateError):
    """A saved resource is disabled or revoked; it is not an incomplete draft."""


class ModelLifecycleBlockedError(ModelCallStateError):
    """The durable lifecycle forbids sending a model request."""

    def __init__(self, lifecycle: str):
        super().__init__("model calls are disabled by the current lifecycle")
        self.lifecycle = lifecycle


class ModelAuthorizationError(ModelCallStateError):
    """Current resource authorization denies a request that has not been sent."""


class StructuredOutputError(ModelGatewayError):
    """The provider response did not satisfy the requested output schema."""


class ProviderCallError(ModelGatewayError):
    """Sanitized provider failure with explicit retry and ambiguity semantics."""

    def __init__(
        self,
        code: str,
        *,
        retryable: bool,
        outcome_unknown: bool,
        usage_unknown: bool = False,
        status_code: int | None = None,
        latency_ms: int | None = None,
        provider_request_id: str | None = None,
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.usage_unknown = usage_unknown
        self.status_code = status_code
        self.latency_ms = None if latency_ms is None else max(0, min(int(latency_ms), 86_400_000))
        self.provider_request_id = (
            provider_request_id[:256] if isinstance(provider_request_id, str) else None
        )

    @property
    def category(self) -> str:
        if self.code in {"provider_auth_failed", "provider_http_401", "provider_http_403"}:
            return "provider_auth_failed"
        if self.code in {"provider_http_404", "provider_model_mismatch"}:
            return "provider_http_404"
        if self.code in {"provider_http_429", "provider_rate_limited"}:
            return "provider_rate_limited"
        if self.code == "provider_http_5xx" or self.code.startswith("provider_http_5"):
            return "provider_http_5xx"
        if self.code in {"provider_connect_failed", "provider_endpoint_rejected"}:
            return "provider_connect_failed"
        if self.code in {"provider_timeout", "provider_transport_timeout"}:
            return "provider_timeout"
        if self.code in {
            "provider_response_invalid",
            "provider_response_empty",
            "provider_response_too_large",
        }:
            return "provider_response_invalid"
        if self.outcome_unknown or self.code == "provider_outcome_unknown":
            return "provider_outcome_unknown"
        return self.code
