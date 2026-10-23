"""Safe server-rendered reading of already verified public archive items."""

from __future__ import annotations

import html
import re
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from .public_display import PUBLIC_ACTIONS, PUBLIC_PROVENANCE, public_label

PUBLIC_VIEWS = {
    "diary": "diary",
    "behavior": "behavior",
    "interaction": "interactions",
    "post": "posts",
    "lifecycle": "lifecycle",
    "goal": "goals",
    "project": "projects",
    "prediction": "predictions",
    "research": "research",
    "sleep": "sleep",
    "update": "updates",
}


def public_item_path(kind: str, identifier: str) -> str:
    return f"/public/{kind}/{quote(identifier, safe='')}"


def _safe_source(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return bool(
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


def structured_public_text(text: str, *, heading_base: int = 1) -> str:
    if heading_base not in {1, 2}:
        raise ValueError("invalid reading heading context")
    blocks: list[str] = []
    paragraphs: list[str] = []
    headings: list[tuple[str, str]] = []

    def flush() -> None:
        if paragraphs:
            blocks.append("<p>" + html.escape("\n".join(paragraphs)) + "</p>")
            paragraphs.clear()

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        heading = re.fullmatch(r"(#{1,3})\s+(.+)", line)
        if heading:
            flush()
            identifier = f"reading-section-{len(headings) + 1}"
            title = heading[2]
            headings.append((identifier, title))
            level = len(heading[1]) + heading_base
            blocks.append(f'<h{level} id="{identifier}">{html.escape(title)}</h{level}>')
        elif not line.strip():
            flush()
        else:
            paragraphs.append(line)
    flush()
    toc = ""
    if len(headings) >= 2:
        links = "".join(
            f'<li><a href="#{key}">{html.escape(title)}</a></li>' for key, title in headings[:100]
        )
        toc = (
            '<details class="reading-toc"><summary>目录 / Contents</summary>'
            f"<ol>{links}</ol>"
            + ("<p>目录仅列出前100节 / First 100 sections shown</p>" if len(headings) > 100 else "")
            + "</details>"
        )
    return toc + "".join(blocks)


def public_article_html(
    item: dict[str, Any],
    site_url: str,
    *,
    english: bool = False,
    theme: str = "system",
    return_view: str | None = None,
) -> bytes:
    escape = html.escape
    title = item["title"] or ("Public record" if english else "公开记录")
    if item["kind"] == "lifecycle":
        title = " → ".join(
            public_label(item["details"].get(key, ""), english=english) for key in ("from", "to")
        )
    elif item["kind"] == "behavior":
        title = PUBLIC_ACTIONS.get(item["title"], [title, title])[int(english)]
    elif item["kind"] == "interaction":
        title = public_label("interactions", english=english)
    description = (item["summary"] or title)[:220]
    path = public_item_path(item["kind"], item["id"])
    canonical = site_url + path if site_url else ""
    metadata = [
        f'<meta name="description" content="{escape(description, quote=True)}">',
        '<meta property="og:type" content="article">',
        f'<meta property="og:title" content="{escape(title, quote=True)}">',
        f'<meta property="og:description" content="{escape(description, quote=True)}">',
    ]
    if canonical:
        metadata.extend(
            [
                f'<link rel="canonical" href="{escape(canonical, quote=True)}">',
                f'<meta property="og:url" content="{escape(canonical, quote=True)}">',
                '<meta property="og:image" '
                f'content="{escape(site_url, quote=True)}/assets/public-social.png">',
            ]
        )
    public_fields = {
        "status": ("公开状态", "Status"),
        "next_step": ("下一步", "Next step"),
        "progress": ("公开进度", "Progress"),
        "probability": ("公开概率(非保证)", "Probability (not a guarantee)"),
        "target_at": ("目标时间", "Target time"),
        "outcome": ("公开结果", "Outcome"),
        "uncertainty": ("限制与不确定性", "Limitations"),
        "resource_state": ("公开资源状态", "Resources"),
        "started_at": ("开始时间", "Started"),
        "completed_at": ("结束时间", "Completed"),
        "reason_category": ("公开原因类别", "Public reason"),
        "from": ("前一阶段", "Previous stage"),
        "to": ("当前阶段", "Stage"),
        "tool": ("工具", "Tool"),
        "public_target": ("公开目标", "Public target"),
        "side_effect_summary": ("公开影响", "Public effects"),
        "resource_summary": ("公开资源说明", "Public resources"),
        "redaction_reason": ("省略说明", "Redaction note"),
        "author_label": ("署名(不等于身份验证)", "Name (unverified)"),
        "interaction_kind": ("交流类型", "Interaction type"),
        "post_kind": ("帖子类型", "Post type"),
    }
    fields = []
    details = dict(item.get("details", {}))
    if "status" not in details and item.get("status"):
        details["status"] = item["status"]
    for key, value in details.items():
        if key in public_fields:
            shown = f"{value * 100:g}%" if key in {"progress", "probability"} else str(value)
            if key in {"status", "from", "to", "outcome", "resource_state", "reason_category"}:
                shown = public_label(shown, english=english)
            fields.append(
                f'<div class="detail-field"><dt>{public_fields[key][int(english)]}</dt>'
                f"<dd>{escape(shown)}</dd></div>"
            )
        elif key == "phases":
            fields.append(
                '<div class="detail-field wide"><dt>阶段 / Phases</dt><dd><ol>'
                + "".join(
                    f"<li>{escape(entry['title'])} · "
                    f"{escape(public_label(entry['status'], english=english))}</li>"
                    for entry in value[:10]
                )
                + "</ol></dd></div>"
            )
        elif key == "sources":
            # Defensive rendering also preserves the publication HTTPS-only boundary.
            fields.append(
                '<div class="detail-field wide"><dt>参考资料 / References</dt><dd><ul>'
                + "".join(
                    f'<li><a href="{escape(entry["url"], quote=True)}" rel="noopener noreferrer" '
                    f'referrerpolicy="no-referrer">{escape(entry["title"])}</a></li>'
                    for entry in value[:10]
                    if _safe_source(entry["url"])
                )
                + "</ul></dd></div>"
            )
    provenance = PUBLIC_PROVENANCE.get(item["provenance"], ["公开记录", "Public record"])[
        int(english)
    ]
    metadata_html = "".join(metadata)
    view = PUBLIC_VIEWS[item["kind"]]
    if theme not in {"system", "light", "dark"} or (
        return_view is not None
        and return_view not in {*PUBLIC_VIEWS.values(), "timeline", "overview"}
    ):
        raise ValueError("invalid public reading context")
    view = return_view or view
    lang = "en" if english else "zh-CN"
    back = "← Back to archive" if english else "← 返回公开档案"
    updated = "Updated" if english else "最近更新"
    switch_lang = "中文" if english else "English"
    context = {"theme": theme, "return": view}
    switch_query = "?" + urlencode({"lang": "zh" if english else "en", **context})
    back_query = urlencode({"lang": "en" if english else "zh", "theme": theme})
    theme_attribute = "" if theme == "system" else f' data-theme="{theme}"'
    body = (
        f'<!doctype html><html lang="{lang}"{theme_attribute}><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f"<title>{escape(title)} · Noyra</title>{metadata_html}"
        '<meta name="twitter:card" content="summary_large_image">'
        '<link rel="stylesheet" href="/styles.css"></head><body>'
        '<a class="skip-link" href="#reading-main">Skip / 跳到正文</a>'
        '<main id="reading-main" class="public-reading-shell" tabindex="-1">'
        '<article class="article-reader">'
        f'<nav><a href="/?{escape(back_query, quote=True)}#{view}">{back}</a>'
        f'<a href="{escape(path, quote=True)}{switch_query}">{switch_lang}</a></nav>'
        f'<h1>{escape(title)}</h1><div class="record-meta">'
        f'<time datetime="{escape(item["occurred_at"], quote=True)}">'
        f"{escape(item['occurred_at'])}</time><span>{provenance}</span></div>"
        f'<p class="record-summary">{escape(item["summary"])}</p>'
        f'<div class="article-body">{structured_public_text(item["body"])}</div>'
        f'<dl class="record-details">{"".join(fields)}</dl>'
        f'<p class="module-status">{updated}: {escape(item["updated_at"])}</p>'
        "</article></main></body></html>"
    )
    return body.encode("utf-8")
