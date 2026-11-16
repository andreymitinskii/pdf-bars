from __future__ import annotations

import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterable, List, Optional, Sequence, Set, Tuple

from pypdf import PdfReader, PdfWriter
from pypdf.generic import (
    ArrayObject,
    DecodedStreamObject,
    DictionaryObject,
    FloatObject,
    NameObject,
    NumberObject,
    TextStringObject,
)


PDFSETTINGS = {
    "screen": "/screen",
    "ebook": "/ebook",
    "printer": "/printer",
    "prepress": "/prepress",
    "default": "/default",
}

COMPRESSION_PRESETS = {
    "maximum": {
        "label": "Максимальное сжатие",
        "image_dpi": 110,
        "mono_dpi": 300,
        "jpeg_quality": 45,
        "pdfsettings": "/screen",
    },
    "medium": {
        "label": "Оптимальное сжатие",
        "image_dpi": 150,
        "mono_dpi": 300,
        "jpeg_quality": 70,
        "pdfsettings": "/ebook",
    },
    "minimum": {
        "label": "Минимальное сжатие без потери качества",
        "lossless": True,
    },
}

COMPRESSION_ESTIMATE_FACTORS = {
    "maximum": 0.30,
    "medium": 0.58,
    "minimum": 0.94,
}


@dataclass(frozen=True)
class TargetCompressionResult:
    path: Path
    target_bytes: int
    actual_bytes: int
    achieved: bool
    image_dpi: Optional[int] = None
    jpeg_quality: Optional[int] = None

    @property
    def target_mb(self) -> float:
        return self.target_bytes / (1024 * 1024)

    @property
    def actual_mb(self) -> float:
        return self.actual_bytes / (1024 * 1024)


@dataclass(frozen=True)
class BatchTargetCompressionResult:
    results: Tuple[TargetCompressionResult, ...]
    target_bytes: int
    actual_bytes: int
    achieved: bool

    @property
    def target_mb(self) -> float:
        return self.target_bytes / (1024 * 1024)

    @property
    def actual_mb(self) -> float:
        return self.actual_bytes / (1024 * 1024)


def estimate_compression_size(
    input_pdfs: Sequence[os.PathLike[str] | str],
    preset: str = "medium",
    color_mode: str = "color",
) -> int:
    """Return a conservative approximate combined output size in bytes."""
    paths = [ensure_pdf_path(path) for path in input_pdfs]
    if not paths:
        return 0
    preset_key = _normalize_compression_preset(preset)
    mode_key = _normalize_pdf_color_mode(color_mode)
    factor = COMPRESSION_ESTIMATE_FACTORS[preset_key]
    if mode_key == "grayscale":
        factor *= 0.78
    elif mode_key == "monochrome":
        factor *= 0.38
    source_size = sum(path.stat().st_size for path in paths)
    return min(source_size, max(len(paths) * 4096, int(source_size * factor)))


def ensure_pdf_path(path: os.PathLike[str] | str) -> Path:
    value = Path(path)
    if value.suffix.lower() != ".pdf":
        raise ValueError(f"Файл должен быть PDF: {value}")
    return value


def parse_pages(spec: str, total_pages: int) -> List[int]:
    """Parse page ranges into zero-based page indexes.

    Supported examples: 1, 1-3, 3-1, odd, even, all, -1 for last page.
    """
    if total_pages < 1:
        return []
    raw = (spec or "all").strip().lower()
    if raw in {"", "all", "*"}:
        return list(range(total_pages))

    result: List[int] = []
    seen: Set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        if part == "odd":
            indexes = range(0, total_pages, 2)
        elif part == "even":
            indexes = range(1, total_pages, 2)
        elif "-" in part and not part.startswith("-"):
            left, right = part.split("-", 1)
            start = _page_number_to_index(left, total_pages)
            end = _page_number_to_index(right, total_pages)
            step = 1 if start <= end else -1
            indexes = range(start, end + step, step)
        else:
            indexes = [_page_number_to_index(part, total_pages)]

        for index in indexes:
            if index < 0 or index >= total_pages:
                raise ValueError(f"Страница вне диапазона: {index + 1}")
            if index not in seen:
                seen.add(index)
                result.append(index)
    return result


def merge_pdfs(inputs: Sequence[os.PathLike[str] | str], output: os.PathLike[str] | str) -> Path:
    if not inputs:
        raise ValueError("Нужно выбрать хотя бы один PDF")
    writer = PdfWriter()
    for input_path in inputs:
        reader = _reader(input_path)
        for page in reader.pages:
            writer.add_page(page)
    return _write(writer, output)


def extract_pages(input_pdf: os.PathLike[str] | str, pages: str, output: os.PathLike[str] | str) -> Path:
    reader = _reader(input_pdf)
    writer = PdfWriter()
    for index in parse_pages(pages, len(reader.pages)):
        writer.add_page(reader.pages[index])
    return _write(writer, output)


def delete_pages(input_pdf: os.PathLike[str] | str, pages: str, output: os.PathLike[str] | str) -> Path:
    reader = _reader(input_pdf)
    deleted = set(parse_pages(pages, len(reader.pages)))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index not in deleted:
            writer.add_page(page)
    if not writer.pages:
        raise ValueError("После удаления не осталось страниц")
    return _write(writer, output)


def rotate_pages(input_pdf: os.PathLike[str] | str, pages: str, degrees: int, output: os.PathLike[str] | str) -> Path:
    if degrees % 90 != 0:
        raise ValueError("Поворот должен быть кратен 90 градусам")
    reader = _reader(input_pdf)
    selected = set(parse_pages(pages, len(reader.pages)))
    writer = PdfWriter()
    for index, page in enumerate(reader.pages):
        if index in selected:
            if hasattr(page, "rotate"):
                page = page.rotate(degrees)
            else:
                page.rotate_clockwise(degrees)
        writer.add_page(page)
    return _write(writer, output)


def split_pdf_each_page(input_pdf: os.PathLike[str] | str, output_dir: os.PathLike[str] | str) -> List[Path]:
    reader = _reader(input_pdf)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = Path(input_pdf).stem
    outputs: List[Path] = []
    for index, page in enumerate(reader.pages, start=1):
        writer = PdfWriter()
        writer.add_page(page)
        out_path = out_dir / f"{base_name}_page_{index:03d}.pdf"
        outputs.append(_write(writer, out_path))
    return outputs


def split_pdf_every(input_pdf: os.PathLike[str] | str, output_dir: os.PathLike[str] | str, pages_per_file: int) -> List[Path]:
    if pages_per_file < 1:
        raise ValueError("Количество страниц в части должно быть больше нуля")
    reader = _reader(input_pdf)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    base_name = Path(input_pdf).stem
    outputs: List[Path] = []
    part = 1
    for start in range(0, len(reader.pages), pages_per_file):
        writer = PdfWriter()
        for page in reader.pages[start : start + pages_per_file]:
            writer.add_page(page)
        out_path = out_dir / f"{base_name}_part_{part:03d}.pdf"
        outputs.append(_write(writer, out_path))
        part += 1
    return outputs


def encrypt_pdf(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    user_password: str,
    owner_password: Optional[str] = None,
) -> Path:
    if not user_password:
        raise ValueError("Нужен пароль пользователя")
    reader = _reader(input_pdf)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    writer.encrypt(user_password=user_password, owner_password=owner_password or user_password)
    return _write(writer, output)


def decrypt_pdf(input_pdf: os.PathLike[str] | str, output: os.PathLike[str] | str, password: str) -> Path:
    reader = PdfReader(str(input_pdf))
    if reader.is_encrypted and not reader.decrypt(password):
        raise ValueError("Не удалось открыть PDF с указанным паролем")
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    return _write(writer, output)


def compress_pdf(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    quality: str = "ebook",
    prefer_ghostscript: bool = True,
) -> Path:
    output_path = ensure_pdf_path(output)
    if prefer_ghostscript:
        gs = find_ghostscript()
        if gs:
            setting = PDFSETTINGS.get(quality, "/ebook")
            output_path.parent.mkdir(parents=True, exist_ok=True)
            command = [
                gs,
                "-sDEVICE=pdfwrite",
                "-dCompatibilityLevel=1.4",
                f"-dPDFSETTINGS={setting}",
                "-dNOPAUSE",
                "-dQUIET",
                "-dBATCH",
                f"-sOutputFile={str(output_path)}",
                str(input_pdf),
            ]
            subprocess.run(command, check=True)
            return output_path

    reader = _reader(input_pdf)
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
        written_page = writer.pages[-1]
        if hasattr(written_page, "compress_content_streams"):
            written_page.compress_content_streams()
    return _write(writer, output_path)


def compress_pdf_percent(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    quality_percent: int = 75,
) -> Path:
    percent = max(1, min(100, int(quality_percent)))
    output_path = ensure_pdf_path(output)
    gs = find_ghostscript()
    if gs:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        image_dpi = int(72 + (percent / 100) * 228)
        jpeg_quality = max(20, min(95, percent))
        command = [
            gs,
            "-sDEVICE=pdfwrite",
            "-dCompatibilityLevel=1.4",
            "-dNOPAUSE",
            "-dQUIET",
            "-dBATCH",
            "-dDetectDuplicateImages=true",
            "-dCompressFonts=true",
            "-dSubsetFonts=true",
            "-dDownsampleColorImages=true",
            "-dDownsampleGrayImages=true",
            "-dDownsampleMonoImages=true",
            "-dColorImageDownsampleType=/Bicubic",
            "-dGrayImageDownsampleType=/Bicubic",
            "-dMonoImageDownsampleType=/Subsample",
            f"-dColorImageResolution={image_dpi}",
            f"-dGrayImageResolution={image_dpi}",
            f"-dMonoImageResolution={max(150, image_dpi)}",
            f"-dJPEGQ={jpeg_quality}",
            f"-sOutputFile={str(output_path)}",
            str(input_pdf),
        ]
        subprocess.run(command, check=True)
        return output_path
    return compress_pdf(input_pdf, output_path, quality="ebook", prefer_ghostscript=False)


def compress_pdf_preset(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    preset: str = "medium",
    color_mode: str = "color",
) -> Path:
    """Keep a smaller candidate, otherwise preserve the original PDF bytes."""
    source = ensure_pdf_path(input_pdf)
    output_path = ensure_pdf_path(output)
    color_mode_key = _normalize_pdf_color_mode(color_mode)
    if source.resolve() == output_path.resolve() or (output_path.exists() and os.path.samefile(source, output_path)):
        raise ValueError("Сохраните сжатый документ под новым именем")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".pdf-bars-compress-", dir=str(output_path.parent)) as temporary:
        candidate = Path(temporary) / "candidate.pdf"
        _compress_pdf_preset_candidate(source, candidate, preset, color_mode_key)
        # A rewritten PDF can be larger even after lossy image recompression.
        if color_mode_key == "color" and candidate.stat().st_size >= source.stat().st_size:
            shutil.copyfile(source, candidate)
        os.replace(candidate, output_path)
    return output_path


def _compress_pdf_preset_candidate(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    preset: str = "medium",
    color_mode: str = "color",
) -> Path:
    """Compress PDF using a user-facing preset.

    maximum/medium use Ghostscript with image recompression and font/object
    optimization. minimum is lossless: qpdf is preferred when available, then
    pypdf stream recompression is used as a fallback.
    """
    output_path = ensure_pdf_path(output)
    preset_key = _normalize_compression_preset(preset)
    color_mode_key = _normalize_pdf_color_mode(color_mode)
    if color_mode_key == "monochrome":
        dpi = {"maximum": 150, "medium": 200, "minimum": 300}[preset_key]
        return _rasterize_pdf_monochrome(input_pdf, output_path, dpi)
    if preset_key == "minimum" and color_mode_key == "color":
        return compress_pdf_lossless(input_pdf, output_path)

    gs = find_ghostscript()
    if not gs:
        if color_mode_key == "grayscale":
            raise RuntimeError("Для сохранения PDF в оттенках серого требуется Ghostscript")
        return compress_pdf(input_pdf, output_path, quality="ebook", prefer_ghostscript=False)

    settings = COMPRESSION_PRESETS[preset_key] if preset_key != "minimum" else {
        "image_dpi": 300,
        "mono_dpi": 600,
        "jpeg_quality": 95,
        "pdfsettings": "/prepress",
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image_dpi = int(settings["image_dpi"])
    mono_dpi = int(settings["mono_dpi"])
    jpeg_quality = int(settings["jpeg_quality"])
    command = [
        gs,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.6",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        f"-dPDFSETTINGS={settings['pdfsettings']}",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dEmbedAllFonts=true",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
        "-dAutoFilterColorImages=false",
        "-dAutoFilterGrayImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dGrayImageFilter=/DCTEncode",
        f"-dColorImageResolution={image_dpi}",
        f"-dGrayImageResolution={image_dpi}",
        f"-dMonoImageResolution={mono_dpi}",
        f"-dJPEGQ={jpeg_quality}",
    ]
    if color_mode_key == "grayscale":
        command.extend(("-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"))
    command.extend((f"-sOutputFile={str(output_path)}", str(input_pdf)))
    subprocess.run(command, check=True)
    return output_path


def compress_pdf_to_size(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    target_mb: float,
    color_mode: str = "color",
) -> TargetCompressionResult:
    """Compress a PDF toward a per-file size limit while preserving readability.

    Profiles are tried from highest to lowest visual quality. The first file
    that fits is selected. If the requested limit cannot be reached with the
    minimum supported profile, the smallest readable candidate is returned
    with ``achieved`` set to ``False``.
    """
    input_path = ensure_pdf_path(input_pdf)
    output_path = ensure_pdf_path(output)
    try:
        target_value = float(target_mb)
    except (TypeError, ValueError) as exc:
        raise ValueError("Целевой размер должен быть числом") from exc
    if not math.isfinite(target_value) or target_value <= 0:
        raise ValueError("Целевой размер должен быть больше нуля")
    if not input_path.exists():
        raise FileNotFoundError(f"Файл не найден: {input_path}")
    if input_path.resolve() == output_path.resolve():
        raise ValueError("Для результата выберите другое имя файла")

    target_bytes = max(1, round(target_value * 1024 * 1024))
    source_size = input_path.stat().st_size
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if source_size <= target_bytes:
        shutil.copyfile(str(input_path), str(output_path))
        return TargetCompressionResult(output_path, target_bytes, source_size, True)

    color_mode_key = _normalize_pdf_color_mode(color_mode)
    best_size = source_size
    best_dpi: Optional[int] = None
    best_quality: Optional[int] = None

    with TemporaryDirectory(prefix="pdf-bars-compress-") as temp_dir_value:
        temp_dir = Path(temp_dir_value)
        best_path = temp_dir / "best.pdf"

        lossless_path = temp_dir / "lossless.pdf"
        compress_pdf_lossless(input_path, lossless_path)
        lossless_size = lossless_path.stat().st_size
        if lossless_size < best_size:
            shutil.copyfile(str(lossless_path), str(best_path))
            best_size = lossless_size
        if lossless_size <= target_bytes:
            shutil.copyfile(str(lossless_path), str(output_path))
            return TargetCompressionResult(output_path, target_bytes, lossless_size, True)

        if color_mode_key == "monochrome":
            profiles = [(dpi, None) for dpi in (300, 250, 220, 200, 170, 150, 120, 100, 85, 72)]
        else:
            if not find_ghostscript():
                if best_path.exists():
                    shutil.copyfile(str(best_path), str(output_path))
                else:
                    shutil.copyfile(str(input_path), str(output_path))
                return TargetCompressionResult(output_path, target_bytes, best_size, False)
            profiles = [
                (220, 90),
                (190, 85),
                (165, 78),
                (145, 70),
                (125, 62),
                (108, 53),
                (92, 44),
                (80, 35),
                (72, 28),
            ]

        for attempt, (dpi, jpeg_quality) in enumerate(profiles, start=1):
            candidate = temp_dir / f"candidate-{attempt}.pdf"
            if color_mode_key == "monochrome":
                _rasterize_pdf_monochrome(input_path, candidate, dpi)
            else:
                _compress_pdf_gs_custom(input_path, candidate, dpi, int(jpeg_quality), color_mode_key)
            candidate_size = candidate.stat().st_size
            if candidate_size < best_size:
                shutil.copyfile(str(candidate), str(best_path))
                best_size = candidate_size
                best_dpi = dpi
                best_quality = jpeg_quality
            if candidate_size <= target_bytes:
                shutil.copyfile(str(candidate), str(output_path))
                return TargetCompressionResult(
                    output_path,
                    target_bytes,
                    candidate_size,
                    True,
                    dpi,
                    jpeg_quality,
                )

        if best_path.exists():
            shutil.copyfile(str(best_path), str(output_path))
        else:
            shutil.copyfile(str(input_path), str(output_path))
        return TargetCompressionResult(
            output_path,
            target_bytes,
            best_size,
            best_size <= target_bytes,
            best_dpi,
            best_quality,
        )


def compress_pdfs_to_total_size(
    input_pdfs: Sequence[os.PathLike[str] | str],
    output_pdfs: Sequence[os.PathLike[str] | str],
    target_mb: float,
    color_mode: str = "color",
) -> BatchTargetCompressionResult:
    """Compress several PDFs so their combined size fits one shared limit."""
    inputs = [ensure_pdf_path(path) for path in input_pdfs]
    outputs = [ensure_pdf_path(path) for path in output_pdfs]
    if not inputs:
        raise ValueError("Добавьте хотя бы один PDF")
    if len(inputs) != len(outputs):
        raise ValueError("Количество исходных и выходных PDF не совпадает")
    try:
        target_value = float(target_mb)
    except (TypeError, ValueError) as exc:
        raise ValueError("Целевой размер должен быть числом") from exc
    if not math.isfinite(target_value) or target_value <= 0:
        raise ValueError("Целевой размер должен быть больше нуля")

    target_bytes = max(1, int(target_value * 1024 * 1024))
    source_sizes = [max(1, path.stat().st_size) for path in inputs]
    remaining_source_size = sum(source_sizes)
    actual_total = 0
    results = []

    for index, (input_path, output_path, source_size) in enumerate(zip(inputs, outputs, source_sizes)):
        remaining_budget = max(1, target_bytes - actual_total)
        if index == len(inputs) - 1:
            file_budget = remaining_budget
        else:
            file_budget = max(1, int(remaining_budget * source_size / remaining_source_size))
        result = compress_pdf_to_size(
            input_path,
            output_path,
            file_budget / (1024 * 1024),
            color_mode,
        )
        results.append(result)
        actual_total += result.actual_bytes
        remaining_source_size -= source_size

    return BatchTargetCompressionResult(
        tuple(results),
        target_bytes,
        actual_total,
        actual_total <= target_bytes,
    )


def _compress_pdf_gs_custom(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    image_dpi: int,
    jpeg_quality: int,
    color_mode: str,
) -> Path:
    gs = find_ghostscript()
    if not gs:
        raise RuntimeError("Для сжатия до заданного размера требуется Ghostscript")
    output_path = ensure_pdf_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        gs,
        "-sDEVICE=pdfwrite",
        "-dCompatibilityLevel=1.6",
        "-dNOPAUSE",
        "-dQUIET",
        "-dBATCH",
        "-dDetectDuplicateImages=true",
        "-dCompressFonts=true",
        "-dSubsetFonts=true",
        "-dEmbedAllFonts=true",
        "-dPassThroughJPEGImages=false",
        "-dDownsampleColorImages=true",
        "-dDownsampleGrayImages=true",
        "-dDownsampleMonoImages=true",
        "-dColorImageDownsampleType=/Bicubic",
        "-dGrayImageDownsampleType=/Bicubic",
        "-dMonoImageDownsampleType=/Subsample",
        "-dColorImageDownsampleThreshold=1.0",
        "-dGrayImageDownsampleThreshold=1.0",
        "-dAutoFilterColorImages=false",
        "-dAutoFilterGrayImages=false",
        "-dColorImageFilter=/DCTEncode",
        "-dGrayImageFilter=/DCTEncode",
        f"-dColorImageResolution={image_dpi}",
        f"-dGrayImageResolution={image_dpi}",
        f"-dMonoImageResolution={max(150, image_dpi * 2)}",
        f"-dJPEGQ={jpeg_quality}",
    ]
    if color_mode == "grayscale":
        command.extend(("-sColorConversionStrategy=Gray", "-dProcessColorModel=/DeviceGray"))
    command.extend((f"-sOutputFile={str(output_path)}", str(input_pdf)))
    subprocess.run(command, check=True)
    return output_path


def _rasterize_pdf_monochrome(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    dpi: int,
) -> Path:
    try:
        import fitz
        from PIL import Image, ImageOps
    except ImportError as exc:
        raise RuntimeError("Для чёрно-белого режима требуются PyMuPDF и Pillow") from exc

    output_path = ensure_pdf_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    pages = []
    zoom = dpi / 72.0
    with fitz.open(str(input_pdf)) as document:
        for page in document:
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), colorspace=fitz.csGRAY, alpha=False)
            image = Image.frombytes("L", (pixmap.width, pixmap.height), pixmap.samples)
            image = ImageOps.autocontrast(image)
            pages.append(image.point(lambda value: 255 if value >= 180 else 0, mode="1"))
            image.close()
    if not pages:
        raise ValueError("PDF не содержит страниц")
    first, rest = pages[0], pages[1:]
    first.save(str(output_path), "PDF", resolution=dpi, save_all=True, append_images=rest)
    for page in pages:
        page.close()
    return output_path


def compress_pdf_lossless(input_pdf: os.PathLike[str] | str, output: os.PathLike[str] | str) -> Path:
    output_path = ensure_pdf_path(output)
    qpdf = find_qpdf()
    if qpdf:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        command = [
            qpdf,
            "--object-streams=generate",
            "--stream-data=compress",
            "--recompress-flate",
            "--compression-level=9",
            str(input_pdf),
            str(output_path),
        ]
        try:
            subprocess.run(command, check=True)
            return output_path
        except subprocess.CalledProcessError:
            pass
    return compress_pdf(input_pdf, output_path, quality="default", prefer_ghostscript=False)


def _normalize_compression_preset(preset: str) -> str:
    value = (preset or "medium").strip().lower()
    aliases = {
        "max": "maximum",
        "maximum": "maximum",
        "максимальное": "maximum",
        "medium": "medium",
        "mid": "medium",
        "среднее": "medium",
        "min": "minimum",
        "minimum": "minimum",
        "минимальное": "minimum",
        "lossless": "minimum",
    }
    if value not in aliases:
        raise ValueError("Неизвестный пресет сжатия")
    return aliases[value]


def _normalize_pdf_color_mode(mode: str) -> str:
    value = (mode or "color").strip().lower()
    aliases = {
        "color": "color",
        "цветной": "color",
        "grayscale": "grayscale",
        "gray": "grayscale",
        "серый": "grayscale",
        "monochrome": "monochrome",
        "mono": "monochrome",
        "blackwhite": "monochrome",
        "чёрно-белый": "monochrome",
        "черно-белый": "monochrome",
    }
    if value not in aliases:
        raise ValueError("Неизвестный цветовой режим PDF")
    return aliases[value]


def add_text_watermark(
    input_pdf: os.PathLike[str] | str,
    output: os.PathLike[str] | str,
    text: str,
    pages: str = "all",
    font_size: int = 48,
    opacity: float = 0.18,
    angle: float = 35.0,
) -> Path:
    if not text:
        raise ValueError("Текст водяного знака не должен быть пустым")
    reader = _reader(input_pdf)
    writer = PdfWriter()
    selected = set(parse_pages(pages, len(reader.pages)))
    font_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica-Bold"),
                NameObject("/Encoding"): NameObject("/WinAnsiEncoding"),
            }
        )
    )
    gs_ref = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/ExtGState"),
                NameObject("/CA"): FloatObject(max(0.0, min(1.0, opacity))),
                NameObject("/ca"): FloatObject(max(0.0, min(1.0, opacity))),
            }
        )
    )

    for index, page in enumerate(reader.pages):
        writer.add_page(page)
        if index in selected:
            _append_watermark_stream(writer, writer.pages[-1], text, font_ref, gs_ref, font_size, angle)
    return _write(writer, output)


def find_ghostscript() -> Optional[str]:
    for candidate in ("gs", "gswin64c", "gswin32c"):
        path = shutil.which(candidate)
        if path:
            return path
    return None


def find_qpdf() -> Optional[str]:
    return shutil.which("qpdf")


def _reader(path: os.PathLike[str] | str) -> PdfReader:
    pdf_path = ensure_pdf_path(path)
    if not pdf_path.exists():
        raise FileNotFoundError(str(pdf_path))
    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        raise ValueError("PDF зашифрован. Сначала выполните расшифровку или укажите пароль.")
    return reader


def _write(writer: PdfWriter, output: os.PathLike[str] | str) -> Path:
    output_path = ensure_pdf_path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("wb") as handle:
        writer.write(handle)
    return output_path


def _page_number_to_index(value: str, total_pages: int) -> int:
    try:
        number = int(value.strip())
    except ValueError as exc:
        raise ValueError(f"Неверный номер страницы: {value}") from exc
    if number == 0:
        raise ValueError("Нумерация страниц начинается с 1")
    if number < 0:
        return total_pages + number
    return number - 1


def _append_watermark_stream(writer: PdfWriter, page, text: str, font_ref, gs_ref, font_size: int, angle: float) -> None:
    resources = page.get(NameObject("/Resources"), DictionaryObject())
    if not isinstance(resources, DictionaryObject):
        resources = DictionaryObject(resources)

    fonts = resources.get(NameObject("/Font"), DictionaryObject())
    if not isinstance(fonts, DictionaryObject):
        fonts = DictionaryObject(fonts)
    fonts[NameObject("/Fwm")] = font_ref
    resources[NameObject("/Font")] = fonts

    ext_states = resources.get(NameObject("/ExtGState"), DictionaryObject())
    if not isinstance(ext_states, DictionaryObject):
        ext_states = DictionaryObject(ext_states)
    ext_states[NameObject("/GSwm")] = gs_ref
    resources[NameObject("/ExtGState")] = ext_states
    page[NameObject("/Resources")] = resources

    media = page.mediabox
    width = float(media.width)
    height = float(media.height)
    radians = math.radians(angle)
    cos_v = math.cos(radians)
    sin_v = math.sin(radians)
    x = width / 2 - len(text) * font_size * 0.18
    y = height / 2
    escaped_text = _escape_pdf_text(text)
    content = (
        "q\n"
        "/GSwm gs\n"
        "0.75 0.10 0.10 rg\n"
        "BT\n"
        f"/Fwm {font_size} Tf\n"
        f"{cos_v:.6f} {sin_v:.6f} {-sin_v:.6f} {cos_v:.6f} {x:.2f} {y:.2f} Tm\n"
        f"({escaped_text}) Tj\n"
        "ET\n"
        "Q\n"
    )
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1", errors="replace"))
    stream_ref = writer._add_object(stream)

    current = page.get(NameObject("/Contents"))
    if current is None:
        page[NameObject("/Contents")] = stream_ref
    elif isinstance(current, ArrayObject):
        current.append(stream_ref)
    else:
        page[NameObject("/Contents")] = ArrayObject([current, stream_ref])


def _escape_pdf_text(text: str) -> str:
    return (
        text.encode("latin-1", errors="replace")
        .decode("latin-1")
        .replace("\\", "\\\\")
        .replace("(", "\\(")
        .replace(")", "\\)")
    )
