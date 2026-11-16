from __future__ import annotations

from .i18n import tr

import math
import os
from dataclasses import replace
from pathlib import Path

from .blank_pages import PAGE_SIZES
from .document_input import open_pdf_input
from .user_errors import input_path


def arrange_pdf_pages(source, output, order, rotations=None) -> Path:
    """Select, duplicate and reorder pages without rasterizing their contents."""
    import fitz

    output = Path(output)
    source = input_path(source)
    if source.resolve() == output.resolve():
        raise ValueError(tr("Сохраните результат под новым именем"))
    if not order:
        raise ValueError(tr("В документе должна остаться хотя бы одна страница"))
    output.parent.mkdir(parents=True, exist_ok=True)
    with open_pdf_input(source) as document:
        if any(index < 0 or index >= document.page_count for index in order):
            raise IndexError(tr("Страница не существует"))
        document.select(list(order))
        for index, angle in (rotations or {}).items():
            document[index].set_rotation((document[index].rotation + angle) % 360)
        document.save(str(output), garbage=4, deflate=True)
    return output


def remap_page_edits(edits, order):
    return [replace(edit, page_index=new_index) for new_index, old_index in enumerate(order)
            for edit in edits if edit.page_index == old_index]


def move_page_selection(count, selected, target):
    selected = set(selected)
    if target in selected:
        return list(range(count))
    moving = [index for index in range(count) if index in selected]
    remaining = [index for index in range(count) if index not in selected]
    position = remaining.index(target) if target in remaining else len(remaining)
    return remaining[:position] + moving + remaining[position:]


def combine_editor_sources(
    inputs: list[os.PathLike[str] | str],
    output_pdf: os.PathLike[str] | str,
) -> Path:
    """Combine PDF files and raster images into one editor document."""
    import fitz

    if not inputs:
        raise ValueError(tr("Нужно выбрать хотя бы один PDF или изображение"))
    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    result = fitz.open()
    try:
        for value in inputs:
            with open_pdf_input(value) as document:
                result.insert_pdf(document)
        if result.page_count == 0:
            raise ValueError(tr("В выбранных файлах нет страниц"))
        result.save(str(output), garbage=4, deflate=True, clean=True)
    finally:
        result.close()
    return output


def split_long_pdf_pages(
    input_pdf: os.PathLike[str] | str,
    output_pdf: os.PathLike[str] | str,
    page_size: str = "A4",
    page_indexes=None,
) -> Path:
    """Split oversized PDF pages into fixed-size sheets without rasterization."""
    import fitz

    if page_size not in PAGE_SIZES:
        raise ValueError(tr(f"Неизвестный размер страницы: {page_size}"))
    source = Path(input_pdf)
    output = Path(output_pdf)
    if not source.is_file() or source.suffix.lower() != ".pdf":
        raise ValueError(tr("Для разрезания требуется существующий PDF"))
    if source.resolve() == output.resolve():
        raise ValueError(tr("Сохраните разрезанный документ под новым именем"))
    output.parent.mkdir(parents=True, exist_ok=True)

    result = fitz.open()
    try:
        with fitz.open(str(source)) as document:
            for page_index, page in enumerate(document):
                if page_indexes is not None and page_index not in page_indexes:
                    result.insert_pdf(document, from_page=page_index, to_page=page_index)
                    continue
                source_width = float(page.rect.width)
                source_height = float(page.rect.height)
                target_width, target_height = PAGE_SIZES[page_size]
                if source_width > source_height:
                    target_width, target_height = target_height, target_width
                scale = target_width / max(1.0, source_width)
                source_segment_height = target_height / max(scale, 1e-6)
                segment_count = max(1, int(math.ceil(max(0.0, source_height - 0.5) / source_segment_height)))
                for segment in range(segment_count):
                    top = segment * source_segment_height
                    bottom = min(source_height, top + source_segment_height)
                    clip = fitz.Rect(0, top, source_width, bottom)
                    target = result.new_page(width=target_width, height=target_height)
                    visible_height = (bottom - top) * scale
                    destination = fitz.Rect(0, 0, target_width, min(target_height, visible_height))
                    target.show_pdf_page(destination, document, page_index, clip=clip, keep_proportion=False)
        metadata = result.metadata or {}
        metadata["producer"] = tr(f"pdf bars — разрезание на {page_size}")
        result.set_metadata(metadata)
        result.save(str(output), garbage=4, deflate=True, clean=True)
    finally:
        result.close()
    return output


def delete_pdf_page(
    input_pdf: os.PathLike[str] | str,
    output_pdf: os.PathLike[str] | str,
    page_index: int,
) -> Path:
    import fitz

    source = Path(input_pdf)
    output = Path(output_pdf)
    output.parent.mkdir(parents=True, exist_ok=True)
    with fitz.open(str(source)) as document:
        if document.page_count <= 1:
            raise ValueError(tr("Нельзя удалить единственную страницу документа"))
        if not 0 <= int(page_index) < document.page_count:
            raise IndexError(tr("Страница для удаления не существует"))
        document.delete_page(int(page_index))
        document.save(str(output), garbage=4, deflate=True, clean=True)
    return output
