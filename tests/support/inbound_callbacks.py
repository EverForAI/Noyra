"""Synthetic WeChat encrypted callback construction; no live platform access."""

from __future__ import annotations

import base64
import hashlib
import os
import time

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def encrypted_wechat_callback(
    message: bytes,
    *,
    key: bytes,
    token: str,
    account_id: str,
    timestamp: str | None = None,
    nonce: str = "synthetic-wechat-nonce",
) -> tuple[bytes, dict[str, str]]:
    timestamp = timestamp or str(int(time.time()))
    plaintext = os.urandom(16) + len(message).to_bytes(4, "big") + message + account_id.encode()
    padding = 32 - len(plaintext) % 32
    encryptor = Cipher(algorithms.AES(key), modes.CBC(key[:16])).encryptor()
    encrypted = encryptor.update(plaintext + bytes([padding]) * padding) + encryptor.finalize()
    text = base64.b64encode(encrypted).decode("ascii")
    signature = hashlib.sha1("".join(sorted((token, timestamp, nonce, text))).encode()).hexdigest()
    return (
        f"<xml><Encrypt><![CDATA[{text}]]></Encrypt></xml>".encode(),
        {
            "X-Wechat-MsgSignature": signature,
            "X-Wechat-Timestamp": timestamp,
            "X-Wechat-Nonce": nonce,
        },
    )
