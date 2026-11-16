from io import BytesIO
from zipfile import ZipFile
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw

from redos_pdf_combiner.layout_docx import detect_layout_tables, _page_lines, _cell_text_lines, write_layout_docx
from redos_pdf_combiner.ocr import IndexedWord, PageText


def png(image):
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def test_dense_prose_is_not_a_table():
    image = Image.new("L", (800, 1100), 255)
    draw = ImageDraw.Draw(image)
    for y in range(70, 1000, 24):
        draw.text((70, y), "This is ordinary continuous text with several words. " * 2, fill=0)
    assert detect_layout_tables(PageText(0, 600, 825, ()), png(image)) == []


def test_distinct_tables_keep_their_own_column_counts(tmp_path):
    images, pages = [], []
    for columns in (2, 4, 9):
        image = Image.new("L", (1000, 1000), 255)
        draw = ImageDraw.Draw(image)
        for x in range(columns + 1):
            draw.line((50 + x * 900 / columns, 100, 50 + x * 900 / columns, 600), fill=0, width=2)
        for y in (100, 200, 300, 400, 500, 600):
            draw.line((50, y, 950, y), fill=0, width=2)
        images.append(png(image))
        pages.append(PageText(len(pages), 600, 600, ()))
    output = tmp_path / "mixed.docx"
    write_layout_docx(output, pages, [b""] * 3, title="Mixed tables", preserve_graphics=False, table_images=images)
    with ZipFile(output) as archive:
        root = ET.fromstring(archive.read("word/document.xml"))
    ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
    assert [len(table.findall("w:tblGrid/w:gridCol", ns)) for table in root.findall(".//w:tbl", ns)] == [2, 4, 9]
    assert not root.findall(".//w:txbxContent", ns)


def test_flowing_layout_keeps_lines_near_page_bottom():
    words = tuple(IndexedWord((30, y, 180, y + 10), "A fairly long line of document text", 0, i, 0)
                  for i, y in enumerate((150, 162, 174, 186)))
    page = PageText(0, 220, 200, words, True)
    lines = _page_lines(page, positioned=False)
    assert len(lines) == 4
    assert [line.rect[1] for line in lines] == [150, 162, 174, 186]


def test_overlapping_ocr_box_heights_do_not_mix_table_lines():
    words = [IndexedWord((50, 268, 140, 295), "First line", 0, 0, 0),
             IndexedWord((50, 281, 110, 295), "Second line", 0, 1, 0)]
    lines = _cell_text_lines(words, scanned=True, base_font=11)
    assert [text for text, _size in lines] == ["First line", "Second line"]
    assert all(10 <= size <= 12.2 for _text, size in lines)


def test_scanned_cell_respects_ocr_reading_order_despite_identical_tops():
    words = [IndexedWord((20, 50, 80, 62), "First", 0, 0, 0),
             IndexedWord((20, 50, 90, 76), "Second", 1, 0, 0),
             IndexedWord((20, 63, 80, 89), "Third", 1, 1, 0)]
    assert [line[0] for line in _cell_text_lines(words, scanned=True)] == ["First", "Second", "Third"]
