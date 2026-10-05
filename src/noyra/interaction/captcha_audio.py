"""Bounded offline audio challenges; answers never enter text metadata."""

from __future__ import annotations

import base64
import io
import random
import secrets
import struct
import wave
from functools import lru_cache
from importlib.resources import files

SAMPLE_RATE = 16000


@lru_cache(maxsize=8)
def _digit_samples(digit: str) -> tuple[int, ...]:
    if digit not in "23456789" or len(digit) != 1:
        raise ValueError("invalid audio CAPTCHA digit")
    data = files("noyra").joinpath("web", "assets", f"captcha-zh-{digit}.wav").read_bytes()
    if len(data) > 100_000:
        raise ValueError("audio CAPTCHA asset oversized")
    with wave.open(io.BytesIO(data), "rb") as stream:
        if (stream.getnchannels(), stream.getsampwidth(), stream.getframerate()) != (
            1,
            2,
            SAMPLE_RATE,
        ):
            raise ValueError("invalid audio CAPTCHA asset")
        count = stream.getnframes()
        if not 1 <= count <= 32_000:
            raise ValueError("invalid audio CAPTCHA duration")
        raw = stream.readframes(count)
    return struct.unpack(f"<{count}h", raw)


def audio_challenge(answer: str) -> str:
    if len(answer) != 6 or any(digit not in "23456789" for digit in answer):
        raise ValueError("invalid audio CAPTCHA answer")
    noise = random.Random(secrets.randbits(128))
    samples: list[int] = [0] * 3200
    for digit in answer:
        gain = noise.uniform(0.85, 1.1)
        samples.extend(
            max(-32768, min(32767, int(value * gain) + noise.randint(-70, 70)))
            for value in _digit_samples(digit)
        )
        samples.extend([0] * noise.randint(3200, 5600))
    output = io.BytesIO()
    with wave.open(output, "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(SAMPLE_RATE)
        stream.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return "data:audio/wav;base64," + base64.b64encode(output.getvalue()).decode("ascii")
