from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zipfile import ZipFile
import threading
import time

import fitz
import pytest
from PIL import Image, ImageDraw

import redos_pdf_combiner.ocr as ocr_module

from redos_pdf_combiner.clipboard import decode_clipboard_payload, encode_clipboard_payload
from redos_pdf_combiner.ocr import (
    IndexedWord,
    PageText,
    enhance_ocr_image,
    ocr_models_available,
    search_text_index,
    select_text_in_rect,
    suppress_table_lines,
    build_text_index,
    recognize_word_region,
)
from redos_pdf_combiner.ocr_docx import page_text_blocks, source_to_ocr_docx


def test_editor_clipboard_payload_roundtrip():
    payload = {
        "version": 1,
        "kind": "text",
        "data": {"rect": [10, 20, 100, 40], "text": "Исправление"},
    }
    assert decode_clipboard_payload(encode_clipboard_payload(payload)) == payload
    assert decode_clipboard_payload("обычный текст") is None


def test_search_index_finds_case_insensitive_phrase_and_yo():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 70, 30), "Пример", 0, 0, 0),
            IndexedWord((75, 10, 120, 30), "Текста", 0, 0, 1),
            IndexedWord((10, 40, 60, 60), "Всё", 0, 1, 0),
        ),
    )
    phrase_hits = search_text_index([page], "ПРИМЕР текста")
    yo_hits = search_text_index([page], "все")
    assert len(phrase_hits) == 1
    assert phrase_hits[0].rect == (10, 10, 120, 30)
    assert len(yo_hits) == 1


def test_russian_and_english_ocr_models_are_packaged():
    assert ocr_models_available()
    assert ocr_models_available(language="rus")
    assert ocr_models_available(language="eng")


def test_word_region_uses_native_text_without_full_page_ocr(tmp_path):
    source = tmp_path / "native.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=595, height=842)
        page.insert_text((50, 80), "INSPECTION", fontsize=20)
        pdf.save(source)
    result = recognize_word_region(source, 0, (105, 73), language="eng")
    assert result.page_index == 0
    assert result.used_ocr is False
    assert [word.text for word in result.words] == ["INSPECTION"]


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_word_region_ocr_keeps_page_coordinates(tmp_path, rotation):
    source = tmp_path / "scanned.pdf"
    with fitz.open() as text_pdf:
        page = text_pdf.new_page(width=595, height=842)
        page.insert_text((50, 80), "INSPECTION", fontsize=20)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        with fitz.open() as scan_pdf:
            scanned_page = scan_pdf.new_page(width=595, height=842)
            scanned_page.insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pixmap)
            scanned_page.insert_text((50, 80), "WRONG", fontsize=20, render_mode=3)
            scanned_page.set_rotation(rotation)
            click = fitz.Point(105, 73) * scanned_page.rotation_matrix
            scan_pdf.save(source)
    result = recognize_word_region(source, 0, (click.x, click.y), language="eng")
    assert result.used_ocr is True
    assert any("INSPECTION" in word.text.upper() for word in result.words)
    assert any(word.rect[0] - 15 < click.x < word.rect[2] + 15
               and word.rect[1] - 15 < click.y < word.rect[3] + 15 for word in result.words)


def test_ocr_preprocessing_increases_contrast_and_keeps_size():
    image = Image.new("RGB", (240, 100), (226, 226, 220))
    draw = ImageDraw.Draw(image)
    draw.text((18, 35), "pdf bars 123", fill=(95, 95, 95))
    prepared, angle = enhance_ocr_image(image)
    assert prepared.mode == "L"
    assert prepared.size == image.size
    assert -3.75 <= angle <= 3.75
    assert angle * 4 == round(angle * 4)
    assert prepared.getextrema()[1] - prepared.getextrema()[0] > 100


def test_ocr_quality_prefers_readable_russian_text():
    readable = [(0, 0, 10, 10, word) for word in ("Пример", "текста", "служебная", "записка")]
    noise = [(0, 0, 10, 10, word) for word in ("|||", "@@@", "###", "_")]
    assert ocr_module._ocr_words_quality(readable, "rus") > ocr_module._ocr_words_quality(noise, "rus")


def test_prepared_ocr_preserves_physical_page_size(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ocr_module, "estimate_skew_angle", lambda image: 0.0)
    monkeypatch.setattr(ocr_module, "_needs_ocr_preparation", lambda *args: True)
    monkeypatch.setattr(ocr_module, "enhance_ocr_image", lambda image, angle: (image.copy(), angle))
    def recognize(page, **kwargs):
        calls.append((tuple(page.rect), kwargs["dpi"]))
        return [(10, 20, 100, 40, "Recognized", 0, 0, 0)]
    monkeypatch.setattr(ocr_module, "_locked_page_ocr_words", recognize)
    with fitz.open() as pdf:
        page = pdf.new_page(width=200, height=300)
        words = ocr_module._ocr_page_words(page, language="rus", dpi=350, tessdata=tmp_path, enhance=True)
    assert calls == [((0.0, 0.0, 200.0, 300.0), 350)] * 2
    assert words[0][:4] == (10, 20, 100, 40)


def test_workers_respect_available_memory(monkeypatch):
    monkeypatch.setattr(ocr_module, "_available_memory_bytes", lambda: 700 * 1024 ** 2)
    assert ocr_module._ocr_worker_count(3, 20, 12_000_000) == 1
    monkeypatch.setattr(ocr_module, "_available_memory_bytes", lambda: 8 * 1024 ** 3)
    assert ocr_module._ocr_worker_count(3, 20, 12_000_000) == 3
    assert ocr_module._ocr_worker_count(1, 20, 12_000_000) == 1


def test_default_fast_ocr_uses_three_workers_on_four_thread_host(tmp_path, monkeypatch):
    source = tmp_path / "parallel.pdf"
    with fitz.open() as document:
        for _ in range(4):
            document.new_page(width=200, height=200)
        document.save(source)
    requested = []
    monkeypatch.setattr(ocr_module, "ocr_models_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ocr_module.os, "cpu_count", lambda: 4)
    monkeypatch.setattr(ocr_module, "_available_memory_bytes", lambda: 8 * 1024 ** 3)
    monkeypatch.setattr(
        ocr_module,
        "_ocr_source_page",
        lambda _source, page_index, *_args: PageText(page_index, 200, 200, (), True),
    )
    def executor(max_workers, **_kwargs):
        requested.append(max_workers)
        return ThreadPoolExecutor(max_workers=max_workers)
    monkeypatch.setattr(ocr_module, "ProcessPoolExecutor", executor)

    build_text_index(source, force_ocr=True, language="rus", quality="fast")

    assert requested == [3]


def test_fast_ocr_keeps_readable_prepared_result_without_second_engine_pass(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ocr_module, "estimate_skew_angle", lambda _image: 0.0)
    monkeypatch.setattr(ocr_module, "_needs_ocr_preparation", lambda *_args: True)
    monkeypatch.setattr(ocr_module, "enhance_ocr_image", lambda image, angle: (image.copy(), angle))
    def recognize(_page, **_kwargs):
        calls.append(1)
        return [(10, 20, 120, 40, "Служебная", 0, 0, 0)]
    monkeypatch.setattr(ocr_module, "_locked_page_ocr_words", recognize)
    with fitz.open() as pdf:
        page = pdf.new_page(width=200, height=300)
        words = ocr_module._ocr_page_words(page, language="rus", dpi=220, tessdata=tmp_path, enhance=True, quality="fast")

    assert words[0][4] == "Служебная"
    assert calls == [1]


def test_rotated_native_text_index_uses_display_coordinates(tmp_path):
    source = tmp_path / "rotated-native.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=200)
        page.insert_text((20, 50), "This document contains sufficient searchable native text")
        page.set_rotation(270)
        expected = page.get_text("words")[0]
        expected_rect = tuple(fitz.Rect(expected[:4]) * page.rotation_matrix)
        pdf.save(source)
    index = build_text_index(source)
    assert not index[0].used_ocr
    assert index[0].words[0].rect == expected_rect


def test_ocr_preprocessing_removes_long_table_rules_only_from_ocr_copy():
    image = Image.new("L", (300, 180), 255)
    draw = ImageDraw.Draw(image)
    draw.line((10, 60, 290, 60), fill=0, width=2)
    draw.line((100, 10, 100, 170), fill=0, width=2)
    draw.text((120, 90), "TEXT", fill=0)

    cleaned = suppress_table_lines(image)

    assert cleaned.getpixel((50, 60)) == 255
    assert cleaned.getpixel((100, 40)) == 255
    assert image.getpixel((50, 60)) == 0


def test_single_header_rule_does_not_trigger_table_preprocessing():
    image = Image.new("L", (400, 240), 255)
    draw = ImageDraw.Draw(image)
    draw.line((20, 70, 380, 70), fill=0, width=3)
    draw.text((60, 110), "Обычный документ", fill=0)

    assert not ocr_module._needs_ocr_preparation(image, 0.0)

    draw.line((20, 150, 380, 150), fill=0, width=3)
    assert ocr_module._needs_ocr_preparation(image, 0.0)


def test_table_cleanup_preserves_dense_text_and_only_erases_rule_extent():
    image = Image.new("L", (600, 300), 255)
    draw = ImageDraw.Draw(image)
    for x in range(20, 560, 12):
        draw.rectangle((x, 30, x + 7, 42), fill=0)
    draw.line((20, 120, 340, 120), fill=0, width=2)
    draw.rectangle((420, 115, 430, 125), fill=0)
    cleaned = suppress_table_lines(image)
    assert cleaned.crop((0, 0, 600, 60)).tobytes() == image.crop((0, 0, 600, 60)).tobytes()
    assert cleaned.getpixel((100, 120)) == 255
    assert cleaned.getpixel((425, 120)) == 0


def test_ocr_supplement_never_overwrites_or_duplicates_preferred_words():
    primary = [(50, 20, 100, 32, "объекта", 0, 0, 0)]
    secondary = [(5, 20, 45, 32, "Название", 2, 1, 0),
                 (51, 19, 101, 33, "обьекта", 2, 1, 1)]
    result = ocr_module._supplement_ocr_words(primary, secondary, "rus")
    assert [word[4] for word in result] == ["Название", "объекта"]
    assert result[1][:5] == primary[0][:5]
    assert [word[5:] for word in result] == [(0, 0, 0), (0, 0, 1)]


def test_ocr_completeness_requires_agreement_and_good_text():
    reference = [(0, 0, 10, 10, token, 0, 0, index)
                 for index, token in enumerate(("Наименование", "объекта", "номер", "вагона", "партии"))]
    candidate = reference + [(20, 0, 80, 10, "Местонахождение", 0, 1, 0)]
    assert ocr_module._prefer_complete_ocr(candidate, reference, "rus")
    assert not ocr_module._prefer_complete_ocr(candidate[2:], reference, "rus")
    assert not ocr_module._prefer_complete_ocr(reference + [(0, 0, 10, 10, "||||||||", 0, 2, 0)], reference, "rus")


def test_select_recognized_text_preserves_line_order():
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 70, 30), "Первая", 0, 0, 0),
            IndexedWord((75, 10, 130, 30), "строка", 0, 0, 1),
            IndexedWord((10, 40, 70, 60), "Вторая", 0, 1, 0),
        ),
    )
    text, rectangles = select_text_in_rect(page, (5, 5, 140, 65))
    assert text == "Первая строка\nВторая"
    assert rectangles == ((10, 10, 70, 30), (75, 10, 130, 30), (10, 40, 70, 60))


def test_ocr_docx_contains_editable_russian_text(tmp_path):
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"source")
    page = PageText(
        0,
        595.0,
        842.0,
        (
            IndexedWord((10, 10, 70, 30), "Пример", 0, 0, 0),
            IndexedWord((75, 10, 120, 30), "Текста", 0, 0, 1),
            IndexedWord((10, 40, 80, 60), "Документ", 0, 1, 0),
        ),
        used_ocr=True,
    )
    output = source_to_ocr_docx(source, tmp_path / "recognized.docx", text_index=[page])
    assert page_text_blocks(page) == [["Пример Текста", "Документ"]]
    with ZipFile(output) as archive:
        assert "word/document.xml" in archive.namelist()
        document_xml = archive.read("word/document.xml").decode("utf-8")
        assert "Пример Текста" in document_xml
        assert "Документ" in document_xml
        assert 'w:type="page"' not in document_xml


def test_ocr_pages_run_in_parallel_and_return_in_page_order(tmp_path, monkeypatch):
    source = tmp_path / "pages.pdf"
    with fitz.open() as document:
        for _ in range(4):
            document.new_page(width=200, height=200)
        document.save(source)
    thread_names = set()
    lock = threading.Lock()

    monkeypatch.setattr(ocr_module, "ocr_models_available", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(ocr_module, "_available_memory_bytes", lambda: 8 * 1024 ** 3)

    def fake_ocr(_source, page_index, _language, _dpi, _model_dir, _enhance, _quality):
        with lock:
            thread_names.add(threading.current_thread().name)
        time.sleep(0.04)
        return PageText(page_index, 200, 200, (), True)

    monkeypatch.setattr(ocr_module, "_ocr_source_page", fake_ocr)
    # Exercise out-of-order completion without trying to pickle a test closure.
    monkeypatch.setattr(ocr_module, "ProcessPoolExecutor", lambda max_workers, **kwargs: ThreadPoolExecutor(max_workers=max_workers))
    pages = build_text_index(source, force_ocr=True, language="rus", workers=4)

    assert [page.page_index for page in pages] == [0, 1, 2, 3]
    assert len(thread_names) > 1


def test_ocr_subset_reads_only_requested_page(tmp_path, monkeypatch):
    source = tmp_path / "subset.pdf"
    with fitz.open() as document:
        for _ in range(5):
            document.new_page(width=200, height=200)
        document.save(source)
    calls = []
    def recognize(source, index, language, dpi, models, enhance, quality):
        calls.append((index, dpi, quality))
        return PageText(index, 200, 200, (), True)
    monkeypatch.setattr(ocr_module, "_ocr_source_page", recognize)
    monkeypatch.setattr(ocr_module, "ProcessPoolExecutor", lambda max_workers, **kwargs: ThreadPoolExecutor(max_workers=max_workers))
    result = build_text_index(source, force_ocr=True, language="rus", dpi=350, page_indexes=[3], quality="best")
    assert calls == [(3, 350, "best")]
    assert [page.page_index for page in result] == [3]


def test_real_ocr_spawn_processes(tmp_path):
    source = tmp_path / "parallel.pdf"
    with fitz.open() as document:
        for number in range(2):
            page = document.new_page(width=260, height=100)
            page.insert_text((15, 45), "pdf bars document sample", fontsize=15)
        document.save(source)
    result = build_text_index(source, force_ocr=True, language="eng", dpi=200,
                              workers=2, quality="fast", enhance=False)
    assert [page.page_index for page in result] == [0, 1]
    assert all(any("document" in word.text.lower() for word in page.words) for page in result)


def test_real_ocr_reads_upright_content_on_rotated_scan(tmp_path):
    source = tmp_path / "rotated-scan.pdf"
    with fitz.open() as original:
        page = original.new_page(width=260, height=100)
        page.insert_text((15, 45), "pdf bars document sample", fontsize=15)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(2, 2))
    with fitz.open() as pdf:
        page = pdf.new_page(width=100, height=260)
        page.set_rotation(270)
        page.insert_image(fitz.Rect(0, 0, 100, 260), pixmap=pixmap, rotate=270)
        pdf.save(source)
    result = build_text_index(source, force_ocr=True, language="eng", dpi=200, enhance=False)
    word = next(word for word in result[0].words if word.text.lower() == "document")
    assert 15 < word.rect[1] < 50
    assert word.rect[2] > word.rect[0]


def test_embedded_ocr_engine_is_never_entered_from_two_threads():
    state = {"active": 0, "maximum": 0}
    state_lock = threading.Lock()

    class FakePage:
        rotation = 0

        def get_textpage_ocr(self, **_kwargs):
            with state_lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.04)
            with state_lock:
                state["active"] -= 1
            return object()

        def get_text(self, _kind, *, textpage):
            assert textpage is not None
            return [(0, 0, 10, 10, "Текст", 0, 0, 0)]

    def recognize():
        return ocr_module._locked_page_ocr_words(
            FakePage(),
            language="rus",
            dpi=300,
            tessdata=Path("tessdata"),
        )

    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(lambda _index: recognize(), range(3)))

    assert state["maximum"] == 1
    assert all(result[0][4] == "Текст" for result in results)


def test_rotated_ocr_reuses_supplied_raster(monkeypatch):
    with fitz.open() as pdf:
        pixmap = pdf.new_page(width=100, height=200).get_pixmap()

    class RotatedPage:
        rotation = 90
        rect = fitz.Rect(0, 0, 100, 200)

        def get_pixmap(self, **kwargs):
            raise AssertionError("The page must not be rasterized twice")

    recognize = ocr_module._locked_page_ocr_words
    calls = []

    def upright(page, **kwargs):
        calls.append(page.rotation)
        return [(0, 0, 30, 10, "Document", 0, 0, 0)]

    monkeypatch.setattr(ocr_module, "_locked_page_ocr_words", upright)
    words = recognize(RotatedPage(), language="eng", dpi=350,
                      tessdata=Path("models"), rendered_pixmap=pixmap)
    assert words[0][4] == "Document"
    assert calls == [0]
