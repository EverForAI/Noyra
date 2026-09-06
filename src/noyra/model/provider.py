from __future__ import annotations

from typing import Protocol

from .types import CompletionRequest, ProviderResponse


class ModelProvider(Protocol):
    name: str

    async def complete(self, request: CompletionRequest) -> ProviderResponse: ...
