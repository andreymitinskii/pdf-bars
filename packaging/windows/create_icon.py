from pathlib import Path

from PIL import Image


project = Path(__file__).resolve().parents[2]
source = project / "src" / "redos_pdf_combiner" / "assets" / "pdf-bars-transparent.png"
output = Path(__file__).with_name("pdf-bars.ico")

with Image.open(source) as image:
    icon = image.convert("RGBA")
    icon.thumbnail((256, 256), Image.Resampling.LANCZOS)
    icon.save(output, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
