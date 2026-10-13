from __future__ import annotations

from noyra.interaction.public_reading import public_article_html, structured_public_text


def test_ssr_public_labels_match_spa_approved_vocabulary() -> None:
    import json
    import re
    from pathlib import Path

    from noyra.interaction.public_display import PUBLIC_ACTIONS, PUBLIC_LABELS

    source = Path("src/noyra/web/app.js").read_text(encoding="utf-8")
    for vocabulary, start, end, indent in (
        (PUBLIC_LABELS, "const labels =", "const kinds =", 4),
        (PUBLIC_ACTIONS, "function actionLabel", "function safePublicLink", 8),
    ):
        block = source[source.index(start) : source.index(end)]
        actual = {
            key: json.loads(values)
            for key, values in re.findall(
                r"^" + " " * indent + r'(\w+): (\["[^\n]+\]),$', block, re.M
            )
        }
        assert actual == vocabulary


def test_ssr_provenance_states_context_and_unknown_labels() -> None:
    from noyra.interaction.public_display import PUBLIC_PROVENANCE

    item = {
        "id": "reading-state",
        "kind": "lifecycle",
        "title": "生命周期变化",
        "summary": "",
        "body": "",
        "details": {"from": "active", "to": "paused"},
        "status": "paused",
        "occurred_at": "2026-10-12T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
        "provenance": "runtime",
    }
    for english in (False, True):
        for provenance, labels in PUBLIC_PROVENANCE.items():
            markup = public_article_html(
                {**item, "provenance": provenance},
                "https://public.example",
                english=english,
                theme="dark",
                return_view="timeline",
            ).decode()
            assert labels[int(english)] in markup
            assert 'data-theme="dark"' in markup
            assert "#timeline" in markup and "theme=dark" in markup
            assert ("Active → Paused" if english else "活跃 → 已暂停") in markup
            assert 'og:description" content=""' not in markup
    unknown = public_article_html({**item, "status": "new_safe_status", "details": {}}, "").decode()
    assert "new_safe_status" in unknown


def test_long_text_toc_is_bounded_and_heading_context_is_explicit() -> None:
    text = "\n".join(f"# Section {i}\ntext" for i in range(101))
    markup = structured_public_text(text)
    assert markup.count("<li><a href=") == 100
    assert "First 100 sections shown" in markup
    assert '<h2 id="reading-section-101">Section 100</h2>' in markup
    assert '<h3 id="reading-section-1">' in structured_public_text(text, heading_base=2)


def test_text_structure_never_executes_html_or_links() -> None:
    markup = structured_public_text(
        "# Heading\n\nA paragraph\nnext line\n\n## <script>bad()</script>\njavascript:bad()"
    )
    assert '<h2 id="reading-section-1">Heading</h2>' in markup
    assert "<p>A paragraph\nnext line</p>" in markup
    assert '<details class="reading-toc">' in markup
    assert "<script>" not in markup
    assert "&lt;script&gt;" in markup


def test_article_metadata_and_body_allowlist_escape_public_text() -> None:
    item = {
        "id": "article-test",
        "kind": "goal",
        "title": "<title>Content",
        "summary": "Public description",
        "body": "# First\ntext\n## Second\nother",
        "occurred_at": "2026-10-12T00:00:00Z",
        "updated_at": "2026-10-12T00:00:00Z",
        "provenance": "operator_summary",
        "details": {
            "status": "complete",
            "private_reason": "PRIVATE-SENTINEL",
            "sources": [
                {"title": "Safe reference", "url": "https://example.test/reading"},
                {"title": "Unsafe", "url": "javascript:bad()"},
                {"title": "Credentials", "url": "https://user:secret@example.test/"},
            ],
        },
    }
    markup = public_article_html(item, "https://public.example").decode()
    assert "<title>&lt;title&gt;Content · Noyra</title>" in markup
    assert "https://public.example/public/goal/article-test" in markup
    assert 'og:type" content="article' in markup
    assert "PRIVATE-SENTINEL" not in markup
    assert "<script" not in markup
    assert "https://example.test/reading" in markup
    assert "javascript:" not in markup
    assert "user:secret" not in markup
    english = public_article_html(item, "", english=True).decode()
    assert '<html lang="en">' in english
    assert 'rel="canonical"' not in english
    assert "Back to archive" in english
