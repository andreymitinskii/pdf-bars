"""Build the illustrated HTML guide from reviewed, versioned user instructions."""
import html
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STYLE = """
@page { size:A4; margin:0; }
* { box-sizing:border-box; }
body { margin:0; background:#e9eef3; color:#182b3c; font:10.5pt/1.46 Arial,sans-serif; letter-spacing:0; }
.page { width:210mm; height:297mm; margin:7mm auto; padding:17mm 18mm 18mm; position:relative; background:white; break-after:page; }
.page:last-child { break-after:auto; }
.eyebrow { color:#005baa; font-size:9pt; margin:0 0 4mm; font-weight:bold; }
h1 { font-size:38pt; line-height:1.12; margin:5mm 0; color:#005baa; }
h2 { font-size:22pt; line-height:1.2; margin:0 0 5mm; color:#0754a0; }
h3 { font-size:12pt; margin:5mm 0 2mm; color:#0754a0; }
p { margin:2mm 0 3mm; }
ol,ul { padding-left:6mm; margin:2mm 0 4mm; }
li { margin:1.8mm 0; padding-left:.7mm; }
strong { color:#113653; }
.shot { display:block; width:100%; height:auto; max-height:100mm; object-fit:contain; object-position:left top; border:1px solid #d2dde7; margin:4mm 0; }
.strip { max-height:32mm; }
.small { width:auto; max-width:100%; max-height:78mm; }
.caption { color:#5c7182; font-size:9pt; }
.note { padding:3mm 4mm; border-left:3px solid #1688ee; background:#eef5fb; margin:4mm 0; }
.warning { border-left-color:#bd7b00; background:#fff5d9; }
table { width:100%; border-collapse:collapse; margin:3mm 0; font-size:9.5pt; }
th,td { padding:2.2mm 2mm; border-bottom:1px solid #d6e0e9; vertical-align:top; text-align:left; }
th { color:#0754a0; background:#eef4f9; }
td.icon { width:10mm; } td.icon img { width:6mm; height:6mm; }
code,kbd { font:9pt Consolas,'DejaVu Sans Mono',monospace; background:#eef3f7; padding:.4mm 1mm; }
pre { white-space:pre-wrap; overflow-wrap:anywhere; background:#f0f4f8; padding:3mm; font:9pt/1.5 Consolas,monospace; }
a { color:#0754a0; text-decoration:none; }
.toc { columns:2; column-gap:9mm; } .toc p { break-inside:avoid; margin:0 0 3mm; font-size:10pt; }
footer { position:absolute; left:18mm; right:18mm; bottom:9mm; border-top:1px solid #d4dfe8; padding-top:2mm; display:flex; justify-content:space-between; color:#60778b; font-size:8pt; }
.cover { border-top:7mm solid #005baa; padding-top:16mm; }
.subtitle { font-size:19pt; color:#173b59; }
@media print { body { background:white; } .page { margin:0; } a { color:inherit; } }
"""


def main():
    content = json.loads((ROOT / "docs/user_guide.json").read_text(encoding="utf-8"))
    screenshot_set = html.escape(content["screenshots"])
    pages = content["pages"]
    sections = []
    for number, page in enumerate(pages, 1):
        body = "".join(page["body"])
        if page.get("contents"):
            body = "<div class='toc'>" + "".join(
                f"<p><a href='#p{i}'>{i:02d} · {html.escape(item['title'])}</a></p>"
                for i, item in enumerate(pages, 1) if i > 2
            ) + "</div>" + body
        image = page.get("image")
        if image:
            filename = ROOT / "docs/screenshots" / content["screenshots"] / (image + ".png")
            if not filename.is_file():
                raise FileNotFoundError(filename)
            body = (f"<img class='shot {html.escape(page.get('image_class', ''))}' "
                    f"src='screenshots/{screenshot_set}/{html.escape(image)}.png' "
                    f"alt='{html.escape(page['image_alt'])}'>" + body)
        heading = "h1" if number == 1 else "h2"
        sections.append(
            f"<section class='page {'cover' if number == 1 else ''}' id='p{number}'>"
            f"<p class='eyebrow'>{html.escape(page['section'])}</p>"
            f"<{heading}>{html.escape(page['title'])}</{heading}>{body}"
            f"<footer><span>pdf bars · Руководство пользователя</span>"
            f"<span>{number:02d}</span></footer></section>"
        )
    output = ROOT / "docs/PDF_Bars_User_Guide.html"
    output.write_text(
        "<!doctype html><html lang='ru'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>pdf bars - руководство пользователя</title>"
        f"<style>{STYLE}</style></head><body>{''.join(sections)}</body></html>",
        encoding="utf-8",
    )
    print("GUIDE_HTML_OK", len(pages), output)


if __name__ == "__main__":
    main()
