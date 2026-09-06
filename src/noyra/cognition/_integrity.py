from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from noyra.core.errors import IntegrityError
from noyra.core.types import strict_bool, strict_finite_float, strict_int, strict_json_loads


@contextmanager
def durable_boundary(kind: str, identifier: object) -> Iterator[None]:
    try:
        yield
    except IntegrityError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}") from error


def durable_int(value: object, kind: str, identifier: object) -> int:
    try:
        return strict_int(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable integer is invalid: {identifier}") from error


def durable_float(value: object, kind: str, identifier: object) -> float:
    try:
        return strict_finite_float(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable number is invalid: {identifier}") from error


def durable_bool(value: object, kind: str, identifier: object) -> bool:
    try:
        return strict_bool(value)
    except (TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable boolean is invalid: {identifier}") from error


def durable_json(value: object, kind: str, identifier: object) -> Any:
    if not isinstance(value, str):
        raise IntegrityError(f"{kind} durable JSON is invalid: {identifier}")
    try:
        return strict_json_loads(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"{kind} durable JSON is invalid: {identifier}") from error


def durable_string_list(value: object, kind: str, identifier: object) -> tuple[str, ...]:
    parsed = durable_json(value, kind, identifier)
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise IntegrityError(f"{kind} durable string list is invalid: {identifier}")
    return tuple(parsed)
