from __future__ import annotations

import re
import os
import threading
import multiprocessing
import time
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Callable, Iterable, List, Sequence, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageOps
from . import diagnostics


RectTuple = Tuple[float, float, float, float]
ProgressCallback = Callable[[int, int, bool], None]


# MuPDF's embedded Tesseract / Leptonica bridge is process-global and is not
# safe to enter from multiple threads. Image preparation may still run in
# parallel, but every OCR engine call must pass through this lock.
_OCR_ENGINE_LOCK = threading.Lock()


@dataclass(frozen=True)
class IndexedWord:
    rect: RectTuple
    text: str
    block: int
    line: int
    order: int


@dataclass(frozen=True)
class PageText:
    page_index: int
    width: float
    height: float
    words: Tuple[IndexedWord, ...]
    used_ocr: bool = False


@dataclass(frozen=True)
class SearchHit:
    page_index: int
    rect: RectTuple
    text: str


def tessdata_path() -> Path:
    return Path(__file__).with_name("assets") / "tessdata"


def ocr_models_available(path: Path | None = None, language: str = "rus+eng") -> bool:
    root = path or tessdata_path()
    languages = [item.strip() for item in language.split("+") if item.strip()]
    return bool(languages) and all((root / f"{item}.traineddata").is_file() for item in languages)


@diagnostics.operation
def build_text_index(
    source: str | Path,
    *,
    force_ocr: bool = False,
    dpi: int = 200,
    progress: ProgressCallback | None = None,
    tessdata: str | Path | None = None,
    language: str = "rus+eng",
    enhance: bool = True,
    workers: int | None = None,
    page_indexes: Sequence[int] | None = None,
    quality: str = "best",
) -> List[PageText]:
    import fitz
    diagnostics.note(dpi=dpi, language=language, quality=quality, enhance=enhance,
                     force_ocr=force_ocr, available_memory_bytes=_available_memory_bytes())

    model_dir = Path(tessdata) if tessdata is not None else tessdata_path()
    if force_ocr and not ocr_models_available(model_dir, language):
        raise RuntimeError("Не найдена встроенная модель выбранного языка OCR")

    pages_by_index: dict[int, PageText] = {}
    ocr_page_indexes: list[int] = []
    largest_raster = 0
    with fitz.open(str(source)) as document:
        indexes = list(range(document.page_count)) if page_indexes is None else sorted(set(page_indexes))
        if any(index < 0 or index >= document.page_count for index in indexes):
            raise IndexError("Страница для распознавания не существует")
        total = len(indexes)
        diagnostics.note(source_pages=document.page_count, requested_pages=total)
        for page_index in indexes:
            page = document[page_index]
            native_words = _displayed_words(page, page.get_text("words"))
            use_ocr = force_ocr or not _contains_meaningful_text(native_words)
            if use_ocr:
                if not ocr_models_available(model_dir, language):
                    raise RuntimeError("Не найдена встроенная модель выбранного языка OCR")
                ocr_page_indexes.append(page_index)
                largest_raster = max(largest_raster, int(page.rect.width * page.rect.height * (dpi / 72.0) ** 2))
            else:
                pages_by_index[page_index] = PageText(
                    page_index,
                    float(page.rect.width),
                    float(page.rect.height),
                    tuple(_indexed_words(native_words)),
                    False,
                )
                if progress:
                    progress(len(pages_by_index), total, False)

    completed = len(pages_by_index)
    if ocr_page_indexes:
        # OCR workers are isolated processes. On a four-thread workstation,
        # two workers left one useful CPU core idle while the UI waited for a
        # long queue of scanned pages. Keep one core available for RED OS and
        # the interface, but allow a third worker when RAM permits it.
        requested_workers = int(workers) if workers is not None else max(1, min(3, (os.cpu_count() or 2) - 1))
        worker_count = _ocr_worker_count(requested_workers, len(ocr_page_indexes), largest_raster)
        diagnostics.note(workers=worker_count, requested_workers=requested_workers,
                         largest_raster_pixels=largest_raster)
        submitted = time.monotonic()
        # Even one-page OCR owns a separate engine process: the GUI can render
        # safely, and subsequent tasks never reuse Leptonica on another thread.
        with ProcessPoolExecutor(max_workers=worker_count, mp_context=multiprocessing.get_context("spawn"),
                                 initializer=_initialize_ocr_worker) as executor:
            futures = {
                executor.submit(_diagnosed_ocr_source_page, source, page_index, language, dpi,
                                model_dir, enhance, quality): page_index
                for page_index in ocr_page_indexes
            }
            for future in as_completed(futures):
                page, metrics, error = future.result()
                diagnostics.record(dict(metrics, kind="ocr_page", run_id=diagnostics.current_run(),
                                        page_index=futures[future], received_seconds=round(time.monotonic()-submitted, 4)))
                if error is not None:
                    raise RuntimeError(error)
                pages_by_index[page.page_index] = page
                completed += 1
                if progress:
                    progress(completed, total, True)
    return [pages_by_index[index] for index in indexes]


def _initialize_ocr_worker() -> None:
    os.environ["OMP_THREAD_LIMIT"] = "1"
    os.environ["OMP_NUM_THREADS"] = "1"


def _diagnosed_ocr_source_page(*args):
    with diagnostics.measurement() as metrics:
        try:
            page = _ocr_source_page(*args)
            metrics.update(status="success", words=len(page.words))
            return page, metrics, None
        except Exception as exc:
            metrics.update(status="error", error_type=type(exc).__name__)
            return None, metrics, str(exc)


def _available_memory_bytes() -> int | None:
    try:
        if os.name == "nt":
            import ctypes

            class MemoryStatus(ctypes.Structure):
                _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                    (name, ctypes.c_ulonglong) for name in
                    ("total", "available", "total_page", "available_page", "total_virtual", "available_virtual", "extended")
                ]

            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.available)
        else:
            with open("/proc/meminfo", encoding="ascii") as stream:
                for line in stream:
                    key, _, value = line.partition(":")
                    if key == "MemAvailable":
                        return int(value.split()[0]) * 1024
    except (OSError, ValueError, AttributeError):
        pass
    return None


def _ocr_worker_count(requested: int, pages: int, raster_pixels: int) -> int:
    count = max(1, min(3, requested, pages))
    available = _available_memory_bytes()
    if available is not None:
        # Leave RAM for the desktop; swapping multiple high-resolution OCR
        # engines is slower than processing pages sequentially.
        per_worker = 256 * 1024 ** 2 + raster_pixels * 24
        count = max(1, min(count, int(available * 0.6) // per_worker))
    return count


def _ocr_source_page(
    source: str | Path,
    page_index: int,
    language: str,
    dpi: int,
    tessdata: Path,
    enhance: bool,
    quality: str = "best",
) -> PageText:
    import fitz

    with fitz.open(str(source)) as document:
        page = document[page_index]
        diagnostics.note(width_points=page.rect.width, height_points=page.rect.height,
                         rotation=page.rotation, dpi=dpi, language=language, quality=quality,
                         image_count=len(page.get_images()))
        raw_words = _ocr_page_words(
            page,
            language=language,
            dpi=max(150, min(400, int(dpi))),
            tessdata=tessdata,
            enhance=enhance,
            quality=quality,
        )
        return PageText(
            page_index,
            float(page.rect.width),
            float(page.rect.height),
            tuple(_indexed_words(raw_words)),
            True,
        )


def recognize_word_region(
    source: str | Path,
    page_index: int,
    point: tuple[float, float],
    *,
    language: str = "rus",
    dpi: int = 350,
) -> PageText:
    """OCR only the line around a click, preserving full-page coordinates."""
    model_dir = tessdata_path()
    with ProcessPoolExecutor(
        max_workers=1,
        mp_context=multiprocessing.get_context("spawn"),
        initializer=_initialize_ocr_worker,
    ) as executor:
        return executor.submit(
            _recognize_word_region_worker,
            str(source),
            page_index,
            point,
            language,
            dpi,
            model_dir,
        ).result()


def _recognize_word_region_worker(
    source: str,
    page_index: int,
    point: tuple[float, float],
    language: str,
    dpi: int,
    model_dir: Path,
) -> PageText:
    import fitz

    with fitz.open(source) as document:
        page = document[page_index]
        width, height = float(page.rect.width), float(page.rect.height)
        x, y = point
        native = _displayed_words(page, page.get_text("words"))
        selected = [word for word in native if word[0] - 3 <= x <= word[2] + 3
                    and word[1] - 3 <= y <= word[3] + 3]
        # A scanned PDF may already have an inaccurate invisible OCR layer.
        # Prefer the image under the click over that layer.
        page_area = max(1.0, page.rect.get_area())
        has_page_scan = any(
            fitz.Rect(info["bbox"]).get_area() >= page_area * 0.35
            for info in page.get_image_info()
        )
        if selected and not has_page_scan:
            return PageText(page_index, width, height, tuple(_indexed_words(selected)), False)
        if not ocr_models_available(model_dir, language):
            raise RuntimeError("Не найдена встроенная модель выбранного языка OCR")
        rotation = page.rotation_matrix
        source_point = fitz.Point(x, y) * page.derotation_matrix
        if page.rotation:
            page.set_rotation(0)
        clip = fitz.Rect(source_point.x - 180, source_point.y - 45,
                         source_point.x + 180, source_point.y + 45) & page.rect
        if clip.is_empty:
            return PageText(page_index, width, height, (), True)
        scale = dpi / 72.0
        pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip,
                                 colorspace=fitz.csRGB, alpha=False)
        with fitz.open() as cropped:
            crop_page = cropped.new_page(width=clip.width, height=clip.height)
            crop_page.insert_image(crop_page.rect, pixmap=pixmap)
            words = _ocr_page_words(crop_page, language=language, dpi=dpi,
                                    tessdata=model_dir, enhance=True, quality="word")
        shifted = [(*tuple(fitz.Rect(word[0] + clip.x0, word[1] + clip.y0,
                                     word[2] + clip.x0, word[3] + clip.y0) * rotation),
                    *word[4:]) for word in words]
        return PageText(page_index, width, height, tuple(_indexed_words(shifted)), True)


@diagnostics.timed("preprocessing")
def enhance_ocr_image(image: Image.Image, angle: float | None = None) -> tuple[Image.Image, float]:
    gray = ImageOps.grayscale(image)
    gray = ImageOps.autocontrast(gray, cutoff=1)
    if angle is None:
        angle = estimate_skew_angle(gray)
    if abs(angle) >= 0.35:
        gray = gray.rotate(angle, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=255)
    gray = suppress_table_lines(gray)
    gray = gray.filter(ImageFilter.MedianFilter(size=3))
    gray = gray.filter(ImageFilter.UnsharpMask(radius=1.2, percent=145, threshold=3))
    return gray, angle


def suppress_table_lines(image: Image.Image) -> Image.Image:
    """Remove long rules from the OCR copy while leaving the source page intact."""
    gray = ImageOps.grayscale(image)
    ink = gray.point(lambda value: 255 if value < 165 else 0)
    pixels = ink.tobytes()
    width, height = gray.size
    # Dense bold text is not a rule. Erase only long connected strokes, and
    # only within their actual span instead of blanking the whole row/column.
    gap = max(1, round(max(width, height) / 2000))
    holes = re.compile(b"\\x00{1," + str(gap).encode("ascii") + b"}(?=\\xff)")
    runs = re.compile(b"\\xff+")

    def spans(values, minimum):
        joined = holes.sub(lambda match: b"\xff" * len(match[0]), values)
        return [(match.start(), match.end()-1) for match in runs.finditer(joined)
                if match.end()-match.start() >= minimum]

    cleaned = gray.copy()
    draw = ImageDraw.Draw(cleaned)
    for y in range(height):
        for start, end in spans(pixels[y*width:(y+1)*width], max(80, width*0.32)):
            draw.line((start, y, end, y), fill=255)
    for x in range(width):
        for start, end in spans(pixels[x::width], max(100, height*0.18)):
            draw.line((x, start, x, end), fill=255)
    gray.close()
    ink.close()
    return cleaned


def _contiguous_ranges(values: Sequence[int]) -> Iterable[tuple[int, int]]:
    if not values:
        return
    start = previous = values[0]
    for value in values[1:]:
        if value > previous + 1:
            yield start, previous
            start = value
        previous = value
    yield start, previous


@diagnostics.timed("skew_detection")
def estimate_skew_angle(image: Image.Image) -> float:
    sample = ImageOps.grayscale(image)
    if max(sample.size) > 800:
        scale = 800.0 / max(sample.size)
        sample = sample.resize(
            (max(1, int(sample.width * scale)), max(1, int(sample.height * scale))),
            Image.Resampling.BILINEAR,
        )
    threshold = int(sum(sample.getextrema()) / 2)
    binary = sample.point(lambda value: 255 if value < threshold else 0)
    best_angle = 0.0
    best_score = -1.0
    def projection_score(angle: float) -> float:
        rotated = binary.rotate(angle, resample=Image.Resampling.NEAREST, expand=False, fillcolor=0)
        pixels = rotated.tobytes()
        width, height = rotated.size
        projection = [pixels[offset : offset + width].count(255) for offset in range(0, width * height, width)]
        if not projection:
            return -1.0
        mean = sum(projection) / len(projection)
        return sum((value - mean) ** 2 for value in projection) / len(projection)

    for angle in (-3.0, -2.0, -1.0, 0.0, 1.0, 2.0, 3.0):
        score = projection_score(angle)
        if score > best_score:
            best_score = score
            best_angle = angle
    coarse_angle = best_angle
    for step in range(-3, 4):
        angle = coarse_angle + step * 0.25
        score = projection_score(angle)
        if score > best_score:
            best_score = score
            best_angle = angle
    return best_angle


def _ocr_page_words(page, *, language: str, dpi: int, tessdata: Path, enhance: bool, quality: str = "best") -> list[tuple]:
    if not enhance:
        return _locked_page_ocr_words(page, language=language, dpi=dpi, tessdata=tessdata)

    import fitz

    try:
        scale = dpi / 72.0
        with diagnostics.stage("render"):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
        diagnostics.note(raster_width=pixmap.width, raster_height=pixmap.height)
        source_image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
        gray = ImageOps.grayscale(source_image)
        angle = estimate_skew_angle(gray)
        diagnostics.note(skew_degrees=angle)
        if not _needs_ocr_preparation(gray, angle):
            source_image.close()
            gray.close()
            diagnostics.note(selected_variant="direct", prepared=False)
            return _locked_page_ocr_words(page, language=language, dpi=dpi, tessdata=tessdata,
                                          rendered_pixmap=pixmap)
        gray.close()
        prepared, _angle = enhance_ocr_image(source_image, angle=angle)
        stream = BytesIO()
        prepared.save(stream, "PNG", compress_level=1)
        source_image.close()
        prepared.close()
        with fitz.open() as ocr_document:
            # A PNG without density metadata defaults to 96 DPI. Converting
            # it directly to PDF and rendering at 350 DPI multiplies pixels
            # by ~13. Keep the physical size of the source page instead.
            ocr_page = ocr_document.new_page(width=page.rect.width, height=page.rect.height)
            ocr_page.insert_image(ocr_page.rect, stream=stream.getvalue())
            words = _locked_page_ocr_words(
                ocr_page,
                language=language,
                dpi=dpi,
                tessdata=tessdata,
            )
            scale_x = float(page.rect.width) / max(1.0, float(ocr_page.rect.width))
            scale_y = float(page.rect.height) / max(1.0, float(ocr_page.rect.height))
            prepared_words = [
                (
                    float(word[0]) * scale_x,
                    float(word[1]) * scale_y,
                    float(word[2]) * scale_x,
                    float(word[3]) * scale_y,
                    *word[4:],
                )
                for word in words
            ]
        prepared_quality = _ocr_words_quality(prepared_words, language)
        # Fast OCR powers search and the editor-wide "Распознать текст"
        # command. A second full Tesseract pass almost doubles the wait on a
        # scan, while a cleaned result with readable words is already useful
        # for these interactive operations. PDF -> DOCX still uses "best" and
        # compares both variants to preserve its higher-fidelity route.
        if quality == "fast" and (prepared_quality >= 0.55 or len(prepared_words) >= 8):
            diagnostics.note(selected_variant="prepared", prepared=True, prepared_quality=round(prepared_quality, 3))
            return prepared_words
        # Replacing a word is deliberately limited to the active page, so it
        # can afford the same direct-versus-prepared comparison as DOCX. Ruled
        # forms are the common case where line removal helps most text but can
        # damage a short word selected by the user.
        direct_words = _locked_page_ocr_words(page, language=language, dpi=dpi, tessdata=tessdata,
                                             rendered_pixmap=pixmap)
        direct_quality = _ocr_words_quality(direct_words, language)
        complete_prepared = _prefer_complete_ocr(prepared_words, direct_words, language)
        diagnostics.note(direct_word_count=len(direct_words), prepared_word_count=len(prepared_words),
                         coverage_preferred=complete_prepared)
        if direct_quality > prepared_quality and not complete_prepared:
            diagnostics.note(
                selected_variant="direct",
                prepared=True,
                direct_quality=round(direct_quality, 3),
                prepared_quality=round(prepared_quality, 3),
            )
            return _supplement_ocr_words(direct_words, prepared_words, language)
        diagnostics.note(
            selected_variant="prepared",
            prepared=True,
            direct_quality=round(direct_quality, 3),
            prepared_quality=round(prepared_quality, 3),
        )
        return _supplement_ocr_words(prepared_words, direct_words, language)
    except Exception as exc:
        diagnostics.note(fallback_error_type=type(exc).__name__, selected_variant="fallback")
        return _locked_page_ocr_words(page, language=language, dpi=dpi, tessdata=tessdata)


def _locked_page_ocr_words(page, *, language: str, dpi: int, tessdata: Path, rendered_pixmap=None) -> list[tuple]:
    if page.rotation:
        import fitz

        # The embedded OCR text-page bridge can lose words on rotated pages.
        # Give it an upright raster with the same displayed dimensions.
        pixmap = rendered_pixmap
        if pixmap is None:
            with diagnostics.stage("render"):
                pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72.0, dpi / 72.0),
                                        colorspace=fitz.csRGB, alpha=False)
        with fitz.open() as normalized:
            upright = normalized.new_page(width=page.rect.width, height=page.rect.height)
            upright.insert_image(upright.rect, pixmap=pixmap)
            return _locked_page_ocr_words(upright, language=language, dpi=dpi, tessdata=tessdata)
    with _OCR_ENGINE_LOCK, diagnostics.stage("ocr_engine"):
        try:
            text_page = page.get_textpage_ocr(
                language=language,
                dpi=dpi,
                full=True,
                tessdata=str(tessdata),
            )
            return _displayed_words(page, page.get_text("words", textpage=text_page))
        except Exception as exc:
            _raise_clear_ocr_error(exc)
            raise


def _displayed_words(page, words) -> list[tuple]:
    if not page.rotation:
        return list(words)
    import fitz

    return [(*tuple(fitz.Rect(word[:4]) * page.rotation_matrix), *word[4:]) for word in words]


def ocr_pixmap_to_pdf(pixmap, *, language: str, tessdata: str | Path, compress: bool = True) -> bytes:
    """Run PyMuPDF pixmap OCR without concurrent Leptonica access."""
    with _OCR_ENGINE_LOCK:
        try:
            return pixmap.pdfocr_tobytes(
                compress=compress,
                language=language,
                tessdata=str(tessdata),
            )
        except Exception as exc:
            _raise_clear_ocr_error(exc)
            raise


def _raise_clear_ocr_error(exc: Exception) -> None:
    details = str(exc).casefold()
    if "ocr initialisation failed" in details or "leptonica from 2 threads" in details:
        raise RuntimeError(
            "Не удалось запустить модуль распознавания текста. "
            "Повторите операцию; если ошибка сохранится, переустановите pdf bars."
        ) from exc


def _ocr_words_quality(words: Sequence[tuple], language: str = "rus") -> float:
    tokens = [str(word[4]).strip() for word in words if len(word) > 4 and str(word[4]).strip()]
    if not tokens:
        return 0.0
    characters = "".join(tokens)
    alphanumeric_ratio = sum(character.isalnum() for character in characters) / max(1, len(characters))
    vowels = "аеёиоуыэюяaeiouy"
    plausible = 0
    for token in tokens:
        letters = "".join(character for character in token.casefold() if character.isalpha())
        if token.isdigit() or (letters and (len(letters) <= 2 or any(vowel in letters for vowel in vowels))):
            plausible += 1
    token_ratio = plausible / len(tokens)
    volume = min(1.0, len(characters) / 40.0)
    script_bonus = 1.0
    if "rus" in language and any("а" <= character.casefold() <= "я" or character.casefold() == "ё" for character in characters):
        script_bonus = 1.05
    return min(1.0, (alphanumeric_ratio * 0.5 + token_ratio * 0.4 + volume * 0.1) * script_bonus)


def _prefer_complete_ocr(candidate: Sequence[tuple], reference: Sequence[tuple], language: str) -> bool:
    # Isolated grid strokes are not words. In a near tie, prefer the variant
    # recovering missing labels only when it also agrees with existing text.
    candidate_text = [w for w in candidate if any(c.isalnum() for c in str(w[4]))]
    reference_text = [w for w in reference if any(c.isalnum() for c in str(w[4]))]
    if not reference_text:
        return False
    score = _ocr_words_quality(candidate_text, language)
    if score < 0.88 or score < _ocr_words_quality(reference_text, language) - 0.012:
        return False
    tokens = lambda words: {"".join(c for c in str(w[4]).casefold() if c.isalnum()) for w in words}
    candidate_tokens, reference_tokens = tokens(candidate_text), tokens(reference_text)
    agreement = len(candidate_tokens & reference_tokens) / max(1, len(reference_tokens))
    volume = lambda words: sum(sum(c.isalnum() for c in str(w[4])) for w in words)
    agrees = agreement >= 0.90 or (agreement >= 0.85 and score >= _ocr_words_quality(reference_text, language))
    return agrees and volume(candidate_text) >= volume(reference_text) * 1.08


def _supplement_ocr_words(primary: Sequence[tuple], secondary: Sequence[tuple], language: str) -> list[tuple]:
    """Recover clear words in empty areas without replacing the preferred OCR."""
    result = list(primary)
    if not primary or _ocr_words_quality(secondary, language) < 0.80:
        return result
    next_block = max(int(word[5]) for word in primary) + 1
    added = 0
    for word in secondary:
        token = str(word[4]).strip()
        letters = "".join(c for c in token.casefold() if c.isalpha())
        if not (token.isdigit() and len(token) >= 2) and not (
            len(letters) >= 3 and any(c in "аеёиоуыэюяaeiouy" for c in letters)
            and sum(c.isalnum() for c in token) >= len(token) * 0.85
        ):
            continue
        x0, y0, x1, y1 = word[:4]
        if x1 <= x0 or y1 <= y0:
            continue
        # Any intersecting preferred word vetoes the candidate, even if the
        # strings differ. This avoids duplicate text from shifted OCR boxes.
        if any(min(x1 + 1, old[2]) > max(x0 - 1, old[0]) and
               min(y1, old[3]) > max(y0, old[1]) for old in result):
            continue
        neighbours = [old for old in primary
                      if abs((old[1] + old[3] - y0 - y1) / 2) < min(y1-y0, old[3]-old[1]) * 0.3
                      and max(x0-old[2], old[0]-x1) < (y1-y0) * 6]
        if neighbours:
            neighbour = min(neighbours, key=lambda old: abs(old[0]-x0))
            block, line = neighbour[5:7]
        else:
            block, line = next_block, 0
            next_block += 1
        result.append((*word[:5], block, line, 0))
        added += 1
    if not added:
        return result
    result.sort(key=lambda word: (word[5], word[6], word[0]))
    counts = {}
    for index, word in enumerate(result):
        key = tuple(word[5:7])
        order = counts.get(key, 0)
        result[index] = (*word[:7], order)
        counts[key] = order + 1
    diagnostics.note(supplemental_words=added)
    return result


def _needs_ocr_preparation(image: Image.Image, angle: float) -> bool:
    """Avoid damaging clean scans while still correcting skewed or ruled pages."""
    if abs(angle) >= 0.35:
        return True
    sample = ImageOps.grayscale(image)
    if max(sample.size) > 1400:
        scale = 1400.0 / max(sample.size)
        sample = sample.resize(
            (max(1, int(sample.width * scale)), max(1, int(sample.height * scale))),
            Image.Resampling.BILINEAR,
        )
    ink = sample.point(lambda value: 255 if value < 165 else 0)
    pixels = ink.tobytes()
    width, height = ink.size
    horizontal_rows = [
        row
        for row, offset in enumerate(range(0, len(pixels), width))
        if pixels[offset : offset + width].count(255) >= width * 0.45
    ]
    vertical_columns = [
        column for column in range(width) if pixels[column::width].count(255) >= height * 0.25
    ]
    horizontal_rules = sum(1 for _range in _contiguous_ranges(horizontal_rows))
    vertical_rules = sum(1 for _range in _contiguous_ranges(vertical_columns))
    sample.close()
    ink.close()
    return horizontal_rules >= 2 or vertical_rules >= 2


def search_text_index(pages: Sequence[PageText], query: str) -> List[SearchHit]:
    normalized_query = _normalize_text(query)
    if not normalized_query:
        return []

    hits: List[SearchHit] = []
    for page in pages:
        grouped: dict[tuple[int, int], list[IndexedWord]] = {}
        for word in page.words:
            grouped.setdefault((word.block, word.line), []).append(word)
        for line_words in grouped.values():
            line_words.sort(key=lambda item: item.order)
            line_text, spans = _line_text_and_spans(line_words)
            normalized_line = _normalize_text(line_text)
            start = 0
            while True:
                found = normalized_line.find(normalized_query, start)
                if found < 0:
                    break
                end = found + len(normalized_query)
                matched = [word for word, word_start, word_end in spans if word_end > found and word_start < end]
                if matched:
                    rect = _union_rect(word.rect for word in matched)
                    hits.append(SearchHit(page.page_index, rect, " ".join(word.text for word in matched)))
                start = max(end, found + 1)
    return hits


def select_text_in_rect(page: PageText, rect: RectTuple) -> tuple[str, tuple[RectTuple, ...]]:
    x0, y0, x1, y1 = (
        min(rect[0], rect[2]),
        min(rect[1], rect[3]),
        max(rect[0], rect[2]),
        max(rect[1], rect[3]),
    )
    selected = [
        word
        for word in page.words
        if word.rect[2] >= x0 and word.rect[0] <= x1 and word.rect[3] >= y0 and word.rect[1] <= y1
    ]
    selected.sort(key=lambda word: (word.block, word.line, word.order))
    if not selected:
        return "", ()

    lines: list[str] = []
    current_line: tuple[int, int] | None = None
    current_words: list[str] = []
    for word in selected:
        line = (word.block, word.line)
        if current_line is not None and line != current_line:
            lines.append(" ".join(current_words))
            current_words = []
        current_line = line
        current_words.append(word.text)
    if current_words:
        lines.append(" ".join(current_words))
    return "\n".join(lines), tuple(word.rect for word in selected)


def _indexed_words(raw_words: Iterable[tuple]) -> Iterable[IndexedWord]:
    for fallback_order, raw in enumerate(raw_words):
        if len(raw) < 5:
            continue
        text = str(raw[4]).strip()
        if not text:
            continue
        block = int(raw[5]) if len(raw) > 5 else 0
        line = int(raw[6]) if len(raw) > 6 else 0
        order = int(raw[7]) if len(raw) > 7 else fallback_order
        yield IndexedWord(
            (float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])),
            text,
            block,
            line,
            order,
        )


def _contains_meaningful_text(raw_words: Iterable[tuple]) -> bool:
    text = " ".join(str(word[4]) for word in raw_words if len(word) > 4)
    return sum(character.isalnum() for character in text) >= 3


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.casefold().replace("ё", "е")).strip()


def _line_text_and_spans(words: Sequence[IndexedWord]) -> tuple[str, list[tuple[IndexedWord, int, int]]]:
    parts: list[str] = []
    spans: list[tuple[IndexedWord, int, int]] = []
    cursor = 0
    for word in words:
        if parts:
            parts.append(" ")
            cursor += 1
        normalized = _normalize_text(word.text)
        start = cursor
        parts.append(normalized)
        cursor += len(normalized)
        spans.append((word, start, cursor))
    return "".join(parts), spans


def _union_rect(rectangles: Iterable[RectTuple]) -> RectTuple:
    values = list(rectangles)
    return (
        min(rect[0] for rect in values),
        min(rect[1] for rect in values),
        max(rect[2] for rect in values),
        max(rect[3] for rect in values),
    )
