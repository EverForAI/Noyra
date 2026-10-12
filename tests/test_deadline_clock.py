from __future__ import annotations

from contextlib import closing
from contextvars import copy_context
from types import SimpleNamespace

import httpx
import pytest

from noyra.core import deadline as clock
from noyra.core.http import read_bounded_sync_response


def test_nested_deadline_uses_high_resolution_and_restores_outer_context(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        clock, "time", SimpleNamespace(perf_counter=lambda: now[0], monotonic=lambda: 0.0)
    )
    with clock.operation_deadline(0.05):
        now[0] += 0.03
        with clock.operation_deadline(1):
            assert clock.remaining_seconds(1) == pytest.approx(0.02)
            assert copy_context().run(clock.remaining_seconds, 1) == pytest.approx(0.02)
        now[0] += 0.03
        with pytest.raises(TimeoutError):
            clock.remaining_seconds(1)
    assert clock.remaining_seconds(1) == 1


def test_response_reader_rejects_elapsed_high_resolution_time_even_if_low_res_clock_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = [100.0]
    monkeypatch.setattr(
        clock, "time", SimpleNamespace(perf_counter=lambda: now[0], monotonic=lambda: 0.0)
    )

    class Stream(httpx.SyncByteStream):
        def __iter__(self):  # type: ignore[no-untyped-def]
            for chunk in (b"first", b"second"):
                now[0] += 0.03
                yield chunk

    with (
        closing(
            httpx.Response(
                200, stream=Stream(), request=httpx.Request("GET", "https://synthetic.test")
            )
        ) as response,
        pytest.raises(httpx.ReadTimeout),
    ):
        read_bounded_sync_response(response, total_timeout_seconds=0.05, deadline=100.05)
