from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from typing import Any, Protocol, cast

from noyra.core.errors import IntegrityError
from noyra.core.types import content_hash, new_id

from .embedding import (
    EmbeddingBudgetLimits,
    EmbeddingCircuitPolicy,
    EmbeddingPricing,
    EmbeddingProviderResponse,
    estimate_embedding_input_tokens,
)
from .embedding_ledger import EmbeddingLedger
from .errors import (
    EmbeddingBudgetExhaustedError,
    EmbeddingCircuitOpenError,
    EmbeddingProviderError,
)


class RawEmbeddingProvider(Protocol):
    @property
    def name(self) -> str: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class EmbeddingAccountingEvent:
    outcome: str
    budget_pressure: float
    input_tokens: int
    pool_pressure: float
    frustration: float


class EmbeddingGateway:
    """Budgeted, ledgered, timeout-bounded embedding provider used by memory recall."""

    def __init__(
        self,
        provider: RawEmbeddingProvider,
        ledger: EmbeddingLedger,
        *,
        subject_id: str,
        resource_id: str,
        model: str,
        limits: EmbeddingBudgetLimits,
        pricing: EmbeddingPricing,
        circuit_policy: EmbeddingCircuitPolicy,
        timeout_seconds: float,
        accounting: Callable[[EmbeddingAccountingEvent], None] | None = None,
        executor: ThreadPoolExecutor | None = None,
    ):
        if not subject_id.strip() or not resource_id.strip() or not model.strip():
            raise ValueError("embedding gateway identity is required")
        if not 0 < timeout_seconds <= 300:
            raise ValueError("embedding gateway timeout is invalid")
        self.provider = provider
        self.ledger = ledger
        self.subject_id = subject_id
        self.resource_id = resource_id
        self.model = model
        self.limits = limits
        self.pricing = pricing
        self.circuit_policy = circuit_policy
        self.timeout_seconds = timeout_seconds
        self.accounting = accounting
        # Caller-provided executors retain their normal lifecycle.  Owned calls
        # use daemon threads so a provider that ignores cancellation cannot keep
        # the interpreter alive after the hard deadline.
        self._executor = executor
        self._owns_executor = False
        self._worker_condition = threading.Condition()
        self._workers: set[threading.Thread] = set()
        self._futures: set[Future[Any]] = set()

    @property
    def name(self) -> str:
        return self.provider.name

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        normalized = tuple(texts)
        if not normalized or len(normalized) > 128:
            raise ValueError("embedding batch size must be between one and 128")
        if any(not isinstance(text, str) or not text for text in normalized):
            raise ValueError("embedding inputs must be nonempty text")
        reserved_tokens = self.estimate_input_tokens(normalized)
        reserved_cost = self.pricing.cost_microusd(reserved_tokens)
        request_hash = content_hash(
            {
                "provider": self.provider.name,
                "model": self.model,
                "texts": normalized,
            }
        )
        try:
            usage, _ = self.ledger.authorize(
                self.subject_id,
                self.resource_id,
                self.provider.name,
                self.model,
                "memory_embedding",
                request_hash,
                new_id("embreq"),
                self.limits,
                self.circuit_policy,
                text_count=len(normalized),
                reserved_tokens=reserved_tokens,
                reserved_cost_microusd=reserved_cost,
            )
        except EmbeddingBudgetExhaustedError:
            self._account("budget_exhausted", reserved_tokens, pool_pressure=1.0, frustration=0.1)
            raise
        except EmbeddingCircuitOpenError:
            self._account("circuit_open", 0, pool_pressure=1.0, frustration=0.2)
            raise
        self.ledger.start(usage.usage_id)

        try:
            response = self._call_with_timeout(normalized)
        except FutureTimeoutError as error:
            provider_error = EmbeddingProviderError("embedding_hard_timeout", usage_unknown=True)
            self._finish_failure(usage.usage_id, provider_error, reserved_tokens)
            raise provider_error from error
        except EmbeddingProviderError as error:
            self._finish_failure(usage.usage_id, error, reserved_tokens)
            raise
        except Exception as error:
            provider_error = EmbeddingProviderError(
                "embedding_provider_contract_failure", usage_unknown=True
            )
            self._finish_failure(usage.usage_id, provider_error, reserved_tokens)
            raise provider_error from error

        try:
            vectors = self._validate_vectors(response.vectors, len(normalized))
            if response.usage is not None and response.usage.input_tokens > reserved_tokens:
                raise EmbeddingProviderError("embedding_response_token_limit", usage_unknown=True)
            actual_tokens = (
                reserved_tokens if response.usage is None else response.usage.input_tokens
            )
            actual_cost = self.pricing.cost_microusd(actual_tokens)
            try:
                self.ledger.succeed(
                    usage.usage_id,
                    input_tokens=actual_tokens,
                    cost_microusd=actual_cost,
                    usage_estimated=response.usage is None,
                    provider_request_id=response.provider_request_id,
                )
            except ValueError as error:
                self._handle_recovered_usage(usage.usage_id, reserved_tokens, error)
                raise EmbeddingProviderError(
                    "embedding_usage_recovered", usage_unknown=True
                ) from error
        except EmbeddingProviderError as error:
            self._finish_failure(usage.usage_id, error, reserved_tokens)
            raise
        except (TypeError, ValueError, OverflowError) as error:
            provider_error = EmbeddingProviderError(
                "embedding_provider_contract_failure", usage_unknown=True
            )
            self._finish_failure(usage.usage_id, provider_error, reserved_tokens)
            raise provider_error from error
        self._account("succeeded", actual_tokens, pool_pressure=0.0, frustration=0.0)
        return [list(vector) for vector in vectors]

    def close(self, *, wait: bool = False, timeout: float | None = None) -> bool:
        if self._owns_executor and self._executor is not None:
            self._executor.shutdown(wait=wait, cancel_futures=True)
        if not wait:
            return not self._workers and not self._futures
        with self._worker_condition:
            if timeout is None:
                while self._workers or self._futures:
                    self._worker_condition.wait()
                return True
            end = time.monotonic() + max(0.0, timeout)
            while self._workers or self._futures:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    return False
                self._worker_condition.wait(remaining)
            return True

    def _call_with_timeout(self, texts: tuple[str, ...]) -> EmbeddingProviderResponse:
        if self._executor is not None:
            future: Future[EmbeddingProviderResponse] = self._executor.submit(
                self._provider_call, texts
            )
            with self._worker_condition:
                self._futures.add(future)

            def complete(completed: Future[Any]) -> None:
                with self._worker_condition:
                    self._futures.discard(completed)
                    self._worker_condition.notify_all()

            future.add_done_callback(complete)
            return future.result(timeout=self.timeout_seconds)

        result: list[EmbeddingProviderResponse] = []
        errors: list[BaseException] = []

        def invoke() -> None:
            try:
                result.append(self._provider_call(texts))
            except BaseException as error:  # propagate provider failures to the caller
                errors.append(error)
            finally:
                with self._worker_condition:
                    self._workers.discard(worker)
                    self._worker_condition.notify_all()

        worker = threading.Thread(target=invoke, name="noyra-embedding-call", daemon=True)
        with self._worker_condition:
            self._workers.add(worker)
        try:
            worker.start()
        except BaseException:
            with self._worker_condition:
                self._workers.discard(worker)
                self._worker_condition.notify_all()
            raise
        worker.join(self.timeout_seconds)
        if worker.is_alive():
            raise FutureTimeoutError()
        if errors:
            raise errors[0]
        if not result:
            raise RuntimeError("embedding provider returned no result")
        return result[0]

    def _handle_recovered_usage(
        self, usage_id: str, reserved_tokens: int, error: ValueError
    ) -> None:
        try:
            record = self.ledger.get(usage_id)
        except (KeyError, IntegrityError):
            raise error from None
        if record.status not in {"unknown", "cancelled", "failed", "succeeded"}:
            raise error from None
        self._account(
            "embedding_usage_recovered",
            reserved_tokens if record.status == "unknown" else 0,
            pool_pressure=1.0,
            frustration=0.25,
        )

    def _finish_failure(
        self,
        usage_id: str,
        error: EmbeddingProviderError,
        reserved_tokens: int,
    ) -> None:
        try:
            self.ledger.fail(
                usage_id,
                error_code=error.code,
                usage_unknown=error.usage_unknown,
                circuit_policy=self.circuit_policy,
            )
        except ValueError as state_error:
            self._handle_recovered_usage(usage_id, reserved_tokens, state_error)
            return
        circuit = self.ledger.circuit(self.subject_id, self.resource_id)
        self._account(
            error.code,
            reserved_tokens if error.usage_unknown else 0,
            pool_pressure=1.0 if circuit.status == "open" else 0.5,
            frustration=0.25,
        )

    def _account(
        self,
        outcome: str,
        input_tokens: int,
        *,
        pool_pressure: float,
        frustration: float,
    ) -> None:
        if self.accounting is None:
            return
        status = self.ledger.budget_status(
            self.subject_id,
            self.resource_id,
            self.limits,
        )
        self.accounting(
            EmbeddingAccountingEvent(
                outcome,
                status.pressure,
                input_tokens,
                pool_pressure,
                frustration,
            )
        )

    def _provider_call(self, texts: tuple[str, ...]) -> EmbeddingProviderResponse:
        embed_with_usage = getattr(self.provider, "embed_with_usage", None)
        if embed_with_usage is not None:
            response = embed_with_usage(texts)
            if not isinstance(response, EmbeddingProviderResponse):
                raise TypeError("embedding provider returned an invalid response type")
            return response
        vectors = self.provider.embed(texts)
        return EmbeddingProviderResponse(
            tuple(tuple(float(value) for value in vector) for vector in vectors),
            None,
        )

    @staticmethod
    def estimate_input_tokens(texts: Sequence[str]) -> int:
        return estimate_embedding_input_tokens(texts)

    @staticmethod
    def _validate_vectors(vectors: Any, expected_count: int) -> tuple[tuple[float, ...], ...]:
        if not isinstance(vectors, (list, tuple)) or len(vectors) != expected_count:
            raise ValueError("embedding provider returned an invalid batch")
        parsed: list[tuple[float, ...]] = []
        dimensions: int | None = None
        for raw_vector in cast(Sequence[Any], vectors):
            if not isinstance(raw_vector, (list, tuple)):
                raise ValueError("embedding provider returned an invalid vector")
            vector = tuple(float(value) for value in raw_vector)
            if not 1 <= len(vector) <= 16_384 or any(not math.isfinite(value) for value in vector):
                raise ValueError("embedding provider returned an invalid vector")
            dimensions = dimensions or len(vector)
            if len(vector) != dimensions:
                raise ValueError("embedding provider returned inconsistent dimensions")
            parsed.append(vector)
        return tuple(parsed)
