from __future__ import annotations

from collections import deque

from .errors import ProviderCallError
from .types import CompletionRequest, ProviderResponse


class FakeProvider:
    name = "fake"

    def __init__(self, outcomes: list[ProviderResponse | ProviderCallError]):
        self._outcomes = deque(outcomes)
        self.requests: list[CompletionRequest] = []

    async def complete(self, request: CompletionRequest) -> ProviderResponse:
        self.requests.append(request)
        if not self._outcomes:
            raise AssertionError("fake provider has no queued outcome")
        outcome = self._outcomes.popleft()
        if isinstance(outcome, ProviderCallError):
            raise outcome
        return outcome
