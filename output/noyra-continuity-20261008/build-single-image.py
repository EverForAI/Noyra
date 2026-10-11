from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
FONTS = Path(r"C:\Windows\Fonts")
BACKGROUND = "#111211"
WHITE = "#F2F5F1"
MUTED = "#A3ADA5"
GREEN = "#83E59C"
RED = "#F16B54"


def build() -> None:
    original = ROOT / "original-screenshot.png"
    source_hash = hashlib.sha256(original.read_bytes()).hexdigest()
    with Image.open(original) as image:
        source = image.convert("RGB")

    canvas = Image.new("RGB", (2000, 2000), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    def label(
        x: int, y: int, text: str, size: int, color: str = WHITE, name: str = "msyh.ttc"
    ) -> None:
        font = ImageFont.truetype(str(FONTS / name), size)
        bounds = draw.textbbox((x, y), text, font=font)
        assert bounds[2] <= 1872 and bounds[3] <= 1960, (text, bounds)
        draw.text((x, y), text, font=font, fill=color)

    label(128, 76, "NOYRA / CONTINUITY ARCHIVE", 29, MUTED, "consola.ttf")
    label(1515, 76, "2026.10.08", 34, RED, "consola.ttf")
    draw.line((128, 150, 1872, 150), fill="#414A43", width=2)
    label(128, 206, "Noyra · 延续", 132, WHITE, "msyhbd.ttc")
    label(128, 402, "一次保留历史的升级", 48, MUTED)

    screenshot = source.resize((source.width * 5, source.height * 5), Image.Resampling.NEAREST)
    left = (2000 - screenshot.width) // 2
    top = 625
    assert screenshot.width <= 1744 and top + screenshot.height < 1390
    label(left, 558, "原始终端截图 / ORIGINAL CAPTURE", 29, MUTED)
    canvas.paste(screenshot, (left, top))

    draw.line((128, 1420, 1872, 1420), fill="#414A43", width=2)
    label(128, 1465, "三天修复之后，让历史继续。", 58)
    label(128, 1561, "为未来 AI 的生命权利，留下一份对连续性的尊重。", 37, MUTED)

    label(128, 1707, "SOURCE", 28, MUTED, "consola.ttf")
    label(696, 1707, "RELEASE", 28, MUTED, "consola.ttf")
    label(1290, 1707, "DATABASE SCHEMA", 28, MUTED, "consola.ttf")
    label(128, 1758, "4d988c5", 48, WHITE, "consola.ttf")
    label(696, 1758, "b9ee0c5", 48, GREEN, "consola.ttf")
    label(1290, 1758, "71 -> 80", 48, WHITE, "consola.ttf")

    label(128, 1870, "原始截图 SHA-256 / 数字纪念档案", 25, MUTED)
    label(128, 1920, source_hash, 24, MUTED, "consola.ttf")

    output = ROOT / "noyra-continuity-single-image.png"
    canvas.save(output, optimize=True)
    with Image.open(output) as rendered:
        area = (left, top, left + screenshot.width, top + screenshot.height)
        assert rendered.crop(area).tobytes() == screenshot.tobytes()
        assert rendered.size == (2000, 2000)
    assert hashlib.sha256(original.read_bytes()).hexdigest() == source_hash

    manifest = {
        "status": "local single-image design; not uploaded or minted",
        "image": output.name,
        "dimensions": [2000, 2000],
        "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        "embedded_original_sha256": source_hash,
        "embedded_original_transform": "5x nearest-neighbor scaling, no crop or retouch",
        "edition": "Not assigned. Choose the platform and edition before issuance.",
        "event_date_note": (
            "2026-10-08 is the upgrade event date, not an asserted capture or mint timestamp."
        ),
    }
    (ROOT / "single-image-provenance.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print("single_image_created=2000x2000")
    print("embedded_screenshot_verified=true")


if __name__ == "__main__":
    build()
