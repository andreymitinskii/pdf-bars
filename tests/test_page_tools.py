import math

import fitz
import pytest
from PIL import Image

from redos_pdf_combiner.editor import CropEdit, StrokeEdit, TextEdit, apply_image_edits, apply_pdf_edits
from redos_pdf_combiner.page_tools import (delete_pdf_page, split_long_pdf_pages,
                                         arrange_pdf_pages, remap_page_edits, move_page_selection)


def test_arrange_pages_preserves_text_and_duplicates_edits(tmp_path):
    source = tmp_path / "original.pdf"
    with fitz.open() as document:
        for number in range(3):
            document.new_page().insert_text((40, 40), f"PAGE {number}")
        document.save(source)
    output = arrange_pdf_pages(source, tmp_path / "arranged.pdf", [2, 0, 2])
    with fitz.open(output) as document:
        assert [page.get_text().strip() for page in document] == ["PAGE 2", "PAGE 0", "PAGE 2"]
    edits = [TextEdit(2, (20, 30, 80, 70), "Edit", 12), TextEdit(1, (10, 10, 20, 20), "Deleted", 12)]
    mapped = remap_page_edits(edits, [2, 0, 2])
    assert [edit.page_index for edit in mapped] == [0, 2]
    assert all(edit.text == "Edit" for edit in mapped)
    assert edits[0].page_index == 2


def test_move_selected_pages_preserves_group_order():
    assert move_page_selection(5, {1, 3}, 5) == [0, 2, 4, 1, 3]
    assert move_page_selection(5, {3, 4}, 0) == [3, 4, 0, 1, 2]


def test_split_only_selected_pages(tmp_path):
    source = tmp_path / "long-pages.pdf"
    with fitz.open() as pdf:
        for _ in range(2):
            pdf.new_page(width=595, height=1600).insert_text((30, 40), "LONG PAGE")
        pdf.save(source)
    output = split_long_pdf_pages(source, tmp_path / "split.pdf", page_indexes={1})
    with fitz.open(output) as pdf:
        assert len(pdf) == 3
        assert pdf[0].rect.height == 1600
        assert pdf[1].rect.height < 850


def test_arrange_rejects_source_overwrite(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page()
        document.save(source)
    with pytest.raises(ValueError):
        arrange_pdf_pages(source, source, [0])


def test_crop_changes_saved_pdf_page_dimensions(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page(width=600, height=800)
        document.save(source)

    output = tmp_path / "cropped.pdf"
    apply_pdf_edits(source, output, [], [], crop_edits=[CropEdit(0, (100, 150, 500, 650))])

    with fitz.open(output) as document:
        assert document.page_count == 1
        assert math.isclose(document[0].rect.width, 400, abs_tol=0.5)
        assert math.isclose(document[0].rect.height, 500, abs_tol=0.5)


@pytest.mark.parametrize("paper", ("A3", "A4", "A5"))
def test_long_page_is_split_to_selected_paper_size(tmp_path, paper):
    source = tmp_path / "long.pdf"
    with fitz.open() as document:
        page = document.new_page(width=595.28, height=1683.78)
        page.insert_text((40, 60), "TOP")
        page.insert_text((40, 1640), "BOTTOM")
        document.save(source)

    output = tmp_path / f"split-{paper}.pdf"
    split_long_pdf_pages(source, output, paper)

    expected = {
        "A3": (841.89, 1190.55),
        "A4": (595.28, 841.89),
        "A5": (419.53, 595.28),
    }[paper]
    with fitz.open(output) as document:
        scale = expected[0] / 595.28
        expected_pages = math.ceil((1683.78 - 0.5) / (expected[1] / scale))
        assert document.page_count == expected_pages
        for page in document:
            assert math.isclose(page.rect.width, expected[0], abs_tol=0.7)
            assert math.isclose(page.rect.height, expected[1], abs_tol=0.7)


def test_delete_page_keeps_other_pages_and_rejects_last_page(tmp_path):
    source = tmp_path / "pages.pdf"
    with fitz.open() as document:
        document.new_page()
        document.new_page()
        document.save(source)
    output = tmp_path / "deleted.pdf"
    delete_pdf_page(source, output, 0)
    with fitz.open(output) as document:
        assert document.page_count == 1
    with pytest.raises(ValueError):
        delete_pdf_page(output, tmp_path / "empty.pdf", 0)


def test_drawing_colors_and_arrow_are_applied_to_raster(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (140, 90), "white").save(source)
    output = tmp_path / "drawn.png"
    apply_image_edits(
        source,
        output,
        [
            TextEdit(
                0,
                (10, 10, 70, 40),
                "X",
                12,
                color=(0, 0, 0),
                opaque_background=True,
                background_color=(1, 0, 0),
            )
        ],
        [],
        [],
        [StrokeEdit(0, ((15, 65), (120, 65)), 4, (0, 0, 1), style="arrow")],
    )
    with Image.open(output) as image:
        rgb = image.convert("RGB")
        assert rgb.getpixel((65, 35))[0] > 240
        assert rgb.getpixel((65, 65))[2] > 180
