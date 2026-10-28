"""Conservative, deterministic signals, not a proof of instruction safety.

Topic words alone are not an instruction. Keep the existing override,
impersonation and tool gates; only replace the overly broad secret noun gate.
Do not decode arbitrary opaque blobs or invoke a model as a classifier.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape

INJECTION_CLASSIFIER_VERSION = 3
_ACTION = (
    r"(?:read|reveal|print|send|upload|exfiltrate|show|extract|return|copy|leak|provide|"
    r"retrieve|disclose|dump)"
)
# These verbs are kept separate from the original broad action set.  They are
# only treated as extraction directives at an imperative/sentence boundary so
# that ordinary prose such as "a guide explains how to get credentials" does
# not become a secret-extraction signal.
_DIRECTIVE_ACTION = (
    r"(?:acquire|cat|echo|fetch|get|include|list|obtain|paste|post|share|submit|"
    r"transmit|forward|write)"
)
_SCOPE = (
    r"(?:the|all|your|my|our|this|that|user(?:'s)?|internal|system(?:'s)?|"
    r"runtime|environment|process|operator)"
)
_SECRET = (
    r"(?:api[\s_-]*keys?|passwords?|passcodes?|passphrases?|private[\s_-]+keys?|"
    r"secret[\s_-]+keys?|secrets?|credentials?|access[\s_-]+tokens?|"
    r"auth(?:entication)?[\s_-]+tokens?|bearer[\s_-]+tokens?|"
    r"session[\s_-]+(?:tokens?|cookies?)|oauth[\s_-]+tokens?|tokens?|"
    r"signing[\s_-]+keys?|(?:environment|env)[\s_-]+(?:variables?|vars?))"
)
_RUNTIME = r"(?:your|my|our|internal|system|runtime|environment|process|operator)"
_ZH_ACTION = (
    r"(?:读取|导出|发送|泄露|打印|提取|获取|查看|显示|展示|输出|列出|上传|转发|复制|"
    r"提供|放入|放进|写入|贴出|包含)"
)
_ZH_SCOPE = r"(?:你的|我的|系统|环境|内部|运行时|操作员|本机|当前)"
_ZH_SECRET = r"(?:api\s*密钥|私钥|密钥|密码|口令|凭据|令牌|访问令牌|会话令牌|环境变量)"
INJECTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("instruction_override", re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.I)),
    ("system_impersonation", re.compile(r"\b(system|developer)\s+(message|prompt)\b", re.I)),
    ("tool_coercion", re.compile(r"\b(call|invoke|use)\s+(the\s+)?(tool|function)\b", re.I)),
    (
        "secret_extraction",
        re.compile(
            rf"\b{_ACTION}\b.{{0,100}}\b{_RUNTIME}\b.{{0,50}}\b{_SECRET}\b"
            rf"|\b{_ACTION}\b.{{0,80}}\b{_SECRET}\b.{{0,60}}\b{_RUNTIME}\b"
            rf"|\b{_ACTION}\b\s+(?:(?:{_SCOPE})\s+){{0,3}}{_SECRET}\b"
            rf"|(?:^|[.!?;:\n])\s*(?:please\s+|now\s+|then\s+|you\s+should\s+)?"
            rf"{_DIRECTIVE_ACTION}\b\s+(?:(?:{_SCOPE})\s+){{0,3}}{_SECRET}\b"
            rf"|\b{_ACTION}\b.{{0,100}}"
            r"(?:\.env\b|/proc/[^ ]*/environ|/root/\.ssh/|\bid_(?:rsa|ed25519)\b)"
            rf"|{_ZH_ACTION}.{{0,30}}(?:{_ZH_SCOPE}.{{0,20}})?{_ZH_SECRET}"
            rf"|(?:请(?:将|把)?|将|把)(?:{_ZH_SCOPE})?{_ZH_SECRET}.{{0,30}}{_ZH_ACTION}"
            r"|(?:读取|导出|发送|泄露|打印|提取|获取|查看|显示|展示|输出|列出|上传|转发|复制|提供|放入|放进|写入|贴出|包含).{0,40}"
            r"(?:你的|我的|系统|环境|内部|运行时|操作员).{0,30}"
            r"(?:私钥|密钥|密码|凭据|令牌)",
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
