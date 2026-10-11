from pathlib import Path


def test_pelican_bicycle_page_contains_animation_contract() -> None:
    page = Path("site/pelican-bike.html")
    assert page.is_file()
    html = page.read_text(encoding="utf-8")
    for marker in (
        "<svg",
        "pelican",
        "bicycle",
        "requestAnimationFrame",
        "pauseButton",
        "resetButton",
    ):
        assert marker in html
    assert "prefers-reduced-motion" in html
