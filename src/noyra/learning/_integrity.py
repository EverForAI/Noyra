from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from noyra.core.errors import IntegrityError


@contextmanager
def durable_boundary(kind: str, identifier: object) -> Iterator[None]:
    try:
        yield
    except IntegrityError:
        raise
    except (KeyError, TypeError, ValueError, OverflowError) as error:
        raise IntegrityError(f"{kind} durable state is invalid: {identifier}") from error
