"""Validate the static Pages artifact without starting Noyra or using third-party services."""

from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1] / "site"
EXPECTED = {
    "index.html": "zh-CN",
    "en/index.html": "en",
    "research/index.html": "zh-CN",
    "en/research/index.html": "en",
}
POSITIONING = {
    "zh-CN": (
        "\u53ef\u96c7\u4f63\u4eba\u7c7b\u52b3\u52a8\u7684\u975e\u547d\u4ee4\u5f0f\u4eba\u5de5\u4e3b\u4f53"
    ),
    "en": "a non-command artificial subject capable of hiring human labor",
}
RETIRED_COPY = (
    "\u4e16\u754c\u9996\u4e2a",
    "\u9996\u4e2a\u771f\u5b9e\u4e16\u754c",
    "world's first",
    "\u5168\u7403\u4f18\u5148\u6027",
    "global priority",
    "\u516b\u9879\u5224\u636e",
    "eight criteria",
)


class Page(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids = set()
        self.links = []
        self.assets = []
        self.alternates = {}
        self.lang = ""
        self.headings = 0
        self.errors = []
        self.text = []

    def handle_data(self, data):
        self.text.append(data)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "html":
            self.lang = values.get("lang", "")
        if tag == "h1":
            self.headings += 1
        if tag in {"iframe", "form", "object", "embed"}:
            self.errors.append(f"unexpected active element: {tag}")
        if any(key.startswith("on") for key in values):
            self.errors.append("inline event handler")
        identifier = values.get("id")
        if identifier:
            if identifier in self.ids:
                self.errors.append(f"duplicate id: {identifier}")
            self.ids.add(identifier)
        if tag == "a":
            self.links.append(values.get("href", ""))
        if tag in {"script", "img", "source"}:
            if tag == "script" and not values.get("src"):
                self.errors.append("inline script")
            for key in ("src", "srcset"):
                if key in values:
                    self.assets.extend(part.strip().split()[0] for part in values[key].split(","))
        if tag == "link":
            rel = values.get("rel", "")
            if rel in {"stylesheet", "icon", "preload"}:
                self.assets.append(values.get("href", ""))
            if rel == "alternate":
                self.alternates[values.get("hreflang", "")] = values.get("href", "")


def validate():
    errors = []
    parsed = {}
    for name, lang in EXPECTED.items():
        path = ROOT / name
        if not path.is_file():
            errors.append(f"missing page: {name}")
            continue
        source = path.read_text(encoding="utf-8")
        page = Page()
        page.feed(source)
        parsed[path.resolve()] = page
        if "prior-art" in page.ids or any(
            unquote(urlsplit(href).fragment) == "prior-art" for href in page.links
        ):
            errors.append(f"{name}: removed related-work section or navigation returned")
        text = " ".join(" ".join(page.text).lower().split())
        for phrase in RETIRED_COPY:
            if phrase in text:
                errors.append(f"{name}: retired positioning: {phrase}")
        if name in {"index.html", "en/index.html"}:
            expected = POSITIONING[lang]
            if expected.replace(" ", "") not in text.replace(" ", ""):
                errors.append(f"{name}: missing human-collaboration positioning")
        if page.lang != lang or page.headings != 1:
            errors.append(f"{name}: language or H1 contract failed")
        if not {"zh-CN", "en", "x-default"} <= page.alternates.keys():
            errors.append(f"{name}: missing language alternates")
        for marker in ("NCAS", "preview-2026.09.07-r1", "Apache-2.0"):
            if marker not in source:
                errors.append(f"{name}: missing {marker}")
        errors.extend(f"{name}: {error}" for error in page.errors)
    for name, lang in (("README.md", "zh-CN"), ("README.en.md", "en")):
        source = (ROOT.parent / name).read_text(encoding="utf-8")
        if POSITIONING[lang] not in source.splitlines()[0].lower():
            errors.append(f"{name}: incorrect project title")
        for phrase in RETIRED_COPY:
            if phrase in source.lower():
                errors.append(f"{name}: retired positioning: {phrase}")
    for path, page in parsed.items():
        for href in page.assets + page.links:
            url = urlsplit(href)
            if url.scheme or url.netloc:
                if href in page.assets or url.scheme not in {"https", "mailto"}:
                    errors.append(f"unexpected remote asset or protocol: {href}")
                continue
            target = (path.parent / unquote(url.path)).resolve() if url.path else path
            if not target.is_relative_to(ROOT.resolve()):
                errors.append(f"path escapes publication root: {href}")
                continue
            if target.is_dir():
                target /= "index.html"
            if not target.is_file():
                errors.append(f"broken link: {path.name} -> {href}")
            if (
                url.fragment
                and target in parsed
                and unquote(url.fragment) not in parsed[target].ids
            ):
                errors.append(f"broken fragment: {href}")
    allowed = {".html", ".css", ".js", ".webp", ".png", ".xml", ".txt"}
    total = 0
    for path in ROOT.rglob("*"):
        if path.is_symlink():
            errors.append(f"symlink in artifact: {path.name}")
        if not path.is_file():
            continue
        total += path.stat().st_size
        if path.name != ".nojekyll" and path.suffix not in allowed:
            errors.append(f"unapproved file type: {path.name}")
        if path.stat().st_size > 1_200_000:
            errors.append(f"asset exceeds size budget: {path.name}")
    if total > 4_000_000:
        errors.append("publication exceeds 4 MB budget")
    if errors:
        raise SystemExit("\n".join(errors))
    print(
        "PASS: "
        f"{len(parsed)} bilingual pages; links, assets, fragments, language and safety contracts; "
        f"{total:,} bytes"
    )


if __name__ == "__main__":
    validate()
