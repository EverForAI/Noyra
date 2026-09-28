from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).parents[1]
PUBLIC_HTML = (ROOT / "src/noyra/web/index.html").read_text(encoding="utf-8")
PUBLIC_CSS = (ROOT / "src/noyra/web/styles.css").read_text(encoding="utf-8")
STATIC_HTML = (ROOT / "site/index.html").read_text(encoding="utf-8")
STATIC_CSS = (ROOT / "site/assets/site.css").read_text(encoding="utf-8")


def test_runtime_public_visual_contract() -> None:
    assert 'name="twitter:card" content="summary_large_image"' in PUBLIC_HTML
    assert 'width="1536" height="1024"' in PUBLIC_HTML
    assert 'fetchpriority="high"' in PUBLIC_HTML
    assert 'alt="晨雾湖畔的阅读空间与轻柔流动的青蓝微光"' in PUBLIC_HTML
    assert (
        'srcset="/assets/public-hero-mobile.webp?v=__PUBLIC_HERO_MOBILE_VERSION__"' in PUBLIC_HTML
    )
    assert "<!-- NOYRA_SEO_METADATA -->" in PUBLIC_HTML
    assert "prefers-reduced-motion" in PUBLIC_CSS
    assert "验证码暂时不可用" in (ROOT / "src/noyra/web/app.js").read_text(encoding="utf-8")


def image_size(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data[:12] == b"RIFF" + data[4:8] + b"WEBP" and data[12:16] == b"VP8L":
        # VP8L stores each dimension minus one in a 14-bit little-endian bit field.
        width = 1 + ((data[21] | (data[22] << 8)) & 0x3FFF)
        height = 1 + (((data[22] >> 6) | (data[23] << 2) | (data[24] << 10)) & 0x3FFF)
        return width, height
    if data[:12] == b"RIFF" + data[4:8] + b"WEBP" and data[12:16] == b"VP8 ":
        if data[23:26] != b"\x9d\x01\x2a":
            raise AssertionError(f"invalid VP8 frame header: {path}")
        return int.from_bytes(data[26:28], "little") & 0x3FFF, int.from_bytes(
            data[28:30], "little"
        ) & 0x3FFF
    if data[:12] == b"RIFF" + data[4:8] + b"WEBP" and data[12:16] == b"VP8X":
        return (
            1 + int.from_bytes(data[24:27], "little"),
            1 + int.from_bytes(data[27:30], "little"),
        )
    raise AssertionError(f"unsupported image format: {path}")


def test_static_public_visual_contract_and_asset_dimensions() -> None:
    for marker in (
        'property="og:image"',
        'property="og:image:width" content="1200"',
        'property="og:image:height" content="630"',
        'name="twitter:image"',
    ):
        assert marker in STATIC_HTML
    assert "prefers-reduced-motion" in STATIC_CSS
    assert image_size(ROOT / "src/noyra/web/assets/public-hero.webp") == (1536, 1024)
    assert image_size(ROOT / "src/noyra/web/assets/public-hero-mobile.webp") == (768, 1024)
    assert image_size(ROOT / "src/noyra/web/assets/public-social.png") == (1200, 630)
    assert image_size(ROOT / "site/assets/social.png") == (1200, 630)
    assert (ROOT / "src/noyra/web/assets/public-hero.webp").stat().st_size < 300_000
    assert (ROOT / "src/noyra/web/assets/public-hero-mobile.webp").stat().st_size < 180_000
    assert (ROOT / "src/noyra/web/assets/public-social.png").stat().st_size < 1_500_000
