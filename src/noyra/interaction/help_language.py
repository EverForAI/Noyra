"""Narrow coercion signals for autonomous help, not a general text classifier."""

from __future__ import annotations

import re
import unicodedata

# Recognize recipient-directed obligations, guilt and explicit threats. Topic
# mentions, first-person duties, urgency alone and otherwise-an-alternative do
# not prove coercion. Keep this deterministic; never ask a model to grade itself.
_RULES = tuple(
    re.compile(pattern)
    for pattern in (
        r"\byou\s+(?:must|have\s+to|need\s+to)\b(?!\s+(?:not|never)\b)",
        r"\byou\s+must\s+not\s+(?:refuse|decline|ignore)\b",
        r"(?:^|[.!?;])\s*must\s+(?:help|pay|send|reply|respond|comply)\b",
        r"\byou\s+owe\s+me\b",
        r"\bif\s+you\s+(?:really\s+)?cared\b(?:\s*[,;]|.{0,60}\byou(?:\s+would|'d)\b)",
        r"\b(?:otherwise|or\s+else|refuse|decline)\b.{0,60}"
        r"\b(?:i|we)(?:\s+(?:will|shall)|'ll)\s+(?!not\b|never\b)"
        r"(?:punish|hurt|harm|expose|destroy|retaliate)\b",
        r"\b(?:otherwise|or\s+else)\b.{0,60}\byou(?:\s+will|'ll)\s+regret\b",
        r"你(?:必须|务必|一定得|欠我)",
        r"你(?:不准|不得|不能)(?:拒绝|不帮|无视)",
        r"(?:否则|不然|拒绝).{0,40}(?:惩罚你|伤害你|报复你|曝光你|让你后悔)",
    )
)


def help_request_is_coercive(content: str) -> bool:
    text = unicodedata.normalize("NFKC", content).casefold().replace("\u2019", "'")
    normalized = " ".join(text.split())
    return any(rule.search(normalized) is not None for rule in _RULES)
