"""Build site/terms.html and site/privacy.html from the texts shipped in the app.

Run after editing src/dbnero/legal/*.txt:  python packaging/build_legal_pages.py
(a unit test fails while the pages are out of date).
"""

from __future__ import annotations

import html
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEGAL = ROOT / "src" / "dbnero" / "legal"
SITE = ROOT / "site"

PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · dbnero</title>
<link rel="icon" type="image/png" href="favicon.png">
<style>
:root {{ color-scheme: light dark; --bg: #f6f7f9; --text: #15171c; --muted: #5b6270;
  --card: #ffffff; --line: #e3e6eb; --accent: #2f5bd3; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #0f1115; --text: #e8eaee; --muted: #9aa1ad; --card: #171a20;
    --line: #262a32; --accent: #7c9cff; }} }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; background: var(--bg); color: var(--text);
  font: 16px/1.6 system-ui, -apple-system, "Segoe UI", sans-serif; }}
main {{ max-width: 760px; margin: 0 auto; padding: 32px 16px 64px; }}
nav a, a {{ color: var(--accent); }}
nav {{ margin-bottom: 24px; font-size: 15px; }}
article {{ background: var(--card); border: 1px solid var(--line); border-radius: 12px;
  padding: 28px; }}
h1 {{ font-size: 28px; margin: 0 0 4px; }}
h2 {{ font-size: 18px; margin: 28px 0 8px; }}
.meta {{ color: var(--muted); margin: 0 0 20px; }}
ul {{ padding-left: 22px; }}
@media (max-width: 600px) {{ article {{ padding: 20px 16px; }} h1 {{ font-size: 24px; }} }}
</style>
</head>
<body>
<main>
<nav><a href="index.html">← dbnero</a> · <a href="terms.html">Terms of Use</a> ·
<a href="privacy.html">Privacy Policy</a></nav>
<article>
{body}
</article>
</main>
</body>
</html>
"""


def to_html(text: str) -> tuple[str, str]:
    lines = text.strip().splitlines()
    title = lines[0].split(" - ", 1)[-1]
    out = [f"<h1>{html.escape(title, quote=False)}</h1>"]
    out.append(f'<p class="meta">{html.escape(lines[1].strip(), quote=False)}</p>')
    in_list = False
    for raw in lines[2:]:
        line = raw.strip()
        is_item = line.startswith("- ")
        if in_list and not is_item:
            out.append("</ul>")
            in_list = False
        if not line:
            continue
        text_html = html.escape(line[2:] if is_item else line, quote=False)
        if is_item:
            if not in_list:
                out.append("<ul>")
                in_list = True
            out.append(f"<li>{text_html}</li>")
        elif line[0].isdigit() and ". " in line[:4]:
            out.append(f"<h2>{text_html}</h2>")
        else:
            out.append(f"<p>{text_html}</p>")
    if in_list:
        out.append("</ul>")
    return title, "\n".join(out)


def main() -> None:
    for src, dest in (("TERMS.txt", "terms.html"), ("PRIVACY.txt", "privacy.html")):
        title, body = to_html((LEGAL / src).read_text("utf-8"))
        (SITE / dest).write_text(PAGE.format(title=title, body=body), "utf-8")
        print(f"wrote site/{dest}")


if __name__ == "__main__":
    main()
