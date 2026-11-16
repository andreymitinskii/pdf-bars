from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import List, Sequence, Tuple

from . import __version__
from .pdf_standards import remove_pdfa_declaration


RectTuple = Tuple[float, float, float, float]
PointTuple = Tuple[float, float]
IMAGE_OUTPUT_FORMATS = {
    ".jpg": "JPEG",
    ".jpeg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
    ".tif": "TIFF",
    ".tiff": "TIFF",
    ".bmp": "BMP",
}


@dataclass(frozen=True)
class TextEdit:
    page_index: int
    rect: RectTuple
    text: str
    font_size: float = 11.0
    font_family: str = "Times New Roman"
    font_file: str | None = None
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    opaque_background: bool = False
    background_color: Tuple[float, float, float] = (1.0, 1.0, 1.0)
    bold: bool = False
    italic: bool = False
    underline: bool = False
    replacement_background: str | None = None
    replacement_background_rect: RectTuple | None = None
    replacement_source_rect: RectTuple | None = None
    original_text: str = ""
    style_confidence: float = 0.0
    scan_blur: float = 0.0
    scan_effect: bool = True


@dataclass(frozen=True)
class WordReplacementPreparation:
    background_path: str
    background_rect: RectTuple
    source_rect: RectTuple
    font_size: float
    font_family: str
    font_file: str | None
    color: Tuple[float, float, float]
    background_color: Tuple[float, float, float]
    bold: bool
    italic: bool
    confidence: float
    scan_blur: float


@dataclass(frozen=True)
class SignatureEdit:
    page_index: int
    rect: RectTuple
    image_path: str
    keep_proportion: bool = True


@dataclass(frozen=True)
class RegionCopyEdit:
    page_index: int
    source_rect: RectTuple
    rect: RectTuple


@dataclass(frozen=True)
class StrokeEdit:
    page_index: int
    points: Tuple[PointTuple, ...]
    width: float = 2.0
    color: Tuple[float, float, float] = (0.0, 0.0, 0.0)
    style: str = "free"


@dataclass(frozen=True)
class CropEdit:
    page_index: int
    rect: RectTuple


@dataclass(frozen=True)
class AlignmentEdit:
    page_index: int
    angle: float


def detect_text_style(
    input_pdf: os.PathLike[str] | str,
    page_index: int,
    x: float,
    y: float,
) -> dict | None:
    """Return style metadata for vector text under a PDF point.

    Scanned pages do not contain font metadata, so None is returned for them.
    """
    import fitz

    with fitz.open(str(input_pdf)) as document:
        page = document[page_index]
        point = fitz.Point(x, y)
        for block in page.get_text("dict").get("blocks", []):
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if point in fitz.Rect(span["bbox"]):
                        color_value = int(span.get("color", 0))
                        color = (
                            ((color_value >> 16) & 255) / 255.0,
                            ((color_value >> 8) & 255) / 255.0,
                            (color_value & 255) / 255.0,
                        )
                        family = span.get("font", "DejaVu Sans")
                        flags = int(span.get("flags", 0))
                        return {
                            "font_family": family,
                            "font_size": float(span.get("size", 11.0)),
                            "font_file": resolve_font_file(family),
                            "color": color,
                            "bold": bool(flags & 16),
                            "italic": bool(flags & 2),
                        }
    return None


def available_cyrillic_fonts() -> List[str]:
    preferred = ("Times New Roman", "PT Sans", "DejaVu Sans", "Liberation Serif", "Liberation Sans", "Arial", "Noto Sans")
    available = [family for family in preferred if resolve_font_file(family)]
    return available or ["Times New Roman"]


def resolve_font_file(family: str, bold: bool = False, italic: bool = False) -> str | None:
    style = ("bold_italic" if bold and italic else "bold" if bold else "italic" if italic else "regular")
    known = {
        "Times New Roman": {
            "regular": ("C:/Windows/Fonts/times.ttf", "/usr/share/fonts/msttcorefonts/times.ttf", "/usr/share/fonts/liberation/LiberationSerif-Regular.ttf", "/usr/share/fonts/liberation-serif/LiberationSerif-Regular.ttf"),
            "bold": ("C:/Windows/Fonts/timesbd.ttf", "/usr/share/fonts/msttcorefonts/timesbd.ttf", "/usr/share/fonts/liberation/LiberationSerif-Bold.ttf", "/usr/share/fonts/liberation-serif/LiberationSerif-Bold.ttf"),
            "italic": ("C:/Windows/Fonts/timesi.ttf", "/usr/share/fonts/msttcorefonts/timesi.ttf", "/usr/share/fonts/liberation/LiberationSerif-Italic.ttf", "/usr/share/fonts/liberation-serif/LiberationSerif-Italic.ttf"),
            "bold_italic": ("C:/Windows/Fonts/timesbi.ttf", "/usr/share/fonts/msttcorefonts/timesbi.ttf", "/usr/share/fonts/liberation/LiberationSerif-BoldItalic.ttf", "/usr/share/fonts/liberation-serif/LiberationSerif-BoldItalic.ttf"),
        },
        "PT Sans": {
            "regular": ("/usr/share/fonts/paratype-pt-sans/PTSans-Regular.ttf",),
            "bold": ("/usr/share/fonts/paratype-pt-sans/PTSans-Bold.ttf",),
            "italic": ("/usr/share/fonts/paratype-pt-sans/PTSans-Italic.ttf",),
            "bold_italic": ("/usr/share/fonts/paratype-pt-sans/PTSans-BoldItalic.ttf",),
        },
        "DejaVu Sans": {
            "regular": ("/usr/share/fonts/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "C:/Windows/Fonts/DejaVuSans.ttf"),
            "bold": ("/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "C:/Windows/Fonts/DejaVuSans-Bold.ttf"),
            "italic": ("/usr/share/fonts/dejavu/DejaVuSans-Oblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf", "C:/Windows/Fonts/DejaVuSans-Oblique.ttf"),
            "bold_italic": ("/usr/share/fonts/dejavu/DejaVuSans-BoldOblique.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf", "C:/Windows/Fonts/DejaVuSans-BoldOblique.ttf"),
        },
        "Liberation Sans": {
            key: tuple(path.replace("Regular", suffix) for path in ("/usr/share/fonts/liberation/LiberationSans-Regular.ttf", "/usr/share/fonts/liberation-sans/LiberationSans-Regular.ttf"))
            for key, suffix in {"regular": "Regular", "bold": "Bold", "italic": "Italic", "bold_italic": "BoldItalic"}.items()
        },
        "Liberation Serif": {
            key: tuple(path.replace("Regular", suffix) for path in ("/usr/share/fonts/liberation/LiberationSerif-Regular.ttf", "/usr/share/fonts/liberation-serif/LiberationSerif-Regular.ttf"))
            for key, suffix in {"regular": "Regular", "bold": "Bold", "italic": "Italic", "bold_italic": "BoldItalic"}.items()
        },
        "Arial": {
            "regular": ("C:/Windows/Fonts/arial.ttf",), "bold": ("C:/Windows/Fonts/arialbd.ttf",),
            "italic": ("C:/Windows/Fonts/ariali.ttf",), "bold_italic": ("C:/Windows/Fonts/arialbi.ttf",),
        },
        "Noto Sans": {
            "regular": ("/usr/share/fonts/google-noto/NotoSans-Regular.ttf",), "bold": ("/usr/share/fonts/google-noto/NotoSans-Bold.ttf",),
            "italic": ("/usr/share/fonts/google-noto/NotoSans-Italic.ttf",), "bold_italic": ("/usr/share/fonts/google-noto/NotoSans-BoldItalic.ttf",),
        },
    }
    normalized = family.replace("-", " ").casefold()
    for display, variants in known.items():
        if display.casefold() in normalized or normalized in display.casefold():
            candidates = variants.get(style, ()) + variants.get("regular", ())
            for candidate in candidates:
                if Path(candidate).exists():
                    return candidate
    if shutil.which("fc-match"):
        requested_style = "Bold Italic" if bold and italic else "Bold" if bold else "Italic" if italic else "Regular"
        completed = subprocess.run(
            ["fc-match", "-f", "%{file}", f"{family}:style={requested_style}"],
            text=True,
            capture_output=True,
        )
        candidate = completed.stdout.strip()
        if completed.returncode == 0 and candidate and Path(candidate).exists():
            return candidate
    # Preserve text size and style when a requested Linux font is absent on Windows.
    for candidate in known["Arial"].get(style, ()) + known["Times New Roman"].get(style, ()):
        if Path(candidate).is_file():
            return candidate
    return None


def prepare_word_replacement(
    input_file: os.PathLike[str] | str,
    output_image: os.PathLike[str] | str,
    page_index: int,
    source_rect: RectTuple,
    original_text: str,
    dpi: int = 300,
    alignment_angle: float = 0.0,
) -> WordReplacementPreparation:
    """Create a transparent patch that removes one scanned word.

    Only pixels identified as glyphs are opaque in the resulting PNG. This
    keeps paper texture and table rules outside the word untouched.
    """
    from PIL import Image, ImageOps

    source = Path(input_file)
    destination = Path(output_image)
    destination.parent.mkdir(parents=True, exist_ok=True)
    rect = _normalize_rect(source_rect)
    word_height = max(1.0, rect[3] - rect[1])
    margin_x = max(3.0, min(16.0, word_height * 0.8))
    margin_y = max(1.5, min(7.0, word_height * 0.4))

    if source.suffix.lower() == ".pdf":
        import fitz

        with fitz.open(str(source)) as document:
            page = document[page_index]
            patch_rect = _clamp_rect(
                (rect[0] - margin_x, rect[1] - margin_y, rect[2] + margin_x, rect[3] + margin_y),
                (float(page.rect.width), float(page.rect.height)),
            )
            zoom = max(1.0, min(6.0, float(dpi) / 72.0))
            if abs(alignment_angle) >= 0.01:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
            else:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), clip=fitz.Rect(patch_rect), alpha=False)
        rendered = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        if abs(alignment_angle) >= 0.01:
            aligned = rotate_raster_image(rendered, alignment_angle)
            rendered.close()
            crop_box = _int_box(_scale_rect(patch_rect, zoom))
            patch = aligned.crop(crop_box)
            aligned.close()
        else:
            patch = rendered
    else:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        if abs(alignment_angle) >= 0.01:
            aligned = rotate_raster_image(image, alignment_angle)
            image.close()
            image = aligned
        patch_rect = _clamp_rect(
            (rect[0] - margin_x, rect[1] - margin_y, rect[2] + margin_x, rect[3] + margin_y),
            (float(image.width), float(image.height)),
        )
        crop_box = _int_box(patch_rect)
        patch = image.crop(crop_box)
        image.close()
        patch_rect = tuple(float(value) for value in crop_box)

    scale_x = patch.width / max(0.001, patch_rect[2] - patch_rect[0])
    scale_y = patch.height / max(0.001, patch_rect[3] - patch_rect[1])
    word_box = (
        int(round((rect[0] - patch_rect[0]) * scale_x)),
        int(round((rect[1] - patch_rect[1]) * scale_y)),
        int(round((rect[2] - patch_rect[0]) * scale_x)),
        int(round((rect[3] - patch_rect[1]) * scale_y)),
    )
    word_box = _clamp_int_box(word_box, patch.size)
    overlay, foreground_mask, _text_color, background_color = _word_background_overlay(patch, word_box)
    overlay.save(destination, "PNG", optimize=True)

    unit_scale = (scale_x + scale_y) / 2.0
    style = _match_scanned_word_style(
        foreground_mask.crop(word_box),
        original_text,
        word_height,
        unit_scale,
    )
    patch.close()
    overlay.close()
    foreground_mask.close()
    return WordReplacementPreparation(
        str(destination),
        tuple(float(value) for value in patch_rect),
        tuple(float(value) for value in rect),
        style[2],
        style[0],
        style[1],
        (0.0, 0.0, 0.0),
        background_color,
        style[3],
        style[4],
        style[5],
        round(max(0.18, min(0.55, word_height * 0.025)), 2),
    )


def measure_editor_text(
    text: str,
    font_family: str,
    font_size: float,
    bold: bool = False,
    italic: bool = False,
) -> float:
    """Measure one editor line in the same coordinate units as font_size."""
    from PIL import Image, ImageDraw, ImageFont

    scale = 4
    font_file = resolve_font_file(font_family, bold, italic)
    try:
        font = ImageFont.truetype(font_file, max(1, int(round(font_size * scale)))) if font_file else ImageFont.load_default()
    except OSError:
        font = ImageFont.load_default()
    image = Image.new("L", (8, 8), 0)
    draw = ImageDraw.Draw(image)
    try:
        width = float(draw.textlength(text or " ", font=font)) / scale
    except AttributeError:
        box = draw.textbbox((0, 0), text or " ", font=font)
        width = float(box[2] - box[0]) / scale
    image.close()
    return max(font_size * 0.5, width)


def _word_cleanup_right_edge(
    source_pixels,
    *,
    x1: int,
    cleanup_x1: int,
    cleanup_y0: int,
    cleanup_y1: int,
    strong_ink_limit: int,
) -> int:
    """Keep a truncated glyph in the cleanup area, but stop before the next word."""
    content_height = max(1, cleanup_y1 - cleanup_y0)
    continuation_gap = max(3, min(8, int(round(content_height * 0.14))))
    word_gap = max(5, min(14, int(round(content_height * 0.28))))
    minimum_ink = max(2, int(round(content_height * 0.14)))
    first_ink: int | None = None
    last_ink = x1 - 1
    for candidate_x in range(x1, cleanup_x1):
        strong_pixels = sum(
            _luma(source_pixels[candidate_x, candidate_y]) < strong_ink_limit
            for candidate_y in range(cleanup_y0, cleanup_y1)
        )
        if strong_pixels < minimum_ink:
            continue
        if first_ink is None:
            # A normal word space is larger than the small gap left by a
            # clipped OCR rectangle. Do not paint over the following word.
            if candidate_x - x1 > continuation_gap:
                return x1
            first_ink = candidate_x
        elif candidate_x - last_ink > word_gap:
            return min(cleanup_x1, last_ink + 1)
        last_ink = candidate_x
    return cleanup_x1


def _word_background_overlay(image, word_box):
    from PIL import Image, ImageFilter

    original = image.convert("RGB")
    width, height = original.size
    x0, y0, x1, y1 = _clamp_int_box(word_box, original.size)
    cleanup_pad_left = max(2, min(5, int(round((y1 - y0) * 0.12))))
    cleanup_pad_right = max(5, min(14, int(round((y1 - y0) * 0.6))))
    cleanup_pad_y = max(1, min(3, int(round((y1 - y0) * 0.06))))
    cleanup_x0 = max(0, x0 - cleanup_pad_left)
    cleanup_y0 = max(0, y0 - cleanup_pad_y)
    cleanup_x1 = min(width, x1 + cleanup_pad_right)
    cleanup_y1 = min(height, y1 + cleanup_pad_y)
    source_pixels = original.load()
    border_samples = []
    border = max(2, min(6, (y1 - y0) // 4))
    for y in range(max(0, cleanup_y0 - border), min(height, cleanup_y1 + border)):
        for x in range(max(0, cleanup_x0 - border), min(width, cleanup_x1 + border)):
            if x < cleanup_x0 or x >= cleanup_x1 or y < cleanup_y0 or y >= cleanup_y1:
                border_samples.append(source_pixels[x, y])
    if not border_samples:
        border_samples = [source_pixels[0, 0]]
    background_rgb = _median_rgb(border_samples)
    background_luma = _luma(background_rgb)

    # A pale fragment of the old glyph can sit just outside an OCR word box.
    # Keep it in the cleanup area until a real word gap is found. The previous
    # first-dark-pixel rule left the last glyph as a visible artefact.
    strong_ink_limit = background_luma - 105
    cleanup_x1 = _word_cleanup_right_edge(
        source_pixels,
        x1=x1,
        cleanup_x1=cleanup_x1,
        cleanup_y0=cleanup_y0,
        cleanup_y1=cleanup_y1,
        strong_ink_limit=strong_ink_limit,
    )

    fill = Image.new("RGB", original.size, background_rgb)
    fill_pixels = fill.load()
    left_xs = range(max(0, cleanup_x0 - border), max(1, cleanup_x0))
    right_xs = range(min(width - 1, cleanup_x1), min(width, cleanup_x1 + border))
    top_ys = range(max(0, cleanup_y0 - border), max(1, cleanup_y0))
    bottom_ys = range(min(height - 1, cleanup_y1), min(height, cleanup_y1 + border))

    def clean_background(samples):
        values = list(samples)
        clean = [sample for sample in values if abs(_luma(sample) - background_luma) <= 18]
        return _median_rgb(clean or values or [background_rgb])

    for y in range(cleanup_y0, cleanup_y1):
        left = clean_background(source_pixels[x, y] for x in left_xs)
        right = clean_background(source_pixels[x, y] for x in right_xs)
        for x in range(cleanup_x0, cleanup_x1):
            top = clean_background(source_pixels[x, sample_y] for sample_y in top_ys)
            bottom = clean_background(source_pixels[x, sample_y] for sample_y in bottom_ys)
            tx = (x - cleanup_x0) / max(1, cleanup_x1 - cleanup_x0 - 1)
            ty = (y - cleanup_y0) / max(1, cleanup_y1 - cleanup_y0 - 1)
            horizontal = tuple(left[channel] * (1.0 - tx) + right[channel] * tx for channel in range(3))
            vertical = tuple(top[channel] * (1.0 - ty) + bottom[channel] * ty for channel in range(3))
            fill_pixels[x, y] = tuple(int(round((horizontal[channel] + vertical[channel]) / 2.0)) for channel in range(3))

    mask = Image.new("L", original.size, 0)
    mask_pixels = mask.load()
    dark_rows = set()
    dark_columns = set()
    dark_limit = background_luma - 24
    for y in range(y0, y1):
        inside_dark = sum(_luma(source_pixels[x, y]) < dark_limit for x in range(x0, x1))
        outside_sides = (range(max(0, x0 - border), x0), range(x1, min(width, x1 + border)))
        # A rule continues on both sides; dense glyphs plus a nearby speck do not.
        if (
            inside_dark >= max(4, int((x1 - x0) * 0.85))
            and all(side and sum(_luma(source_pixels[x, y]) < dark_limit for x in side)
                    >= max(1, len(side) * 0.55) for side in outside_sides)
        ):
            dark_rows.update((y - 1, y, y + 1))
    for x in range(x0, x1):
        inside_dark = sum(_luma(source_pixels[x, y]) < dark_limit for y in range(y0, y1))
        outside_sides = (range(max(0, y0 - border), y0), range(y1, min(height, y1 + border)))
        if (
            inside_dark >= max(4, int((y1 - y0) * 0.85))
            and all(side and sum(_luma(source_pixels[x, y]) < dark_limit for y in side)
                    >= max(1, len(side) * 0.55) for side in outside_sides)
        ):
            dark_columns.update((x - 1, x, x + 1))

    foreground_samples = []
    for y in range(cleanup_y0, cleanup_y1):
        for x in range(cleanup_x0, cleanup_x1):
            current = source_pixels[x, y]
            estimated = fill_pixels[x, y]
            luma_drop = _luma(estimated) - _luma(current)
            color_distance = sum(abs(current[channel] - estimated[channel]) for channel in range(3))
            if (luma_drop >= 12 and color_distance >= 24) or color_distance >= 105:
                mask_pixels[x, y] = 255
                foreground_samples.append(current)
    for y in dark_rows:
        if cleanup_y0 <= y < cleanup_y1:
            for x in range(cleanup_x0, cleanup_x1):
                mask_pixels[x, y] = 0
    for x in dark_columns:
        if cleanup_x0 <= x < cleanup_x1:
            for y in range(cleanup_y0, cleanup_y1):
                mask_pixels[x, y] = 0
    analysis_mask = mask.copy()
    # Include antialiased and compression halos around the old glyphs. A
    # smaller expansion can leave a pale outline that is especially visible
    # after a dark replacement is drawn over a light scanned page.
    expanded_mask = mask.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.GaussianBlur(0.8))
    mask.close()
    mask = expanded_mask
    mask_pixels = mask.load()
    for y in dark_rows:
        if cleanup_y0 <= y < cleanup_y1:
            for x in range(cleanup_x0, cleanup_x1):
                mask_pixels[x, y] = 0
    for x in dark_columns:
        if cleanup_x0 <= x < cleanup_x1:
            for y in range(cleanup_y0, cleanup_y1):
                mask_pixels[x, y] = 0

    # Keep paper RGB even under zero alpha: black transparent pixels can
    # bleed into PDF image interpolation and produce a contour around masks.
    transparent = fill.convert("RGBA")
    transparent.putalpha(mask)
    text_rgb = _median_rgb(foreground_samples) if foreground_samples else (0, 0, 0)
    original.close()
    fill.close()
    mask.close()
    return (
        transparent,
        analysis_mask,
        tuple(value / 255.0 for value in text_rgb),
        tuple(value / 255.0 for value in background_rgb),
    )


def _match_scanned_word_style(mask, text: str, word_height: float, unit_scale: float):
    from PIL import Image, ImageChops, ImageDraw, ImageFont

    target = mask.convert("L")
    target_box = target.getbbox()
    families = available_cyrillic_fonts()
    fallback_family = "Times New Roman" if resolve_font_file("Times New Roman") else families[0]
    fallback_size = max(6.0, word_height * 1.25)
    if not text.strip() or target_box is None:
        return fallback_family, resolve_font_file(fallback_family), fallback_size, False, False, 0.0
    target = target.crop(target_box)
    target = target.point(lambda value: 255 if value >= 64 else 0)
    best = None
    styles = ((False, False), (True, False), (False, True), (True, True))
    for family in families:
        normalized_family = family.casefold()
        family_penalty = -0.16 if normalized_family == "times new roman" else (-0.04 if "serif" in normalized_family else 0.03)
        for bold, italic in styles:
            font_file = resolve_font_file(family, bold, italic)
            if not font_file:
                continue
            for factor_percent in range(85, 171, 5):
                factor = factor_percent / 100.0
                font_size = max(6.0, word_height * factor)
                pixel_size = max(4, int(round(font_size * unit_scale)))
                try:
                    font = ImageFont.truetype(font_file, pixel_size)
                except OSError:
                    continue
                probe = Image.new("L", (max(64, target.width * 3), max(48, target.height * 3)), 0)
                draw = ImageDraw.Draw(probe)
                box = draw.textbbox((6, 6), text, font=font)
                draw.text((6 - box[0], 6 - box[1]), text, font=font, fill=255)
                rendered_box = probe.getbbox()
                if rendered_box is None:
                    probe.close()
                    continue
                rendered = probe.crop(rendered_box)
                width_error = abs(math.log(max(1, rendered.width) / max(1, target.width)))
                height_error = abs(math.log(max(1, rendered.height) / max(1, target.height)))
                resized = rendered.resize(target.size, getattr(getattr(Image, "Resampling", Image), "BILINEAR"))
                resized = resized.point(lambda value: 255 if value >= 96 else 0)
                difference = ImageChops.difference(target, resized)
                shape_error = sum(1 for value in difference.getdata() if value) / max(1, target.width * target.height)
                score = width_error * 0.8 + height_error * 1.35 + shape_error * 0.4 + family_penalty
                candidate = (score, family, font_file, font_size, bold, italic)
                if best is None or candidate[0] < best[0]:
                    best = candidate
                difference.close()
                resized.close()
                rendered.close()
                probe.close()
    target.close()
    if best is None:
        return fallback_family, resolve_font_file(fallback_family), fallback_size, False, False, 0.0
    score, family, font_file, font_size, bold, italic = best
    confidence = max(0.0, min(1.0, 1.0 - score / 2.25))
    return family, font_file, round(font_size, 1), bold, italic, round(confidence, 3)


def _median_rgb(samples):
    values = list(samples)
    if not values:
        return 255, 255, 255
    return tuple(int(sorted(int(value[channel]) for value in values)[len(values) // 2]) for channel in range(3))


def _luma(color) -> float:
    return float(color[0]) * 0.2126 + float(color[1]) * 0.7152 + float(color[2]) * 0.0722


def _clamp_int_box(rect, size):
    width, height = size
    x0 = max(0, min(width - 1, int(rect[0])))
    y0 = max(0, min(height - 1, int(rect[1])))
    x1 = max(x0 + 1, min(width, int(rect[2])))
    y1 = max(y0 + 1, min(height, int(rect[3])))
    return x0, y0, x1, y1


def pdf_has_signatures(input_pdf: os.PathLike[str] | str) -> bool:
    import fitz

    with fitz.open(str(input_pdf)) as document:
        try:
            return document.get_sigflags() > 0
        except (AttributeError, RuntimeError):
            return False


def apply_pdf_edits(
    input_pdf: os.PathLike[str] | str,
    output_pdf: os.PathLike[str] | str,
    text_edits: Sequence[TextEdit],
    signature_edits: Sequence[SignatureEdit],
    region_edits: Sequence[RegionCopyEdit] = (),
    stroke_edits: Sequence[StrokeEdit] = (),
    crop_edits: Sequence[CropEdit] = (),
    alignment_edits: Sequence[AlignmentEdit] = (),
) -> Path:
    import fitz

    from .document_input import open_pdf_input
    from .user_errors import input_path

    source = input_path(input_pdf)
    output = Path(output_pdf)
    if _same_document_path(source, output):
        raise ValueError("Сохраните изменённый документ под новым именем")
    if region_edits and pdf_has_signatures(source):
        raise RuntimeError("Копирование областей отключено для PDF с криптографической подписью")
    output.parent.mkdir(parents=True, exist_ok=True)

    with _aligned_pdf_source(source, alignment_edits) as aligned_source, open_pdf_input(aligned_source) as document:
        by_page = {}
        for edit in text_edits:
            by_page.setdefault(edit.page_index, []).append(edit)

        for page_index, edits in by_page.items():
            page = document[page_index]
            opaque_edits = [edit for edit in edits if edit.opaque_background]
            for edit in opaque_edits:
                page.add_redact_annot(_page_edit_rect(page, edit.rect), fill=edit.background_color)
            if opaque_edits:
                page.apply_redactions(images=fitz.PDF_REDACT_IMAGE_NONE)

        for edit in text_edits:
            if not edit.replacement_background or not edit.replacement_background_rect:
                continue
            background = Path(edit.replacement_background)
            if background.is_file():
                page = document[edit.page_index]
                page.insert_image(
                    _page_edit_rect(page, edit.replacement_background_rect),
                    filename=str(background),
                    rotate=page.rotation,
                    keep_proportion=False,
                    overlay=True,
                )

        for edit in signature_edits:
            page = document[edit.page_index]
            rect = _page_edit_rect(page, edit.rect)
            page.insert_image(
                rect,
                filename=edit.image_path,
                rotate=page.rotation,
                keep_proportion=edit.keep_proportion,
                overlay=True,
            )

        if region_edits:
            with fitz.open(str(aligned_source)) as original:
                for edit in region_edits:
                    source_page = original[edit.page_index]
                    pixmap = source_page.get_pixmap(
                        matrix=fitz.Matrix(2.0, 2.0),
                        clip=fitz.Rect(edit.source_rect),
                        alpha=False,
                    )
                    page = document[edit.page_index]
                    page.insert_image(
                        _page_edit_rect(page, edit.rect),
                        stream=pixmap.tobytes("png"),
                        rotate=page.rotation,
                        keep_proportion=False,
                        overlay=True,
                    )

        for edit in stroke_edits:
            page = document[edit.page_index]
            points = edit.points
            if edit.style in {"line", "arrow"} and len(points) >= 2:
                points = (points[0], points[-1])
            if edit.style == "eraser":
                radius = max(0.5, edit.width / 2)
                for point in points:
                    page.draw_rect(
                        _page_edit_rect(page, (point[0] - radius, point[1] - radius, point[0] + radius, point[1] + radius)),
                        color=edit.color,
                        fill=edit.color,
                        width=0,
                        overlay=True,
                    )
            if len(edit.points) == 1:
                point = _page_edit_point(page, edit.points[0])
                if edit.style != "eraser":
                    page.draw_circle(point, max(0.5, edit.width / 2), color=edit.color, fill=edit.color, overlay=True)
                continue
            for start, end in zip(points, points[1:]):
                page.draw_line(
                    _page_edit_point(page, start),
                    _page_edit_point(page, end),
                    color=edit.color,
                    width=max(0.5, edit.width),
                    overlay=True,
                )
            if edit.style == "arrow" and len(points) >= 2:
                for start, end in _arrow_head(points[-2], points[-1], edit.width):
                    page.draw_line(
                        _page_edit_point(page, start),
                        _page_edit_point(page, end),
                        color=edit.color,
                        width=max(0.5, edit.width),
                        overlay=True,
                    )

        # Text is the topmost editor layer and has no background by default.
        for page_index, edits in by_page.items():
            page = document[page_index]
            for edit_index, edit in enumerate(edits):
                if not edit.text:
                    continue
                rect = fitz.Rect(edit.rect)
                font_file = edit.font_file or resolve_font_file(edit.font_family, edit.bold, edit.italic) or _find_text_font()
                font_name = f"pdfbars{page_index}_{edit_index}"
                if font_file:
                    page.insert_font(fontname=font_name, fontfile=font_file)
                    try:
                        font_ascender = max(0.1, float(fitz.Font(fontfile=font_file).ascender))
                    except (RuntimeError, ValueError, OSError):
                        font_ascender = 1.0
                else:
                    font_name = "helv"
                    font_ascender = float(fitz.Font("helv").ascender)
                raster_word = bool(
                    (edit.replacement_source_rect and edit.scan_effect)
                    or (not edit.replacement_source_rect and edit.scan_blur > 0)
                )
                if raster_word:
                    blur_stream, blur_rect = _scanned_text_blur_png(edit)
                    page.insert_image(
                        _page_edit_rect(page, blur_rect),
                        stream=blur_stream,
                        rotate=page.rotation,
                        keep_proportion=False,
                        overlay=True,
                    )
                # The editor anchors text at rect.x0 / rect.y0. Deriving the
                # baseline from the lower edge made text jump after resizing.
                baseline = rect.y0 + edit.font_size * font_ascender
                page.insert_text(
                    _page_edit_point(page, (rect.x0, baseline)),
                    edit.text,
                    rotate=page.rotation,
                    fontsize=edit.font_size,
                    fontname=font_name,
                    color=edit.color,
                    lineheight=1.2,
                    overlay=True,
                    render_mode=3 if raster_word else 0,
                )
                if edit.underline and not raster_word:
                    _draw_pdf_underline(page, edit, font_name, baseline)

        for edit in crop_edits:
            page = document[edit.page_index]
            crop = _clamp_rect(edit.rect, (page.rect.width, page.rect.height))
            if crop[2] - crop[0] >= 5 and crop[3] - crop[1] >= 5:
                box = _page_edit_rect(page, crop)
                offset = page.cropbox_position
                page.set_cropbox(box + (offset.x, offset.y, offset.x, offset.y))

        metadata = document.metadata or {}
        metadata["producer"] = f"pdf bars {__version__} — документ отредактирован"
        metadata["keywords"] = "корректировка; копирование области; визуальная подпись не является электронной подписью"
        document.set_metadata(metadata)
        remove_pdfa_declaration(document)

        temporary_output = _temporary_pdf_output_path(output)
        try:
            # PyMuPDF does not overwrite an existing destination. Saving beside
            # it first also keeps the prior file intact if the network share
            # disconnects or writing the edited copy fails.
            document.save(str(temporary_output), garbage=4, deflate=True, clean=True)
            os.replace(temporary_output, output)
        except Exception:
            temporary_output.unlink(missing_ok=True)
            raise
    return output


def _same_document_path(source: Path, output: Path) -> bool:
    """Compare paths defensively, including aliases on mounted network shares."""
    try:
        if source.exists() and output.exists() and os.path.samefile(source, output):
            return True
    except OSError:
        pass
    try:
        source_value = source.expanduser().resolve(strict=False)
        output_value = output.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        source_value = source.expanduser().absolute()
        output_value = output.expanduser().absolute()
    return os.path.normcase(os.path.realpath(str(source_value))) == os.path.normcase(os.path.realpath(str(output_value)))


def _temporary_pdf_output_path(output: Path) -> Path:
    """Reserve a sibling path, then remove it because PyMuPDF requires a new file."""
    descriptor, name = tempfile.mkstemp(
        prefix=f".{output.stem}-",
        suffix=output.suffix or ".pdf",
        dir=str(output.parent),
    )
    os.close(descriptor)
    temporary = Path(name)
    temporary.unlink()
    return temporary


def apply_image_edits(
    input_image: os.PathLike[str] | str,
    output_file: os.PathLike[str] | str,
    text_edits: Sequence[TextEdit],
    signature_edits: Sequence[SignatureEdit],
    region_edits: Sequence[RegionCopyEdit] = (),
    stroke_edits: Sequence[StrokeEdit] = (),
    crop_rect: RectTuple | None = None,
    dpi: int = 150,
    alignment_edits: Sequence[AlignmentEdit] = (),
) -> Path:
    """Apply visual editor objects to a raster image and save it as image or PDF."""
    from PIL import Image, ImageOps

    source = Path(input_image)
    output = Path(output_file)
    output.parent.mkdir(parents=True, exist_ok=True)

    canvas = render_image_page_image(source, text_edits, signature_edits, region_edits,
                                     stroke_edits, crop_rect, alignment_edits)
    try:
        _save_raster(canvas, output, dpi)
    finally:
        canvas.close()
    return output


def render_image_page_image(source, text_edits=(), signature_edits=(), region_edits=(),
                            stroke_edits=(), crop_rect=None, alignment_edits=()):
    """Compose image edits in memory, also used by the clipboard and eyedropper."""
    from PIL import Image, ImageOps

    with Image.open(source) as original_image:
        original = ImageOps.exif_transpose(original_image).convert("RGBA")
    canvas = original.copy()
    angle = alignment_angle_for_page(alignment_edits, 0)
    if abs(angle) >= 0.01:
        rotated = rotate_raster_image(canvas, angle)
        canvas.close()
        canvas = rotated
        original.close()
        original = canvas.copy()
    canvas = _compose_raster_page(
        canvas,
        original,
        [edit for edit in text_edits if edit.page_index == 0],
        [edit for edit in signature_edits if edit.page_index == 0],
        [edit for edit in region_edits if edit.page_index == 0],
        [edit for edit in stroke_edits if edit.page_index == 0],
        crop_rect,
        scale=1.0,
    )
    original.close()
    return canvas


def render_pdf_page_edits_to_image(
    input_pdf: os.PathLike[str] | str,
    output_file: os.PathLike[str] | str,
    page_index: int,
    text_edits: Sequence[TextEdit],
    signature_edits: Sequence[SignatureEdit],
    region_edits: Sequence[RegionCopyEdit] = (),
    stroke_edits: Sequence[StrokeEdit] = (),
    crop_rect: RectTuple | None = None,
    dpi: int = 150,
    alignment_edits: Sequence[AlignmentEdit] = (),
) -> Path:
    """Save one visually edited PDF page as a flat image."""
    output = Path(output_file)
    output.parent.mkdir(parents=True, exist_ok=True)
    canvas = render_pdf_page_image(input_pdf, page_index, text_edits, signature_edits,
                                  region_edits, stroke_edits, crop_rect, dpi, alignment_edits)
    try:
        _save_raster(canvas, output, dpi)
    finally:
        canvas.close()
    return output


def render_pdf_page_image(input_pdf, page_index, text_edits=(), signature_edits=(),
                          region_edits=(), stroke_edits=(), crop_rect=None, dpi=150,
                          alignment_edits=()):
    """Compose a page in RAM for export or an edited thumbnail."""
    import fitz
    from PIL import Image

    zoom = dpi / 72.0
    with fitz.open(str(input_pdf)) as document:
        page = document[page_index]
        pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    canvas = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples).convert("RGBA")
    original = canvas.copy()
    angle = alignment_angle_for_page(alignment_edits, page_index)
    if abs(angle) >= 0.01:
        canvas = rotate_raster_image(canvas, angle)
        original.close()
        original = canvas.copy()
    canvas = _compose_raster_page(
        canvas,
        original,
        [edit for edit in text_edits if edit.page_index == page_index],
        [edit for edit in signature_edits if edit.page_index == page_index],
        [edit for edit in region_edits if edit.page_index == page_index],
        [edit for edit in stroke_edits if edit.page_index == page_index],
        crop_rect,
        scale=zoom,
    )
    original.close()
    return canvas


def _compose_raster_page(
    canvas,
    original,
    text_edits: Sequence[TextEdit],
    signature_edits: Sequence[SignatureEdit],
    region_edits: Sequence[RegionCopyEdit],
    stroke_edits: Sequence[StrokeEdit],
    crop_rect: RectTuple | None,
    scale: float,
):
    from PIL import Image, ImageDraw

    draw = ImageDraw.Draw(canvas)
    # Closing rectangles are the bottom editor layer. They cover the source
    # page, while pasted regions, drawings and text remain visible above them.
    for edit in text_edits:
        if edit.opaque_background:
            rect = _scale_rect(edit.rect, scale)
            draw.rectangle(_int_box(rect), fill=_rgb255(edit.background_color) + (255,))

    for edit in text_edits:
        if not edit.replacement_background or not edit.replacement_background_rect:
            continue
        background = Path(edit.replacement_background)
        if not background.is_file():
            continue
        rect = _scale_rect(edit.replacement_background_rect, scale)
        with Image.open(background) as opened:
            overlay = opened.convert("RGBA")
        _paste_fit(canvas, overlay, rect, keep_proportion=False)
        overlay.close()

    for edit in signature_edits:
        rect = _scale_rect(edit.rect, scale)
        with Image.open(edit.image_path) as signature:
            overlay = signature.convert("RGBA")
        _paste_fit(canvas, overlay, rect, keep_proportion=edit.keep_proportion)
        overlay.close()

    for edit in region_edits:
        source_rect = _int_box(_scale_rect(edit.source_rect, scale))
        target_rect = _int_box(_scale_rect(edit.rect, scale))
        if source_rect[2] > source_rect[0] and source_rect[3] > source_rect[1]:
            piece = original.crop(source_rect)
            piece = piece.resize(
                (max(1, target_rect[2] - target_rect[0]), max(1, target_rect[3] - target_rect[1])),
                _lanczos_filter(Image),
            )
            canvas.alpha_composite(piece, (target_rect[0], target_rect[1]))
            piece.close()

    for edit in stroke_edits:
        points = [(x * scale, y * scale) for x, y in edit.points]
        if edit.style in {"line", "arrow"} and len(points) >= 2:
            points = [points[0], points[-1]]
        width = max(1, int(edit.width * scale))
        fill = _rgb255(edit.color) + (255,)
        if len(points) == 1:
            x, y = points[0]
            radius = max(1, width // 2)
            if edit.style == "eraser":
                draw.rectangle((x - radius, y - radius, x + radius, y + radius), fill=fill)
            else:
                draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)
        elif points:
            try:
                draw.line(points, fill=fill, width=width, joint="curve")
            except TypeError:
                draw.line(points, fill=fill, width=width)
            if edit.style == "eraser":
                radius = max(1, width // 2)
                for x, y in points:
                    draw.rectangle((x - radius, y - radius, x + radius, y + radius), fill=fill)
            if edit.style == "arrow" and len(points) >= 2:
                for start, end in _arrow_head(points[-2], points[-1], width):
                    draw.line((start, end), fill=fill, width=width)

    for edit in text_edits:
        rect = _scale_rect(edit.rect, scale)
        if edit.text:
            font = _image_font(
                edit.font_file or resolve_font_file(edit.font_family, edit.bold, edit.italic),
                max(1, int(edit.font_size * scale)),
            )
            if (
                edit.replacement_source_rect
                or (not edit.replacement_source_rect and edit.scan_blur > 0)
            ):
                blur_layer, bounds = word_text_overlay(edit)
                target = _scale_rect(bounds, scale)
                resized = blur_layer.resize((max(1, round(target[2]-target[0])), max(1, round(target[3]-target[1]))), Image.Resampling.LANCZOS)
                blur_layer.close()
                blur_layer, blur_left, blur_top = resized, round(target[0]), round(target[1])
                _alpha_composite_clipped(canvas, blur_layer, blur_left, blur_top)
                blur_layer.close()
                draw = ImageDraw.Draw(canvas)
                continue
            draw.multiline_text(
                (rect[0], rect[1]),
                edit.text,
                font=font,
                fill=_rgb255(edit.color) + (255,),
                spacing=max(1, int(edit.font_size * scale * 0.15)),
            )
            if edit.underline:
                _draw_raster_underline(draw, edit, rect, font, scale)

    if crop_rect:
        crop = _int_box(_clamp_rect(_scale_rect(crop_rect, scale), canvas.size))
        if crop[2] - crop[0] >= 5 and crop[3] - crop[1] >= 5:
            cropped = canvas.crop(crop)
            canvas.close()
            return cropped
    return canvas


def _save_raster(image, output: Path, dpi: int) -> None:
    suffix = output.suffix.lower()
    if suffix == ".pdf":
        page = _flatten_for_jpeg(image)
        page.save(str(output), "PDF", resolution=dpi)
        page.close()
        return
    if suffix not in IMAGE_OUTPUT_FORMATS:
        raise ValueError("Поддерживаются форматы: PDF, PNG, JPG, WEBP, TIFF, BMP")
    if suffix in {".jpg", ".jpeg", ".bmp"}:
        page = _flatten_for_jpeg(image)
        kwargs = {"quality": 95, "optimize": True} if suffix in {".jpg", ".jpeg"} else {}
        page.save(str(output), IMAGE_OUTPUT_FORMATS[suffix], **kwargs)
        page.close()
        return
    image.save(str(output), IMAGE_OUTPUT_FORMATS[suffix], optimize=suffix in {".png", ".webp"})


def _lanczos_filter(image_module):
    return getattr(getattr(image_module, "Resampling", image_module), "LANCZOS")


def _paste_fit(canvas, overlay, rect: RectTuple, keep_proportion: bool = True) -> None:
    from PIL import Image

    target = _int_box(rect)
    width = max(1, target[2] - target[0])
    height = max(1, target[3] - target[1])
    if keep_proportion:
        overlay.thumbnail((width, height))
        left = target[0] + (width - overlay.width) // 2
        top = target[1] + (height - overlay.height) // 2
        canvas.alpha_composite(overlay, (left, top))
    else:
        resized = overlay.resize((width, height), _lanczos_filter(Image))
        canvas.alpha_composite(resized, (target[0], target[1]))
        resized.close()


def _image_font(font_file: str | None, size: int):
    from PIL import ImageFont

    if font_file and Path(font_file).exists():
        try:
            return ImageFont.truetype(font_file, size=size)
        except OSError:
            pass
    fallback = resolve_font_file("DejaVu Sans")
    if fallback:
        try:
            return ImageFont.truetype(fallback, size=size)
        except OSError:
            pass
    return ImageFont.load_default()


def _scanned_text_blur_layer(edit: TextEdit, scale: float):
    from PIL import Image, ImageDraw, ImageFilter

    scaled_rect = _scale_rect(edit.rect, scale)
    radius = max(0.0, float(edit.scan_blur) * scale)
    padding = max(2, int(math.ceil(radius * 3.0)))
    width = max(1, int(math.ceil(scaled_rect[2] - scaled_rect[0]))) + padding * 2
    height = max(1, int(math.ceil(scaled_rect[3] - scaled_rect[1]))) + padding * 2
    layer = Image.new("RGBA", (width, height), _rgb255(edit.color) + (0,))
    font = _image_font(
        edit.font_file or resolve_font_file(edit.font_family, edit.bold, edit.italic),
        max(1, int(round(edit.font_size * scale))),
    )
    draw = ImageDraw.Draw(layer)
    draw.multiline_text(
        (padding, padding),
        edit.text,
        font=font,
        fill=_rgb255(edit.color) + (255,),
        spacing=max(1, int(edit.font_size * scale * 0.15)),
    )
    if edit.underline:
        _draw_raster_underline(draw, edit, (padding, padding, width-padding, height-padding), font, scale)
    blurred = layer.filter(ImageFilter.GaussianBlur(radius))
    layer.close()
    return blurred, int(math.floor(scaled_rect[0])) - padding, int(math.floor(scaled_rect[1])) - padding


def _scanned_text_blur_png(edit: TextEdit, scale: float = 4.0) -> tuple[bytes, RectTuple]:
    layer, rect = word_text_overlay(edit, scale)
    buffer = BytesIO()
    layer.save(buffer, "PNG", optimize=True)
    layer.close()
    return buffer.getvalue(), rect


def word_text_overlay(edit: TextEdit, scale: float = 4.0):
    """One visible glyph layer for editor, raster export and saved PDF."""
    if edit.replacement_source_rect and not edit.scan_effect:
        return _vector_word_overlay(edit, scale)
    layer, left, top = _scanned_text_blur_layer(edit, scale)
    rect = (
        left / scale,
        top / scale,
        (left + layer.width) / scale,
        (top + layer.height) / scale,
    )
    return layer, rect


def _vector_word_overlay(edit: TextEdit, scale: float):
    import fitz
    from dataclasses import replace
    from PIL import Image

    # Match saved vector text, including its baseline, instead of Tk font metrics.
    padding = max(4.0, edit.font_size * 0.5)
    width = max(edit.rect[2] - edit.rect[0], measure_editor_text(
        edit.text, edit.font_family, edit.font_size, edit.bold, edit.italic)) + padding * 2
    height = max(edit.rect[3] - edit.rect[1], edit.font_size * 1.5) + padding * 2
    font_file = edit.font_file or resolve_font_file(edit.font_family, edit.bold, edit.italic) or _find_text_font()
    with fitz.open() as document:
        page = document.new_page(width=width, height=height)
        font_name = "wordpreview" if font_file else "helv"
        if font_file:
            page.insert_font(fontname=font_name, fontfile=font_file)
        ascender = fitz.Font(fontfile=font_file).ascender if font_file else fitz.Font("helv").ascender
        baseline = padding + edit.font_size * max(0.1, ascender)
        page.insert_text((padding, baseline), edit.text, fontsize=edit.font_size,
                         fontname=font_name, color=edit.color, lineheight=1.2)
        if edit.underline:
            local = replace(edit, rect=(padding, padding, width-padding, height-padding))
            _draw_pdf_underline(page, local, font_name, baseline)
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=True)
    # MuPDF exposes premultiplied alpha, while Pillow expects straight alpha.
    premultiplied = Image.frombytes("RGBa", (pix.width, pix.height), pix.samples)
    layer = premultiplied.convert("RGBA")
    premultiplied.close()
    left, top = edit.rect[0] - padding, edit.rect[1] - padding
    return layer, (left, top, left + layer.width / scale, top + layer.height / scale)


def _alpha_composite_clipped(canvas, overlay, left: int, top: int) -> None:
    destination_x = max(0, int(left))
    destination_y = max(0, int(top))
    source_x = max(0, -int(left))
    source_y = max(0, -int(top))
    width = min(overlay.width - source_x, canvas.width - destination_x)
    height = min(overlay.height - source_y, canvas.height - destination_y)
    if width <= 0 or height <= 0:
        return
    if source_x == 0 and source_y == 0 and width == overlay.width and height == overlay.height:
        canvas.alpha_composite(overlay, (destination_x, destination_y))
        return
    clipped = overlay.crop((source_x, source_y, source_x + width, source_y + height))
    canvas.alpha_composite(clipped, (destination_x, destination_y))
    clipped.close()


def alignment_angle_for_page(alignment_edits: Sequence[AlignmentEdit], page_index: int) -> float:
    for edit in reversed(alignment_edits):
        if edit.page_index == page_index:
            return max(-15.0, min(15.0, float(edit.angle)))
    return 0.0


def rotate_raster_image(image, angle: float):
    from PIL import Image

    resampling = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
    fill = (255, 255, 255, 255) if image.mode == "RGBA" else 255 if image.mode == "L" else "white"
    return image.rotate(float(angle), resample=resampling, expand=False, fillcolor=fill)


def estimate_page_alignment(input_file: os.PathLike[str] | str, page_index: int = 0, dpi: int = 120) -> float:
    from PIL import Image, ImageOps
    from .ocr import estimate_skew_angle

    source = Path(input_file)
    if source.suffix.lower() == ".pdf":
        import fitz

        with fitz.open(str(source)) as document:
            page = document[page_index]
            zoom = max(1.0, min(3.0, dpi / 72.0))
            pixmap = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    else:
        with Image.open(source) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
    try:
        angle = float(estimate_skew_angle(image))
        return round(max(-15.0, min(15.0, angle)), 2)
    finally:
        image.close()


@contextmanager
def _aligned_pdf_source(source: Path, alignment_edits: Sequence[AlignmentEdit]):
    edits = {edit.page_index: alignment_angle_for_page(alignment_edits, edit.page_index) for edit in alignment_edits}
    edits = {index: angle for index, angle in edits.items() if abs(angle) >= 0.01}
    if not edits:
        yield source
        return

    from pypdf import PdfReader, PdfWriter, Transformation

    with tempfile.TemporaryDirectory(prefix="pdf-bars-align-") as temp_dir:
        destination = Path(temp_dir) / "aligned.pdf"
        reader = PdfReader(str(source))
        writer = PdfWriter()
        for page_index, page in enumerate(reader.pages):
            writer.add_page(page)
            target_page = writer.pages[-1]
            angle = edits.get(page_index, 0.0)
            if angle:
                width = float(target_page.mediabox.width)
                height = float(target_page.mediabox.height)
                transform = (
                    Transformation()
                    .translate(-width / 2.0, -height / 2.0)
                    .rotate(angle)
                    .translate(width / 2.0, height / 2.0)
                )
                target_page.add_transformation(transform)
        with destination.open("wb") as stream:
            writer.write(stream)
        yield destination


def _page_edit_rect(page, rect):
    import fitz

    # Editor coordinates follow the displayed page; PDF drawing uses the
    # unrotated page coordinate system, including for already rotated scans.
    return fitz.Rect(rect) * page.derotation_matrix


def _page_edit_point(page, point):
    import fitz

    return fitz.Point(*point) * page.derotation_matrix


def _draw_pdf_underline(page, edit: TextEdit, font_name: str, baseline: float) -> None:
    line_height = max(1.0, edit.font_size * 1.2)
    for line_index, line in enumerate(edit.text.splitlines() or [edit.text]):
        if not line:
            continue
        try:
            width = page.get_text_length(line, fontname=font_name, fontsize=edit.font_size)
        except Exception:
            width = len(line) * edit.font_size * 0.55
        y = baseline + line_index * line_height + max(0.7, edit.font_size * 0.08)
        page.draw_line(
            _page_edit_point(page, (edit.rect[0], y)),
            _page_edit_point(page, (min(edit.rect[2], edit.rect[0] + width), y)),
            color=edit.color,
            width=max(0.5, edit.font_size * 0.055),
            overlay=True,
        )


def _draw_raster_underline(draw, edit: TextEdit, rect: RectTuple, font, scale: float) -> None:
    y = rect[1]
    spacing = max(1, int(edit.font_size * scale * 0.15))
    for line in edit.text.splitlines() or [edit.text]:
        bbox = draw.textbbox((rect[0], y), line or " ", font=font)
        if line:
            underline_y = bbox[3] + max(1, int(edit.font_size * scale * 0.06))
            draw.line((rect[0], underline_y, min(rect[2], bbox[2]), underline_y), fill=_rgb255(edit.color) + (255,), width=max(1, int(edit.font_size * scale * 0.055)))
        y = bbox[3] + spacing


def _flatten_for_jpeg(image):
    from PIL import Image

    if image.mode == "RGB":
        return image.copy()
    base = Image.new("RGBA", image.size, "white")
    base.alpha_composite(image.convert("RGBA"))
    result = base.convert("RGB")
    base.close()
    return result


def _rgb255(color: Tuple[float, float, float]) -> Tuple[int, int, int]:
    return tuple(max(0, min(255, int(round(value * 255)))) for value in color)


def _scale_rect(rect: RectTuple, scale: float) -> RectTuple:
    return tuple(value * scale for value in _normalize_rect(rect))


def _normalize_rect(rect: RectTuple) -> RectTuple:
    x0, y0, x1, y1 = rect
    return min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1)


def _clamp_rect(rect: RectTuple, size: Tuple[float, float]) -> RectTuple:
    width, height = size
    x0, y0, x1, y1 = _normalize_rect(rect)
    return (
        max(0.0, min(width, x0)),
        max(0.0, min(height, y0)),
        max(0.0, min(width, x1)),
        max(0.0, min(height, y1)),
    )


def _int_box(rect: RectTuple) -> Tuple[int, int, int, int]:
    x0, y0, x1, y1 = _normalize_rect(rect)
    return int(round(x0)), int(round(y0)), int(round(x1)), int(round(y1))


def signature_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        directory = base / "PDFBars" / "signatures"
    else:
        directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pdf-bars" / "signatures"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def list_signatures() -> List[Path]:
    return sorted(signature_directory().glob("*.png"), key=lambda path: path.stem.casefold())


def import_signature(
    source: os.PathLike[str] | str,
    name: str,
    remove_background: bool = True,
    enhance: bool = False,
) -> Path:
    from PIL import Image

    clean_name = re.sub(r"[^0-9A-Za-zА-Яа-яЁё._ -]+", "_", name).strip(" ._")
    if not clean_name:
        raise ValueError("Укажите название подписи")
    destination = signature_directory() / f"{clean_name}.png"
    if destination.exists():
        raise FileExistsError(f"Подпись «{clean_name}» уже существует")

    with Image.open(source) as image:
        result = image.convert("RGBA")
        result.thumbnail((2400, 1200), _lanczos_filter(Image))
        if enhance:
            result = enhance_signature(result)
        if remove_background:
            result = _make_white_transparent(result)
        bbox = result.getchannel("A").getbbox()
        if not bbox:
            raise ValueError("Не удалось выделить штрихи подписи. Выберите более чёткое изображение или включите усиление.")
        result = result.crop(bbox)
        result.save(destination, "PNG", optimize=True)
    return destination


def enhance_signature(image):
    from PIL import Image

    red, green, blue, alpha = image.convert("RGBA").split()
    table = [max(0, round(255 - (255 - value) * 2.4)) for value in range(256)]
    return Image.merge("RGBA", (red.point(table), green.point(table), blue.point(table), alpha))


def prepare_signature_for_placement(source, destination):
    """Clean library signatures only; copied page fragments keep their paper."""
    from PIL import Image, ImageChops

    with Image.open(source) as image:
        cleaned = image.convert("RGBA")
        if cleaned.getchannel("A").getextrema()[0] == 255:
            cleaned = _make_white_transparent(cleaned)
        else:
            # Cropped transparent signatures can have ink on every visible
            # border pixel; do not mistake that ink for a paper background.
            red, green, blue, alpha = cleaned.split()
            minimum = ImageChops.darker(ImageChops.darker(red, green), blue)
            foreground = minimum.point(lambda value: 0 if value >= 245 else 255)
            cleaned.putalpha(ImageChops.multiply(alpha, foreground))
        cleaned.save(destination, "PNG")
        cleaned.close()
    return Path(destination)


def delete_signature(path: os.PathLike[str] | str) -> None:
    signature = Path(path)
    if signature.parent.resolve() != signature_directory().resolve():
        raise ValueError("Можно удалять только подписи из библиотеки pdf bars")
    signature.unlink(missing_ok=True)


def _arrow_head(start: PointTuple, end: PointTuple, width: float) -> tuple[tuple[PointTuple, PointTuple], ...]:
    dx = end[0] - start[0]
    dy = end[1] - start[1]
    length = math.hypot(dx, dy)
    if length < 1e-6:
        return ()
    ux, uy = dx / length, dy / length
    size = min(length * 0.45, max(8.0, float(width) * 4.5))
    wing = size * 0.55
    base_x, base_y = end[0] - ux * size, end[1] - uy * size
    perpendicular_x, perpendicular_y = -uy, ux
    return (
        (end, (base_x + perpendicular_x * wing, base_y + perpendicular_y * wing)),
        (end, (base_x - perpendicular_x * wing, base_y - perpendicular_y * wing)),
    )


def _make_white_transparent(image):
    width, height = image.size
    if width < 2 or height < 2:
        return image

    source = image.load()
    step = max(1, min(width, height) // 180)
    border = []
    for x in range(0, width, step):
        border.extend((source[x, 0], source[x, height - 1]))
    for y in range(0, height, step):
        border.extend((source[0, y], source[width - 1, y]))
    visible_border = [pixel for pixel in border if pixel[3] > 16]
    if not visible_border:
        return image

    def median_channel(index: int) -> int:
        values = sorted(pixel[index] for pixel in visible_border)
        return values[len(values) // 2]

    background = tuple(median_channel(index) for index in range(3))
    border_distances = sorted(
        math.sqrt(sum((pixel[index] - background[index]) ** 2 for index in range(3)))
        for pixel in visible_border
    )
    noise_distance = border_distances[min(len(border_distances) - 1, int(len(border_distances) * 0.9))]
    clear_distance = max(14.0, min(55.0, noise_distance + 10.0))
    solid_distance = min(135.0, clear_distance + 70.0)

    cleaned = []
    for red, green, blue, alpha in image.getdata():
        if alpha == 0:
            cleaned.append((red, green, blue, 0))
            continue
        distance = math.sqrt(
            (red - background[0]) ** 2
            + (green - background[1]) ** 2
            + (blue - background[2]) ** 2
        )
        if distance <= clear_distance:
            opacity = 0.0
        elif distance >= solid_distance:
            opacity = 1.0
        else:
            progress = (distance - clear_distance) / (solid_distance - clear_distance)
            opacity = progress * progress * (3.0 - 2.0 * progress)
        new_alpha = int(round(alpha * opacity))
        if new_alpha < 8:
            cleaned.append((0, 0, 0, 0))
        else:
            cleaned.append((red, green, blue, new_alpha))
    image.putdata(cleaned)
    return image


def _find_text_font() -> str | None:
    candidates = (
        Path("/usr/share/fonts/dejavu/DejaVuSans.ttf"),
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    )
    for candidate in candidates:
        if candidate.exists():
            return str(candidate)
    return None
