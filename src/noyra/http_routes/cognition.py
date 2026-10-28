"""Cognition HTTP domain; preserves existing request behavior."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from typing import TYPE_CHECKING

from pydantic import SecretStr

from noyra.core.admission import OperationInvalidated, bind_lease
from noyra.core.errors import IntegrityError, NotFoundError
from noyra.model import (
    CognitiveResourceGroupInput,
    CognitiveResourceGroupUpdate,
    EmbeddingResourceInput,
    OpenAICompatibleProvider,
)
from noyra.model.capabilities import CapabilityProfileStore
from noyra.model.errors import (
    BudgetExhaustedError,
    ModelAuthorizationError,
    ModelCallStateError,
    ModelLifecycleBlockedError,
    ModelResourceInactiveError,
    ProviderCallError,
    StructuredOutputError,
)
from noyra.model.reasoning import (
    REASONING_EFFORTS,
    ReasoningEffort,
    reasoning_profile,
    reasoning_profile_payload,
)

from .validation import _cognitive_resource_payload

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def configure_model_resource(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    try:
        proposal = CognitiveResourceGroupInput.model_validate(payload)
        if proposal.reasoning_effort is not None:
            capability = CapabilityProfileStore(owner.kernel.database).get(
                owner.kernel.subject_id,
                proposal.provider_type,
                proposal.base_url,
                proposal.model,
            )
            if (
                capability is None
                or capability.status != "known"
                or capability.parameter != "reasoning_effort"
                or proposal.reasoning_effort not in capability.tested_efforts
            ):
                self._json(
                    HTTPStatus.CONFLICT,
                    {"error": "model_capability_probe_required"},
                )
                return
        record = owner.cognitive_resources.configure(
            owner.kernel.subject_id,
            proposal,
            actor=self._actor(),
        )
    except ValueError as error:
        if str(error) == "cognitive resource label already exists in this pool":
            self._json(
                HTTPStatus.CONFLICT,
                {"error": "model_resource_label_exists"},
            )
            return
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "group_id": record.group_id,
            "pool": record.pool,
            "label": record.label,
            "status": record.status,
        },
    )


def draft_model_credentials(
    self: RequestHandler, owner: NoyraHTTPServer, payload: object, *, require_model: bool
) -> tuple[str, str, SecretStr, ReasoningEffort | None]:
    if not isinstance(payload, dict):
        raise ValueError("model draft must be an object")
    allowed = {"base_url", "model", "api_keys", "api_key", "reasoning_effort"}
    if set(payload) - allowed:
        raise ValueError("unknown model draft field")
    base_url = payload.get("base_url")
    model = payload.get("model", "model-discovery-placeholder")
    keys = payload.get("api_keys")
    if keys is None and isinstance(payload.get("api_key"), str):
        keys = [payload["api_key"]]
    if not isinstance(base_url, str) or not base_url.strip():
        raise ValueError("model draft base URL is required")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model draft model is required")
    if require_model and model == "model-discovery-placeholder":
        raise ValueError("model draft model is required")
    if (
        not isinstance(keys, list)
        or not keys
        or len(keys) > 128
        or any(not isinstance(key, str) or not key.strip() or len(key) > 4096 for key in keys)
    ):
        raise ValueError("model draft API key is invalid")
    selected = payload.get("reasoning_effort")
    if selected is not None and not isinstance(selected, str):
        raise ValueError("model draft reasoning effort is invalid")
    if selected is not None and selected not in REASONING_EFFORTS:
        raise ValueError("model draft reasoning effort is invalid")
    profile = reasoning_profile(model)
    reasoning_effort = selected if selected in profile.candidate_efforts else None
    return base_url, model, SecretStr(keys[0]), reasoning_effort


def test_unstored_model_resource(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        base_url, model, api_key, reasoning_effort = self._draft_model_credentials(
            payload, require_model=True
        )
        result = asyncio.run(
            OpenAICompatibleProvider.probe_unstored(
                base_url=base_url,
                model=model,
                api_key=api_key,
                reasoning_effort=reasoning_effort,
            )
        )
    except (ValueError, TypeError):
        owner.audit_admin_event(
            "model_resource_draft_test_rejected",
            self._actor(),
            {"error": "invalid_model_resource_test"},
        )
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
        return
    except ProviderCallError as error:
        owner.audit_admin_event(
            "model_resource_draft_test_failed",
            self._actor(),
            {"error": error.code},
        )
        self._json(HTTPStatus.BAD_GATEWAY, {"error": error.code})
        return
    owner.audit_admin_event(
        "model_resource_draft_test_succeeded",
        self._actor(),
        {"model": result.get("model")},
    )
    stored = (
        CapabilityProfileStore(owner.kernel.database).record_probe_success(
            owner.kernel.subject_id,
            "openai_compatible",
            base_url,
            model,
            reasoning_effort,
        )
        if reasoning_effort is not None
        else None
    )
    result["reasoning_profile"] = (
        stored.public(selected=reasoning_effort)
        if stored is not None
        else reasoning_profile_payload(
            model,
            "openai_compatible",
            selected=reasoning_effort,
        )
    )
    self._json(HTTPStatus.OK, result)


def discover_unstored_models(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        base_url, _, api_key, _ = self._draft_model_credentials(payload, require_model=False)
        models = asyncio.run(
            OpenAICompatibleProvider.list_models_unstored(base_url=base_url, api_key=api_key)
        )
    except (ValueError, TypeError):
        owner.audit_admin_event(
            "model_resource_discovery_rejected",
            self._actor(),
            {"error": "invalid_model_discovery"},
        )
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_discovery"})
        return
    except ProviderCallError as error:
        status = (
            HTTPStatus.NOT_IMPLEMENTED
            if error.code == "model_discovery_unsupported"
            else HTTPStatus.BAD_GATEWAY
        )
        owner.audit_admin_event(
            "model_resource_discovery_failed",
            self._actor(),
            {"error": error.code},
        )
        self._json(status, {"error": error.code})
        return
    owner.audit_admin_event(
        "model_resource_discovery_succeeded",
        self._actor(),
        {"model_count": len(models)},
    )
    self._json(HTTPStatus.OK, {"models": models})


def configure_embedding_resource(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    try:
        record = owner.embedding_resources.configure(
            owner.kernel.subject_id,
            EmbeddingResourceInput.model_validate(payload),
            actor=self._actor(),
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_embedding_resource"})
        return
    self._json(
        HTTPStatus.CREATED,
        {
            "config_id": record.config_id,
            "label": record.label,
            "model": record.model,
            "status": record.status,
            "runtime": owner.embedding_runtime_status(),
        },
    )


def test_model_resource(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
        return
    reason = payload.get("reason")
    group_id = self.path.removeprefix("/api/config/model-resources/").removesuffix("/test")
    if (
        set(payload) != {"reason"}
        or not group_id
        or "/" in group_id
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
        return
    gateway = owner.cognition_gateway
    if gateway is None:
        self._json(HTTPStatus.SERVICE_UNAVAILABLE, {"error": "cognition_unavailable"})
        return
    try:
        lease = owner.kernel.admission.begin("operator_model_test")
    except OperationInvalidated:
        self._json(HTTPStatus.CONFLICT, {"error": "runtime_unavailable"})
        return
    try:
        with bind_lease(lease):
            result = asyncio.run(
                gateway.test_resource(group_id, subject_id=owner.kernel.subject_id)
            )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return
    except BudgetExhaustedError as error:
        self._json(
            HTTPStatus.TOO_MANY_REQUESTS,
            {
                "error": "model_cost_only_pricing_unknown"
                if str(error) == "cost_only_pricing_unknown"
                else "model_test_budget_exhausted"
            },
        )
        return
    except ModelLifecycleBlockedError as error:
        self._json(
            HTTPStatus.CONFLICT,
            {"error": "model_test_lifecycle_blocked", "lifecycle": error.lifecycle},
        )
        return
    except ProviderCallError as error:
        self._json(HTTPStatus.BAD_GATEWAY, {"error": error.code})
        return
    except StructuredOutputError:
        self._json(HTTPStatus.BAD_GATEWAY, {"error": "model_test_output_invalid"})
        return
    except (ModelAuthorizationError, PermissionError):
        self._json(HTTPStatus.FORBIDDEN, {"error": "model_test_not_authorized"})
        return
    except ModelResourceInactiveError:
        self._json(HTTPStatus.CONFLICT, {"error": "model_test_resource_inactive"})
        return
    except ModelCallStateError:
        self._json(HTTPStatus.CONFLICT, {"error": "model_test_state_conflict"})
        return
    except ValueError:
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_test"})
        return
    finally:
        owner.kernel.admission.finish(lease)
    self._json(HTTPStatus.OK, result)


def change_embedding_resource_status(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    reason = payload.get("reason")
    suffix = self.path.removeprefix("/api/config/embedding-resources/")
    config_id, operation = suffix.rsplit("/", 1) if "/" in suffix else ("", "")
    if (
        not isinstance(reason, str)
        or not config_id
        or operation
        not in {
            "enable",
            "disable",
            "revoke",
        }
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_embedding_resource"})
        return
    try:
        method = {
            "enable": owner.embedding_resources.enable,
            "disable": owner.embedding_resources.disable,
            "revoke": owner.embedding_resources.revoke,
        }[operation]
        record = method(
            config_id,
            reason=reason,
            actor=self._actor(),
            subject_id=owner.kernel.subject_id,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "embedding_resource_not_found"})
        return
    except (ValueError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_embedding_resource"})
        return
    self._json(
        HTTPStatus.OK,
        {**record.__dict__, "runtime": owner.embedding_runtime_status()},
    )


def change_model_resource_status(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    parts = self.path.removeprefix("/api/config/model-resources/").split("/")
    reason = payload.get("reason")
    if (
        len(parts) != 2
        or not parts[0]
        or not parts[1]
        or set(payload) != {"reason"}
        or not isinstance(reason, str)
        or not reason.strip()
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    group_id, operation = parts
    try:
        if operation == "enable":
            record = owner.cognitive_resources.enable(
                group_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            )
        elif operation == "disable":
            record = owner.cognitive_resources.disable(
                group_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            )
        elif operation == "revoke":
            record = owner.cognitive_resources.revoke(
                group_id,
                reason=reason,
                actor=self._actor(),
                subject_id=owner.kernel.subject_id,
            )
        else:
            raise ValueError("unsupported model resource operation")
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return
    self._json(
        HTTPStatus.OK,
        {"group_id": record.group_id, "status": record.status},
    )


def update_model_resource(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    # `_request_json` currently returns a mapping, but keep this
    # guard at the handler boundary so a future decoder change
    # cannot turn malformed input into an AttributeError/500.
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_update"})
        return
    prefix = "/api/config/model-resources/"
    suffix = self.path.removeprefix(prefix)
    if not suffix.endswith("/update"):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_update"})
        return
    group_id = suffix.removesuffix("/update")
    reason = payload.get("reason")
    update_payload = {key: value for key, value in payload.items() if key != "reason"}
    if (
        not group_id
        or "/" in group_id
        or not isinstance(reason, str)
        or not reason.strip()
        or set(update_payload)
        - {
            "priority",
            "weight",
            "daily_attempts",
            "daily_input_tokens",
            "daily_output_tokens",
            "daily_cost_limit_usd",
            "input_usd_per_million",
            "output_usd_per_million",
            "max_attempts",
            "limit_enabled",
            "expected_updated_at",
            "expected_limit_revision",
            "pricing_status",
        }
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_update"})
        return
    try:
        proposal = CognitiveResourceGroupUpdate.model_validate(update_payload)
        record = owner.cognitive_resources.update(
            group_id,
            proposal,
            reason=reason,
            actor=self._actor(),
            subject_id=owner.kernel.subject_id,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(
            HTTPStatus.BAD_REQUEST,
            {"error": "invalid_model_resource_update"},
        )
        return
    self._json(
        HTTPStatus.OK,
        _cognitive_resource_payload(record, owner.kernel.database),
    )


def add_model_resource_keys(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    group_id = self.path.removeprefix("/api/config/model-resources/").removesuffix("/keys")
    raw_keys = payload.get("api_keys")
    try:
        if set(payload) != {"api_keys"}:
            raise TypeError("unexpected model resource key fields")
        if not isinstance(raw_keys, list) or not all(isinstance(value, str) for value in raw_keys):
            raise TypeError("api_keys must be a list of strings")
        record = owner.cognitive_resources.add_keys(
            group_id,
            tuple(SecretStr(value) for value in raw_keys),
            actor=self._actor(),
            subject_id=owner.kernel.subject_id,
        )
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource"})
        return
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return
    self._json(
        HTTPStatus.OK,
        {"group_id": record.group_id, "key_count": record.key_count},
    )


def revoke_model_resource_key(self: RequestHandler, owner: NoyraHTTPServer) -> None:
    if not self._authorized():
        self._discard_small_request_body()
        self._json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
        return
    if self.headers.get_content_type() != "application/json":
        self._discard_small_request_body()
        self._json(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, {"error": "json_required"})
        return
    payload = self._request_json()
    if payload is None:
        return
    if not isinstance(payload, dict):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_key"})
        return
    parts = self.path.removeprefix("/api/config/model-resources/").split("/")
    reason = payload.get("reason")
    if (
        len(parts) != 4
        or parts[1] != "keys"
        or parts[3] != "revoke"
        or not parts[0]
        or not parts[2]
        or not isinstance(reason, str)
        or not reason.strip()
        or set(payload) != {"reason"}
    ):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_key"})
        return
    try:
        key = next(
            (
                item
                for item in owner.cognitive_resources.keys(
                    parts[0], subject_id=owner.kernel.subject_id
                )
                if item.key_id == parts[2]
            ),
            None,
        )
        if key is None:
            raise NotFoundError("model resource key not found")
        record = owner.cognitive_resources.revoke_key(
            key.key_id,
            reason=reason,
            actor=self._actor(),
            subject_id=owner.kernel.subject_id,
        )
    except NotFoundError:
        self._json(HTTPStatus.NOT_FOUND, {"error": "model_resource_key_not_found"})
        return
    except IntegrityError:
        self._json(
            HTTPStatus.SERVICE_UNAVAILABLE,
            {"error": "model_resource_integrity_unavailable"},
            retry_after=60,
        )
        return
    except (ValueError, TypeError, PermissionError):
        self._json(HTTPStatus.BAD_REQUEST, {"error": "invalid_model_resource_key"})
        return
    self._json(
        HTTPStatus.OK,
        {"key_id": record.key_id, "group_id": record.group_id, "status": record.status},
    )
