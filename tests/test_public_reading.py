from __future__ import annotations

from noyra.interaction.public_reading import public_article_html, structured_public_text


def test_text_structure_never_executes_html_or_links() -> None:
    markup = structured_public_text(
        "# Heading\n\nA paragraph\nnext line\n\n## <script>bad()</script>\njavascript:bad()"
    )
    assert '<h3 id="reading-section-1">Heading</h3>' in markup
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
