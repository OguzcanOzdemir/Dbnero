"""Convert the PNG screenshots in site/screenshots to WebP (about a third of
the size) and point the page at them. Run by the Website workflow after it
copies docs/screenshots; the PNGs stay for social previews (og:image)."""

from __future__ import annotations

import re
import sys
from pathlib import Path

from PIL import Image

SITE = Path(sys.argv[1] if len(sys.argv) > 1 else "site")


def main() -> None:
    shots = SITE / "screenshots"
    for png in sorted(shots.glob("*.png")):
        with Image.open(png) as im:
            im.convert("RGB").save(png.with_suffix(".webp"), "WEBP", quality=82, method=6)
    page = SITE / "index.html"
    text = page.read_text("utf-8")
    # Only relative references (src, srcset, href); the absolute og:image stays PNG.
    text = re.sub(r'(["\s])screenshots/([\w.-]+)\.png', r"\1screenshots/\2.webp", text)
    page.write_text(text, "utf-8")
    print(f"converted {len(list(shots.glob('*.webp')))} screenshots to WebP")


if __name__ == "__main__":
    main()
