from pathlib import Path
from types import SimpleNamespace
import xml.etree.ElementTree as ET

import fitz
import pytest
from PIL import Image, ImageChops, ImageDraw, ImageFont

from redos_pdf_combiner import converters
from redos_pdf_combiner.editor import (
    AlignmentEdit,
    RegionCopyEdit,
    SignatureEdit,
    StrokeEdit,
    TextEdit,
    apply_image_edits,
    apply_pdf_edits,
    available_cyrillic_fonts,
    import_signature,
    estimate_page_alignment,
    measure_editor_text,
    prepare_word_replacement,
    render_pdf_page_edits_to_image,
    resolve_font_file,
)
from redos_pdf_combiner.gui import PdfCombinerApp


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_rotated_page_edits_match_display_coordinates(tmp_path, rotation):
    source, output = tmp_path / "rotated.pdf", tmp_path / "saved.pdf"
    stamp = tmp_path / "stamp.png"
    image = Image.new("RGB", (40, 20), "red")
    ImageDraw.Draw(image).rectangle((0, 0, 19, 19), fill="blue")
    image.save(stamp)
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=300)
        page.set_rotation(rotation)
        pdf.save(source)
    apply_pdf_edits(source, output,
                    [TextEdit(0, (30, 100, 200, 140), "HELLO", 14)],
                    [SignatureEdit(0, (40, 40, 80, 60), str(stamp))],
                    stroke_edits=[StrokeEdit(0, ((150, 50),), 12, (0, 1, 0), "eraser")])
    with fitz.open(output) as pdf:
        page = pdf[0]
        pix = page.get_pixmap()
        rendered = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        assert rendered.getpixel((45, 50)) == (0, 0, 255)
        assert rendered.getpixel((75, 50)) == (255, 0, 0)
        assert rendered.getpixel((150, 50)) == (0, 255, 0)
        text_rect = page.search_for("HELLO")[0] * page.rotation_matrix
        assert abs(text_rect.x0 - 30) < 1
        assert 95 < text_rect.y0 < 110
        assert text_rect.width > text_rect.height


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_region_copy_and_crop(tmp_path, rotation):
    from redos_pdf_combiner.editor import CropEdit

    source, output = tmp_path / "source.pdf", tmp_path / "copy.pdf"
    stamp = Image.new("RGB", (20, 40), "white")
    ImageDraw.Draw(stamp).rectangle((0, 0, 8, 10), fill="red")
    image_path = tmp_path / "fragment.png"
    stamp.save(image_path)
    with fitz.open() as pdf:
        page = pdf.new_page(width=200, height=300)
        page.set_rotation(rotation)
        pdf.save(source)
    apply_pdf_edits(source, output, [], [SignatureEdit(0, (20, 20, 40, 60), str(image_path))])
    final = tmp_path / "cropped.pdf"
    apply_pdf_edits(output, final, [], [],
                    region_edits=[RegionCopyEdit(0, (20, 20, 40, 60), (80, 20, 100, 60))],
                    crop_edits=[CropEdit(0, (10, 10, 150, 100))])
    with fitz.open(final) as pdf:
        pix = pdf[0].get_pixmap()
        image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        assert image.size == (140, 90)
        assert image.getpixel((73, 13)) == (255, 0, 0)


def test_text_correction_and_signature_image(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        page = document.new_page(width=300, height=200)
        page.insert_text((40, 60), "teh contract", fontsize=14)
        document.save(source)

    image_path = tmp_path / "signature.jpg"
    image = Image.new("RGB", (240, 100), "white")
    draw = ImageDraw.Draw(image)
    draw.line((20, 70, 220, 30), fill="black", width=5)
    image.save(image_path)
    signature = import_signature(image_path, "Test signature", remove_background=True)
    with Image.open(signature) as stored:
        assert stored.mode == "RGBA"
        assert stored.getchannel("A").getextrema()[0] == 0

    with fitz.open(source) as document:
        rect = tuple(document[0].search_for("teh")[0])
    output = tmp_path / "edited.pdf"
    apply_pdf_edits(
        source,
        output,
        [TextEdit(0, rect, "the", 14, opaque_background=True)],
        [SignatureEdit(0, (120, 110, 260, 175), str(signature))],
    )

    assert output.exists()
    with fitz.open(output) as document:
        page = document[0]
        assert not page.search_for("teh")
        assert page.search_for("the")
        assert not page.search_for("Визуальная подпись")
        assert page.first_annot is None
        assert page.get_images(full=True)


def test_pdf_save_replaces_existing_destination_without_touching_source(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    with fitz.open() as document:
        document.new_page().insert_text((40, 60), "SOURCE")
        document.save(source)
    source_before = source.read_bytes()
    output.write_bytes(b"previous incomplete result")

    apply_pdf_edits(source, output, [TextEdit(0, (40, 80, 180, 105), "EDITED", 12)], [])

    assert source.read_bytes() == source_before
    with fitz.open(output) as document:
        assert "SOURCE" in document[0].get_text()
        assert "EDITED" in document[0].get_text()


def test_pdf_save_rejects_normalized_source_destination(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page()
        document.save(source)

    with pytest.raises(ValueError, match="новым именем"):
        apply_pdf_edits(source, tmp_path / "." / "source.pdf", [], [])


def test_signature_import_removes_tinted_background_and_crops(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    source = tmp_path / "signature_tinted.png"
    image = Image.new("RGB", (500, 220), (232, 224, 208))
    draw = ImageDraw.Draw(image)
    draw.line((90, 145, 225, 70, 410, 130), fill=(18, 28, 65), width=9)
    image.save(source)

    signature = import_signature(source, "Tinted signature", remove_background=True)

    with Image.open(signature) as stored:
        alpha = stored.getchannel("A")
        assert stored.width < image.width
        assert stored.height < image.height
        assert alpha.getextrema()[0] == 0
        assert alpha.getextrema()[1] > 240
        transparent = sum(value == 0 for value in alpha.getdata())
        visible = sum(value > 200 for value in alpha.getdata())
        assert transparent > visible
        assert visible > 100


def test_pdf_to_docx_uses_r7_x2t(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        Path(command[2]).write_bytes(b"docx")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(converters.sys, "platform", "linux")
    monkeypatch.setattr(converters, "find_r7_converter", lambda: "/opt/r7-office/desktopeditors/converter/x2t")
    monkeypatch.setattr(converters, "_font_family_installed", lambda _family: True)
    monkeypatch.setattr(converters.subprocess, "run", fake_run)
    outputs = converters.pdf_to_docx([source], tmp_path / "out")

    assert outputs == [tmp_path / "out" / "source.docx"]
    assert calls[0][0].endswith("/x2t")
    assert calls[0][1:] == [str(source), str(outputs[0])]


def test_document_conversion_never_overwrites_existing_result(tmp_path, monkeypatch):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")
    existing = tmp_path / "out" / "source.docx"
    existing.parent.mkdir()
    existing.write_bytes(b"original result")

    def fake_run(command, **kwargs):
        Path(command[2]).write_bytes(b"new result")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(converters.sys, "platform", "linux")
    monkeypatch.setattr(converters, "find_r7_converter", lambda: "/opt/r7-office/desktopeditors/converter/x2t")
    monkeypatch.setattr(converters.subprocess, "run", fake_run)
    outputs = converters.pdf_to_docx([source], existing.parent)

    assert existing.read_bytes() == b"original result"
    assert outputs == [existing.parent / "source (2).docx"]
    assert outputs[0].read_bytes() == b"new result"


def test_r7_uses_user_font_cache_and_xml_task_for_docx_pdf(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    font_cache = tmp_path / "r7-data" / "fonts" / "AllFonts.js"
    font_cache.parent.mkdir(parents=True)
    font_cache.write_text("window.g_fonts_selection_bin=[];", encoding="utf-8")
    captured = {}

    def fake_run(command, **kwargs):
        assert len(command) == 2
        root = ET.parse(command[1]).getroot()
        captured["task"] = {child.tag: child.text for child in root}
        Path(captured["task"]["m_sFileTo"]).write_bytes(b"pdf")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(converters.sys, "platform", "linux")
    monkeypatch.setattr(converters, "find_r7_converter", lambda: "/opt/r7-office/desktopeditors/converter/x2t")
    monkeypatch.setattr(converters, "find_r7_font_cache", lambda: font_cache)
    monkeypatch.setattr(converters, "_font_family_installed", lambda _family: False)
    monkeypatch.setattr(converters.subprocess, "run", fake_run)

    outputs = converters.docx_to_pdf([source], tmp_path / "out")

    assert outputs[0].is_file()
    assert captured["task"]["m_sFileFrom"] == str(source.resolve())
    assert captured["task"]["m_nFormatTo"] == "513"
    assert captured["task"]["m_sAllFontsPath"] == str(font_cache.resolve())
    assert captured["task"]["m_sFontDir"] == str(font_cache.parent.resolve())


def test_r7_docx_pdf_reports_uninitialized_font_cache(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    monkeypatch.setattr(converters.sys, "platform", "linux")
    monkeypatch.setattr(converters, "find_r7_converter", lambda: "/opt/r7-office/desktopeditors/converter/x2t")
    monkeypatch.setattr(converters, "find_r7_font_cache", lambda: None)

    try:
        converters.docx_to_pdf([source], tmp_path / "out")
    except RuntimeError as exc:
        assert "базу шрифтов" in str(exc)
        assert "R7-Office" in str(exc)
    else:
        raise AssertionError("Expected missing R7 font cache error")


def test_windows_document_conversion_uses_microsoft_word(tmp_path, monkeypatch):
    source = tmp_path / "source.docx"
    source.write_bytes(b"docx")
    calls = []

    def fake_run(command, **kwargs):
        calls.append(command)
        output = Path(command[command.index("-OutputPath") + 1])
        output.write_bytes(b"pdf")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(converters.sys, "platform", "win32")
    monkeypatch.setattr(converters, "find_microsoft_word", lambda: r"C:\Program Files\Microsoft Office\WINWORD.EXE")
    monkeypatch.setattr(converters, "_powershell_executable", lambda: "powershell.exe")
    monkeypatch.setattr(converters.subprocess, "run", fake_run)

    outputs = converters.docx_to_pdf([source], tmp_path / "out")

    assert outputs == [tmp_path / "out" / "source.pdf"]
    assert calls[0][0] == "powershell.exe"
    assert calls[0][calls[0].index("-Target") + 1] == "pdf"


def test_copy_page_region_without_visible_marker(tmp_path):
    source = tmp_path / "drawing.pdf"
    with fitz.open() as document:
        page = document.new_page(width=300, height=220)
        page.draw_rect(fitz.Rect(30, 30, 100, 100), fill=(0, 0, 1))
        document.save(source)

    output = tmp_path / "copied.pdf"
    region = RegionCopyEdit(0, (25, 25, 105, 105), (170, 110, 250, 190))
    apply_pdf_edits(source, output, [], [], [region])

    with fitz.open(output) as document:
        page = document[0]
        assert page.first_annot is None
        assert not page.search_for("Копия области")
        pixmap = page.get_pixmap(colorspace=fitz.csRGB, clip=fitz.Rect(190, 130, 210, 150))
        pixels = list(zip(*(iter(pixmap.samples),) * 3))
        assert any(blue > 150 and red < 100 for red, _green, blue in pixels)


def test_pasted_region_fills_target_rect_without_shrinking(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page(width=120, height=100)
        document.save(source)
    pasted = tmp_path / "copied-region.png"
    Image.new("RGB", (80, 20), (0, 180, 0)).save(pasted)
    output = tmp_path / "pasted.pdf"

    apply_pdf_edits(
        source,
        output,
        [],
        [SignatureEdit(0, (20, 20, 80, 80), str(pasted), keep_proportion=False)],
    )

    with fitz.open(output) as document:
        pixmap = document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    red, green, blue = image.getpixel((42, 42))
    image.close()
    assert green > 140 and red < 80 and blue < 80


def test_closing_area_stays_below_pasted_region(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (100, 80), "blue").save(source)
    pasted = tmp_path / "region.png"
    Image.new("RGB", (30, 10), "green").save(pasted)
    output = tmp_path / "layered.png"

    apply_image_edits(
        source,
        output,
        [TextEdit(0, (20, 20, 70, 60), "", opaque_background=True, background_color=(1.0, 1.0, 1.0))],
        [SignatureEdit(0, (20, 20, 70, 60), str(pasted), keep_proportion=False)],
    )

    with Image.open(output) as result:
        red, green, blue = result.convert("RGB").getpixel((22, 22))
    assert green > 80 and red < 80 and blue < 80


def test_eraser_uses_square_brush_tip(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (100, 80), "blue").save(source)
    output = tmp_path / "erased.png"

    apply_image_edits(
        source,
        output,
        [],
        [],
        stroke_edits=[StrokeEdit(0, ((50, 40),), 20, (1.0, 1.0, 1.0), style="eraser")],
    )

    with Image.open(output) as result:
        rgb = result.convert("RGB")
        assert rgb.getpixel((41, 31)) == (255, 255, 255)
        assert rgb.getpixel((38, 28))[2] > 150


def test_image_visual_edits_save_as_raster(tmp_path):
    source = tmp_path / "source.png"
    image = Image.new("RGB", (100, 80), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((10, 10, 30, 30), fill="blue")
    image.save(source)

    output = tmp_path / "edited.png"
    apply_image_edits(
        source,
        output,
        [],
        [],
        [RegionCopyEdit(0, (10, 10, 30, 30), (35, 35, 55, 55))],
        [
            StrokeEdit(0, ((5, 5), (50, 5)), 4, (0, 0, 0)),
            StrokeEdit(0, ((5, 5), (50, 5)), 10, (1, 1, 1)),
        ],
        (0, 0, 60, 60),
    )

    with Image.open(output) as result:
        assert result.size == (60, 60)
        red, green, blue = result.convert("RGB").getpixel((45, 45))
        assert blue > 150 and red < 100 and green < 150
        assert result.convert("RGB").getpixel((25, 5)) == (255, 255, 255)


def test_transparent_text_is_drawn_above_eraser(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (120, 80), "blue").save(source)
    output = tmp_path / "edited.png"
    font_file = resolve_font_file("DejaVu Sans") or resolve_font_file("Arial")

    apply_image_edits(
        source,
        output,
        [TextEdit(0, (10, 5, 115, 45), "ТЕКСТ", 30, "DejaVu Sans", font_file, (1.0, 0.0, 0.0))],
        [],
        [],
        [StrokeEdit(0, ((0, 20), (120, 20)), 20, (1.0, 1.0, 1.0))],
    )

    with Image.open(output) as result:
        rgb = result.convert("RGB")
        assert rgb.getpixel((110, 40))[2] > 150
        assert rgb.getpixel((110, 20)) == (255, 255, 255)
        red_pixels = 0
        for red, green, blue in rgb.crop((5, 5, 110, 35)).getdata():
            if red > 150 and green < 100 and blue < 100:
                red_pixels += 1
        assert red_pixels > 10


def test_editor_mousewheel_zoom_supports_windows_and_x11():
    zoom_steps = []
    app = SimpleNamespace(_change_editor_zoom=zoom_steps.append)

    assert PdfCombinerApp._editor_mousewheel_zoom(app, SimpleNamespace(num=0, delta=120)) == "break"
    assert PdfCombinerApp._editor_mousewheel_zoom(app, SimpleNamespace(num=0, delta=-120)) == "break"
    assert PdfCombinerApp._editor_mousewheel_zoom(app, SimpleNamespace(num=4, delta=0)) == "break"
    assert PdfCombinerApp._editor_mousewheel_zoom(app, SimpleNamespace(num=5, delta=0)) == "break"
    assert zoom_steps == [10, -10, 10, -10]


def test_times_new_roman_is_the_default_editor_font():
    assert TextEdit(0, (0, 0, 20, 20), "Текст").font_family == "Times New Roman"
    assert available_cyrillic_fonts()[0] == "Times New Roman"
    assert resolve_font_file("Times New Roman")


def test_page_alignment_preserves_pdf_text_layer(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        page = document.new_page(width=240, height=160)
        page.insert_text((30, 45), "Alignment test", fontsize=14)
        document.save(source)

    output = tmp_path / "aligned.pdf"
    apply_pdf_edits(source, output, [], [], alignment_edits=[AlignmentEdit(0, 4.0)])

    with fitz.open(output) as document:
        assert document[0].search_for("Alignment test")
        assert document[0].rect == fitz.Rect(0, 0, 240, 160)


def test_alignment_detection_and_raster_rotation(tmp_path):
    source = tmp_path / "tilted.png"
    base = Image.new("RGB", (600, 400), "white")
    draw = ImageDraw.Draw(base)
    for y in range(100, 310, 35):
        draw.rectangle((90, y, 510, y + 7), fill="black")
    tilted = base.rotate(3.0, resample=Image.Resampling.BICUBIC, fillcolor="white")
    tilted.save(source)
    base.close()
    tilted.close()

    detected = estimate_page_alignment(source)
    assert -4.0 <= detected <= -2.0

    output = tmp_path / "straightened.png"
    apply_image_edits(source, output, [], [], alignment_edits=[AlignmentEdit(0, detected)])
    assert abs(estimate_page_alignment(output)) <= 0.5


def test_bold_italic_and_underlined_text_is_saved_to_image(tmp_path):
    source = tmp_path / "source.png"
    Image.new("RGB", (260, 100), "white").save(source)
    output = tmp_path / "styled.png"
    font_file = resolve_font_file("DejaVu Sans", bold=True, italic=True)
    edit = TextEdit(
        0,
        (15, 15, 245, 75),
        "Текст",
        30,
        "DejaVu Sans",
        font_file,
        (0.0, 0.0, 0.0),
        bold=True,
        italic=True,
        underline=True,
    )

    apply_image_edits(source, output, [edit], [])

    with Image.open(output) as result:
        gray = result.convert("L")
        dark_by_row = [sum(pixel < 80 for pixel in gray.crop((10, y, 250, y + 1)).getdata()) for y in range(100)]
        assert max(dark_by_row[42:70]) >= 20


def test_pdf_text_stays_at_top_left_when_editor_frame_is_resized(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page(width=420, height=300)
        document.save(source)
    output = tmp_path / "edited.pdf"
    font_file = resolve_font_file("Times New Roman")
    edits = [
        TextEdit(0, (40, 55, 190, 85), "Короткая рамка", 18, font_file=font_file),
        TextEdit(0, (220, 55, 390, 210), "Высокая рамка", 18, font_file=font_file),
    ]

    apply_pdf_edits(source, output, edits, [])

    with fitz.open(output) as document:
        spans = [
            span
            for block in document[0].get_text("dict")["blocks"]
            for line in block.get("lines", [])
            for span in line.get("spans", [])
        ]
    assert len(spans) == 2
    assert abs(spans[0]["bbox"][1] - 55.0) < 1.0
    assert abs(spans[1]["bbox"][1] - 55.0) < 1.0


def test_saved_pdf_text_matches_flat_editor_preview(tmp_path):
    source = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page(width=420, height=300)
        document.save(source)
    edit = TextEdit(
        0,
        (40, 55, 350, 180),
        "Проверка текста\nВторая строка",
        18,
        font_file=resolve_font_file("Times New Roman"),
    )
    edited_pdf = tmp_path / "edited.pdf"
    flat_preview = tmp_path / "preview.png"
    rendered_pdf = tmp_path / "rendered.png"

    apply_pdf_edits(source, edited_pdf, [edit], [])
    render_pdf_page_edits_to_image(source, flat_preview, 0, [edit], [], dpi=144)
    with fitz.open(edited_pdf) as document:
        document[0].get_pixmap(matrix=fitz.Matrix(2, 2), alpha=False).save(rendered_pdf)

    boxes = []
    for path in (flat_preview, rendered_pdf):
        with Image.open(path) as image:
            boxes.append(ImageChops.invert(image.convert("L")).getbbox())
    preview_box, pdf_box = boxes
    assert preview_box is not None and pdf_box is not None
    assert abs(preview_box[0] - pdf_box[0]) <= 1
    assert abs(preview_box[1] - pdf_box[1]) <= 2
    assert abs(preview_box[2] - pdf_box[2]) <= 4
    assert abs(preview_box[3] - pdf_box[3]) <= 6


def test_scanned_word_replacement_restores_background_and_draws_new_text(tmp_path):
    source = tmp_path / "scan.png"
    image = Image.new("RGB", (420, 150), (243, 241, 236))
    pixels = image.load()
    for y in range(image.height):
        for x in range(image.width):
            variation = ((x * 7 + y * 3) % 5) - 2
            pixels[x, y] = tuple(max(0, min(255, value + variation)) for value in pixels[x, y])
    font_file = resolve_font_file("Times New Roman")
    font = ImageFont.truetype(font_file, 36) if font_file else ImageFont.load_default()
    draw = ImageDraw.Draw(image)
    origin = (70, 52)
    original_text = "ОПЕЧАТКА"
    draw.text(origin, original_text, font=font, fill=(25, 25, 25))
    text_box = draw.textbbox(origin, original_text, font=font)
    image.save(source)
    image.close()

    background = tmp_path / "word-background.png"
    prepared = prepare_word_replacement(
        source,
        background,
        0,
        tuple(float(value) for value in text_box),
        original_text,
    )
    replacement = TextEdit(
        page_index=0,
        rect=(float(text_box[0]), float(text_box[1]), float(text_box[2]), float(text_box[3])),
        text="ТЕКСТ",
        font_size=prepared.font_size,
        font_family=prepared.font_family,
        font_file=prepared.font_file,
        color=prepared.color,
        bold=prepared.bold,
        italic=prepared.italic,
        replacement_background=prepared.background_path,
        replacement_background_rect=prepared.background_rect,
        replacement_source_rect=prepared.source_rect,
        original_text=original_text,
        style_confidence=prepared.confidence,
    )
    output = tmp_path / "replaced.png"
    apply_image_edits(source, output, [replacement], [])

    with Image.open(background) as patch_image:
        alpha = patch_image.convert("RGBA").getchannel("A")
        assert alpha.getextrema() == (0, 255)
    with Image.open(output) as result:
        gray = result.convert("L")
        right_tail = gray.crop((text_box[0] + int((text_box[2] - text_box[0]) * 0.72), text_box[1], text_box[2], text_box[3]))
        assert min(right_tail.getdata()) > 150
        replacement_area = gray.crop((text_box[0], text_box[1], text_box[2], text_box[3]))
        assert min(replacement_area.getdata()) < 80


def test_scanned_word_replacement_prefers_times_style_and_removes_edge_artifacts(tmp_path):
    source = tmp_path / "scan-with-edge-artifact.png"
    image = Image.new("RGB", (520, 170), (246, 245, 242))
    draw = ImageDraw.Draw(image)
    font_file = resolve_font_file("Times New Roman", bold=True, italic=True)
    assert font_file is not None
    font = ImageFont.truetype(font_file, 34)
    original_text = "Заявлен"
    origin = (80, 60)
    text_box = draw.textbbox(origin, original_text, font=font)
    draw.text(origin, original_text, font=font, fill=(18, 18, 18))
    tight_box = (text_box[0], text_box[1], text_box[2] - 2, text_box[3])
    artifact_x = tight_box[2] + 2
    artifact_y = tight_box[1] + 9
    draw.rectangle((artifact_x, artifact_y, artifact_x + 2, artifact_y + 2), fill=(45, 45, 45))
    image.save(source)
    image.close()

    prepared = prepare_word_replacement(
        source,
        tmp_path / "word-background.png",
        0,
        tuple(float(value) for value in tight_box),
        original_text,
    )

    assert prepared.font_family == "Times New Roman"
    assert prepared.bold is True
    assert prepared.italic is True
    assert abs(prepared.font_size - 34.0) <= 2.0
    assert prepared.color == (0.0, 0.0, 0.0)
    assert prepared.scan_blur > 0
    with Image.open(prepared.background_path) as overlay:
        alpha = overlay.getchannel("A")
        local_x = int(round(artifact_x - prepared.background_rect[0]))
        local_y = int(round(artifact_y - prepared.background_rect[1]))
        assert alpha.getpixel((local_x, local_y)) > 0


def test_word_background_removes_truncated_glyph_without_touching_next_word():
    from redos_pdf_combiner.editor import _word_background_overlay

    image = Image.new("RGB", (460, 120), (247, 246, 243))
    draw = ImageDraw.Draw(image)
    font_file = resolve_font_file("Times New Roman")
    assert font_file is not None
    font = ImageFont.truetype(font_file, 36)
    origin = (50, 35)
    old_box = draw.textbbox(origin, "Осмотра", font=font)
    draw.text(origin, "Осмотра", font=font, fill=(20, 20, 20))
    next_box = draw.textbbox((old_box[2] + 12, origin[1]), "лес", font=font)
    draw.text((old_box[2] + 12, origin[1]), "лес", font=font, fill=(20, 20, 20))
    # Tesseract occasionally loses the last part of the selected word.
    tight_box = (old_box[0], old_box[1], old_box[2] - 7, old_box[3])

    _overlay, mask, _color, _background = _word_background_overlay(image, tight_box)

    suffix = mask.crop((old_box[2] - 6, old_box[1], old_box[2], old_box[3]))
    next_word = mask.crop(next_box)
    assert max(suffix.getdata()) > 0
    assert next_word.getbbox() is None
    image.close()
    _overlay.close()
    mask.close()


def test_scanned_word_blur_keeps_pdf_text_selectable(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    with fitz.open() as document:
        document.new_page(width=300, height=160)
        document.save(source)
    font_file = resolve_font_file("Times New Roman", bold=True)
    edit = TextEdit(
        page_index=0,
        rect=(42.0, 48.0, 180.0, 80.0),
        text="Заявлен",
        font_size=22.0,
        font_family="Times New Roman",
        font_file=font_file,
        color=(0.0, 0.0, 0.0),
        bold=True,
        scan_blur=0.4,
    )

    apply_pdf_edits(source, output, [edit], [])

    with fitz.open(output) as document:
        page = document[0]
        assert "Заявлен" in page.get_text()
        assert page.get_images(full=True)


def test_scanned_word_effect_can_be_disabled_without_raster_overlay(tmp_path):
    source = tmp_path / "source.pdf"
    output = tmp_path / "output.pdf"
    with fitz.open() as document:
        document.new_page(width=300, height=160)
        document.save(source)
    edit = TextEdit(
        page_index=0,
        rect=(42.0, 48.0, 180.0, 80.0),
        text="Заявлен",
        font_size=22.0,
        font_family="Times New Roman",
        font_file=resolve_font_file("Times New Roman", bold=True),
        color=(0.0, 0.0, 0.0),
        bold=True,
        replacement_source_rect=(42.0, 48.0, 180.0, 80.0),
        scan_blur=0.4,
        scan_effect=False,
    )

    apply_pdf_edits(source, output, [edit], [])

    with fitz.open(output) as document:
        page = document[0]
        assert "Заявлен" in page.get_text()
        assert not page.get_images(full=True)


def test_editor_text_measurement_distinguishes_word_lengths():
    short = measure_editor_text("акт", "Times New Roman", 18)
    long = measure_editor_text("документация", "Times New Roman", 18)
    assert long > short * 2


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
@pytest.mark.parametrize("replacement", [True, False])
@pytest.mark.parametrize("underline", [True, False])
@pytest.mark.parametrize("scan_effect", [True, False])
@pytest.mark.parametrize("font_size", [11.4, 24])
def test_replacement_pdf_has_one_visible_layer_matching_overlay(tmp_path, rotation, replacement, underline, scan_effect, font_size):
    from redos_pdf_combiner.editor import word_text_overlay
    from PIL import ImageStat

    source, output = tmp_path / "source.pdf", tmp_path / "result.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=250, height=250)
        page.set_rotation(rotation)
        pdf.save(source)
    edit = TextEdit(0, (40, 50, 160, 90), "СП-4", font_size, bold=True,
                    font_file=resolve_font_file("Times New Roman", bold=True),
                    replacement_source_rect=(40, 50, 150, 90) if replacement else None,
                    underline=underline, scan_blur=0.3, scan_effect=scan_effect)
    overlay, rect = word_text_overlay(edit)
    expected = Image.new("RGBA", (1000, 1000), "white")
    expected.alpha_composite(overlay, (round(rect[0]*4), round(rect[1]*4)))
    apply_pdf_edits(source, output, [edit], [])
    with fitz.open(output) as pdf:
        page = pdf[0]
        pix = page.get_pixmap(matrix=fitz.Matrix(4, 4))
        actual = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        assert all(span["type"] == (0 if replacement and not scan_effect else 3)
                   for span in page.get_texttrace())
        assert "СП-4" in page.get_text().replace("\u00ad", "-")
    difference = ImageChops.difference(actual, expected.convert("RGB"))
    assert max(ImageStat.Stat(difference).mean) < 0.1
    expected_ink = expected.convert("L").point(lambda pixel: 255 if pixel < 200 else 0)
    actual_ink = actual.convert("L").point(lambda pixel: 255 if pixel < 200 else 0)
    expected_bounds, actual_bounds = expected_ink.getbbox(), actual_ink.getbbox()
    assert expected_bounds and actual_bounds
    assert max(abs(a-b) for a, b in zip(expected_bounds, actual_bounds)) <= 1
    expected_area = expected_ink.histogram()[255]
    actual_area = actual_ink.histogram()[255]
    assert abs(actual_area - expected_area) / expected_area < 0.05


def test_word_background_keeps_real_table_rules():
    from redos_pdf_combiner.editor import _word_background_overlay

    image = Image.new("RGB", (160, 90), "white")
    draw = ImageDraw.Draw(image)
    draw.line((0, 35, 159, 35), fill="black")
    draw.line((65, 0, 65, 89), fill="black")
    draw.rectangle((35, 20, 45, 28), fill="black")
    overlay, mask, _, _ = _word_background_overlay(image, (25, 15, 120, 65))
    alpha = overlay.getchannel("A")
    assert alpha.getpixel((65, 25)) == 0
    assert alpha.getpixel((40, 35)) == 0
    assert alpha.getpixel((40, 24)) == 255
    image.close()
    overlay.close()
    mask.close()
    alpha.close()


def test_word_background_does_not_preserve_dense_glyphs_as_a_rule():
    from redos_pdf_combiner.editor import _word_background_overlay

    image = Image.new("RGB", (160, 90), "white")
    draw = ImageDraw.Draw(image)
    for x in range(25, 118, 10):
        draw.rectangle((x, 30, x + 6, 38), fill="black")
    draw.rectangle((122, 32, 124, 34), fill="black")
    overlay, mask, _, _ = _word_background_overlay(image, (25, 20, 120, 50))
    alpha = overlay.getchannel("A")
    assert alpha.getpixel((122, 33)) == 255
    assert alpha.getpixel((40, 33)) == 255
    image.close()
    overlay.close()
    mask.close()
    alpha.close()


def test_word_background_keeps_paper_rgb_under_transparent_mask(tmp_path):
    from redos_pdf_combiner.editor import _word_background_overlay

    scan = Image.new("RGB", (200, 80), (248, 247, 245))
    ImageDraw.Draw(scan).text((30, 20), "OLD", fill="black")
    patch, mask, color, background = _word_background_overlay(scan, (25, 15, 100, 45))
    assert patch.getpixel((0, 0)) == (248, 247, 245, 0)
    assert patch.getchannel("A").getextrema() == (0, 255)


def test_signature_strengthening_and_transparent_placement(tmp_path, monkeypatch):
    from redos_pdf_combiner.editor import prepare_signature_for_placement

    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    source = tmp_path / "pale.png"
    image = Image.new("RGB", (160, 70), "white")
    ImageDraw.Draw(image).line((20, 50, 130, 15), fill=(200, 205, 230), width=3)
    image.save(source)
    normal = import_signature(source, "normal", enhance=False)
    strong = import_signature(source, "strong", enhance=True)
    with Image.open(normal) as a, Image.open(strong) as b:
        white_a = Image.new("RGBA", a.size, "white")
        white_b = Image.new("RGBA", b.size, "white")
        white_a.alpha_composite(a.convert("RGBA"))
        white_b.alpha_composite(b.convert("RGBA"))
        assert min(white_b.convert("L").getdata()) < min(white_a.convert("L").getdata())
    placed = prepare_signature_for_placement(source, tmp_path / "placed.png")
    with Image.open(placed) as image:
        assert image.getpixel((0, 0))[3] == 0
    placed = prepare_signature_for_placement(strong, tmp_path / "placed-strong.png")
    with Image.open(placed) as image, Image.open(strong) as original:
        assert image.getchannel("A").tobytes() == original.getchannel("A").tobytes()
