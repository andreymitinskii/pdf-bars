from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
import math
import re
from statistics import median
from typing import Iterable, List, Sequence, Tuple
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .ocr import IndexedWord, PageText


W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
V_NS = "urn:schemas-microsoft-com:vml"
O_NS = "urn:schemas-microsoft-com:office:office"
W10_NS = "urn:schemas-microsoft-com:office:word"
REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CP_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
DC_NS = "http://purl.org/dc/elements/1.1/"
DCTERMS_NS = "http://purl.org/dc/terms/"
XSI_NS = "http://www.w3.org/2001/XMLSchema-instance"
XML_NS = "http://www.w3.org/XML/1998/namespace"
DOCX_FONT = "Liberation Serif"

for prefix, namespace in (
    ("w", W_NS),
    ("r", R_NS),
    ("cp", CP_NS),
    ("dc", DC_NS),
    ("dcterms", DCTERMS_NS),
    ("xsi", XSI_NS),
    ("v", V_NS),
    ("o", O_NS),
    ("w10", W10_NS),
):
    ET.register_namespace(prefix, namespace)


@dataclass(frozen=True)
class DetectedTable:
    x_edges: Tuple[float, ...]
    y_edges: Tuple[float, ...]

    @property
    def rect(self) -> Tuple[float, float, float, float]:
        return self.x_edges[0], self.y_edges[0], self.x_edges[-1], self.y_edges[-1]


@dataclass(frozen=True)
class PositionedLine:
    text: str
    rect: Tuple[float, float, float, float]
    font_size: float
    bold: bool = False
    alignment: str = "left"
    tab_stops: Tuple[float, ...] = ()

    def __iter__(self):
        yield self.text
        yield self.rect
        yield self.font_size

    def __getitem__(self, index: int):
        return (self.text, self.rect, self.font_size)[index]

    def __len__(self) -> int:
        return 3


@dataclass(frozen=True)
class FlowParagraph:
    text: str
    font_size: float
    bold: bool = False
    alignment: str = "left"
    left_indent: float = 0.0
    first_line_indent: float = 0.0
    tab_stops: Tuple[float, ...] = ()
    space_before: float = 0.0
    space_after: float = 3.0


def sanitize_layout_pages(pages: Sequence[PageText], language: str) -> List[PageText]:
    sanitized: List[PageText] = []
    russian_only = language.strip() == "rus"
    for page in pages:
        bounded_words = []
        for word in page.words:
            rect = _bounded_rect(word.rect, page.width, page.height)
            if rect is not None:
                bounded_words.append(IndexedWord(rect, word.text, word.block, word.line, word.order))
        source_heights = [
            max(1.0, word.rect[3] - word.rect[1])
            for word in bounded_words
            if any(character.isalnum() for character in word.text)
        ]
        typical_height = median(source_heights) if source_heights else 12.0
        words = []
        for word in bounded_words:
            text = " ".join(word.text.split()).strip()
            is_list_marker = text in {"-", "–", "—", "•", "·", "▪"}
            if not text or (not any(character.isalnum() for character in text) and not is_list_marker):
                continue
            if page.used_ocr and not is_list_marker and not _is_reliable_ocr_word(word, text, typical_height):
                continue
            if russian_only and not is_list_marker:
                has_cyrillic = any("\u0400" <= character <= "\u052f" for character in text)
                has_digit = any(character.isdigit() for character in text)
                if not has_cyrillic and not has_digit:
                    continue
            words.append(IndexedWord(word.rect, text, word.block, word.line, word.order))
        unique_words: list[IndexedWord] = []
        for word in words:
            if any(
                word.text.casefold() == existing.text.casefold() and _word_overlap(word, existing) > 0.55
                for existing in unique_words
            ):
                continue
            unique_words.append(word)
        sanitized.append(PageText(page.page_index, page.width, page.height, tuple(unique_words), page.used_ocr))
    return sanitized


def _is_reliable_ocr_word(word: IndexedWord, text: str, typical_height: float) -> bool:
    """Discard obvious OCR debris without removing ordinary Russian document text."""
    width = max(0.0, word.rect[2] - word.rect[0])
    height = max(0.0, word.rect[3] - word.rect[1])
    if width < 1.5 or height < max(2.5, typical_height * 0.38) or height > typical_height * 2.8:
        return False
    if any(ord(character) < 32 for character in text):
        return False
    meaningful = sum(character.isalnum() for character in text)
    suspicious = sum(
        not (character.isalnum() or character in "-–—.,:;!?()[]{}№%+/\\'\"")
        for character in text
    )
    if meaningful < max(1, len(text) // 3) or suspicious > max(1, len(text) // 4):
        return False
    if len(text) >= 5 and len(set(text.casefold())) == 1:
        return False
    if len(text) == 1 and text.isalpha() and text.islower() and text.casefold() not in "авикосуяa":
        return False
    return True


def exclude_colored_ink_words(
    source: str | Path,
    pages: Sequence[PageText],
    *,
    dpi: int = 120,
) -> List[PageText]:
    """Keep coloured signatures and stamps as image content instead of OCR text."""
    import fitz

    scale = float(dpi) / 72.0
    filtered: List[PageText] = []
    with fitz.open(str(source)) as document:
        for text_page in pages:
            page = document[text_page.page_index]
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            words = tuple(word for word in text_page.words if not _contains_colored_ink(image, word.rect, scale))
            image.close()
            filtered.append(
                PageText(text_page.page_index, text_page.width, text_page.height, words, text_page.used_ocr)
            )
    return filtered




def _contains_colored_ink(image: Image.Image, rect: Tuple[float, float, float, float], scale: float) -> bool:
    if not math.isfinite(scale) or scale <= 0:
        return False
    bounded = _bounded_rect(tuple(value * scale for value in rect), image.width, image.height)
    if bounded is None:
        return False
    x0, y0 = math.floor(bounded[0]), math.floor(bounded[1])
    x1, y1 = math.ceil(bounded[2]), math.ceil(bounded[3])
    crop = image.crop((x0, y0, x1, y1))
    colored = 0
    dark = 0
    for red, green, blue in crop.getdata():
        if min(red, green, blue) < 190:
            dark += 1
            if max(red, green, blue) - min(red, green, blue) >= 45:
                colored += 1
    crop.close()
    return dark >= 6 and colored >= max(3, int(dark * 0.08))


def _bounded_rect(rect, width, height):
    """Reject invalid OCR geometry and clip partially visible words to the page."""
    if len(rect) != 4 or not all(math.isfinite(value) for value in (*rect, width, height)):
        return None
    x0, y0 = max(0.0, rect[0]), max(0.0, rect[1])
    x1, y1 = min(width, rect[2]), min(height, rect[3])
    return (x0, y0, x1, y1) if x1 > x0 and y1 > y0 else None


def render_table_detection_images(
    source: str | Path,
    pages: Sequence[PageText],
    *,
    dpi: int = 180,
) -> List[bytes]:
    import fitz

    scale = float(dpi) / 72.0
    output: List[bytes] = []
    with fitz.open(str(source)) as document:
        if document.page_count != len(pages):
            raise ValueError("Количество распознанных страниц не совпадает с PDF")
        for page, text_page in zip(document, pages):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            stream = BytesIO()
            image.save(stream, "JPEG", quality=88, optimize=True, dpi=(dpi, dpi))
            image.close()
            output.append(stream.getvalue())
    return output


def render_clean_page_backgrounds(
    source: str | Path,
    pages: Sequence[PageText],
    *,
    dpi: int = 180,
) -> List[bytes]:
    """Keep page graphics while removing text that will be editable in Word."""
    import fitz

    scale = float(dpi) / 72.0
    output: List[bytes] = []
    with fitz.open(str(source)) as document:
        if document.page_count != len(pages):
            raise ValueError("Количество распознанных страниц не совпадает с PDF")
        for page, text_page in zip(document, pages):
            pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
            image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
            _erase_recognized_words(image, text_page.words, scale)
            stream = BytesIO()
            image.save(stream, "JPEG", quality=92, optimize=True, dpi=(dpi, dpi))
            image.close()
            output.append(stream.getvalue())
    return output


def write_layout_docx(
    output: str | Path,
    pages: Sequence[PageText],
    page_images: Sequence[bytes],
    *,
    title: str,
    preserve_graphics: bool = False,
    table_images: Sequence[bytes] | None = None,
    detect_tables: bool = True,
) -> Path:
    if not pages or len(pages) != len(page_images):
        raise ValueError("Для DOCX требуются распознанные страницы и фон каждой страницы")
    if detect_tables and table_images is None:
        table_images = page_images
    if detect_tables and (table_images is None or len(pages) != len(table_images)):
        raise ValueError("Для DOCX требуются изображения для анализа таблиц каждой страницы")
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    page_images = list(page_images)
    document = ET.Element(_w("document"))
    body = ET.SubElement(document, _w("body"))
    page_tables = (
        [detect_layout_tables(page, image) for page, image in zip(pages, table_images)]
        if detect_tables and table_images is not None
        else [[] for _page in pages]
    )
    for page_number, (page, background, tables) in enumerate(zip(pages, page_images, page_tables), start=1):
        if preserve_graphics:
            clean_background = _remove_table_regions(background, page, tables)
            page_images[page_number - 1] = clean_background
            _append_background(body, page, f"rIdImage{page_number}", page_number)
        table_rects = tuple(table.rect for table in tables)
        lines = _page_lines(page, excluded_rects=table_rects, positioned=preserve_graphics)
        if preserve_graphics:
            for table in tables:
                _append_positioned_table(body, page, table, positioned=True)
            for line in lines:
                _append_positioned_line(body, line)
        else:
            _append_flowing_page(body, page, lines, tables)
        if page_number < len(pages):
            if preserve_graphics:
                paragraph = ET.SubElement(body, _w("p"))
                properties = ET.SubElement(paragraph, _w("pPr"))
                _append_section(properties, page, next_page=True)

    final_page = pages[-1] if preserve_graphics else pages[0]
    _append_section(body, final_page, flowing=not preserve_graphics)

    with ZipFile(output_path, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types())
        archive.writestr("_rels/.rels", _package_relationships())
        archive.writestr("docProps/core.xml", _core_properties(title))
        archive.writestr("docProps/app.xml", _app_properties())
        archive.writestr("word/document.xml", _xml_bytes(document))
        archive.writestr("word/styles.xml", _styles())
        archive.writestr("word/settings.xml", _settings())
        archive.writestr("word/fontTable.xml", _font_table())
        archive.writestr(
            "word/_rels/document.xml.rels",
            _document_relationships(len(page_images) if preserve_graphics else 0),
        )
        if preserve_graphics:
            for index, background in enumerate(page_images, start=1):
                archive.writestr(f"word/media/page-{index:04d}.jpg", background)
    return output_path


def _append_section(
    parent: ET.Element,
    page: PageText,
    *,
    next_page: bool = False,
    flowing: bool = False,
) -> None:
    section = ET.SubElement(parent, _w("sectPr"))
    if next_page:
        ET.SubElement(section, _w("type"), {_w("val"): "nextPage"})
    ET.SubElement(section, _w("pgSz"), {_w("w"): str(_twips(page.width)), _w("h"): str(_twips(page.height))})
    ET.SubElement(
        section,
        _w("pgMar"),
        {
            _w("top"): str(_twips(42.0)) if flowing else "0",
            _w("right"): str(_twips(42.0)) if flowing else "0",
            _w("bottom"): str(_twips(42.0)) if flowing else "0",
            _w("left"): str(_twips(42.0)) if flowing else "0",
            _w("header"): "0",
            _w("footer"): "0",
            _w("gutter"): "0",
        },
    )


def _erase_recognized_words(image: Image.Image, words: Iterable[IndexedWord], scale: float) -> None:
    pixels = image.load()
    for word in words:
        x0 = max(0, int(word.rect[0] * scale) - 1)
        y0 = max(0, int(word.rect[1] * scale) - 1)
        x1 = min(image.width, int(word.rect[2] * scale) + 2)
        y1 = min(image.height, int(word.rect[3] * scale) + 2)
        if x1 <= x0 or y1 <= y0:
            continue
        samples = []
        for x in range(x0, x1):
            samples.append(pixels[x, y0])
            samples.append(pixels[x, y1 - 1])
        for y in range(y0, y1):
            samples.append(pixels[x0, y])
            samples.append(pixels[x1 - 1, y])
        bright = [color for color in samples if sum(color) >= 420]
        palette = bright or samples
        fill = tuple(int(median(color[channel] for color in palette)) for channel in range(3))
        for y in range(y0, y1):
            for x in range(x0, x1):
                pixels[x, y] = fill


def _remove_table_regions(
    background: bytes,
    page: PageText,
    tables: Sequence[DetectedTable],
) -> bytes:
    if not tables:
        return background
    with Image.open(BytesIO(background)) as source:
        image = source.convert("RGB")
    scale_x = image.width / max(1.0, page.width)
    scale_y = image.height / max(1.0, page.height)
    draw = ImageDraw.Draw(image)
    for table in tables:
        x0, y0, x1, y1 = table.rect
        draw.rectangle(
            (
                max(0, int(x0 * scale_x) - 2),
                max(0, int(y0 * scale_y) - 2),
                min(image.width, int(x1 * scale_x) + 2),
                min(image.height, int(y1 * scale_y) + 2),
            ),
            fill="white",
        )
    stream = BytesIO()
    image.save(stream, "JPEG", quality=92, optimize=True)
    image.close()
    return stream.getvalue()


def detect_layout_tables(page: PageText, background: bytes) -> List[DetectedTable]:
    """Require connected rules in both directions, not the density of letters."""
    try:
        with Image.open(BytesIO(background)) as source:
            gray = source.convert("L").filter(ImageFilter.MinFilter(3))
    except OSError:
        return []
    width, height = gray.size
    if width < 120 or height < 120:
        return []
    binary = gray.point(lambda value: 0 if value < 190 else 255)
    horizontal_mask = binary.copy()
    vertical_mask = binary.copy()
    for offset in range(-6, 7):
        horizontal_mask = ImageChops.darker(horizontal_mask, ImageChops.offset(binary, 0, offset))
    for offset in range(-4, 5):
        vertical_mask = ImageChops.darker(vertical_mask, ImageChops.offset(binary, offset, 0))
    pixels = horizontal_mask.tobytes()
    columns = vertical_mask.transpose(Image.Transpose.TRANSPOSE).tobytes()
    raw_columns = binary.transpose(Image.Transpose.TRANSPOSE).tobytes()
    gap = max(2, round(width / max(page.width, 1) * 1.2))
    holes = re.compile(b"\\xff{1," + str(gap).encode("ascii") + b"}(?=\\x00)")
    runs = re.compile(b"\\x00+")

    def segments(values, minimum):
        joined = holes.sub(lambda match: b"\x00" * len(match[0]), values)
        return [(match.start(), match.end() - 1) for match in runs.finditer(joined)
                if match.end() - match.start() >= minimum]

    horizontal = []
    for y in range(height):
        for left, right in segments(pixels[y * width:(y + 1) * width], width * 0.25):
            horizontal.append((y, left, right))
    rules = [
        (round(median(item[0] for item in group)),
         round(median(item[1] for item in group)),
         round(median(item[2] for item in group)))
        for group in _group_horizontal_lines(horizontal)
    ]
    # Continuations can start/end halfway through a row at the page boundary.
    vertical = []
    for x in range(width):
        for top, bottom in segments(columns[x * height:(x + 1) * height], max(30, height * 0.035)):
            vertical.append((x, top, bottom))
    if vertical:
        top = int(median(item[1] for item in vertical))
        bottom = int(median(item[2] for item in vertical))
        for y in (top, bottom):
            if not any(abs(y - rule[0]) < 10 for rule in rules):
                rules.append((y, min(v[0] for v in vertical), max(v[0] for v in vertical)))
    rules.sort()
    vertical_by_x = {}
    for x, top, bottom in vertical:
        vertical_by_x.setdefault(x, []).append((top, bottom))
    connected_rules = []
    for y, left, right in rules:
        crossing = [x for x, spans in vertical_by_x.items()
                    if any(top-gap <= y <= bottom+gap for top,bottom in spans)]
        if len(_group_line_centres(crossing)) < 3:
            continue
        if left > min(crossing) + gap * 3 or right < max(crossing) - gap * 3:
            continue
        connected_rules.append((y,left,right))
    rules = connected_rules
    tables = []
    scale_x, scale_y = page.width / width, page.height / height
    for upper, lower in zip(rules, rules[1:]):
        top, bottom = upper[0], lower[0]
        if bottom - top < max(10, 5 / scale_y):
            continue
        left, right = max(upper[1], lower[1]), min(upper[2], lower[2])
        if right - left < width * 0.25:
            continue
        candidates = []
        for x in range(max(0, left - gap), min(width, right + gap + 1)):
            if not any(a <= top + gap and b >= bottom - gap for a,b in vertical_by_x.get(x, ())):
                continue
            values = columns[x * height + top:x * height + bottom + 1]
            if bottom - top < height * 0.12:
                support = max(raw_columns[column * height + top:column * height + bottom + 1].count(0)
                              for column in range(max(0, x-1), min(width, x+2)))
                if support < len(values) * 0.80:
                    continue
            if values.count(0) >= len(values) * 0.82:
                if segments(values, len(values) * 0.8):
                    candidates.append(x)
        edges = _group_line_centres(candidates)
        strengths = {x: max((b-a for column in range(max(0, x-2), min(width, x+3))
                             for a, b in vertical_by_x.get(column, ())), default=0) for x in edges}
        edges = [x for x in edges if not any(
            0 < abs(x-other) * scale_x < 12 and strengths[x] < strengths[other] * 0.3
            for other in edges)]
        if len(edges) < 3:
            continue
        x_edges = tuple(x * scale_x for x in edges)
        y_edges = (top * scale_y, bottom * scale_y)
        if tables and abs(tables[-1].y_edges[-1] - y_edges[0]) < 3 and len(tables[-1].x_edges) == len(x_edges) and all(
            abs(a-b) < 3 for a,b in zip(tables[-1].x_edges, x_edges)
        ):
            previous = tables[-1]
            tables[-1] = DetectedTable(previous.x_edges, (*previous.y_edges, y_edges[1]))
        else:
            tables.append(DetectedTable(x_edges, y_edges))
    gray.close()
    binary.close()
    horizontal_mask.close()
    vertical_mask.close()
    return tables




def _longest_ink_run(values: bytes, *, gap: int) -> tuple[int, int]:
    best = (0, -1)
    start: int | None = None
    last = -1
    for index, value in enumerate(values):
        if value < 175:
            if start is None:
                start = index
            last = index
        elif start is not None and index - last > gap:
            if last - start > best[1] - best[0]:
                best = (start, last)
            start = None
    if start is not None and last - start > best[1] - best[0]:
        best = (start, last)
    return best


def _ink_extent(values: bytes, *, threshold: int = 175) -> tuple[int, int, int]:
    positions = [index for index, value in enumerate(values) if value < threshold]
    if not positions:
        return 0, -1, 0
    return positions[0], positions[-1], len(positions)


def _group_horizontal_lines(lines: Sequence[tuple[int, int, int]]) -> list[list[tuple[int, int, int]]]:
    groups: list[list[tuple[int, int, int]]] = []
    for line in lines:
        if groups and line[0] <= groups[-1][-1][0] + 4:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _group_vertical_lines(lines: Sequence[tuple[int, int, int, float]]) -> list[list[tuple[int, int, int, float]]]:
    groups: list[list[tuple[int, int, int, float]]] = []
    for line in lines:
        if groups and line[0] <= groups[-1][-1][0] + 5:
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


def _cluster_vertical_table_lines(
    lines: Sequence[tuple[int, int, int, float]],
    image_width: int,
) -> list[list[tuple[int, int, int, float]]]:
    clusters: list[list[tuple[int, int, int, float]]] = []
    maximum_gap = max(80, int(image_width * 0.36))
    for line in lines:
        if not clusters:
            clusters.append([line])
            continue
        previous = clusters[-1][-1]
        overlap = max(0, min(line[2], previous[2]) - max(line[1], previous[1]))
        shorter = max(1, min(line[2] - line[1], previous[2] - previous[1]))
        if line[0] - previous[0] <= maximum_gap and overlap / shorter >= 0.62:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    return [cluster for cluster in clusters if len(cluster) >= 3]


def _filter_close_vertical_edges(
    lines: Sequence[tuple[int, int, int, float]],
    image_width: int,
) -> list[tuple[int, int, int, float]]:
    if not lines:
        return []
    minimum_gap = max(18, int(image_width * 0.055))
    groups: list[list[tuple[int, int, int, float]]] = [[lines[0]]]
    for line in lines[1:]:
        if line[0] - groups[-1][-1][0] < minimum_gap:
            groups[-1].append(line)
        else:
            groups.append([line])
    return [max(group, key=lambda item: item[3]) for group in groups]


def _horizontal_cell_crossings(
    pixels: bytes,
    width: int,
    height: int,
    y: int,
    x_edges: Sequence[int],
) -> int:
    crossings = 0
    for left, right in zip(x_edges, x_edges[1:]):
        inset = min(10, max(2, (right - left) // 12))
        cell_left = left + inset
        cell_right = right - inset
        if cell_right <= cell_left:
            continue
        required = max(8, int((cell_right - cell_left) * 0.32))
        best = 0
        for sample_y in range(max(0, y - 5), min(height, y + 6)):
            row = pixels[sample_y * width + cell_left : sample_y * width + cell_right]
            best = max(best, sum(value < 190 for value in row))
        if best >= required:
            crossings += 1
    return crossings


def _cluster_table_lines(lines: Sequence[tuple[int, int, int]], image_height: int) -> list[list[tuple[int, int, int]]]:
    clusters: list[list[tuple[int, int, int]]] = []
    maximum_gap = max(70, int(image_height * 0.18))
    for line in lines:
        if not clusters:
            clusters.append([line])
            continue
        previous = clusters[-1][-1]
        overlap = max(0, min(line[2], previous[2]) - max(line[1], previous[1]))
        shorter = max(1, min(line[2] - line[1], previous[2] - previous[1]))
        if line[0] - previous[0] <= maximum_gap and overlap / shorter >= 0.72:
            clusters[-1].append(line)
        else:
            clusters.append([line])
    return [cluster for cluster in clusters if len(cluster) >= 3]


def _group_line_centres(values: Sequence[int]) -> Tuple[int, ...]:
    if not values:
        return ()
    groups = [[values[0]]]
    for value in values[1:]:
        if value <= groups[-1][-1] + 4:
            groups[-1].append(value)
        else:
            groups.append([value])
    return tuple(int(round(median(group))) for group in groups)


def _deduplicate_edges(values: Sequence[int], *, tolerance: int) -> Tuple[int, ...]:
    result: list[int] = []
    for value in sorted(values):
        if result and value - result[-1] <= tolerance:
            result[-1] = int(round((result[-1] + value) / 2))
        else:
            result.append(value)
    return tuple(result)


def _remove_overlapping_tables(tables: Sequence[DetectedTable]) -> List[DetectedTable]:
    result: List[DetectedTable] = []
    for table in sorted(tables, key=lambda item: (item.y_edges[0], item.x_edges[0])):
        if any(_rect_overlap_ratio(table.rect, existing.rect) > 0.65 for existing in result):
            continue
        result.append(table)
    return result


def _normalize_repeated_table_columns(
    pages: Sequence[PageText],
    page_tables: Sequence[Sequence[DetectedTable]],
) -> List[List[DetectedTable]]:
    """Repair isolated missed or false columns in a table continued across pages."""
    candidates: list[tuple[int, DetectedTable]] = []
    for page_index, (page, tables) in enumerate(zip(pages, page_tables)):
        for table in tables:
            columns = len(table.x_edges) - 1
            if 2 <= columns <= 10 and table.rect[2] - table.rect[0] >= page.width * 0.55:
                candidates.append((page_index, table))
    if len(candidates) < 2:
        return [list(tables) for tables in page_tables]

    frequencies: dict[int, int] = {}
    for _page_index, table in candidates:
        columns = len(table.x_edges) - 1
        frequencies[columns] = frequencies.get(columns, 0) + 1
    expected_columns, support = max(frequencies.items(), key=lambda item: (item[1], item[0]))
    if support < 3:
        # A continuation page can lose faint borders in a scan. Its text then
        # creates several false vertical candidates, while a preceding page
        # has a smaller, reliable set of the same outer boundaries. Reuse that
        # smaller edge set only when there is no stable column majority.
        continuation_templates: list[tuple[tuple[float, ...], list[tuple[int, DetectedTable]]]] = []
        for template_page_index, template in candidates:
            template_page = pages[template_page_index]
            template_edges = tuple(value / template_page.width for value in template.x_edges)
            template_support: list[tuple[int, DetectedTable]] = []
            for page_index, table in candidates:
                page = pages[page_index]
                edges = tuple(value / page.width for value in table.x_edges)
                if len(edges) < len(template_edges):
                    continue
                if abs(edges[0] - template_edges[0]) > 0.035 or abs(edges[-1] - template_edges[-1]) > 0.035:
                    continue
                if all(any(abs(edge - reference) <= 0.025 for edge in edges) for reference in template_edges):
                    template_support.append((page_index, table))
            if len(template_support) >= 2 and any(
                len(table.x_edges) > len(template_edges) for _page, table in template_support
            ):
                continuation_templates.append((template_edges, template_support))
        if continuation_templates:
            template_edges, template_support = min(
                continuation_templates,
                key=lambda item: (len(item[0]), -len(item[1])),
            )
            support_keys = {(page_index, id(table)) for page_index, table in template_support}
            normalized: List[List[DetectedTable]] = []
            for page_index, tables in enumerate(page_tables):
                page = pages[page_index]
                normalized.append(
                    [
                        DetectedTable(tuple(value * page.width for value in template_edges), table.y_edges)
                        if (page_index, id(table)) in support_keys
                        else table
                        for table in tables
                    ]
                )
            return normalized
        return [list(tables) for tables in page_tables]

    references = [
        (pages[page_index], table)
        for page_index, table in candidates
        if len(table.x_edges) - 1 == expected_columns
    ]
    normalized_template = tuple(
        median(table.x_edges[index] / page.width for page, table in references)
        for index in range(expected_columns + 1)
    )
    if any(right - left < 0.035 for left, right in zip(normalized_template, normalized_template[1:])):
        return [list(tables) for tables in page_tables]

    normalized: List[List[DetectedTable]] = []
    for page_index, tables in enumerate(page_tables):
        page = pages[page_index]
        updated = []
        for table in tables:
            edges = tuple(value / page.width for value in table.x_edges)
            matched_template_edges = sum(
                any(abs(edge - template_edge) <= 0.025 for edge in edges)
                for template_edge in normalized_template
            )
            if (
                table.rect[2] - table.rect[0] >= page.width * 0.55
                and matched_template_edges >= min(4, expected_columns)
            ):
                updated.append(
                    DetectedTable(
                        tuple(value * page.width for value in normalized_template),
                        table.y_edges,
                    )
                )
            else:
                updated.append(table)
        normalized.append(updated)
    return normalized


def _rect_overlap_ratio(first, second) -> float:
    x0 = max(first[0], second[0])
    y0 = max(first[1], second[1])
    x1 = min(first[2], second[2])
    y1 = min(first[3], second[3])
    overlap = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    first_area = max(1.0, (first[2] - first[0]) * (first[3] - first[1]))
    return overlap / first_area


def _append_positioned_table(
    body: ET.Element,
    page: PageText,
    table: DetectedTable,
    *,
    positioned: bool = True,
) -> None:
    rows = len(table.y_edges) - 1
    columns = len(table.x_edges) - 1
    cell_words: list[list[list[IndexedWord]]] = [[[] for _ in range(columns)] for _ in range(rows)]
    for word in page.words:
        center_x = (word.rect[0] + word.rect[2]) / 2.0
        center_y = (word.rect[1] + word.rect[3]) / 2.0
        if not _point_in_rect(center_x, center_y, table.rect):
            continue
        column = min(columns - 1, max(0, bisect_right(table.x_edges, center_x) - 1))
        row = min(rows - 1, max(0, bisect_right(table.y_edges, center_y) - 1))
        cell_words[row][column].append(word)

    heights = [word.rect[3]-word.rect[1] for row in cell_words for cell in row for word in cell
               if 6 <= word.rect[3]-word.rect[1] <= 22 and any(c.isalnum() for c in word.text)]
    base_font = max(8.0, min(14.0, median(heights) * 0.9)) if heights else 10.0

    element = ET.SubElement(body, _w("tbl"))
    properties = ET.SubElement(element, _w("tblPr"))
    table_width = table.x_edges[-1] - table.x_edges[0]
    if positioned:
        ET.SubElement(
            properties,
            _w("tblpPr"),
            {
                _w("leftFromText"): "0",
                _w("rightFromText"): "0",
                _w("topFromText"): "0",
                _w("bottomFromText"): "0",
                _w("vertAnchor"): "page",
                _w("horzAnchor"): "page",
                _w("tblpX"): str(_twips(table.x_edges[0])),
                _w("tblpY"): str(_twips(table.y_edges[0])),
            },
        )
    else:
        ET.SubElement(properties, _w("tblInd"), {_w("w"): "0", _w("type"): "dxa"})
    # WordprocessingML requires table properties in schema order. Word repairs
    # the package (and R7 Office can reject it) when tblpPr follows tblW.
    ET.SubElement(properties, _w("tblW"), {_w("w"): str(_twips(table_width)), _w("type"): "dxa"})
    borders = ET.SubElement(properties, _w("tblBorders"))
    for name in ("top", "left", "bottom", "right", "insideH", "insideV"):
        ET.SubElement(borders, _w(name), {_w("val"): "single", _w("sz"): "4", _w("color"): "707070"})
    ET.SubElement(properties, _w("tblLayout"), {_w("type"): "fixed"})
    margins = ET.SubElement(properties, _w("tblCellMar"))
    for name in ("top", "left", "bottom", "right"):
        ET.SubElement(margins, _w(name), {_w("w"): "35", _w("type"): "dxa"})
    grid = ET.SubElement(element, _w("tblGrid"))
    for index in range(columns):
        ET.SubElement(
            grid,
            _w("gridCol"),
            {_w("w"): str(_twips(table.x_edges[index + 1] - table.x_edges[index]))},
        )

    for row_index in range(rows):
        row = ET.SubElement(element, _w("tr"))
        if positioned:
            row_properties = ET.SubElement(row, _w("trPr"))
            ET.SubElement(
                row_properties,
                _w("trHeight"),
                {
                    _w("val"): str(_twips(max(10.0, table.y_edges[row_index + 1] - table.y_edges[row_index]))),
                    _w("hRule"): "atLeast",
                },
            )
        for column_index in range(columns):
            cell = ET.SubElement(row, _w("tc"))
            cell_properties = ET.SubElement(cell, _w("tcPr"))
            cell_width = table.x_edges[column_index + 1] - table.x_edges[column_index]
            ET.SubElement(cell_properties, _w("tcW"), {_w("w"): str(_twips(cell_width)), _w("type"): "dxa"})
            ET.SubElement(cell_properties, _w("vAlign"), {_w("val"): "top"})
            lines = _cell_text_lines(cell_words[row_index][column_index], scanned=page.used_ocr, base_font=base_font)
            if not lines:
                ET.SubElement(cell, _w("p"))
            for text, font_size in lines:
                _append_cell_paragraph(cell, text, font_size)


def _cell_text_lines(words: Sequence[IndexedWord], *, scanned: bool = False, base_font: float = 10.0) -> list[tuple[str, float]]:
    if not words:
        return []
    # MuPDF OCR boxes can overlap multiple lines. Inside an isolated cell,
    # OCR reading order is more reliable than sorting those box coordinates.
    if scanned:
        by_line = {}
        for word in words:
            by_line.setdefault((word.block, word.line), []).append(word)
        groups = [by_line[key] for key in sorted(by_line)]
    else:
        groups = _geometric_line_groups(words)
    lines = []
    for group in groups:
        group.sort(key=lambda item: item.rect[0])
        unique: list[IndexedWord] = []
        for word in group:
            if any(word.text.casefold() == existing.text.casefold() and _word_overlap(word, existing) > 0.55 for existing in unique):
                continue
            unique.append(word)
        text = " ".join(item.text for item in unique).strip()
        if not text or (len(unique) == 1 and len(text) == 1 and not text.isdigit()):
            continue
        measured = median(max(1.0, item.rect[3] - item.rect[1]) for item in unique)
        size = max(6.5, min(10.0, measured * 0.66))
        if scanned:
            size = max(base_font * 0.95, min(base_font * 1.1, measured * 0.9))
            if measured > base_font * 1.7:
                size = base_font
        lines.append((text, size))
    return lines


def _word_overlap(first: IndexedWord, second: IndexedWord) -> float:
    x0 = max(first.rect[0], second.rect[0])
    y0 = max(first.rect[1], second.rect[1])
    x1 = min(first.rect[2], second.rect[2])
    y1 = min(first.rect[3], second.rect[3])
    overlap = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = max(1.0, (first.rect[2] - first.rect[0]) * (first.rect[3] - first.rect[1]))
    return overlap / area


def _append_cell_paragraph(cell: ET.Element, text: str, font_size: float) -> None:
    paragraph = ET.SubElement(cell, _w("p"))
    properties = ET.SubElement(paragraph, _w("pPr"))
    ET.SubElement(properties, _w("spacing"), {_w("before"): "0", _w("after"): "0", _w("line"): "190", _w("lineRule"): "auto"})
    run = ET.SubElement(paragraph, _w("r"))
    run_properties = ET.SubElement(run, _w("rPr"))
    ET.SubElement(
        run_properties,
        _w("rFonts"),
        {_w("ascii"): DOCX_FONT, _w("hAnsi"): DOCX_FONT, _w("eastAsia"): DOCX_FONT, _w("cs"): DOCX_FONT},
    )
    ET.SubElement(run_properties, _w("sz"), {_w("val"): str(max(12, int(round(font_size * 2))))})
    ET.SubElement(run_properties, _w("szCs"), {_w("val"): str(max(12, int(round(font_size * 2))))})
    ET.SubElement(run_properties, _w("lang"), {_w("val"): "ru-RU"})
    node = ET.SubElement(run, _w("t"))
    node.set(f"{{{XML_NS}}}space", "preserve")
    node.text = text


def _point_in_rect(x: float, y: float, rect: Tuple[float, float, float, float]) -> bool:
    return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]


def _page_lines(
    page: PageText,
    *,
    excluded_rects: Sequence[Tuple[float, float, float, float]] = (),
    positioned: bool = True,
) -> list[PositionedLine]:
    candidates: list[IndexedWord] = []
    for word in page.words:
        center_x = (word.rect[0] + word.rect[2]) / 2.0
        center_y = (word.rect[1] + word.rect[3]) / 2.0
        if any(_point_in_rect(center_x, center_y, rect) for rect in excluded_rects):
            continue
        candidates.append(word)
    grouped = _geometric_line_groups(candidates)
    reasonable_heights = [
        word.rect[3] - word.rect[1]
        for word in page.words
        if 6.0 <= word.rect[3] - word.rect[1] <= 22.0 and any(character.isalnum() for character in word.text)
    ]
    base_height = median(reasonable_heights) if reasonable_heights else 12.0
    font_ratio = 0.95 if page.used_ocr and not positioned else 0.76
    base_font = max(8.0, min(14.0, base_height * font_ratio))
    lines = []
    for words in grouped:
        words.sort(key=lambda item: item.rect[0])
        text, tab_stops = _line_text_with_tabs(words)
        if not text:
            continue
        rect = (
            min(word.rect[0] for word in words),
            min(word.rect[1] for word in words),
            max(word.rect[2] for word in words),
            max(word.rect[3] for word in words),
        )
        measured_height = median(max(1.0, word.rect[3] - word.rect[1]) for word in words)
        if measured_height < base_height * 0.65 or measured_height > base_height * 1.75:
            font_size = base_font
        else:
            font_size = max(base_font * 0.82, min(base_font * 1.28, measured_height * font_ratio))
        estimated_width = max(18.0, len(text.expandtabs(4)) * font_size * 0.52)
        available_width = max(18.0, page.width - rect[0] - 4.0)
        visual_width = min(available_width, max(rect[2] - rect[0] + 5.0, estimated_width))
        wrapped_rows = max(1, int((estimated_width + visual_width - 1.0) // visual_width))
        reliable_height = base_height * 0.65 <= measured_height <= base_height * 1.75
        source_height = measured_height + 2.0 if reliable_height else base_font * 1.42
        visual_height = max(source_height, font_size * 1.42 * wrapped_rows)
        letters = [character for character in text if character.isalpha()]
        uppercase_ratio = sum(character.isupper() for character in letters) / max(1, len(letters))
        left_margin = rect[0]
        right_margin = page.width - rect[2]
        centered = (
            abs(left_margin - right_margin) <= page.width * 0.085
            and min(left_margin, right_margin) >= page.width * 0.20
        )
        short_centered = centered and visual_width <= page.width * 0.78 and not tab_stops
        alignment = "center" if short_centered else "left"
        if not positioned:
            line_rect = rect
        elif alignment == "center":
            line_rect = (0.0, rect[1], page.width, rect[1] + visual_height)
        else:
            line_rect = (rect[0], rect[1], rect[0] + visual_width, rect[1] + visual_height)
        bold = len(letters) >= 4 and uppercase_ratio >= 0.78
        lines.append(
            PositionedLine(
                text,
                line_rect,
                font_size,
                bold=bold,
                alignment=alignment,
                tab_stops=tab_stops,
            )
        )
    lines.sort(key=lambda item: (item.rect[1], item.rect[0]))
    return _resolve_line_collisions(lines, page.height) if positioned else lines


def _geometric_line_groups(words: Sequence[IndexedWord]) -> list[list[IndexedWord]]:
    """Repair fragmented OCR blocks without merging adjacent scan lines."""
    ordered = sorted(words, key=lambda item: (item.rect[1], item.rect[0]))
    groups: list[list[IndexedWord]] = []
    for word in ordered:
        top = word.rect[1]
        height = max(1.0, word.rect[3] - word.rect[1])
        best_group: list[IndexedWord] | None = None
        best_distance = float("inf")
        for group in groups[-8:]:
            group_top = median(item.rect[1] for item in group)
            group_height = median(max(1.0, item.rect[3] - item.rect[1]) for item in group)
            distance = abs(top - group_top)
            if distance <= max(2.4, min(height, group_height) * 0.28) and distance < best_distance:
                best_group = group
                best_distance = distance
        if best_group is None:
            groups.append([word])
        else:
            best_group.append(word)
    groups.sort(key=lambda group: (median(item.rect[1] for item in group), min(item.rect[0] for item in group)))
    return groups


def _line_text_with_tabs(words: Sequence[IndexedWord]) -> tuple[str, Tuple[float, ...]]:
    if not words:
        return "", ()
    ordered = sorted(words, key=lambda item: item.rect[0])
    heights = [max(1.0, word.rect[3] - word.rect[1]) for word in ordered]
    gap_threshold = max(24.0, median(heights) * 2.4)
    parts = [ordered[0].text]
    tab_stops: list[float] = []
    previous = ordered[0]
    for word in ordered[1:]:
        gap = word.rect[0] - previous.rect[2]
        if gap >= gap_threshold:
            parts.append("\t")
            tab_stops.append(word.rect[0])
        else:
            parts.append(" ")
        parts.append(word.text)
        previous = word
    return "".join(parts).strip(), tuple(tab_stops)


def _append_background(body: ET.Element, page: PageText, relationship: str, page_number: int) -> None:
    paragraph = ET.SubElement(body, _w("p"))
    properties = ET.SubElement(paragraph, _w("pPr"))
    ET.SubElement(properties, _w("spacing"), {_w("before"): "0", _w("after"): "0"})
    run = ET.SubElement(paragraph, _w("r"))
    picture = ET.SubElement(run, _w("pict"))
    rectangle = ET.SubElement(
        picture,
        f"{{{V_NS}}}rect",
        {
            "id": f"pdfbars-page-{page_number}",
            f"{{{O_NS}}}spid": f"_x0000_s{1025 + page_number}",
            "style": (
                f"position:absolute;margin-left:0pt;margin-top:0pt;width:{page.width:.2f}pt;"
                f"height:{page.height:.2f}pt;z-index:-251654144;"
                "mso-position-horizontal-relative:page;mso-position-vertical-relative:page"
            ),
            "stroked": "f",
        },
    )
    ET.SubElement(
        rectangle,
        f"{{{V_NS}}}imagedata",
        {f"{{{R_NS}}}id": relationship, f"{{{O_NS}}}title": ""},
    )
    ET.SubElement(rectangle, f"{{{W10_NS}}}wrap", {"anchorx": "page", "anchory": "page"})


def _resolve_line_collisions(
    lines: Sequence[PositionedLine],
    page_height: float,
) -> list[PositionedLine]:
    resolved: list[PositionedLine] = []
    for line in lines:
        text, rect, font_size = line
        x0, y0, x1, y1 = rect
        height = max(9.0, y1 - y0)
        for previous_line in resolved:
            previous = previous_line.rect
            horizontal = max(0.0, min(x1, previous[2]) - max(x0, previous[0]))
            if horizontal <= min(x1 - x0, previous[2] - previous[0]) * 0.18:
                continue
            if y0 < previous[3] + 1.0 and y1 > previous[1]:
                y0 = previous[3] + 1.0
                y1 = y0 + height
        if y0 >= page_height - 4.0:
            continue
        y1 = min(page_height - 2.0, y1)
        resolved.append(
            PositionedLine(
                text,
                (x0, y0, x1, y1),
                font_size,
                line.bold,
                line.alignment,
                line.tab_stops,
            )
        )
    return resolved


def _append_flowing_page(
    body: ET.Element,
    page: PageText,
    lines: Sequence[PositionedLine],
    tables: Sequence[DetectedTable],
) -> None:
    items: list[tuple[float, int, object]] = [(line.rect[1], 0, line) for line in lines]
    items.extend((table.rect[1], 1, table) for table in tables)
    items.sort(key=lambda item: (item[0], item[1]))
    pending: list[PositionedLine] = []
    for _top, kind, item in items:
        if kind == 0:
            pending.append(item)  # type: ignore[arg-type]
            continue
        for paragraph in _flow_paragraphs(page, pending):
            _append_flow_paragraph(body, paragraph)
        pending.clear()
        _append_positioned_table(body, page, item, positioned=False)  # type: ignore[arg-type]
    for paragraph in _flow_paragraphs(page, pending):
        _append_flow_paragraph(body, paragraph)


def _flow_paragraphs(page: PageText, lines: Sequence[PositionedLine]) -> list[FlowParagraph]:
    if not lines:
        return []
    ordered = sorted(lines, key=lambda line: (line.rect[1], line.rect[0]))
    normal_sizes = [line.font_size for line in ordered if not line.bold and line.alignment == "left"]
    normal_size = median(normal_sizes) if normal_sizes else median(line.font_size for line in ordered)
    paragraphs: list[FlowParagraph] = []
    group: list[PositionedLine] = []

    def flush() -> None:
        if not group:
            return
        first = group[0]
        text = ""
        for line in group:
            part = line.text.strip()
            if not part:
                continue
            if text.endswith("-") and part[:1].islower():
                text = text[:-1] + part
            else:
                text = f"{text} {part}".strip()
        font_size = median(line.font_size for line in group)
        bold = any(line.bold for line in group)
        alignment = first.alignment if all(line.alignment == first.alignment for line in group) else "left"
        left_indent = 0.0
        first_line_indent = 0.0
        tab_stops: Tuple[float, ...] = ()
        if alignment == "left":
            group_left = min(line.rect[0] for line in group)
            left_indent = max(0.0, min(108.0, group_left - 42.0))
            first_line_indent = max(0.0, min(72.0, first.rect[0] - group_left))
            if len(group) == 1 and first.tab_stops:
                tab_stops = tuple(max(0.0, stop - 42.0) for stop in first.tab_stops)
        paragraphs.append(
            FlowParagraph(
                text,
                max(8.0, min(16.0, font_size)),
                bold=bold,
                alignment=alignment,
                left_indent=left_indent,
                first_line_indent=first_line_indent,
                tab_stops=tab_stops,
                space_before=6.0 if bold or alignment == "center" else 0.0,
                space_after=(0.0 if tab_stops else (4.0 if bold or alignment == "center" else 2.0)),
            )
        )
        group.clear()

    for line in ordered:
        if not group:
            group.append(line)
            continue
        previous = group[-1]
        if _starts_new_flow_paragraph(page, previous, line, normal_size):
            flush()
        group.append(line)
    flush()
    return paragraphs


def _starts_new_flow_paragraph(
    page: PageText,
    previous: PositionedLine,
    current: PositionedLine,
    normal_size: float,
) -> bool:
    if previous.alignment != current.alignment or previous.bold != current.bold:
        return True
    if previous.tab_stops or current.tab_stops:
        return True
    if current.bold or current.alignment == "center":
        return True
    vertical_gap = current.rect[1] - previous.rect[3]
    if vertical_gap > max(7.0, normal_size * 0.85):
        return True
    previous_left = previous.rect[0]
    current_left = current.rect[0]
    if current_left - previous_left > max(18.0, page.width * 0.035):
        return True
    text = current.text.lstrip()
    if text.startswith(("•", "- ", "– ", "— ")):
        return True
    if len(text) >= 2 and text[0].isdigit() and text[1] in ".)":
        return True
    if len(text) >= 3 and text[0].isdigit() and text[1].isdigit() and text[2] in ".)":
        return True
    if re.match(r"^\d{1,2}\s+\D", text):
        return True
    return False


def _append_flow_paragraph(body: ET.Element, paragraph: FlowParagraph) -> None:
    element = ET.SubElement(body, _w("p"))
    properties = ET.SubElement(element, _w("pPr"))
    ET.SubElement(properties, _w("jc"), {_w("val"): paragraph.alignment})
    ET.SubElement(
        properties,
        _w("spacing"),
        {
            _w("before"): str(_twips(paragraph.space_before)),
            _w("after"): str(_twips(paragraph.space_after)),
            _w("line"): "276",
            _w("lineRule"): "auto",
        },
    )
    if paragraph.left_indent > 0.5:
        indentation = {_w("left"): str(_twips(paragraph.left_indent))}
        if paragraph.first_line_indent > 0.5:
            indentation[_w("firstLine")] = str(_twips(paragraph.first_line_indent))
        ET.SubElement(properties, _w("ind"), indentation)
    elif paragraph.first_line_indent > 0.5:
        ET.SubElement(properties, _w("ind"), {_w("firstLine"): str(_twips(paragraph.first_line_indent))})
    if paragraph.tab_stops:
        tabs = ET.SubElement(properties, _w("tabs"))
        for position in paragraph.tab_stops:
            ET.SubElement(tabs, _w("tab"), {_w("val"): "left", _w("pos"): str(_twips(position))})
    run = ET.SubElement(element, _w("r"))
    run_properties = ET.SubElement(run, _w("rPr"))
    ET.SubElement(
        run_properties,
        _w("rFonts"),
        {_w("ascii"): DOCX_FONT, _w("hAnsi"): DOCX_FONT, _w("eastAsia"): DOCX_FONT, _w("cs"): DOCX_FONT},
    )
    size = str(max(12, int(round(paragraph.font_size * 2))))
    ET.SubElement(run_properties, _w("sz"), {_w("val"): size})
    ET.SubElement(run_properties, _w("szCs"), {_w("val"): size})
    ET.SubElement(run_properties, _w("lang"), {_w("val"): "ru-RU"})
    if paragraph.bold:
        ET.SubElement(run_properties, _w("b"))
    _append_run_text_with_tabs(run, paragraph.text)


def _append_run_text_with_tabs(run: ET.Element, value: str) -> None:
    parts = value.split("\t")
    for index, part in enumerate(parts):
        if index:
            ET.SubElement(run, _w("tab"))
        if part:
            text_node = ET.SubElement(run, _w("t"))
            text_node.set(f"{{{XML_NS}}}space", "preserve")
            text_node.text = part


def _append_positioned_line(
    body: ET.Element,
    line: PositionedLine,
) -> None:
    text, (x0, y0, x1, y1), font_size = line
    width = max(18.0, x1 - x0 + 5.0)
    height = max(9.0, y1 - y0)
    paragraph = ET.SubElement(body, _w("p"))
    properties = ET.SubElement(paragraph, _w("pPr"))
    ET.SubElement(
        properties,
        _w("framePr"),
        {
            _w("w"): str(_twips(width)),
            _w("h"): str(_twips(height)),
            _w("x"): str(_twips(x0)),
            _w("y"): str(_twips(y0)),
            _w("hAnchor"): "page",
            _w("vAnchor"): "page",
            _w("wrap"): "none",
            _w("hRule"): "exact",
        },
    )
    ET.SubElement(
        properties,
        _w("spacing"),
        {_w("before"): "0", _w("after"): "0", _w("line"): str(_twips(height)), _w("lineRule"): "exact"},
    )
    ET.SubElement(properties, _w("jc"), {_w("val"): line.alignment})
    run = ET.SubElement(paragraph, _w("r"))
    run_properties = ET.SubElement(run, _w("rPr"))
    ET.SubElement(
        run_properties,
        _w("rFonts"),
        {_w("ascii"): DOCX_FONT, _w("hAnsi"): DOCX_FONT, _w("eastAsia"): DOCX_FONT, _w("cs"): DOCX_FONT},
    )
    ET.SubElement(run_properties, _w("sz"), {_w("val"): str(max(12, int(round(font_size * 2))))})
    ET.SubElement(run_properties, _w("szCs"), {_w("val"): str(max(12, int(round(font_size * 2))))})
    ET.SubElement(run_properties, _w("lang"), {_w("val"): "ru-RU"})
    if line.bold:
        ET.SubElement(run_properties, _w("b"))
    _append_run_text_with_tabs(run, text)


def _twips(points: float) -> int:
    return max(0, int(round(float(points) * 20.0)))


def _w(name: str) -> str:
    return f"{{{W_NS}}}{name}"


def _xml_bytes(element: ET.Element) -> bytes:
    return ET.tostring(element, encoding="utf-8", xml_declaration=True)


def _content_types() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="jpg" ContentType="image/jpeg"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/word/settings.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"/>
  <Override PartName="/word/fontTable.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.fontTable+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
  <Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>'''


def _package_relationships() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="{REL_NS}">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>'''


def _document_relationships(image_count: int = 0) -> str:
    # Some RED OS office builds reject a valid but prefixed ``ns0:Relationships``
    # root, so keep the package relationship namespace explicit and unprefixed.
    lines = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        f'<Relationships xmlns="{REL_NS}">',
    ]
    for index in range(1, image_count + 1):
        lines.append(
            '<Relationship '
            f'Id="rIdImage{index}" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="media/page-{index:04d}.jpg"/>'
        )
    for relationship_id, relationship_type, target in (
        ("rIdStyles", "styles", "styles.xml"),
        ("rIdSettings", "settings", "settings.xml"),
        ("rIdFontTable", "fontTable", "fontTable.xml"),
    ):
        lines.append(
            '<Relationship '
            f'Id="{relationship_id}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/{relationship_type}" '
            f'Target="{target}"/>'
        )
    lines.append("</Relationships>")
    return "\n".join(lines)


def _styles() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="{DOCX_FONT}" w:hAnsi="{DOCX_FONT}" w:eastAsia="{DOCX_FONT}" w:cs="{DOCX_FONT}"/><w:lang w:val="ru-RU"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr/></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Обычный"/><w:qFormat/></w:style></w:styles>'''


def _settings() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:settings xmlns:w="{W_NS}"><w:defaultTabStop w:val="708"/><w:characterSpacingControl w:val="doNotCompress"/></w:settings>'''


def _font_table() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:fonts xmlns:w="{W_NS}"><w:font w:name="{DOCX_FONT}"><w:family w:val="roman"/></w:font></w:fonts>'''


def _core_properties(title: str) -> bytes:
    properties = ET.Element(f"{{{CP_NS}}}coreProperties")
    ET.SubElement(properties, f"{{{DC_NS}}}title").text = title
    ET.SubElement(properties, f"{{{DC_NS}}}creator").text = "pdf bars"
    timestamp = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    created = ET.SubElement(properties, f"{{{DCTERMS_NS}}}created")
    created.set(f"{{{XSI_NS}}}type", "dcterms:W3CDTF")
    created.text = timestamp
    return _xml_bytes(properties)


def _app_properties() -> str:
    return '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"><Application>pdf bars</Application></Properties>'''
