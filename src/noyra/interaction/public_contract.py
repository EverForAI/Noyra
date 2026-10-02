"""Versioned limits and response metadata for anonymous public projections."""

from __future__ import annotations

PUBLIC_CONTRACT_ID = "public-contract-v1"
PUBLIC_DEFAULT_LIMIT = 100
PUBLIC_MAX_ROWS = 1_000
PUBLIC_MAX_RESPONSE_BYTES = 2_000_000
PUBLIC_CACHE_CONTROL = "no-store"

PUBLIC_ENDPOINTS = (
    "/api/state",
    "/api/diary",
    "/api/behavior",
    "/api/interactions",
    "/api/public-posts",
)
