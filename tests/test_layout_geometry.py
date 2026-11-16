import math

import fitz
import pytest
from PIL import Image

from redos_pdf_combiner.layout_docx import (
    _contains_colored_ink, exclude_colored_ink_words, sanitize_layout_pages,
)
from redos_pdf_combiner.ocr import IndexedWord, PageText


@pytest.mark.parametrize("rect", [
    (101, 10, 120, 30), (10, 101, 30, 120), (-30, 10, -10, 30),
    (10, -30, 30, -10), (40, 10, 20, 30), (10, 20, 30, 20),
    (math.nan, 10, 30, 40), (10, 10, math.inf, 30),
])
def test_color_filter_rejects_invalid_or_off_page_rectangles(rect):
    with Image.new("RGB", (100, 100), "blue") as image:
        assert not _contains_colored_ink(image, rect, 1)


def test_color_filter_clips_partially_visible_word():
    with Image.new("RGB", (100, 100), "blue") as image:
        assert _contains_colored_ink(image, (90, 90, 120, 120), 1)


@pytest.mark.parametrize("rotation", [0, 90, 270])
def test_color_filter_uses_actual_page_index_and_display_coordinates(tmp_path, rotation):
    source = tmp_path / "pages.pdf"
    with fitz.open() as pdf:
        pdf.new_page(width=100, height=160)
        page = pdf.new_page(width=100, height=160)
        page.draw_rect(page.rect, fill=(0, 0, 1), color=None)
        page.set_rotation(rotation)
        width, height = page.rect.width, page.rect.height
        pdf.save(source)
    word = IndexedWord((10, 10, 30, 30), "Text", 0, 0, 0)
    result = exclude_colored_ink_words(source, [PageText(1, width, height, (word,), True)])
    assert result[0].page_index == 1
    assert not result[0].words


def test_sanitize_drops_bad_geometry_and_clips_visible_words():
    rects = [(101, 10, 120, 30), (math.nan, 10, 30, 40), (90, 10, 120, 30)]
    words = tuple(IndexedWord(rect, "Text", 0, 0, i) for i, rect in enumerate(rects))
    result = sanitize_layout_pages([PageText(0, 100, 100, words, False)], "eng")
    assert len(result[0].words) == 1
    assert result[0].words[0].rect == (90, 10, 100, 30)


def test_docx_conversion_completes_with_off_page_ocr_words(tmp_path):
    from zipfile import ZipFile
    from redos_pdf_combiner.layout_ocr import source_to_layout_ocr_docx

    source, output = tmp_path / "source.pdf", tmp_path / "result.docx"
    with fitz.open() as pdf:
        page = pdf.new_page(width=200, height=300)
        page.insert_text((20, 40), "Example")
        pdf.save(source)
    words = (
        IndexedWord((20, 25, 80, 42), "Example", 0, 0, 0),
        IndexedWord((300, 25, 350, 42), "Outside", 0, 0, 1),
    )
    source_to_layout_ocr_docx(source, output,
        text_index=[PageText(0, 200, 300, words, True)], language="eng")
    with ZipFile(output) as archive:
        xml = archive.read("word/document.xml").decode("utf-8")
    assert "Example" in xml
    assert "Outside" not in xml
