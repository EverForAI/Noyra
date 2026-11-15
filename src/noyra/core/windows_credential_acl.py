"""Read-only Windows credential ACL checks bound to the already-open file.

No PowerShell process, pathname re-open, permission repair, or ACL cache is used.
The trusted principals match the at-rest boundary: service user, SYSTEM, admins.
"""

from __future__ import annotations

import ctypes
import os
import struct
from typing import Any


def _require_trusted_acl(
    owner: str, current: str, entries: list[tuple[int, int, int, str]]
) -> None:
    allowed = {current, "S-1-5-18", "S-1-5-32-544"}
    if owner not in allowed:
        raise OSError("credential ACL has an untrusted owner")
    for kind, flags, mask, sid in entries:
        if flags & 0x08:  # INHERIT_ONLY_ACE does not apply to this object.
            continue
        if kind == 1:  # ACCESS_DENIED_ACE cannot grant access.
            continue
        if kind != 0:  # No unparsed object/callback/conditional grants.
            raise OSError("credential ACL contains an unsupported access rule")
        if mask and sid not in allowed:
            raise OSError("credential ACL grants access to another principal")


def require_private_credential_handle(descriptor: int) -> None:
    """Reject unreadable/unsafe security descriptors before any secret bytes are read."""
    if os.name != "nt":
        raise OSError("Windows credential ACL backend is unavailable")
    import msvcrt
    from ctypes import wintypes

    api: Any = ctypes
    kernel = api.WinDLL("kernel32", use_last_error=True)
    security = api.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.POINTER(ctypes.c_void_p)

    def bind(library: Any, name: str, args: list[Any], result: Any) -> Any:
        function = getattr(library, name)
        function.argtypes = args
        function.restype = result
        return function

    get_security = bind(
        security,
        "GetSecurityInfo",
        [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
            pointer,
            pointer,
            pointer,
            pointer,
            pointer,
        ],
        wintypes.DWORD,
    )
    valid_acl = bind(security, "IsValidAcl", [ctypes.c_void_p], wintypes.BOOL)
    get_ace = bind(security, "GetAce", [ctypes.c_void_p, wintypes.DWORD, pointer], wintypes.BOOL)
    valid_sid = bind(security, "IsValidSid", [ctypes.c_void_p], wintypes.BOOL)
    sid_text = bind(security, "ConvertSidToStringSidW", [ctypes.c_void_p, pointer], wintypes.BOOL)
    release = bind(kernel, "LocalFree", [ctypes.c_void_p], ctypes.c_void_p)
    close = bind(kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL)
    process = bind(kernel, "GetCurrentProcess", [], wintypes.HANDLE)
    thread = bind(kernel, "GetCurrentThread", [], wintypes.HANDLE)
    open_thread = bind(
        security,
        "OpenThreadToken",
        [wintypes.HANDLE, wintypes.DWORD, wintypes.BOOL, pointer],
        wintypes.BOOL,
    )
    open_process = bind(
        security, "OpenProcessToken", [wintypes.HANDLE, wintypes.DWORD, pointer], wintypes.BOOL
    )
    get_token = bind(
        security,
        "GetTokenInformation",
        [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ],
        wintypes.BOOL,
    )

    def sid_value(sid: int | None) -> str:
        if not sid or not valid_sid(sid):
            raise OSError("credential ACL SID is unavailable")
        text = ctypes.c_void_p()
        try:
            if not sid_text(sid, ctypes.byref(text)) or not text.value:
                raise OSError("credential ACL SID cannot be resolved")
            return ctypes.wstring_at(text.value)
        finally:
            if text.value:
                release(text)

    token = ctypes.c_void_p()
    # ERROR_NO_TOKEN permits process identity only when no impersonation exists.
    if not open_thread(thread(), 0x0008, True, ctypes.byref(token)) and (
        api.get_last_error() != 1008 or not open_process(process(), 0x0008, ctypes.byref(token))
    ):
        raise OSError("credential effective identity is unavailable")
    try:
        size = wintypes.DWORD()
        get_token(token, 1, None, 0, ctypes.byref(size))  # TokenUser
        if not ctypes.sizeof(ctypes.c_void_p) <= size.value <= 65536:
            raise OSError("credential effective identity size is invalid")
        buffer = ctypes.create_string_buffer(size.value)
        if not get_token(token, 1, buffer, size.value, ctypes.byref(size)):
            raise OSError("credential effective identity cannot be read")
        current = sid_value(ctypes.cast(buffer, pointer).contents.value)
    finally:
        close(token)

    owner = ctypes.c_void_p()
    dacl = ctypes.c_void_p()
    security_descriptor = ctypes.c_void_p()
    try:
        # SE_FILE_OBJECT; OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION.
        result = get_security(
            msvcrt.get_osfhandle(descriptor),
            1,
            0x00000005,
            ctypes.byref(owner),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if result != 0 or not security_descriptor.value:
            raise OSError("credential security descriptor is unavailable")
        if not dacl.value or not valid_acl(dacl):
            raise OSError("credential DACL is missing or invalid")
        owner_value = sid_value(owner.value)
        _revision, _unused, acl_size, count, _unused2 = struct.unpack(
            "<BBHHH", ctypes.string_at(dacl.value, 8)
        )
        if acl_size < 8 or count > 4096:
            raise OSError("credential DACL exceeds its entry budget")
        entries: list[tuple[int, int, int, str]] = []
        for index in range(count):
            ace = ctypes.c_void_p()
            if not get_ace(dacl, index, ctypes.byref(ace)) or not ace.value:
                raise OSError("credential access rule is unavailable")
            kind, flags, length = struct.unpack("<BBH", ctypes.string_at(ace.value, 4))
            if kind == 1 or flags & 0x08:
                entries.append((kind, flags, 0, ""))
                continue
            if kind != 0 or length < 16:
                raise OSError("credential ACL contains an unsupported access rule")
            mask = struct.unpack("<I", ctypes.string_at(ace.value + 4, 4))[0]
            sid = ace.value + 8
            subauthority_count = ctypes.string_at(sid + 1, 1)[0]
            if subauthority_count > 15 or 8 + 4 * subauthority_count > length - 8:
                raise OSError("credential access rule SID is invalid")
            entries.append((kind, flags, mask, sid_value(sid)))
        _require_trusted_acl(owner_value, current, entries)
    finally:
        if security_descriptor.value:
            release(security_descriptor)
