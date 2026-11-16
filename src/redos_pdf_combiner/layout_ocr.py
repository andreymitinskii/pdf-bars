from __future__ import annotations

from pathlib import Path
from typing import Callable, List, Sequence

from . import __version__, diagnostics
from .converters import available_output_path
from .layout_docx import (
    exclude_colored_ink_words,
    render_clean_page_backgrounds,
    render_table_detection_images,
    sanitize_layout_pages,
    write_layout_docx,
)
from .ocr import PageText, build_text_index, ocr_models_available, ocr_pixmap_to_pdf, tessdata_path


LayoutProgress = Callable[[int, int, str], None]


@diagnostics.operation
def source_to_layout_ocr_docx(
    source: str | Path,
    output: str | Path,
    *,
    text_index: Sequence[PageText] | None = None,
    dpi: int = 350,
    language: str = "rus+eng",
    preserve_graphics: bool = False,
    detect_tables: bool = True,
    progress: LayoutProgress | None = None,
) -> Path:
    source_path = Path(source)
    output_path = Path(output)
    if not source_path.is_file():
        raise FileNotFoundError(str(source_path))
    if output_path.suffix.lower() != ".docx":
        raise ValueError("Выходной файл должен иметь расширение .docx")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if text_index is None:
        def page_progress(current: int, total: int, _used_ocr: bool) -> None:
            if progress:
                progress(current, total + 1, f"Распознавание: {source_path.name}")

        pages = build_text_index(
            source_path,
            force_ocr=True,
            dpi=dpi,
            language=language,
            enhance=True,
            progress=page_progress,
        )
    else:
        pages = list(text_index)
    if not pages:
        raise ValueError("Документ не содержит страниц")

    with diagnostics.stage("layout_sanitize"):
        pages = sanitize_layout_pages(pages, language)
    with diagnostics.stage("colored_ink_filter"):
        pages = exclude_colored_ink_words(source_path, pages)
    if progress and detect_tables:
        progress(len(pages), len(pages) + 1, f"Анализ таблиц: {source_path.name}")
    # Text removal is needed only for an optional page background. It erases
    # parts of a ruled grid, so table geometry must be detected from the
    # untouched scan.
    table_images = None
    if detect_tables:
        with diagnostics.stage("layout_table_sources"):
            table_images = render_table_detection_images(source_path, pages, dpi=min(240, max(180, dpi)))
    page_images = [b""] * len(pages)
    if preserve_graphics:
        with diagnostics.stage("layout_backgrounds"):
            page_images = render_clean_page_backgrounds(source_path, pages, dpi=min(240, max(180, dpi)))
    with diagnostics.stage("docx_write"):
        result = write_layout_docx(
            output_path,
            pages,
            page_images,
            title=source_path.stem,
            preserve_graphics=preserve_graphics,
            table_images=table_images,
            detect_tables=detect_tables,
        )
    if progress:
        progress(len(pages) + 1, len(pages) + 1, f"Готово: {source_path.name}")
    return result


def pdfs_to_layout_ocr_docx(
    documents: Sequence[str | Path],
    output_dir: str | Path,
    *,
    dpi: int = 350,
    language: str = "rus+eng",
    preserve_graphics: bool = False,
    detect_tables: bool = True,
    progress: LayoutProgress | None = None,
) -> List[Path]:
    if not documents:
        raise ValueError("Нужно выбрать хотя бы один PDF")
    destination = Path(output_dir)
    destination.mkdir(parents=True, exist_ok=True)
    import fitz

    sources = [Path(document) for document in documents]
    total_pages = 0
    for source in sources:
        try:
            with fitz.open(str(source)) as document:
                total_pages += document.page_count
        except Exception:
            total_pages += 1
    outputs: List[Path] = []
    completed_pages = 0
    for source in sources:
        if source.suffix.lower() != ".pdf":
            raise ValueError(f"Неподдерживаемый формат документа: {source}")
        if not source.is_file():
            raise FileNotFoundError(str(source))
        output = available_output_path(destination / f"{source.stem}_ocr.docx")

        def page_progress(current: int, _total: int, _used_ocr: bool) -> None:
            if progress:
                progress(completed_pages + current, total_pages, f"Распознавание: {source.name}")

        pages = build_text_index(
            source,
            force_ocr=True,
            dpi=dpi,
            language=language,
            enhance=True,
            progress=page_progress,
        )
        source_to_layout_ocr_docx(
            source,
            output,
            text_index=pages,
            dpi=dpi,
            language=language,
            preserve_graphics=preserve_graphics,
            detect_tables=detect_tables,
        )
        outputs.append(output)
        completed_pages += len(pages)
        if progress:
            progress(completed_pages, total_pages, f"Готово: {source.name}")
    return outputs


def create_searchable_pdf(
    source: str | Path,
    output: str | Path,
    *,
    dpi: int = 200,
    language: str = "rus+eng",
) -> Path:
    import fitz

    source_path = Path(source)
    output_path = Path(output)
    if source_path.suffix.lower() != ".pdf" or not source_path.is_file():
        raise ValueError("Для распознавания требуется существующий PDF-файл")
    if not 150 <= int(dpi) <= 400:
        raise ValueError("DPI распознавания должен быть от 150 до 400")
    models = tessdata_path()
    if not ocr_models_available(models, language):
        raise RuntimeError("Не найдены встроенные модели OCR для выбранного языка")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    scale = float(dpi) / 72.0

    searchable = fitz.open()
    try:
        with fitz.open(str(source_path)) as document:
            if document.page_count == 0:
                raise ValueError("PDF не содержит страниц")
            for page in document:
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB, alpha=False)
                ocr_bytes = ocr_pixmap_to_pdf(
                    pixmap,
                    compress=True,
                    language=language,
                    tessdata=str(models),
                )
                with fitz.open(stream=ocr_bytes, filetype="pdf") as ocr_page_document:
                    target = searchable.new_page(width=page.rect.width, height=page.rect.height)
                    target.show_pdf_page(target.rect, ocr_page_document, 0)
            metadata = document.metadata or {}
            metadata["producer"] = f"pdf bars {__version__} — полное OCR перед DOCX"
            metadata["keywords"] = "OCR; русский; английский; редактируемый DOCX"
            searchable.set_metadata(metadata)
        searchable.save(str(output_path), garbage=4, deflate=True, clean=True)
    finally:
        searchable.close()
    return output_path
