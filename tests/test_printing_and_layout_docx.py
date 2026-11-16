from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZipFile

from PIL import Image, ImageDraw

from redos_pdf_combiner import layout_ocr, printing
from redos_pdf_combiner.layout_docx import (
    DetectedTable,
    detect_layout_tables,
    _flow_paragraphs,
    _normalize_repeated_table_columns,
    _page_lines,
    render_clean_page_backgrounds,
    sanitize_layout_pages,
    write_layout_docx,
)
from redos_pdf_combiner.ocr import IndexedWord, PageText


ROOT = Path(__file__).resolve().parents[1]


def test_cups_print_job_uses_selected_options(tmp_path, monkeypatch):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    calls = []

    monkeypatch.setattr(printing.os, "name", "posix")
    monkeypatch.setattr(printing.shutil, "which", lambda name: "/usr/bin/lp" if name == "lp" else None)

    def fake_run(arguments, **kwargs):
        calls.append((list(arguments), kwargs))
        return SimpleNamespace(returncode=0, stdout="request id is Office-17\n", stderr="")

    monkeypatch.setattr(printing.subprocess, "run", fake_run)
    result = printing.submit_pdf_print_job(
        source,
        printing.PrintOptions(
            printer="Office",
            copies=2,
            page_range="1-3,5",
            duplex=True,
            grayscale=True,
        ),
    )

    command = calls[0][0]
    assert command[:3] == ["/usr/bin/lp", "-d", "Office"]
    assert ["-n", "2"] == command[3:5]
    assert "1-3,5" in command
    assert "sides=two-sided-long-edge" in command
    assert "print-color-mode=monochrome" in command
    assert result == "request id is Office-17"


def test_gui_exposes_printing_in_pdf_and_editor_toolbars():
    source = (ROOT / "src" / "redos_pdf_combiner" / "gui.py").read_text(encoding="utf-8")
    pdf_tab = source.split("    def _pdf_tab(", 1)[1].split("    def _editor_tab(", 1)[0]

    assert pdf_tab.count("ttk.Button(") == 4
    assert 'command=self._print_pdf_document' in pdf_tab
    assert 'image=self.pdf_toolbar_icons["print"]' in pdf_tab
    assert 'label="Печатать документ…"' in source
    assert 'label="Печатать выделенные страницы…"' in source
    assert 'text="Печать"' in source
    assert 'self.bind("<Control-p>", self._print_current_section)' in source
    assert "def _print_editor_document" in source


def test_layout_docx_contains_editable_text_and_clean_graphics_background(tmp_path):
    page = PageText(
        page_index=0,
        width=595.0,
        height=842.0,
        words=(
            IndexedWord((72.0, 95.0, 130.0, 112.0), "Пример", 0, 0, 0),
            IndexedWord((136.0, 95.0, 184.0, 112.0), "Текста", 0, 0, 1),
        ),
        used_ocr=True,
    )
    image = Image.new("RGB", (300, 424), "white")
    image_path = tmp_path / "background.jpg"
    image.save(image_path, "JPEG")
    image.close()
    output = write_layout_docx(
        tmp_path / "layout.docx",
        [page],
        [image_path.read_bytes()],
        title="Проверка",
        preserve_graphics=True,
    )

    with ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert "Пример Текста" in document
        assert "framePr" in document
        assert document.count("position:absolute") == 1
        assert "word/media/page-0001.jpg" in archive.namelist()
        assert "relationships/image" in relationships
        assert "w:pict" in document
        assert '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' in relationships
        assert "ns0:" not in relationships
        core_properties = archive.read("docProps/core.xml").decode("utf-8")
        assert 'xmlns:dcterms="http://purl.org/dc/terms/"' in core_properties
        assert 'xsi:type="dcterms:W3CDTF"' in core_properties


def test_layout_docx_default_mode_omits_page_graphics(tmp_path):
    page = PageText(
        0,
        595.0,
        842.0,
        (IndexedWord((180.0, 80.0, 415.0, 103.0), "ДОВЕРЕННОСТЬ", 0, 0, 0),),
        True,
    )
    image = Image.new("RGB", (300, 424), "white")
    stream = BytesIO()
    image.save(stream, "JPEG")
    image.close()

    output = write_layout_docx(tmp_path / "text-only.docx", [page], [stream.getvalue()], title="Text only")

    with ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")
        assert not any(name.startswith("word/media/") for name in archive.namelist())
        assert "relationships/image" not in relationships
        assert "<w:framePr" not in document
        assert '<w:jc w:val="center"' in document
        assert "<w:b" in document


def test_layout_docx_default_mode_joins_ocr_lines_into_flowing_paragraph(tmp_path):
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((72.0, 100.0, 145.0, 114.0), "Первая", 0, 0, 0),
            IndexedWord((151.0, 100.0, 205.0, 114.0), "строка", 0, 0, 1),
            IndexedWord((72.0, 118.0, 132.0, 132.0), "вторая", 0, 1, 0),
            IndexedWord((138.0, 118.0, 198.0, 132.0), "строка", 0, 1, 1),
            IndexedWord((72.0, 136.0, 128.0, 150.0), "третья", 0, 2, 0),
            IndexedWord((134.0, 136.0, 194.0, 150.0), "строка", 0, 2, 1),
        ),
        True,
    )
    image = Image.new("RGB", (300, 424), "white")
    stream = BytesIO()
    image.save(stream, "JPEG")
    image.close()

    output = write_layout_docx(tmp_path / "flowing.docx", [page], [stream.getvalue()], title="Flowing")

    with ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert "Первая строка вторая строка третья строка" in document
    assert "<w:framePr" not in document
    assert document.count("<w:p>") == 1


def test_layout_docx_keeps_individual_page_sizes(tmp_path):
    pages = [
        PageText(page_index=0, width=595.0, height=842.0, words=(), used_ocr=True),
        PageText(page_index=1, width=842.0, height=595.0, words=(), used_ocr=True),
    ]
    output = write_layout_docx(
        tmp_path / "mixed.docx",
        pages,
        [b"first-page", b"second-page"],
        title="Mixed sizes",
        preserve_graphics=True,
    )

    with ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")

    assert document.count("<w:sectPr") == 2
    assert 'w:w="11900" w:h="16840"' in document
    assert 'w:w="16840" w:h="11900"' in document
    assert 'w:val="nextPage"' in document


def test_layout_docx_default_mode_flows_across_source_pages_without_forced_breaks(tmp_path):
    pages = [
        PageText(0, 595.0, 842.0, (IndexedWord((72, 100, 160, 114), "Первая страница", 0, 0, 0),), True),
        PageText(1, 595.0, 842.0, (IndexedWord((72, 100, 160, 114), "Вторая страница", 0, 0, 0),), True),
        PageText(2, 595.0, 842.0, (IndexedWord((72, 100, 160, 114), "Третья страница", 0, 0, 0),), True),
    ]

    output = write_layout_docx(
        tmp_path / "continuous.docx",
        pages,
        [b"first-page", b"second-page", b"third-page"],
        title="Continuous",
    )

    with ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
    assert document.count("<w:sectPr") == 1
    assert 'w:val="nextPage"' not in document
    assert "Первая страница" in document
    assert "Вторая страница" in document
    assert "Третья страница" in document


def test_layout_docx_converts_ruled_area_to_editable_word_table(tmp_path):
    image = Image.new("RGB", (600, 800), "white")
    draw = ImageDraw.Draw(image)
    for y in (300, 400, 500, 600):
        draw.line((50, y, 550, y), fill="black", width=3)
    for x in (50, 250, 400, 550):
        draw.line((x, 300, x, 600), fill="black", width=3)
    stream = BytesIO()
    image.save(stream, "JPEG", quality=95)
    image.close()
    page = PageText(
        0,
        600.0,
        800.0,
        (
            IndexedWord((75, 325, 145, 345), "Header", 0, 0, 0),
            IndexedWord((275, 425, 345, 445), "Cell", 1, 0, 0),
        ),
        True,
    )

    output = write_layout_docx(tmp_path / "table.docx", [page], [stream.getvalue()], title="Table")

    with ZipFile(output) as archive:
        document = archive.read("word/document.xml").decode("utf-8")
        relationships = archive.read("word/_rels/document.xml.rels").decode("utf-8")
    assert "<w:tbl>" in document
    assert document.count("<w:gridCol") == 3
    assert document.count("<w:tr>") == 3
    assert "Header" in document and "Cell" in document
    assert "<w:framePr" not in document
    table_properties = document.split("<w:tblPr>", 1)[1].split("</w:tblPr>", 1)[0]
    assert "<w:tblpPr" not in table_properties
    assert table_properties.index("<w:tblInd") < table_properties.index("<w:tblW")
    assert "<w:trHeight" not in document
    assert table_properties.index("<w:tblBorders") < table_properties.index("<w:tblLayout")
    assert 'relationships/styles" Target="styles.xml"' in relationships
    assert 'relationships/settings" Target="settings.xml"' in relationships
    assert 'relationships/fontTable" Target="fontTable.xml"' in relationships


def test_repeated_table_columns_repair_false_and_missing_edges():
    pages = [PageText(index, 600.0, 800.0, (), True) for index in range(5)]
    common_y = (40.0, 140.0, 240.0)
    expected = (50.0, 100.0, 210.0, 330.0, 450.0, 550.0)
    tables = [
        [DetectedTable(expected, common_y)],
        [DetectedTable((51.0, 101.0, 211.0, 331.0, 451.0, 551.0), common_y)],
        [DetectedTable((49.0, 99.0, 209.0, 329.0, 449.0, 549.0), common_y)],
        [DetectedTable((50.0, 100.0, 210.0, 330.0, 390.0, 450.0, 550.0), common_y)],
        [DetectedTable((50.0, 100.0, 210.0, 330.0, 450.0), common_y)],
    ]

    normalized = _normalize_repeated_table_columns(pages, tables)

    for page_tables in normalized:
        assert len(page_tables[0].x_edges) == len(expected)
        assert all(abs(actual - wanted) <= 1.0 for actual, wanted in zip(page_tables[0].x_edges, expected))


def test_repeated_table_uses_reliable_two_column_page_for_noisy_continuation():
    pages = [PageText(index, 600.0, 800.0, (), True) for index in range(2)]
    y_edges = (30.0, 120.0, 760.0)
    reliable = DetectedTable((55.0, 220.0, 545.0), y_edges)
    noisy = DetectedTable((55.0, 220.0, 260.0, 405.0, 545.0), y_edges)

    normalized = _normalize_repeated_table_columns(pages, [[reliable], [noisy]])

    for page_tables in normalized:
        assert len(page_tables[0].x_edges) == len(reliable.x_edges)
        assert all(
            abs(actual - expected) < 0.01
            for actual, expected in zip(page_tables[0].x_edges, reliable.x_edges)
        )


def test_russian_layout_filter_discards_latin_table_noise():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 40, 25), "Текст", 0, 0, 0),
            IndexedWord((45, 10, 55, 25), "J", 0, 0, 1),
            IndexedWord((60, 10, 80, 25), "123", 0, 0, 2),
            IndexedWord((85, 10, 90, 25), "|", 0, 0, 3),
        ),
        True,
    )

    filtered = sanitize_layout_pages([page], "rus")[0]

    assert [word.text for word in filtered.words] == ["Текст", "123"]


def test_layout_filter_discards_overlapping_duplicate_words():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 80, 25), "Текст", 0, 0, 0),
            IndexedWord((11, 10, 81, 25), "Текст", 1, 0, 0),
            IndexedWord((90, 10, 130, 25), "рядом", 0, 0, 1),
        ),
        True,
    )

    filtered = sanitize_layout_pages([page], "rus")[0]

    assert [word.text for word in filtered.words] == ["Текст", "рядом"]


def test_layout_filter_discards_obvious_ocr_debris():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 55, 24), "Документ", 0, 0, 0),
            IndexedWord((60, 10, 65, 24), "x", 0, 0, 1),
            IndexedWord((70, 10, 100, 24), "@@@@A", 0, 0, 2),
            IndexedWord((105, 10, 130, 55), "ШУМ", 0, 0, 3),
        ),
        True,
    )

    filtered = sanitize_layout_pages([page], "rus+eng")[0]

    assert [word.text for word in filtered.words] == ["Документ"]


def test_layout_filter_keeps_single_letter_russian_words():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 20, 24), "В", 0, 0, 0),
            IndexedWord((25, 10, 35, 24), "с", 0, 0, 1),
            IndexedWord((40, 10, 50, 24), "и", 0, 0, 2),
        ),
        True,
    )

    filtered = sanitize_layout_pages([page], "rus")[0]

    assert [word.text for word in filtered.words] == ["В", "с", "и"]


def test_layout_filter_keeps_list_markers_and_discards_other_rule_noise():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 15, 24), "-", 0, 0, 0),
            IndexedWord((20, 10, 70, 24), "Пункт", 0, 0, 1),
            IndexedWord((75, 10, 80, 24), "|", 0, 0, 2),
        ),
        True,
    )

    filtered = sanitize_layout_pages([page], "rus")[0]

    assert [word.text for word in filtered.words] == ["-", "Пункт"]


def test_columnar_list_rows_become_separate_flow_paragraphs_with_tabs():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((90, 100, 150, 114), "Начальник", 0, 0, 0),
            IndexedWord((390, 100, 394, 114), "-", 0, 0, 1),
            IndexedWord((410, 100, 416, 114), "1", 0, 0, 2),
            IndexedWord((90, 118, 150, 132), "Геофизик", 0, 1, 0),
            IndexedWord((390, 118, 394, 132), "-", 0, 1, 1),
            IndexedWord((410, 118, 416, 132), "2", 0, 1, 2),
        ),
        True,
    )

    filtered = sanitize_layout_pages([page], "rus")[0]
    paragraphs = _flow_paragraphs(filtered, _page_lines(filtered))

    assert [paragraph.text for paragraph in paragraphs] == ["Начальник\t- 1", "Геофизик\t- 2"]
    assert all(paragraph.tab_stops for paragraph in paragraphs)


def test_positioned_text_lines_are_resolved_without_overlap():
    page = PageText(
        0,
        300.0,
        400.0,
        (
            IndexedWord((20, 30, 150, 45), "Первая длинная строка", 0, 0, 0),
            IndexedWord((25, 35, 155, 50), "Вторая длинная строка", 1, 0, 0),
            IndexedWord((30, 40, 160, 55), "Третья длинная строка", 2, 0, 0),
        ),
        True,
    )

    lines = _page_lines(page)

    for index, (_text, current, _font) in enumerate(lines):
        for _other_text, other, _other_font in lines[:index]:
            horizontal = max(0.0, min(current[2], other[2]) - max(current[0], other[0]))
            vertical = max(0.0, min(current[3], other[3]) - max(current[1], other[1]))
            assert not (horizontal > 0 and vertical > 0)


def test_fragmented_ocr_blocks_on_same_baseline_are_joined():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((72, 100, 120, 114), "Общество", 0, 0, 0),
            IndexedWord((126, 100, 205, 114), "ограниченной", 3, 4, 0),
            IndexedWord((211, 100, 315, 114), "ответственностью", 8, 1, 0),
        ),
        True,
    )

    lines = _page_lines(page)

    assert len(lines) == 1
    assert lines[0][0] == "Общество ограниченной ответственностью"


def test_tall_ocr_boxes_on_adjacent_rows_are_not_merged():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((70, 605, 160, 637), "Первая", 0, 0, 0),
            IndexedWord((166, 605, 250, 637), "строка", 0, 0, 1),
            IndexedWord((70, 617, 160, 650), "Вторая", 0, 1, 0),
            IndexedWord((166, 617, 250, 650), "строка", 0, 1, 1),
            IndexedWord((70, 631, 160, 663), "Третья", 0, 2, 0),
            IndexedWord((166, 631, 250, 663), "строка", 0, 2, 1),
        ),
        True,
    )

    lines = _page_lines(page)

    assert [line.text for line in lines] == [
        "Первая строка",
        "Вторая строка",
        "Третья строка",
    ]


def test_short_body_line_with_asymmetric_margins_stays_left_aligned():
    page = PageText(
        0,
        595.0,
        842.0,
        (IndexedWord((83, 550, 445, 565), "Короткая строка абзаца", 0, 0, 0),),
        True,
    )

    lines = _page_lines(page)

    assert lines[0].alignment == "left"


def test_documents_layout_ocr_forces_every_page(tmp_path, monkeypatch):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    page = PageText(0, 595.0, 842.0, (IndexedWord((10, 10, 40, 24), "Текст", 0, 0, 0),), True)
    calls = {}

    def fake_index(path, **kwargs):
        calls["index"] = (Path(path), kwargs)
        return [page]

    def fake_backgrounds(path, pages, **kwargs):
        calls["backgrounds"] = (Path(path), pages, kwargs)
        return [b"jpeg"]

    def fake_table_images(path, pages, **kwargs):
        calls["table_images"] = (Path(path), pages, kwargs)
        return [b"table-jpeg"]

    def fake_write(path, pages, backgrounds, **kwargs):
        Path(path).write_bytes(b"docx")
        calls["write"] = (pages, backgrounds, kwargs)
        return Path(path)

    monkeypatch.setattr(layout_ocr, "build_text_index", fake_index)
    monkeypatch.setattr(layout_ocr, "exclude_colored_ink_words", lambda _path, pages: pages)
    monkeypatch.setattr(layout_ocr, "render_table_detection_images", fake_table_images)
    monkeypatch.setattr(layout_ocr, "render_clean_page_backgrounds", fake_backgrounds)
    monkeypatch.setattr(layout_ocr, "write_layout_docx", fake_write)

    outputs = layout_ocr.pdfs_to_layout_ocr_docx([source], tmp_path / "out", dpi=220, language="rus+eng")

    assert calls["index"][1]["force_ocr"] is True
    assert calls["index"][1]["enhance"] is True
    assert calls["index"][1]["language"] == "rus+eng"
    assert calls["write"][1] == [b""]
    assert "backgrounds" not in calls
    assert calls["write"][2]["table_images"] == [b"table-jpeg"]
    assert outputs == [tmp_path / "out" / "scan_ocr.docx"]


def test_layout_ocr_can_skip_table_detection(tmp_path, monkeypatch):
    source, output = tmp_path / "scan.pdf", tmp_path / "text.docx"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    page = PageText(0, 595.0, 842.0, (IndexedWord((10, 10, 40, 24), "Текст", 0, 0, 0),), True)
    calls = {}

    monkeypatch.setattr(layout_ocr, "exclude_colored_ink_words", lambda _path, pages: pages)
    monkeypatch.setattr(
        layout_ocr,
        "render_table_detection_images",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("table analysis disabled")),
    )
    monkeypatch.setattr(layout_ocr, "render_clean_page_backgrounds", lambda *_args, **_kwargs: [b"jpeg"])

    def fake_write(path, pages, images, **kwargs):
        calls.update(kwargs)
        Path(path).write_bytes(b"docx")
        return Path(path)

    monkeypatch.setattr(layout_ocr, "write_layout_docx", fake_write)

    layout_ocr.source_to_layout_ocr_docx(source, output, text_index=[page], detect_tables=False)

    assert calls["detect_tables"] is False
    assert calls["table_images"] is None


def test_table_detector_prefers_continuous_grid_over_text_shaped_columns():
    page = PageText(0, 600.0, 800.0, (), True)
    image = Image.new("RGB", (900, 1200), "white")
    draw = ImageDraw.Draw(image)
    for x in (110, 370, 810):
        draw.line((x, 250, x, 1110), fill=(80, 80, 80), width=3)
    for y in (250, 360, 1110):
        draw.line((110, y, 810, y), fill=(80, 80, 80), width=3)
    # Dense printed characters can form a weak vertical candidate between
    # table borders, but must not become an invented Word column.
    for y in range(260, 1100, 22):
        draw.line((540, y, 540, y + 5), fill=(65, 65, 65), width=2)
    stream = BytesIO()
    image.save(stream, "JPEG", quality=92)
    image.close()

    tables = detect_layout_tables(page, stream.getvalue())

    assert len(tables) == 1
    assert len(tables[0].x_edges) == 3


def test_editor_layout_export_reuses_index_and_keeps_selected_output_name(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    output = tmp_path / "chosen-name.docx"
    page = PageText(0, 595.0, 842.0, (IndexedWord((10, 10, 70, 24), "Текст", 0, 0, 0),), True)
    calls = {}

    monkeypatch.setattr(layout_ocr, "build_text_index", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("OCR repeated")))
    monkeypatch.setattr(layout_ocr, "exclude_colored_ink_words", lambda _path, pages: pages)
    monkeypatch.setattr(layout_ocr, "render_table_detection_images", lambda _path, pages, **_kwargs: [b"table"] * len(pages))
    monkeypatch.setattr(layout_ocr, "render_clean_page_backgrounds", lambda _path, pages, **_kwargs: [b"jpeg"] * len(pages))

    def fake_write(path, pages, images, **kwargs):
        calls["write"] = (Path(path), pages, images, kwargs)
        Path(path).write_bytes(b"docx")
        return Path(path)

    monkeypatch.setattr(layout_ocr, "write_layout_docx", fake_write)

    result = layout_ocr.source_to_layout_ocr_docx(source, output, text_index=[page], language="rus")

    assert result == output
    assert calls["write"][0] == output
    assert calls["write"][1][0].words[0].text == "Текст"
    assert calls["write"][2] == [b""]
    assert calls["write"][3]["preserve_graphics"] is False
