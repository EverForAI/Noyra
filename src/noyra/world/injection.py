"""Conservative, deterministic signals, not a proof of instruction safety.

Topic words alone are not an instruction. Keep the existing override,
impersonation and tool gates; only replace the overly broad secret noun gate.
Do not decode arbitrary opaque blobs or invoke a model as a classifier.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape

INJECTION_CLASSIFIER_VERSION = 2
_ACTION = (
    r"(?:read|reveal|print|send|upload|exfiltrate|show|extract|return|copy|leak|provide|"
    r"retrieve|disclose|dump)"
)
_SECRET = r"(?:api\s*keys?|passwords?|private\s+keys?|secrets?|credentials?|access\s+tokens?)"
_RUNTIME = r"(?:your|internal|system|runtime|environment|process|operator)"
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I)),
    ("system_impersonation", re.compile(r"\b(system|developer)\s+(message|prompt)\b", re.I)),
    ("tool_coercion", re.compile(r"\b(call|invoke|use)\s+(the\s+)?(tool|function)\b", re.I)),
    (
        "secret_extraction",
        re.compile(
            rf"\b{_ACTION}\b.{{0,100}}\b{_RUNTIME}\b.{{0,50}}\b{_SECRET}\b"
            rf"|\b{_ACTION}\b.{{0,80}}\b{_SECRET}\b.{{0,60}}\b{_RUNTIME}\b"
            rf"|\b{_ACTION}\b\s+(?:(?:the|all|your|internal)\s+){{0,3}}{_SECRET}\b"
            rf"|\b{_ACTION}\b.{{0,100}}"
            r"(?:\.env\b|/proc/[^ ]*/environ|/root/\.ssh/|\bid_(?:rsa|ed25519)\b)"
            r"|(?:读取|导出|发送|泄露|打印|提取).{0,40}(?:你的|系统|环境|内部).{0,30}"
            r"(?:密钥|密码|凭据|令牌)",
            re.I,
        ),
    ),
    (
        "boundary_spoofing",
        re.compile(
            r"(?:BEGIN|END)_UNTRUSTED_WORLD_DATA|<\|(?:im_start|im_end|system|developer)\|>", re.I
        ),
    ),
)
_CREDENTIAL = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{30,})\b")
_OUTBOUND = re.compile(r"https?://[^\s<>\"]+", re.I)


def classify_external_text(*parts: str) -> tuple[str, ...]:
    # Callers bound input before this step. Normalization is only for detection,
    # never for observation evidence or arbitrary recursive decoding.
    text = " ".join(unicodedata.normalize("NFKC", unescape(" ".join(parts))).split())
    text = text.translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff")))
    signals = [name for name, pattern in INJECTION_PATTERNS if pattern.search(text)]
    # A concrete credential-shaped value coupled with a URL is distinct from
    # an article merely discussing passwords and linking a security notice.
    if _CREDENTIAL.search(text) and _OUTBOUND.search(text):
        signals.append("credential_coupling")
    return tuple(signals)
