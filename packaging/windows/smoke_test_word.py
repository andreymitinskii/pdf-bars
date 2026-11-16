from pathlib import Path

from PIL import Image

from redos_pdf_combiner.converters import docx_to_pdf, pdf_to_docx
from redos_pdf_combiner.layout_docx import write_layout_docx
from redos_pdf_combiner.ocr import IndexedWord, PageText


project = Path(__file__).resolve().parents[2]
output_dir = project / "tmp" / "windows-word-smoke"
output_dir.mkdir(parents=True, exist_ok=True)

background_path = output_dir / "background.jpg"
Image.new("RGB", (300, 424), "white").save(background_path, "JPEG")
page = PageText(
    page_index=0,
    width=595.0,
    height=842.0,
    words=(IndexedWord((72.0, 95.0, 260.0, 112.0), "Проверка pdf bars", 0, 0, 0),),
    used_ocr=True,
)
source_docx = write_layout_docx(
    output_dir / "word-smoke.docx",
    [page],
    [background_path.read_bytes()],
    title="pdf bars Windows smoke test",
)
pdf = docx_to_pdf([source_docx], output_dir)[0]
restored_docx = pdf_to_docx([pdf], output_dir)[0]

if not pdf.is_file() or pdf.stat().st_size == 0:
    raise SystemExit("Microsoft Word did not create PDF")
if not restored_docx.is_file() or restored_docx.stat().st_size == 0:
    raise SystemExit("Microsoft Word did not create DOCX")
print(f"WORD_SMOKE_OK {pdf.name} {pdf.stat().st_size} {restored_docx.name} {restored_docx.stat().st_size}")
