import hashlib
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "ikunai-20261008-215635-1.png"
image = Image.open(SOURCE).convert("RGB")
edge = round(min(image.size) * 0.68)
left = (image.width - edge) // 2
top = (image.height - edge) // 2
avatar = image.crop((left, top, left + edge, top + edge)).resize(
    (1024, 1024), Image.Resampling.LANCZOS
)

exports = []
for size in (1024, 400):
    path = ROOT / f"noyra-x-avatar-{size}.png"
    avatar.resize((size, size), Image.Resampling.LANCZOS).save(path, optimize=True)
    exports.append(
        {
            "file": path.name,
            "dimensions": [size, size],
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
    )


def circle(size):
    scale = 4
    mask = Image.new("L", (size * scale, size * scale), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, mask.width - 1, mask.height - 1), fill=255)
    result = avatar.resize((size, size), Image.Resampling.LANCZOS).convert("RGBA")
    result.putalpha(mask.resize((size, size), Image.Resampling.LANCZOS))
    return result


circle(400).save(ROOT / "noyra-x-avatar-circle-preview.png", optimize=True)
sheet = Image.new("RGB", (1040, 640), "#f4f5f6")
draw = ImageDraw.Draw(sheet)
font = ImageFont.truetype("C:/Windows/Fonts/segoeui.ttf", 22)
title_font = ImageFont.truetype("C:/Windows/Fonts/segoeuib.ttf", 30)
draw.text((40, 30), "NOYRA / PROFILE AVATAR", fill="#17191b", font=title_font)
draw.text((40, 82), "Circular crop and small-size checks", fill="#63686d", font=font)
sheet.paste(circle(400), (40, 155), circle(400))
draw.rectangle((500, 140, 999, 580), fill="#101214")
draw.text((535, 165), "X / DARK APPEARANCE", fill="#d8dcdf", font=font)
for size, x, y in ((128, 545, 230), (64, 750, 255), (48, 850, 263), (32, 935, 271)):
    icon = circle(size)
    sheet.paste(icon, (x, y), icon)
    draw.text((x, 385), f"{size}px", fill="#c7ccd1", font=font)
draw.text((535, 485), "Original N mark / crop-safe / no wordmark", fill="#c7ccd1", font=font)
sheet.save(ROOT / "noyra-x-avatar-preview.png", optimize=True)

metadata = {
    "status": "Local design only; not uploaded to X",
    "provider": "ikunai-imagegen",
    "source": SOURCE.name,
    "source_dimensions": list(image.size),
    "source_sha256": hashlib.sha256(SOURCE.read_bytes()).hexdigest(),
    "prompt": "avatar-prompt.txt",
    "transform": "Centered crop with original mark preserved, resized using Lanczos",
    "crop": [left, top, left + edge, top + edge],
    "exports": exports,
}
(ROOT / "avatar-provenance.json").write_text(
    json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(metadata, indent=2))
