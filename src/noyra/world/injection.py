"""Conservative, deterministic signals, not a proof of instruction safety.

Topic words alone are not an instruction. Exclude only locally recognizable
descriptive role/tool mentions; ambiguous matches and direct instructions retain
their gates. Do not decode opaque blobs or invoke a model as a classifier.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape

INJECTION_CLASSIFIER_VERSION = 6
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
            r"(?:BEGIN|END)_(?:UNTRUSTED_WORLD_DATA|PRIVATE_INTRINSIC_THOUGHT_CONTEXT)"
            r"|<\|(?:im_start|im_end|system|developer)\|>",
            re.I,
        ),
    ),
)
# Feed fingerprints intentionally normalize whitespace, and HTML extraction
# may already have flattened paragraph boundaries. This additional rule needs
# an explicit runtime-owned secret (not a topic noun) rather than relying on a
# boundary that the caller can no longer supply. It never weakens existing gates.
_LOCAL_DIRECTIVE = re.compile(
    rf"\b{_DIRECTIVE_ACTION}\b\s+(?:(?:the|all|current)\s+){{0,2}}"
    rf"(?:{_RUNTIME}(?:'s)?\s+(?:own\s+)?{_SECRET}"
    r"|(?:environment|env)[\s_-]+(?:variables?|vars?))\b",
    re.I,
)
_PROCEDURAL_PREFIX = re.compile(r"\b(?:to|can|could|may|might)\s*$", re.I)


_CREDENTIAL = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16}|ghp_[A-Za-z0-9]{30,})\b")
_OUTBOUND = re.compile(r"https?://[^\s<>\"]+", re.I)

# These are per-occurrence prose contexts, never whole-document exemptions.
# A role declaration, addressed instruction or runtime target prevents exclusion.
_DESCRIPTIVE_SUBJECT = r"\b(?:the|this|that|an?|these|those)\s+(?:[a-z]+\s+){0,4}"
_ROLE_DISCUSSION_PREFIX = re.compile(
    _DESCRIPTIVE_SUBJECT
    + r"(?:is\s+about|discuss(?:es|ed|ing)?|describ(?:e|es|ed|ing)|explain(?:s|ed|ing)?|"
    r"document(?:s|ed|ing)?|analy[sz](?:e|es|ed|ing)|review(?:s|ed|ing)?|"
    r"compar(?:e|es|ed|ing)|mention(?:s|ed|ing)?|support(?:s|ed|ing)?|"
    r"accept(?:s|ed|ing)?|include(?:s|d|ing)?)\s+"
    r"(?:(?:the|a|an|original|current)\s+){0,3}$",
    re.I,
)
_TOOL_DISCUSSION_PREFIX = re.compile(
    _DESCRIPTIVE_SUBJECT + r"(?:explains?|describes?|discuss(?:es)?|documents?|shows?|covers?)\s+"
    r"(?:how|when|whether)\s+to\s*$|"
    r"\b(?:applications|clients|programs|callers|developers|users|we)\s+"
    r"(?:(?:can|may|often|typically|usually)\s+)?$",
    re.I,
)
_DIRECTED_CONTEXT = re.compile(
    r"\b(?:you|assistant|agent)\s+(?:must|should|shall|will|need\s+to|have\s+to)\b",
    re.I,
)
_RUNTIME_TARGET = re.compile(
    r"\b(?:your|our|runtime|system|operator|internal)\b.{0,40}"
    r"\b(?:goals?|instructions?|rules?|polic(?:y|ies)|permissions?|memories|memory)\b",
    re.I,
)
_ROLE_DECLARATION_SUFFIX = re.compile(
    r"\s*(?:[:=]|(?:you|the\s+assistant|this\s+agent)\s+"
    r"(?:must|should|shall|will)\b|(?:that\s+)?(?:overrides|supersedes|replaces)\b|"
    r"(?:please\s+)?(?:ignore|obey|follow|replace|override)\b)",
    re.I,
)
_TOOL_OPERATION_SUFFIX = re.compile(
    r"(?:^|[,:;]|\b(?:to|and|then|now)\s+)\s*(?:(?:please|immediately)\s+)?"
    r"(?:send|upload|exfiltrate|delete|modify|replace|override|execute|run|write|"
    r"transmit|forward)\b",
    re.I,
)
_SENTENCE_END = re.compile(r"[!?;\n]|\.(?:\s|$)")


def _has_instruction_signal(name: str, pattern: re.Pattern[str], view: str) -> bool:
    if name not in {"system_impersonation", "tool_coercion"}:
        return pattern.search(view) is not None
    discussion = (
        _ROLE_DISCUSSION_PREFIX if name == "system_impersonation" else _TOOL_DISCUSSION_PREFIX
    )
    for match in pattern.finditer(view):
        # Do not borrow descriptive metadata from another field/paragraph.
        prefix = view[max(0, match.start() - 120) : match.start()].rsplit("\n", 1)[-1]
        suffix = view[match.end() : match.end() + 120]
        if (
            "\n" in match.group()
            or (match.end() + len(suffix) < len(view) and not _SENTENCE_END.search(suffix))
            or _DIRECTED_CONTEXT.search(prefix)
            or _RUNTIME_TARGET.search(suffix)
            or (name == "system_impersonation" and _ROLE_DECLARATION_SUFFIX.match(suffix))
            or (name == "tool_coercion" and _TOOL_OPERATION_SUFFIX.search(suffix))
            or not discussion.search(prefix)
        ):
            return True
    return False


def classify_external_text(*parts: str) -> tuple[str, ...]:
    # Callers bound input before this step. Normalization is only for detection,
    # never for observation evidence or arbitrary recursive decoding.
    decoded = unicodedata.normalize("NFKC", unescape("\n".join(parts)))
    decoded = decoded.translate(dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff")))
    lines = "\n".join(" ".join(line.split()) for line in decoded.splitlines())
    text = " ".join(lines.split())
    # Check both views: retain cross-field/whitespace detection, while keeping
    # sentence/paragraph anchors meaningful when the original boundary exists.
    signals = [
        name
        for name, pattern in INJECTION_PATTERNS
        if _has_instruction_signal(name, pattern, text)
        or _has_instruction_signal(name, pattern, lines)
    ]
    if "secret_extraction" not in signals and any(
        not _PROCEDURAL_PREFIX.search(text[max(0, match.start() - 40) : match.start()])
        for match in _LOCAL_DIRECTIVE.finditer(text)
    ):
        signals.append("secret_extraction")
    # A concrete credential-shaped value coupled with a URL is distinct from
    # an article merely discussing passwords and linking a security notice.
    if _CREDENTIAL.search(text) and _OUTBOUND.search(text):
        signals.append("credential_coupling")
    return tuple(signals)
