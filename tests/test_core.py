from pathlib import Path
import shutil

import fitz
from PIL import Image
from pypdf import PdfReader, PdfWriter

from redos_pdf_combiner.converters import images_to_individual_pdfs, images_to_pdf
from redos_pdf_combiner.core import (
    TargetCompressionResult,
    add_text_watermark,
    compress_pdf_preset,
    compress_pdf_to_size,
    compress_pdfs_to_total_size,
    delete_pages,
    estimate_compression_size,
    extract_pages,
    merge_pdfs,
    parse_pages,
    rotate_pages,
    split_pdf_every,
)
import redos_pdf_combiner.core as core_module


def make_pdf(path: Path, pages: int) -> Path:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    with path.open("wb") as handle:
        writer.write(handle)
    return path


def test_parse_pages():
    assert parse_pages("1-3,5,-1", 8) == [0, 1, 2, 4, 7]
    assert parse_pages("even", 5) == [1, 3]
    assert parse_pages("3-1", 5) == [2, 1, 0]


def test_compression_size_estimate_respects_preset_and_color(tmp_path):
    source = make_pdf(tmp_path / "estimate.pdf", 4)
    with source.open("ab") as handle:
        handle.write(b"x" * 100_000)

    medium = estimate_compression_size([source], "medium", "color")
    maximum = estimate_compression_size([source], "maximum", "color")
    monochrome = estimate_compression_size([source], "maximum", "monochrome")

    assert 0 < monochrome < maximum < medium < source.stat().st_size


def test_pdf_operations(tmp_path):
    first = make_pdf(tmp_path / "first.pdf", 2)
    second = make_pdf(tmp_path / "second.pdf", 3)

    merged = merge_pdfs([first, second], tmp_path / "merged.pdf")
    assert len(PdfReader(str(merged)).pages) == 5

    extracted = extract_pages(merged, "2-4", tmp_path / "extracted.pdf")
    assert len(PdfReader(str(extracted)).pages) == 3

    deleted = delete_pages(merged, "1,5", tmp_path / "deleted.pdf")
    assert len(PdfReader(str(deleted)).pages) == 3

    rotated = rotate_pages(merged, "1", 90, tmp_path / "rotated.pdf")
    assert int(PdfReader(str(rotated)).pages[0].get("/Rotate")) == 90

    parts = split_pdf_every(merged, tmp_path / "parts", 2)
    assert len(parts) == 3

    marked = add_text_watermark(merged, tmp_path / "marked.pdf", "TEST")
    assert len(PdfReader(str(marked)).pages) == 5


def test_images_to_pdf_supports_webp(tmp_path):
    png = tmp_path / "first.png"
    webp = tmp_path / "second.webp"
    Image.new("RGB", (120, 80), "white").save(png)
    Image.new("RGB", (80, 120), "blue").save(webp, "WEBP")

    combined = images_to_pdf([png, webp], tmp_path / "images.pdf")
    assert len(PdfReader(str(combined)).pages) == 2

    outputs = images_to_individual_pdfs([png, webp], tmp_path / "individual")
    assert len(outputs) == 2
    assert all(path.exists() for path in outputs)


def test_monochrome_compression_mode(tmp_path):
    source = tmp_path / "color.pdf"
    with fitz.open() as document:
        page = document.new_page(width=200, height=200)
        page.draw_rect(fitz.Rect(20, 20, 180, 180), fill=(0, 0, 1))
        document.save(source)

    output = compress_pdf_preset(source, tmp_path / "mono.pdf", "maximum", "monochrome")
    assert output.exists()


def test_target_compression_keeps_file_that_already_fits(tmp_path):
    source = make_pdf(tmp_path / "source.pdf", 1)
    result = compress_pdf_to_size(source, tmp_path / "result.pdf", 1.0)

    assert result.achieved
    assert result.actual_bytes == source.stat().st_size
    assert result.path.read_bytes() == source.read_bytes()


def test_target_compression_uses_first_profile_under_limit(tmp_path, monkeypatch):
    source = make_pdf(tmp_path / "source.pdf", 1)
    with source.open("ab") as handle:
        handle.write(b"0" * 12000)

    def fake_lossless(input_pdf, output_pdf):
        shutil.copyfile(input_pdf, output_pdf)
        return Path(output_pdf)

    def fake_custom(_input_pdf, output_pdf, image_dpi, _jpeg_quality, _color_mode):
        size = 8000 if image_dpi == 220 else 4500
        Path(output_pdf).write_bytes(b"P" * size)
        return Path(output_pdf)

    monkeypatch.setattr(core_module, "compress_pdf_lossless", fake_lossless)
    monkeypatch.setattr(core_module, "find_ghostscript", lambda: "gs")
    monkeypatch.setattr(core_module, "_compress_pdf_gs_custom", fake_custom)

    result = compress_pdf_to_size(source, tmp_path / "result.pdf", 0.005)

    assert result.achieved
    assert result.image_dpi == 190
    assert result.actual_bytes == 4500


def test_batch_target_compression_uses_one_shared_limit(tmp_path, monkeypatch):
    sources = [tmp_path / "large.pdf", tmp_path / "small.pdf"]
    sources[0].write_bytes(b"A" * 7000)
    sources[1].write_bytes(b"B" * 3000)
    outputs = [tmp_path / "large_out.pdf", tmp_path / "small_out.pdf"]
    allocated_bytes = []

    def fake_compress(input_pdf, output_pdf, target_mb, _color_mode):
        target_bytes = max(1, round(target_mb * 1024 * 1024))
        allocated_bytes.append(target_bytes)
        actual_bytes = min(Path(input_pdf).stat().st_size, int(target_bytes * 0.9))
        Path(output_pdf).write_bytes(b"P" * actual_bytes)
        return TargetCompressionResult(Path(output_pdf), target_bytes, actual_bytes, True)

    monkeypatch.setattr(core_module, "compress_pdf_to_size", fake_compress)
    result = compress_pdfs_to_total_size(sources, outputs, 0.01)

    assert result.achieved
    assert result.actual_bytes == sum(path.stat().st_size for path in outputs)
    assert result.actual_bytes <= result.target_bytes
    assert allocated_bytes[1] == result.target_bytes - result.results[0].actual_bytes
    assert all(path.exists() for path in outputs)
