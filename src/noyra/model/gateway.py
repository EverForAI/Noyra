from __future__ import annotations

import asyncio
import random
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from noyra.core.admission import OperationInvalidated, assert_current_lease
from noyra.core.errors import IntegrityError, InvalidTransitionError
from noyra.core.private_sources import InvitationWithdrawnError
from noyra.core.redaction import redact_payload
from noyra.core.types import canonical_json, content_hash

from .errors import (
    BudgetExhaustedError,
    ModelAuthorizationError,
    ModelCallStateError,
    ModelLifecycleBlockedError,
    ProviderCallError,
    StructuredOutputError,
)
from .ledger import ModelLedger
from .provider import ModelProvider
from .reasoning import REASONING_EFFORTS, normalize_reasoning_effort
from .types import (
    BudgetLimits,
    CallRecord,
    CompletionRequest,
    GatewayResult,
    ModelMessage,
    ModelPricing,
    ModelUsage,
    OutputT,
    RetryPolicy,
)


class ModelGateway:
    """Validated, budgeted, idempotent boundary around an untrusted model provider."""

    def __init__(
        self,
        provider: ModelProvider,
        ledger: ModelLedger,
        *,
        model: str,
        limits: BudgetLimits,
        pricing: ModelPricing | None = None,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        random_source: Callable[[], float] = random.random,
        resource_pool: str = "deep",
        resource_group_id: str | None = None,
        pool_limits: BudgetLimits | None = None,
        capture_model_io: bool = False,
        capture_model_io_getter: Callable[[], bool] | None = None,
        enforce_training_policy: bool = False,
        resource_revision: int | None = None,
        resource_key_id: str | None = None,
        resource_key_fingerprint: str | None = None,
        reasoning_effort: str | None = None,
        reasoning_capability_verified: bool = False,
    ):
        if not model.strip():
            raise ValueError("model name is required")
        self.provider = provider
        self.ledger = ledger
        self.model = model
        self.limits = limits
        self.pricing = pricing or ModelPricing()
        self.retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep
        self._random = random_source
        if resource_pool not in {"economy", "deep"}:
            raise ValueError("model resource pool is invalid")
        self.resource_pool = resource_pool
        self.resource_group_id = resource_group_id
        self.pool_limits = pool_limits
        self.capture_model_io = capture_model_io
        self.capture_model_io_getter = capture_model_io_getter
        self.enforce_training_policy = enforce_training_policy
        self.resource_revision = resource_revision
        self.resource_key_id = resource_key_id
        self.resource_key_fingerprint = resource_key_fingerprint
        self.reasoning_effort = reasoning_effort
        self.reasoning_capability_verified = reasoning_capability_verified

    async def complete_structured(
        self,
        subject_id: str,
        purpose: str,
        messages: Sequence[ModelMessage],
        output_type: type[OutputT],
        *,
        idempotency_key: str,
        max_output_tokens: int = 1024,
        temperature: float = 0.2,
        reasoning_effort: str | None = None,
    ) -> GatewayResult[OutputT]:
        if not subject_id.strip() or not purpose.strip() or not idempotency_key.strip():
            raise ValueError("subject_id, purpose and idempotency_key are required")
        self.ledger.check_lifecycle(subject_id, purpose)
        selected_reasoning = self.reasoning_effort if reasoning_effort is None else reasoning_effort
        if self.reasoning_capability_verified:
            if selected_reasoning != self.reasoning_effort:
                raise ValueError("reasoning effort was not verified for this resource")
            if selected_reasoning is not None and selected_reasoning not in REASONING_EFFORTS:
                raise ValueError("reasoning effort is invalid")
            normalized_reasoning = selected_reasoning
        else:
            normalized_reasoning = normalize_reasoning_effort(self.model, selected_reasoning)
        request = CompletionRequest(
            model=self.model,
            messages=tuple(messages),
            max_output_tokens=max_output_tokens,
            temperature=temperature,
            schema_name=output_type.__name__,
            output_schema=output_type.model_json_schema(),
            reasoning_effort=normalized_reasoning,
        )
        request_hash = content_hash(request.model_dump(mode="json"))
        call, created = self.ledger.prepare_call(
            subject_id,
            self.provider.name,
            self.model,
            purpose,
            request_hash,
            idempotency_key,
            resource_pool=self.resource_pool,
            resource_group_id=self.resource_group_id,
            request=(
                redact_payload(request.model_dump(mode="json"))
                if self.enforce_training_policy or self._capture_model_io_enabled()
                else None
            ),
            enforce_training_policy=self.enforce_training_policy,
        )
        if not created:
            if call.status == "succeeded":
                return self._cached_result(call, output_type)
            if call.status != "prepared":
                raise ModelCallStateError(
                    f"idempotent model call already exists in state {call.status}"
                )

        reserved_usage = ModelUsage(
            input_tokens=self.estimate_input_tokens(request),
            output_tokens=request.max_output_tokens,
        )
        reserved_cost = self.pricing.cost_microusd(reserved_usage)

        for attempt_index in range(1, self.retry_policy.max_attempts + 1):
            try:
                assert_current_lease()
            except OperationInvalidated:
                self.ledger.finish_call(
                    call.call_id,
                    "unknown",
                    error_code="runtime_epoch_invalidated",
                )
                raise
            try:
                attempt = self.ledger.authorize_attempt(
                    call.call_id,
                    self.limits,
                    reserved_input_tokens=reserved_usage.input_tokens,
                    reserved_output_tokens=reserved_usage.output_tokens,
                    reserved_cost_microusd=reserved_cost,
                    resource_group_id=self.resource_group_id,
                    pool_limits=self.pool_limits,
                    continuation=attempt_index > 1,
                    expected_resource_revision=self.resource_revision,
                    refresh_pool_limits=self.resource_revision is not None,
                    resource_key_id=self.resource_key_id,
                    resource_key_fingerprint=self.resource_key_fingerprint,
                    pricing=self.pricing,
                )
            except (
                BudgetExhaustedError,
                InvitationWithdrawnError,
                ModelAuthorizationError,
                ModelLifecycleBlockedError,
            ) as error:
                self.ledger.finish_call(
                    call.call_id,
                    "failed",
                    error_code=(
                        "cost_only_pricing_unknown"
                        if str(error) == "cost_only_pricing_unknown"
                        else "budget_exhausted"
                    )
                    if isinstance(error, BudgetExhaustedError)
                    else "model_lifecycle_blocked"
                    if isinstance(error, ModelLifecycleBlockedError)
                    else "model_authorization_denied",
                )
                raise
            except InvalidTransitionError as error:
                if str(error) != "evidence_capacity_reached":
                    raise
                # No new attempt was admitted; preserve all earlier settled usage.
                self.ledger.finish_call(
                    call.call_id, "failed", error_code="evidence_capacity_reached"
                )
                raise

            send_denied = False

            def before_send() -> None:
                nonlocal send_denied
                try:
                    assert_current_lease()
                except OperationInvalidated:
                    send_denied = True
                    raise
                self.ledger.check_request_authorization(
                    call.call_id,
                    resource_key_id=self.resource_key_id,
                    resource_key_fingerprint=self.resource_key_fingerprint,
                    expected_resource_revision=self.resource_revision,
                )
                try:
                    assert_current_lease()
                except OperationInvalidated:
                    send_denied = True
                    raise

            try:
                self.ledger.start_attempt(
                    attempt.attempt_id,
                    resource_key_id=self.resource_key_id,
                    resource_key_fingerprint=self.resource_key_fingerprint,
                    expected_resource_revision=self.resource_revision,
                )
                before_send()
            except (
                InvitationWithdrawnError,
                ModelAuthorizationError,
                ModelLifecycleBlockedError,
                OperationInvalidated,
            ) as error:
                code = (
                    "runtime_epoch_invalidated"
                    if isinstance(error, OperationInvalidated)
                    else "model_lifecycle_blocked"
                    if isinstance(error, ModelLifecycleBlockedError)
                    else "model_authorization_denied"
                )
                self.ledger.cancel_unsent_attempt(attempt.attempt_id, error_code=code)
                self.ledger.finish_call(call.call_id, "failed", error_code=code)
                raise

            from .openai_compatible import OpenAICompatibleProvider

            if isinstance(self.provider, OpenAICompatibleProvider):
                self.provider.before_send = before_send

            stale_operation = False
            try:
                before_send()
                provider_response = await self.provider.complete(request)
                try:
                    assert_current_lease()
                except OperationInvalidated:
                    stale_operation = True
            except (ModelAuthorizationError, ModelLifecycleBlockedError) as error:
                code = (
                    "model_lifecycle_blocked"
                    if isinstance(error, ModelLifecycleBlockedError)
                    else "model_authorization_denied"
                )
                self.ledger.cancel_unsent_attempt(attempt.attempt_id, error_code=code)
                self.ledger.finish_call(call.call_id, "failed", error_code=code)
                raise
            except OperationInvalidated:
                if send_denied:
                    self.ledger.cancel_unsent_attempt(
                        attempt.attempt_id, error_code="runtime_epoch_invalidated"
                    )
                    self.ledger.finish_call(
                        call.call_id, "failed", error_code="runtime_epoch_invalidated"
                    )
                    raise
                self.ledger.finish_attempt(
                    attempt.attempt_id,
                    "unknown",
                    usage=None,
                    cost_microusd=None,
                    error_code="runtime_epoch_invalidated",
                )
                self.ledger.finish_call(
                    call.call_id,
                    "unknown",
                    error_code="runtime_epoch_invalidated",
                )
                raise
            except asyncio.CancelledError:
                self.ledger.finish_attempt(
                    attempt.attempt_id,
                    "unknown",
                    usage=None,
                    cost_microusd=None,
                    error_code="model_call_cancelled",
                )
                self.ledger.finish_call(
                    call.call_id,
                    "unknown",
                    error_code="model_call_cancelled",
                )
                raise
            except ProviderCallError as error:
                if error.outcome_unknown:
                    self.ledger.finish_attempt(
                        attempt.attempt_id,
                        "unknown",
                        usage=None,
                        cost_microusd=None,
                        error_code=error.code,
                    )
                    self.ledger.finish_call(call.call_id, "unknown", error_code=error.code)
                    raise
                self.ledger.finish_attempt(
                    attempt.attempt_id,
                    "failed",
                    usage=None if error.usage_unknown else ModelUsage(0, 0),
                    cost_microusd=None if error.usage_unknown else 0,
                    error_code=error.code,
                )
                if error.retryable and attempt_index < self.retry_policy.max_attempts:
                    try:
                        assert_current_lease()
                    except OperationInvalidated:
                        self.ledger.finish_call(
                            call.call_id,
                            "unknown",
                            error_code="runtime_epoch_invalidated",
                        )
                        raise
                    await self._retry_delay(attempt_index)
                    continue
                self.ledger.finish_call(call.call_id, "failed", error_code=error.code)
                raise
            except Exception as error:
                self.ledger.finish_attempt(
                    attempt.attempt_id,
                    "unknown",
                    usage=None,
                    cost_microusd=None,
                    error_code="provider_contract_failure",
                )
                self.ledger.finish_call(
                    call.call_id,
                    "unknown",
                    error_code="provider_contract_failure",
                )
                raise ProviderCallError(
                    "provider_contract_failure",
                    retryable=False,
                    outcome_unknown=True,
                ) from error

            usage_estimated = provider_response.usage is None
            usage = provider_response.usage or reserved_usage
            cost_microusd = self.pricing.cost_microusd(usage)
            self.ledger.finish_attempt(
                attempt.attempt_id,
                "succeeded",
                usage=usage,
                cost_microusd=cost_microusd,
                provider_request_id=provider_response.provider_request_id,
                usage_estimated=usage_estimated,
            )
            if stale_operation:
                # The provider side effect is already complete, so preserve
                # its accounting before fencing the result out of cognition.
                # Do not retry or surface a parser/provider error from this
                # stale epoch: either terminal accounting outcome must still
                # be represented by OperationInvalidated to the caller.
                stale_response_record: dict[str, Any] = {
                    "content": provider_response.content,
                    "usage": {
                        "input_tokens": usage.input_tokens,
                        "output_tokens": usage.output_tokens,
                    },
                    "cost_microusd": self._total_call_cost(call.call_id),
                    "finish_reason": provider_response.finish_reason,
                    "provider_request_id": provider_response.provider_request_id,
                    "attempts": attempt_index,
                }
                try:
                    output_type.model_validate_json(provider_response.content)
                except Exception as error:
                    self.ledger.finish_call(
                        call.call_id,
                        "failed",
                        error_code="structured_output_invalid",
                    )
                    raise OperationInvalidated(
                        "model result belongs to a stale runtime epoch"
                    ) from error
                self.ledger.finish_call(
                    call.call_id,
                    "succeeded",
                    response=stale_response_record,
                    usage_estimated=usage_estimated,
                )
                raise OperationInvalidated("model result belongs to a stale runtime epoch")
            try:
                output = output_type.model_validate_json(provider_response.content)
            except Exception as error:
                if (
                    self.retry_policy.retry_invalid_output
                    and attempt_index < self.retry_policy.max_attempts
                ):
                    try:
                        assert_current_lease()
                    except OperationInvalidated:
                        self.ledger.finish_call(
                            call.call_id,
                            "unknown",
                            error_code="runtime_epoch_invalidated",
                        )
                        raise
                    await self._retry_delay(attempt_index)
                    continue
                self.ledger.finish_call(
                    call.call_id,
                    "failed",
                    error_code="structured_output_invalid",
                )
                raise StructuredOutputError("structured_output_invalid") from error

            response_record: dict[str, Any] = {
                "content": provider_response.content,
                "usage": {
                    "input_tokens": usage.input_tokens,
                    "output_tokens": usage.output_tokens,
                },
                "cost_microusd": self._total_call_cost(call.call_id),
                "finish_reason": provider_response.finish_reason,
                "provider_request_id": provider_response.provider_request_id,
                "attempts": attempt_index,
            }
            self.ledger.finish_call(
                call.call_id,
                "succeeded",
                response=response_record,
                usage_estimated=usage_estimated,
            )
            return GatewayResult(
                output=output,
                raw_content=provider_response.content,
                usage=usage,
                usage_estimated=usage_estimated,
                cost_microusd=int(response_record["cost_microusd"]),
                call_id=call.call_id,
                attempts=attempt_index,
                cached=False,
            )

        raise AssertionError("retry loop ended without a terminal model-call result")

    async def _retry_delay(self, completed_attempts: int) -> None:
        unjittered = min(
            self.retry_policy.max_delay_seconds,
            self.retry_policy.base_delay_seconds * (2 ** (completed_attempts - 1)),
        )
        await self._sleep(unjittered * (0.5 + self._random() * 0.5))

    @staticmethod
    def estimate_input_tokens(request: CompletionRequest) -> int:
        serialized = canonical_json(request.model_dump(mode="json"))
        return max(1, len(serialized.encode("utf-8")) + 32)

    def _capture_model_io_enabled(self) -> bool:
        getter = self.capture_model_io_getter
        if getter is None:
            return self.capture_model_io
        try:
            return bool(getter())
        except Exception:
            # A policy read failure must fail closed for privacy.
            return False

    def _total_call_cost(self, call_id: str) -> int:
        return sum(
            attempt.cost_microusd
            if attempt.cost_microusd is not None
            else attempt.reserved_cost_microusd
            for attempt in self.ledger.attempts(call_id)
            if attempt.status != "cancelled"
        )

    @staticmethod
    def _cached_result(call: CallRecord, output_type: type[OutputT]) -> GatewayResult[OutputT]:
        response = call.response
        if response is None:
            raise IntegrityError(f"successful model call has no response: {call.call_id}")
        try:
            content = response["content"]
            usage_payload = response["usage"]
            usage = ModelUsage(
                input_tokens=int(usage_payload["input_tokens"]),
                output_tokens=int(usage_payload["output_tokens"]),
            )
            output = output_type.model_validate_json(content)
            cost_microusd = int(response["cost_microusd"])
            attempts = int(response["attempts"])
            if not isinstance(content, str) or cost_microusd < 0 or attempts < 1:
                raise ValueError("invalid cached model response")
        except Exception as error:
            raise IntegrityError(f"cached model response is invalid: {call.call_id}") from error
        return GatewayResult(
            output=output,
            raw_content=content,
            usage=usage,
            usage_estimated=call.usage_estimated,
            cost_microusd=cost_microusd,
            call_id=call.call_id,
            attempts=attempts,
            cached=True,
        )
