"""Opaque, deterministic sitemap routes; never an authorization to publish content."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from noyra.core.at_rest import validate_private_file, validate_private_root
from noyra.core.credentials import read_secret_file
from noyra.core.locking import ProcessLock
from noyra.core.types import canonical_json

_AAD = b"noyra-public-sitemap-v2"
_DEVELOPMENT_KEY = secrets.token_bytes(32)
_CREATE_LOCK = threading.Lock()


class SitemapTokens:
    def __init__(self, keys: tuple[bytes, ...] | None = None):
        material = keys if keys is not None else (_DEVELOPMENT_KEY,)
        if not 1 <= len(material) <= 4 or any(len(key) != 32 for key in material):
            raise ValueError("invalid sitemap keyring")
        if len(set(material)) != len(material):
            raise ValueError("duplicate sitemap keys")
        self.persistent = keys is not None
        self._keys: dict[bytes, AESSIV] = {}
        for key in material:
            derived = HKDF(algorithm=hashes.SHA256(), length=64, salt=None, info=_AAD).derive(key)
            identifier = hashlib.sha256(derived).digest()[:8]
            if identifier in self._keys:
                raise ValueError("ambiguous sitemap keys")
            self._keys[identifier] = AESSIV(derived)
        self._active = next(iter(self._keys))

    @classmethod
    def from_file(cls, path: Path) -> SitemapTokens:
        data = json.loads(read_secret_file(path, label="sitemap keyring", max_bytes=4096))
        if (
            not isinstance(data, dict)
            or set(data) != {"format", "keys"}
            or data["format"] != "noyra-sitemap-keyring/v1"
            or not isinstance(data["keys"], list)
            or not 1 <= len(data["keys"]) <= 4
            or any(not isinstance(key, str) or len(key) != 64 for key in data["keys"])
        ):
            raise ValueError("invalid sitemap keyring")
        return cls(tuple(bytes.fromhex(key) for key in data["keys"]))

    @classmethod
    def managed(cls, directory: Path) -> SitemapTokens:
        """Initialize only after runtime ownership, before serving any HTTP request."""
        root = validate_private_root(directory, label="sitemap secret root")
        with _CREATE_LOCK:
            path = validate_private_file(root, "public-sitemap-keyring.json")
            if path.exists():
                return cls.from_file(path)
            lock_path = validate_private_file(root, ".public-sitemap-keyring.lock")
            lock = ProcessLock(lock_path)
            lock.acquire()
            try:
                os.chmod(lock_path, 0o600)
                path = validate_private_file(root, "public-sitemap-keyring.json")
                if path.exists():
                    return cls.from_file(path)
                temporary = validate_private_file(root, f".sitemap-{secrets.token_hex(16)}.tmp")
                descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(
                            canonical_json(
                                {
                                    "format": "noyra-sitemap-keyring/v1",
                                    "keys": [secrets.token_hex(32)],
                                }
                            ).encode()
                        )
                        stream.flush()
                        os.fsync(stream.fileno())
                    temporary.replace(path)
                    if os.name == "posix":
                        folder = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
                        try:
                            os.fsync(folder)
                        finally:
                            os.close(folder)
                finally:
                    temporary.unlink(missing_ok=True)
                return cls.from_file(path)
            finally:
                lock.release()

    def seal(self, data: object) -> str:
        header = b"\x02" + self._active
        ciphertext = self._keys[self._active].encrypt(canonical_json(data).encode(), [_AAD, header])
        return base64.urlsafe_b64encode(header + ciphertext).decode()

    def open(self, token: str) -> object:
        if not isinstance(token, str) or len(token) > 1500:
            raise ValueError("invalid sitemap partition")
        raw = base64.b64decode(token, altchars=b"-_", validate=True)
        if len(raw) < 25 or raw[0] != 2:
            raise ValueError("invalid sitemap partition")
        header = raw[:9]
        cipher = self._keys.get(header[1:])
        if cipher is None:
            raise ValueError("unknown sitemap key")
        return json.loads(cipher.decrypt(raw[9:], [_AAD, header]))
