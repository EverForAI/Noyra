from __future__ import annotations

import asyncio
import ipaddress
import json
from contextlib import suppress
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from noyra.core.http import (
    DEFAULT_MAX_HEADER_BYTES,
    HTTPResponseLimitError,
    PublicDNSAsyncHTTPTransport,
    read_bounded_response,
    validate_response_headers,
)

from .config import OpenAICompatibleSettings
from .errors import ProviderCallError
from .types import CompletionRequest, ModelUsage, ProviderResponse


class _UsagePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)


class _MessagePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: str


class _ChoicePayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _MessagePayload
    finish_reason: str | None = None


class _CompletionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str | None = None
    choices: list[_ChoicePayload]
    usage: _UsagePayload | None = None


class OpenAICompatibleProvider:
    name = "openai_compatible"

    def __init__(
        self,
        settings: OpenAICompatibleSettings,
        *,
        client: httpx.AsyncClient | None = None,
    ):
        self.settings = settings
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            timeout = httpx.Timeout(
                settings.timeout_seconds,
                connect=settings.connect_timeout_seconds,
            )
            limits = httpx.Limits(max_connections=settings.max_connections)
            parsed = urlsplit(settings.base_url)
            hostname = parsed.hostname or ""
            local = hostname.casefold() == "localhost"
            with suppress(ValueError):
                local = ipaddress.ip_address(hostname.split("%", 1)[0]).is_loopback
            local_endpoint = settings.allow_local_endpoint and local
            self._client = httpx.AsyncClient(
                timeout=timeout,
                limits=limits,
                follow_redirects=False,
                trust_env=settings.trust_environment if local_endpoint else False,
                transport=(
                    None
                    if local_endpoint
                    else PublicDNSAsyncHTTPTransport(max_connections=settings.max_connections)
                ),
            )

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        if request.model != self.settings.model:
            raise ProviderCallError(
                "provider_model_mismatch", retryable=False, outcome_unknown=False
            )
        headers = {
            "Accept": "application/json",
            # Keep the response-size contract in terms of bytes received.  A
            # provider must not transparently expand an unbounded compressed
            # response outside the bounded reader.
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "Noyra/0.1.0",
        }
        api_key = self.settings.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.settings.timeout_seconds
        try:
            # httpx's read timeout is an idle timeout.  The outer scope is the
            # operation contract: DNS, connection, response headers, and every
            # response chunk must fit inside one absolute wall-clock deadline.
            async with asyncio.timeout(self.settings.timeout_seconds):
                async with self._client.stream(
                    "POST",
                    f"{self.settings.base_url}/chat/completions",
                    headers=headers,
                    json=self._request_payload(request),
                    follow_redirects=False,
                ) as response:
                    return await self._parse_response(response, deadline=deadline)
        except ProviderCallError:
            raise
        except HTTPResponseLimitError:
            raise ProviderCallError(
                "provider_response_too_large",
                retryable=False,
                outcome_unknown=True,
                usage_unknown=True,
            ) from None
        except TimeoutError:
            raise ProviderCallError(
                "provider_outcome_unknown", retryable=False, outcome_unknown=True
            ) from None
        except (httpx.ConnectError, httpx.ConnectTimeout):
            raise ProviderCallError(
                "provider_connect_failed", retryable=True, outcome_unknown=False
            ) from None
        except (
            httpx.ReadTimeout,
            httpx.ReadError,
            httpx.WriteTimeout,
            httpx.WriteError,
            httpx.RemoteProtocolError,
        ):
            raise ProviderCallError(
                "provider_outcome_unknown", retryable=False, outcome_unknown=True
            ) from None
        except httpx.HTTPError:
            raise ProviderCallError(
                "provider_transport_failed", retryable=False, outcome_unknown=True
            ) from None

    @classmethod
    async def probe_unstored(
        cls,
        *,
        base_url: str,
        model: str,
        api_key: Any,
        client: httpx.AsyncClient | None = None,
    ) -> dict[str, Any]:
        """Probe draft credentials without creating a durable resource."""
        settings = OpenAICompatibleSettings(
            base_url=base_url,
            model=model,
            api_key=api_key,
            timeout_seconds=30,
            max_response_bytes=256_000,
        )
        provider = cls(settings, client=client)
        started = asyncio.get_running_loop().time()
        deadline = started + settings.timeout_seconds
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with provider._client.stream(
                    "POST",
                    f"{settings.base_url}/chat/completions",
                    headers=provider._headers(),
                    json={
                        "model": settings.model,
                        "messages": [{"role": "user", "content": "Reply with OK."}],
                        "max_tokens": 8,
                        "temperature": 0,
                    },
                    follow_redirects=False,
                ) as response:
                    parsed = await provider._parse_response(response, deadline=deadline)
            if not parsed.content.strip():
                raise ProviderCallError(
                    "provider_response_empty", retryable=False, outcome_unknown=True
                )
            return {
                "ok": True,
                "model": settings.model,
                "elapsed_ms": round((asyncio.get_running_loop().time() - started) * 1000),
            }
        except ProviderCallError:
            raise
        except (
            json.JSONDecodeError,
            HTTPResponseLimitError,
            TimeoutError,
            httpx.TimeoutException,
        ) as error:
            raise ProviderCallError(
                "provider_timeout"
                if isinstance(error, (TimeoutError, httpx.TimeoutException))
                else "provider_response_invalid",
                retryable=False,
                outcome_unknown=True,
            ) from None
        except httpx.HTTPError:
            raise ProviderCallError(
                "provider_connect_failed", retryable=False, outcome_unknown=False
            ) from None
        finally:
            await provider.aclose()

    @classmethod
    async def list_models_unstored(
        cls,
        *,
        base_url: str,
        api_key: Any,
        client: httpx.AsyncClient | None = None,
    ) -> list[str]:
        """Return bounded, de-duplicated model ids without persisting credentials."""
        settings = OpenAICompatibleSettings(
            base_url=base_url,
            model="model-discovery-placeholder",
            api_key=api_key,
            timeout_seconds=30,
            max_response_bytes=512_000,
        )
        provider = cls(settings, client=client)
        deadline = asyncio.get_running_loop().time() + settings.timeout_seconds
        try:
            async with asyncio.timeout(settings.timeout_seconds):
                async with provider._client.stream(
                    "GET",
                    f"{settings.base_url}/models",
                    headers=provider._headers(),
                    follow_redirects=False,
                ) as response:
                    validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
                    if response.status_code in {404, 405, 501}:
                        raise ProviderCallError(
                            "model_discovery_unsupported",
                            retryable=False,
                            outcome_unknown=False,
                            status_code=response.status_code,
                        )
                    if response.status_code in {401, 403}:
                        raise ProviderCallError(
                            "provider_auth_failed",
                            retryable=False,
                            outcome_unknown=False,
                            status_code=response.status_code,
                        )
                    if 300 <= response.status_code < 400:
                        raise ProviderCallError(
                            "provider_redirected",
                            retryable=False,
                            outcome_unknown=False,
                            status_code=response.status_code,
                        )
                    if response.status_code < 200 or response.status_code >= 300:
                        raise ProviderCallError(
                            f"provider_http_{response.status_code}",
                            retryable=False,
                            outcome_unknown=False,
                            status_code=response.status_code,
                        )
                    body = await read_bounded_response(
                        response,
                        max_body_bytes=settings.max_response_bytes,
                        max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
                        total_timeout_seconds=settings.timeout_seconds,
                        deadline=deadline,
                    )
            payload = json.loads(body)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, list):
                raise ProviderCallError(
                    "model_discovery_invalid_response",
                    retryable=False,
                    outcome_unknown=False,
                )
            models_set: set[str] = set()
            for item in data:
                if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                    raise ProviderCallError(
                        "model_discovery_invalid_response", retryable=False, outcome_unknown=False
                    )
                model_id = item["id"].strip()
                if not model_id or len(model_id) > 256:
                    raise ProviderCallError(
                        "model_discovery_invalid_response", retryable=False, outcome_unknown=False
                    )
                models_set.add(model_id)
            models = sorted(models_set)
            return models[:500]
        except ProviderCallError:
            raise
        except (json.JSONDecodeError, HTTPResponseLimitError, TimeoutError, httpx.TimeoutException):
            raise ProviderCallError(
                "model_discovery_failed", retryable=False, outcome_unknown=True
            ) from None
        except httpx.HTTPError:
            raise ProviderCallError(
                "provider_connect_failed", retryable=False, outcome_unknown=False
            ) from None
        finally:
            await provider.aclose()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json",
            "User-Agent": "Noyra/0.1.0",
        }
        api_key = self.settings.api_key.get_secret_value()
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    async def _parse_response(
        self,
        response: httpx.Response,
        *,
        deadline: float | None = None,
    ) -> ProviderResponse:
        validate_response_headers(response, max_header_bytes=DEFAULT_MAX_HEADER_BYTES)
        if response.status_code == 429:
            raise ProviderCallError(
                f"provider_http_{response.status_code}",
                retryable=True,
                outcome_unknown=False,
                usage_unknown=False,
                status_code=response.status_code,
            )
        if response.status_code in {408, 409, 425} or response.status_code >= 500:
            # A POST may have reached inference before the upstream emitted an
            # error status.  Without a provider idempotency contract, retrying
            # or failing over here can duplicate a billable model call.
            raise ProviderCallError(
                f"provider_http_{response.status_code}",
                retryable=False,
                outcome_unknown=True,
                usage_unknown=True,
                status_code=response.status_code,
            )
        if response.status_code in {401, 403}:
            raise ProviderCallError(
                "provider_auth_failed",
                retryable=False,
                outcome_unknown=False,
                status_code=response.status_code,
            )
        if 300 <= response.status_code < 400:
            raise ProviderCallError(
                f"provider_http_{response.status_code}",
                retryable=False,
                outcome_unknown=False,
                status_code=response.status_code,
            )
        if response.status_code < 200 or response.status_code >= 300:
            raise ProviderCallError(
                f"provider_http_{response.status_code}",
                retryable=False,
                outcome_unknown=False,
                status_code=response.status_code,
            )

        content_length = response.headers.get("content-length")
        if content_length is not None:
            try:
                declared_size = int(content_length)
            except ValueError:
                declared_size = 0
            if declared_size > self.settings.max_response_bytes:
                raise ProviderCallError(
                    "provider_response_too_large",
                    retryable=False,
                    outcome_unknown=True,
                    usage_unknown=True,
                )
        body = await read_bounded_response(
            response,
            max_body_bytes=self.settings.max_response_bytes,
            max_header_bytes=DEFAULT_MAX_HEADER_BYTES,
            total_timeout_seconds=self.settings.timeout_seconds,
            deadline=deadline,
        )
        try:
            payload = _CompletionPayload.model_validate_json(body)
        except (ValueError, ValidationError):
            raise ProviderCallError(
                "provider_response_invalid",
                retryable=False,
                outcome_unknown=True,
                usage_unknown=True,
            ) from None
        if not payload.choices:
            raise ProviderCallError(
                "provider_response_empty",
                retryable=False,
                outcome_unknown=True,
                usage_unknown=True,
            )
        choice = payload.choices[0]
        usage = None
        if payload.usage is not None:
            usage = ModelUsage(
                input_tokens=payload.usage.prompt_tokens,
                output_tokens=payload.usage.completion_tokens,
            )
        request_id = payload.id or response.headers.get("x-request-id")
        return ProviderResponse(
            content=choice.message.content,
            usage=usage,
            finish_reason=choice.finish_reason,
            provider_request_id=request_id,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    @staticmethod
    def _request_payload(request: CompletionRequest) -> dict[str, Any]:
        return {
            "model": request.model,
            "messages": [message.model_dump() for message in request.messages],
            "max_tokens": request.max_output_tokens,
            "temperature": request.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": request.schema_name,
                    "strict": True,
                    "schema": request.output_schema,
                },
            },
        }
