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


class ModelCallConflictError(ModelGatewayError):
    """An idempotency key conflicts with another logical model call."""


class ModelCallStateError(ModelGatewayError):
    """A model call cannot advance from its current durable state."""


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
    ):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.outcome_unknown = outcome_unknown
        self.usage_unknown = usage_unknown
        self.status_code = status_code
