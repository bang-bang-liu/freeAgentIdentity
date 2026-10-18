"""Vendor licensed Google Fonts and a compact CJK subset for the interface.

Run with Python 3, from any directory. The app never requests remote fonts.
Only public UI characters are sent to the font subset service, no runtime data.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import re
import urllib.parse
import urllib.request


ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / "frontend/public/assets/fonts"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    return urllib.request.urlopen(request, timeout=30).read()


def face(family_query: str, filename: str, *, text: str = "", latin: bool = False) -> str:
    url = "https://fonts.googleapis.com/css2?family=" + family_query + "&display=swap"
    if text:
        url += "&text=" + urllib.parse.quote(text)
    css = fetch(url).decode()
    blocks = re.findall(r"@font-face\s*\{[^}]+\}", css)
    if latin:
        blocks = [block for block in blocks if "U+0000-00FF" in block]
    if len(blocks) != 1:
        raise RuntimeError(f"Expected one verified font face for {filename}, got {len(blocks)}")
    block = blocks[0]
    source = re.search(r"url\((https://fonts\.gstatic\.com/[^)]+)\)", block)
    if source is None:
        raise RuntimeError("Unexpected font origin")
    binary = fetch(source.group(1))
    if binary[:4] != b"wOF2":
        raise RuntimeError(f"Expected WOFF2 font for {filename}")
    (DEST / filename).write_bytes(binary)
    return block.replace(source.group(1), "/assets/fonts/" + filename)


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    characters: set[str] = set()
    for source in (ROOT / "frontend/src").rglob("*"):
        if source.suffix in {".ts", ".tsx"}:
            characters.update(re.findall(r"[\u3000-\u30ff\u3400-\u9fff\uff00-\uffef]", source.read_text(encoding="utf-8")))
    cjk = "".join(sorted(characters))
    jobs = [
        ("Space+Grotesk:wght@400..700", "space-grotesk-latin.woff2", "", True),
        ("IBM+Plex+Mono:wght@400", "ibm-plex-mono-regular.woff2", "", True),
        ("IBM+Plex+Mono:wght@500", "ibm-plex-mono-medium.woff2", "", True),
    ]
    jobs += [
        ("Noto+Sans+SC:wght@400..700", f"noto-sans-sc-ui-{index // 180 + 1:02d}.woff2", cjk[index:index + 180], False)
        for index in range(0, len(cjk), 180)
    ]
    with ThreadPoolExecutor(max_workers=5) as pool:
        blocks = list(pool.map(lambda job: face(job[0], job[1], text=job[2], latin=job[3]), jobs))
    (ROOT / "frontend/src/fonts.css").write_text(
        "/* Locally served fonts. Regenerate with scripts/build_ui_fonts.py. */\n" + "\n\n".join(blocks) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    for directory, name in [("spacegrotesk", "SpaceGrotesk"), ("ibmplexmono", "IBMPlexMono"), ("notosanssc", "NotoSansSC")]:
        (DEST / f"{name}-OFL.txt").write_bytes(fetch(f"https://raw.githubusercontent.com/google/fonts/main/ofl/{directory}/OFL.txt"))
    total = sum((DEST / job[1]).stat().st_size for job in jobs)
    print(f"Vendored {len(jobs)} WOFF2 files, {len(cjk)} CJK characters, {total / 1024:.0f} KiB; no runtime font requests.")


if __name__ == "__main__":
    main()
