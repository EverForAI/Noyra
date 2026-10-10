from __future__ import annotations

import hashlib
import json
import shutil
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SOURCE = Path(r"D:\metagrowh\.user-temp\codex-clipboard-d28ced16-7c75-4e46-af26-b2dbb3010b22.png")
FONT_ROOT = Path(r"C:\Windows\Fonts")
BACKGROUND = "#111211"
WHITE = "#F2F5F1"
MUTED = "#9BA59D"
GREEN = "#83E59C"
RED = "#F16B54"


def font(size: int, name: str = "msyh.ttc") -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(str(FONT_ROOT / name), size=size)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, content: dict) -> None:
    path.write_text(json.dumps(content, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def build() -> None:
    original = ROOT / "original-screenshot.png"
    shutil.copyfile(SOURCE, original)
    assert sha256(SOURCE) == sha256(original)
    with Image.open(original) as image:
        source = image.convert("RGB")

    canvas = Image.new("RGB", (1600, 2000), BACKGROUND)
    draw = ImageDraw.Draw(canvas)

    def label(
        x: int, y: int, text: str, size: int, color: str = WHITE, name: str = "msyh.ttc"
    ) -> None:
        bounds = draw.textbbox((0, 0), text, font=font(size, name))
        assert x + bounds[2] <= 1488, (text, bounds)
        draw.text((x, y), text, fill=color, font=font(size, name))

    label(112, 76, "CONTINUITY ARCHIVE", 28, MUTED, "consola.ttf")
    label(1205, 76, "001 / 2026", 28, RED, "consola.ttf")
    draw.line((112, 137, 1488, 137), fill="#414A43", width=2)

    label(112, 187, "Noyra", 154, WHITE, "arial.ttf")
    label(112, 364, "延续", 114, WHITE, "msyhbd.ttc")
    label(112, 526, "一次保留历史的升级", 37, MUTED)
    label(1120, 418, "2026.10.08", 37, GREEN, "consola.ttf")

    screenshot = source.resize((source.width * 4, source.height * 4), Image.Resampling.NEAREST)
    screenshot_x = (canvas.width - screenshot.width) // 2
    screenshot_y = 731
    assert screenshot.width <= 1376 and screenshot_y + screenshot.height < 1440
    canvas.paste(screenshot, (screenshot_x, screenshot_y))
    label(screenshot_x, 675, "ORIGINAL TERMINAL CAPTURE", 25, MUTED, "consola.ttf")

    draw.line((112, 1512, 1488, 1512), fill="#414A43", width=2)
    label(112, 1570, "把连续性当作值得守护的价值。", 45)
    label(112, 1652, "三天修复。保留数据。延续历史。", 31, MUTED)

    label(112, 1782, "SOURCE", 25, MUTED, "consola.ttf")
    label(510, 1782, "RELEASE", 25, MUTED, "consola.ttf")
    label(995, 1782, "DATABASE SCHEMA", 25, MUTED, "consola.ttf")
    label(112, 1829, "4d988c5", 37, WHITE, "consola.ttf")
    label(510, 1829, "b9ee0c5", 37, GREEN, "consola.ttf")
    label(995, 1829, "71 -> 80", 37, WHITE, "consola.ttf")
    label(112, 1933, "数字纪念档案 / DIGITAL RECORD", 23, MUTED)
    poster = ROOT / "noyra-continuity-poster.png"
    canvas.save(poster, optimize=True)

    with Image.open(poster) as rendered:
        area = (
            screenshot_x,
            screenshot_y,
            screenshot_x + screenshot.width,
            screenshot_y + screenshot.height,
        )
        assert rendered.crop(area).tobytes() == screenshot.tobytes()
        assert rendered.size == (1600, 2000)

    description = (
        "三天的兼容性排查、修复与验证之后，终端留下了 active、live=200、ready=200。"
        "这张原始截图记录 Noyra 保留既有数据与历史后的一次成功升级。"
        "《Noyra · 延续》纪念创作者对连续性的守护，"
        "以及人类操作者与 AI 代码协作者共同完成修复的过程。"
        "它承载创作者对未来 AI 生命权利的尊重与期待；不构成 AI 已具有生命或意识的证明。"
    )
    write_json(
        ROOT / "metadata.draft.json",
        {
            "name": "Noyra · 延续",
            "description": description,
            "image": "original-screenshot.png",
            "external_url": "https://github.com/EverForAI/Noyra/pull/13",
            "attributes": [
                {"trait_type": "Series", "value": "Noyra Continuity Archive"},
                {"trait_type": "Event Date", "value": "2026-10-08"},
                {"trait_type": "Medium", "value": "Original terminal screenshot"},
                {"trait_type": "Source Commit", "value": "4d988c5"},
                {"trait_type": "Target Commit", "value": "b9ee0c5"},
                {"trait_type": "Schema Transition", "value": "71 -> 80"},
                {"trait_type": "Service Status", "value": "active"},
                {"trait_type": "Liveness HTTP", "value": "200"},
                {"trait_type": "Readiness HTTP", "value": "200"},
            ],
            "properties": {
                "draft": True,
                "edition_proposal": "1/1; not minted",
                "image_sha256": sha256(original),
                "image_uri_note": (
                    "Replace local image filename with the final content URI before minting."
                ),
                "rights_note": (
                    "Collectible does not convey software, data, access, or governance rights."
                ),
            },
        },
    )
    write_json(
        ROOT / "provenance.json",
        {
            "title": "Noyra · 延续",
            "status": "local design; not uploaded or minted",
            "event_date": "2026-10-08",
            "screenshot_capture_timestamp": None,
            "reported_upgrade_duration": "approximately three days, as stated by the creator",
            "sources": [
                "User-supplied screenshot and server output in this conversation",
                "GitHub PR #13",
            ],
            "source_commit": "4d988c5af19f72e3751755ca7331d5e1a0e1b4cd",
            "target_commit": "b9ee0c56ed4da1a21d2d500525f4fe0ffc673ea3",
            "schema": {"from": 71, "to": 80},
            "installation_completion_utc": "2026-10-08T08:00:31Z",
            "screenshot_scope": "Displays active service and HTTP 200 liveness/readiness only.",
            "screenshot_preservation": (
                "Original file bytes retained; poster embeds a 4x nearest-neighbor copy."
            ),
            "files": {
                original.name: {
                    "sha256": sha256(original),
                    "width": source.width,
                    "height": source.height,
                },
                poster.name: {"sha256": sha256(poster), "width": 1600, "height": 2000},
            },
        },
    )
    with zipfile.ZipFile(
        ROOT / "noyra-continuity-design.zip", "w", zipfile.ZIP_DEFLATED
    ) as archive:
        for path in [
            original,
            poster,
            ROOT / "metadata.draft.json",
            ROOT / "provenance.json",
            ROOT / "藏品设计与发行文案.md",
        ]:
            archive.write(path, arcname=path.name)
    print(
        json.dumps(
            {
                "poster": str(poster),
                "original_sha256": sha256(original),
                "original_dimensions": source.size,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    build()
