from __future__ import annotations

import os
import uuid
from pathlib import Path

from pypdf import PdfWriter

from .storage import cleanup_cache_files


PAGE_SIZES = {
    "A5": (419.53, 595.28),
    "A4": (595.28, 841.89),
    "A3": (841.89, 1190.55),
}


def create_blank_pdf(
    directory: os.PathLike[str] | str,
    page_size: str = "A4",
    orientation: str = "portrait",
) -> Path:
    if page_size not in PAGE_SIZES:
        raise ValueError(f"Неизвестный размер страницы: {page_size}")
    if orientation not in {"portrait", "landscape"}:
        raise ValueError(f"Неизвестная ориентация: {orientation}")
    width, height = PAGE_SIZES[page_size]
    if orientation == "landscape":
        width, height = height, width
    destination_dir = Path(directory)
    destination_dir.mkdir(parents=True, exist_ok=True)
    cleanup_cache_files(
        destination_dir,
        ("blank-*.pdf",),
        max_age_seconds=7 * 86400,
        max_total_bytes=16 * 1024 * 1024,
        protect_recent_seconds=3600,
    )
    destination = destination_dir / f"blank-{uuid.uuid4().hex}.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=height)
    with destination.open("wb") as stream:
        writer.write(stream)
    return destination
