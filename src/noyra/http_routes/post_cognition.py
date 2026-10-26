"""Post cognition HTTP domain; preserves existing request behavior."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from noyra.service import NoyraHTTPServer

    from .contracts import RequestHandler


def admin_model_calls_reconcile(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._reconcile_model_call()
    return True
    return False


def config_model_resources(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_model_resource()
    return True
    return False


def config_model_resources_test(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._test_unstored_model_resource()
    return True
    return False


def config_model_resources_models(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._discover_unstored_models()
    return True
    return False


def config_embedding_resources(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._configure_embedding_resource()
    return True
    return False


def config_model_resources_test_variant_2(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._test_model_resource()
    return True
    return False


def config_model_resources_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    key_parts = self.path.removeprefix("/api/config/model-resources/").split("/")
    if len(key_parts) == 4 and key_parts[1] == "keys" and key_parts[3] == "revoke":
        self._revoke_model_resource_key()
        return True
    return False


def config_model_resources_keys(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._add_model_resource_keys()
    return True
    return False


def config_model_resources_update(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._update_model_resource()
    return True
    return False


def config_model_resources_item_variant_2(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._change_model_resource_status()
    return True
    return False


def config_embedding_resources_item(self: RequestHandler, owner: NoyraHTTPServer) -> bool:
    self._change_embedding_resource_status()
    return True
    return False
