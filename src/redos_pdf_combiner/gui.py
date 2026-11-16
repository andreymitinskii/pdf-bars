from __future__ import annotations

import os
import math
import platform
import queue
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import traceback
from dataclasses import asdict, dataclass, replace
from io import BytesIO
from pathlib import Path
from tkinter import colorchooser, filedialog, messagebox, simpledialog, ttk

from PIL import Image, ImageChops, ImageDraw, ImageOps, ImageTk
from pypdf import PdfReader, PdfWriter

from . import __version__
from .pdf_standards import declared_pdfa
from .blank_pages import create_blank_pdf
from .clipboard import decode_clipboard_payload, encode_clipboard_payload, new_clipboard_image_path
from .converters import available_output_path, docx_to_pdf, images_to_individual_pdfs, images_to_pdf, pdf_to_docx, pdf_to_images
from .core import (
    BatchTargetCompressionResult,
    TargetCompressionResult,
    compress_pdf_preset,
    compress_pdf_to_size,
    compress_pdfs_to_total_size,
)
from .editor import (
    AlignmentEdit,
    CropEdit,
    RegionCopyEdit,
    SignatureEdit,
    StrokeEdit,
    TextEdit,
    apply_pdf_edits,
    apply_image_edits,
    available_cyrillic_fonts,
    delete_signature,
    detect_text_style,
    estimate_page_alignment,
    import_signature,
    list_signatures,
    measure_editor_text,
    pdf_has_signatures,
    prepare_word_replacement,
    prepare_signature_for_placement,
    word_text_overlay,
    render_pdf_page_edits_to_image,
    render_pdf_page_image,
    render_image_page_image,
    resolve_font_file,
    rotate_raster_image,
)
from .ocr import PageText, SearchHit, build_text_index, ocr_models_available, recognize_word_region, search_text_index, select_text_in_rect
from .page_tools import (combine_editor_sources, delete_pdf_page, split_long_pdf_pages,
                         arrange_pdf_pages, remap_page_edits, move_page_selection)
from .printing import (
    PrintOptions,
    available_printers,
    remove_temporary_print_file,
    submit_pdf_print_job,
    temporary_print_path,
)
from .launcher import _monitor_geometry_for_point
from .layout_ocr import pdfs_to_layout_ocr_docx, source_to_layout_ocr_docx
from .preferences import (
    existing_recent_files,
    load_preferences,
    normalize_word_styles,
    remember_folder,
    remember_recent_files,
    save_preferences,
    usable_last_folder,
)
from .recovery import clear_recovery, has_recoverable_content, load_recovery, recovery_path, save_recovery
from .storage import available_workspace_source, app_cache_dir, cleanup_cache_files, snapshot_workspace_source
from .render_budget import bounded_preview_scale
from .session_files import EditorSessionFiles
from .user_errors import is_permission_denied, input_path, user_notice
from .document_input import open_pdf_input

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD

    BaseTk = TkinterDnD.Tk
except Exception:  # pragma: no cover - depends on native TkDnD availability
    DND_FILES = None
    BaseTk = tk.Tk


APP_TITLE = "pdf bars"
WHATS_NEW_RELEASE = "1.7"
BLUE = "#0868D7"
DEEP_BLUE = "#0754B8"
HEADER_BLUE = "#E4EEFD"
CYAN = "#1688EE"
BG = "#F3F6FB"
PANEL = "#FFFFFF"
NAV_BG = "#EDF2FA"
DROP_ZONE_BG = "#F7F9FC"
TEXT = "#18304F"
MUTED = "#536780"
BORDER = "#D9E2EF"
SURFACE_ALT = "#F4F8FD"
HOVER_BLUE = "#075CC4"
FONT_FAMILY = "Arial"
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
EDITOR_SAVE_TYPE_SUFFIXES = {
    "pdf": ".pdf",
    "word docx с распознанным текстом": ".docx",
    "docx": ".docx",
    "png": ".png",
    "jpeg": ".jpg",
    "jpg": ".jpg",
    "webp": ".webp",
    "tiff": ".tiff",
    "bmp": ".bmp",
}
APP_ICON_PATH = Path(__file__).with_name("assets") / "pdf-bars-transparent.png"
DROP_ZONE_ICON_PATH = Path(__file__).with_name("assets") / "pdf-drop-zone.png"
if getattr(sys, "frozen", False):
    GUIDE_PATH = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "docs" / "PDF_Bars_User_Guide.pdf"
else:
    GUIDE_PATH = Path(__file__).resolve().parents[2] / "docs" / "PDF_Bars_User_Guide.pdf"
LANCZOS = getattr(getattr(Image, "Resampling", Image), "LANCZOS")
EDITOR_BASIC_COLORS = (
    ("#000000", "Чёрный"),
    ("#FFFFFF", "Белый"),
    ("#E53935", "Красный"),
    ("#F6C344", "Жёлтый"),
    ("#2E7D32", "Зелёный"),
    ("#087FD4", "Синий"),
    ("#7E57C2", "Фиолетовый"),
    ("#808080", "Серый"),
)


@dataclass(frozen=True)
class PageRef:
    source: Path
    page_index: int
    page_count: int
    rotation: int = 0
    label: str = ""
    cached_source: Path | None = None


def resolve_page_ref_source(ref: PageRef) -> Path | None:
    # Once a network snapshot exists, use that stable session copy first so a
    # disconnect during a long save cannot invalidate later pages.
    if ref.cached_source:
        cached = available_workspace_source(ref.cached_source, ref.source)
        if cached is not None:
            return cached
    return available_workspace_source(ref.source)


def missing_workspace_sources_message(sources: list[Path]) -> str:
    unique = list(dict.fromkeys(str(path) for path in sources))
    visible = unique[:8]
    details = "\n".join(f"• {path}" for path in visible)
    if len(unique) > len(visible):
        details += f"\n• … и ещё {len(unique) - len(visible)}"
    return (
        "Не удалось сохранить документ: исходные файлы больше недоступны.\n\n"
        f"{details}\n\n"
        "Проверьте подключение сетевой папки или добавьте указанные файлы заново."
    )


def editor_save_path_for_type(path: Path, selected_type: str, default_suffix: str) -> Path:
    selected = str(selected_type).strip().casefold()
    suffix = ""
    for label, candidate in EDITOR_SAVE_TYPE_SUFFIXES.items():
        if label in selected:
            suffix = candidate
            break
    if not suffix:
        current = path.suffix.casefold()
        known = {".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff", ".bmp"}
        suffix = current if current in known else default_suffix
    if path.suffix.casefold() != suffix:
        path = path.with_suffix(suffix)
    return path


def replacement_word_rect(
    source_rect: tuple[float, float, float, float],
    current_rect: tuple[float, float, float, float],
    text_width: float,
    page_size: tuple[float, float],
    offset_x: float,
    offset_y: float,
) -> tuple[float, float, float, float]:
    page_width, page_height = page_size
    width = max(1.0, float(text_width))
    height = max(1.0, current_rect[3] - current_rect[1])
    x0 = max(0.0, min(max(0.0, page_width - width), source_rect[0] + offset_x))
    y0 = max(0.0, min(max(0.0, page_height - height), source_rect[1] + offset_y))
    return x0, y0, min(page_width, x0 + width), min(page_height, y0 + height)


def editor_output_is_source(source: Path, output: Path) -> bool:
    try:
        source_value = source.expanduser().resolve(strict=False)
        output_value = output.expanduser().resolve(strict=False)
    except (OSError, RuntimeError):
        source_value = source.expanduser().absolute()
        output_value = output.expanduser().absolute()
    return os.path.normcase(str(source_value)) == os.path.normcase(str(output_value))


def duplicate_selected_page_refs(pages, selected) -> tuple[list, set[int]]:
    duplicated = []
    duplicate_indexes: set[int] = set()
    selection = set(selected)
    for index, page in enumerate(pages):
        duplicated.append(page)
        if index in selection:
            duplicated.append(page)
            duplicate_indexes.add(len(duplicated) - 1)
    return duplicated, duplicate_indexes


def page_refs_for_editor(pages, selected, selected_only: bool | None = None) -> list:
    selection = set(selected)
    use_selection = bool(selection) if selected_only is None else bool(selected_only)
    if use_selection:
        return [page for index, page in enumerate(pages) if index in selection]
    return list(pages)


def select_page_indexes(
    index: int,
    anchor: int | None = None,
    extend: bool = False,
    toggle: bool = False,
    selected: set[int] | None = None,
) -> set[int]:
    current = set(selected or ())
    if extend and anchor is not None:
        start, end = sorted((anchor, index))
        page_range = set(range(start, end + 1))
        return current | page_range if toggle else page_range
    if toggle:
        if index in current:
            current.remove(index)
        else:
            current.add(index)
        return current
    return {index}


def create_action_icon(kind: str, color: str, size: int = 20) -> ImageTk.PhotoImage:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    line_width = max(1, size // 10)
    if kind == "add":
        draw.rounded_rectangle((3, 2, size - 4, size - 2), radius=2, outline=color, width=line_width)
        draw.line((size - 8, 2, size - 4, 6), fill=color, width=line_width)
        draw.line((size // 2, 7, size // 2, size - 6), fill=color, width=line_width)
        draw.line((6, size // 2, size - 6, size // 2), fill=color, width=line_width)
    elif kind == "save":
        draw.rounded_rectangle((2, 2, size - 2, size - 2), radius=2, outline=color, width=line_width)
        draw.rectangle((6, 2, size - 6, 7), outline=color, width=line_width)
        draw.rectangle((6, size - 9, size - 6, size - 3), outline=color, width=line_width)
    elif kind == "clear":
        draw.line((5, 6, size - 5, 6), fill=color, width=line_width)
        draw.line((8, 3, size - 8, 3), fill=color, width=line_width)
        draw.rounded_rectangle((7, 7, size - 7, size - 3), radius=1, outline=color, width=line_width)
    elif kind == "print":
        draw.rounded_rectangle((5, 2, size - 5, 8), radius=1, outline=color, width=line_width)
        draw.rounded_rectangle((3, 7, size - 3, size - 6), radius=2, outline=color, width=line_width)
        draw.rounded_rectangle((5, size - 10, size - 5, size - 2), radius=1, outline=color, width=line_width)
        draw.ellipse((size - 7, 10, size - 5, 12), fill=color)
    elif kind == "folder":
        draw.line((2, 6, 2, size - 3, size - 2, size - 3, size - 2, 6, 10, 6, 8, 3, 2, 3, 2, 6), fill=color, width=line_width)
    elif kind == "image":
        draw.rectangle((2, 2, size - 2, size - 2), outline=color, width=line_width)
        draw.ellipse((5, 5, 8, 8), outline=color, width=line_width)
        draw.line((3, size - 4, 8, 11, 12, 15, 16, 9, size - 3, 13), fill=color, width=line_width)
    elif kind in {"document", "layers"}:
        if kind == "layers":
            draw.line((3, 6, size//2, 2, size-3, 6, size//2, 10, 3, 6), fill=color, width=line_width)
            draw.line((3, 11, size//2, 15, size-3, 11), fill=color, width=line_width)
            draw.line((3, 16, size//2, 20, size-3, 16), fill=color, width=line_width)
        else:
            draw.rectangle((4, 2, size-4, size-2), outline=color, width=line_width)
            for y in (7, 11, 15):
                draw.line((7, y, size-7, y), fill=color, width=line_width)
    elif kind == "info":
        draw.ellipse((2, 2, size-2, size-2), outline=color, width=line_width)
        draw.ellipse((size//2-1, 5, size//2+1, 7), fill=color)
        draw.line((size//2, 10, size//2, size-5), fill=color, width=line_width)
    elif kind == "grid":
        for x in (3, 12):
            for y in (3, 12):
                draw.rectangle((x, y, x+5, y+5), outline=color, width=line_width)
    elif kind in {"undo", "redo"}:
        draw.arc((4, 5, size-3, size-3), 220 if kind == "redo" else 140,
                 40 if kind == "redo" else 320, fill=color, width=line_width)
        x = size-4 if kind == "redo" else 4
        direction = -1 if kind == "redo" else 1
        draw.line((x+direction*5, 2, x, 7, x+direction*5, 11), fill=color, width=line_width)
    return ImageTk.PhotoImage(image)


def create_editor_tool_icon(kind: str, color: str = DEEP_BLUE, size: int = 22) -> ImageTk.PhotoImage:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    width = max(2, size // 11)
    left, top, right, bottom = 3, 3, size - 4, size - 4
    if kind == "select":
        draw.polygon(((4, 3), (4, 17), (8, 13), (11, 20), (14, 18), (11, 12), (17, 12)), fill=color)
    elif kind == "text":
        draw.line((4, 5, 18, 5), fill=color, width=width)
        draw.line((11, 5, 11, 18), fill=color, width=width)
        draw.line((7, 18, 15, 18), fill=color, width=width)
    elif kind == "replace_word":
        draw.line((3, 7, 9, 7), fill=color, width=width)
        draw.line((13, 7, 19, 7), fill=color, width=width)
        draw.line((5, 15, 17, 15), fill=color, width=width)
        draw.line((9, 4, 13, 10), fill=color, width=width)
        draw.line((13, 4, 9, 10), fill=color, width=width)
    elif kind == "erase":
        draw.rounded_rectangle((4, 5, 18, 17), radius=2, outline=color, width=width)
        draw.rectangle((7, 8, 15, 14), fill=color)
    elif kind == "copy_region":
        draw.rectangle((3, 3, 15, 15), outline=color, width=width)
        draw.rectangle((8, 8, 19, 19), outline=color, width=width)
    elif kind == "crop":
        draw.line((5, 2, 5, 16, 19, 16), fill=color, width=width)
        draw.line((2, 6, 16, 6, 16, 20), fill=color, width=width)
    elif kind == "align":
        draw.line((4, 6, 18, 4), fill=color, width=width)
        draw.line((4, 10, 18, 10), fill=color, width=width)
        draw.line((4, 15, 18, 15), fill=color, width=width)
        draw.line((11, 18, 11, 21), fill=color, width=width)
    elif kind == "pencil":
        draw.line((5, 17, 16, 6), fill=color, width=4)
        draw.polygon(((3, 20), (5, 15), (8, 18)), fill=color)
        draw.line((15, 5, 18, 8), fill=color, width=3)
    elif kind == "line":
        draw.line((4, 18, 18, 4), fill=color, width=width)
        draw.ellipse((2, 16, 6, 20), fill=color)
        draw.ellipse((16, 2, 20, 6), fill=color)
    elif kind == "arrow":
        draw.line((4, 18, 18, 4), fill=color, width=width)
        draw.line((11, 4, 18, 4, 18, 11), fill=color, width=width)
    elif kind == "free_erase":
        draw.polygon(((4, 15), (12, 5), (19, 11), (11, 20)), outline=color)
        draw.line((4, 15, 11, 20), fill=color, width=width)
        draw.line((8, 18, 17, 18), fill=color, width=width)
    elif kind == "signature":
        draw.line((3, 16, 7, 11, 8, 17, 12, 7, 13, 16, 19, 13), fill=color, width=width)
        draw.line((3, 19, 19, 19), fill=color, width=1)
    elif kind == "eyedropper":
        draw.line((5, 16, 15, 6), fill=color, width=4)
        draw.line((11, 4, 17, 10), fill=color, width=width)
        draw.polygon(((3, 19), (4, 14), (8, 18)), outline=color)
    elif kind == "preset_load":
        draw.polygon(((2, 6), (8, 6), (10, 9), (19, 9), (17, 18), (2, 18)), outline=color)
        draw.line((9, 10, 9, 16, 6, 13), fill=color, width=width)
        draw.line((9, 16, 12, 13), fill=color, width=width)
    else:
        draw.rectangle((left, top, right, bottom), outline=color, width=width)
    return ImageTk.PhotoImage(image)


def editor_preview_font_spec(edit: TextEdit, scale: float) -> tuple[str, int, str]:
    """Return a Tk font that follows PDF pixels and the resolved font file."""
    family = edit.font_family
    font_name = Path(edit.font_file or "").stem.casefold().replace("-", "").replace("_", "")
    for marker, resolved_family in (
        ("liberationserif", "Liberation Serif"),
        ("liberationsans", "Liberation Sans"),
        ("dejavuserif", "DejaVu Serif"),
        ("dejavusans", "DejaVu Sans"),
        ("ptsans", "PT Sans"),
        ("notosans", "Noto Sans"),
        ("times", "Times New Roman"),
        ("arial", "Arial"),
    ):
        if marker in font_name:
            family = resolved_family
            break
    styles = " ".join(
        style
        for enabled, style in (
            (edit.bold, "bold"),
            (edit.italic, "italic"),
            (edit.underline, "underline"),
        )
        if enabled
    )
    # A negative Tk size is measured in pixels. PDF points rendered at the
    # current page scale are pixels too, so zoom no longer changes proportions.
    pixel_size = -max(1, int(round(edit.font_size * scale)))
    return family, pixel_size, styles


def create_rounded_rectangle(canvas: tk.Canvas, x1, y1, x2, y2, radius=18, **options):
    radius = max(2, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = (
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    )
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class RoundedPanel(tk.Frame):
    """Tk container with a drawn rounded background and regular child layout."""

    def __init__(
        self,
        master,
        *,
        fill=PANEL,
        outer_bg=BG,
        radius=16,
        border=BORDER,
        border_width=1,
        dash=None,
        padx=12,
        pady=12,
        **kwargs,
    ) -> None:
        super().__init__(
            master,
            bg=outer_bg,
            borderwidth=0,
            highlightthickness=0,
            padx=padx,
            pady=pady,
            **kwargs,
        )
        self._panel_fill = fill
        self._panel_outer_bg = outer_bg
        self._panel_radius = radius
        self._panel_border = border
        self._panel_border_width = border_width
        self._panel_dash = dash
        self._panel_canvas = tk.Canvas(
            self,
            bg=outer_bg,
            highlightthickness=0,
            borderwidth=0,
            takefocus=False,
        )
        self._panel_canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self._panel_canvas.tk.call("lower", self._panel_canvas._w)
        self.bind("<Configure>", self._redraw_panel, add="+")

    def _redraw_panel(self, _event=None) -> None:
        width = self.winfo_width()
        height = self.winfo_height()
        if width < 4 or height < 4:
            return
        self._panel_canvas.delete("panel")
        options = {
            "radius": self._panel_radius,
            "fill": self._panel_fill,
            "outline": self._panel_border,
            "width": self._panel_border_width,
            "tags": "panel",
        }
        if self._panel_dash:
            options["dash"] = self._panel_dash
        create_rounded_rectangle(
            self._panel_canvas,
            1,
            1,
            width - 2,
            height - 2,
            **options,
        )

    def set_panel_border(self, color: str, width: int | None = None) -> None:
        self._panel_border = color
        if width is not None:
            self._panel_border_width = width
            self.configure(highlightthickness=width)
        self.configure(highlightbackground=color, highlightcolor=color)
        self._redraw_panel()

    def set_panel_fill(self, color: str) -> None:
        if self._panel_fill != color:
            self._panel_fill = color
            self._redraw_panel()


def create_rounded_card(
    master,
    *,
    fill=PANEL,
    outer_bg=BG,
    radius=16,
    border=BORDER,
    border_width=1,
    padding=12,
    **kwargs,
):
    """Create a rounded shell and an inset body that cannot cover its outline."""
    shell = RoundedPanel(
        master,
        fill=fill,
        outer_bg=outer_bg,
        radius=radius,
        border=border,
        border_width=border_width,
        padx=0,
        pady=0,
        **kwargs,
    )
    body = tk.Frame(shell, bg=fill, borderwidth=0, highlightthickness=0)
    body.pack(fill="both", expand=True, padx=padding, pady=padding)
    return shell, body


class ToolTip:
    def __init__(self, widget, text: str, delay_ms: int = 450) -> None:
        self.widget = widget
        self.text = text
        self.delay_ms = delay_ms
        self._after_id = None
        self._tip_window = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")
        widget.bind("<FocusIn>", self._schedule, add="+")
        widget.bind("<FocusOut>", self._hide, add="+")

    def _schedule(self, _event=None) -> None:
        self._cancel()
        self._after_id = self.widget.after(self.delay_ms, self._show)

    def _cancel(self) -> None:
        if self._after_id:
            self.widget.after_cancel(self._after_id)
            self._after_id = None

    def _show(self) -> None:
        if self._tip_window or not self.text:
            return
        x = self.widget.winfo_rootx() + 18
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 8
        self._tip_window = tw = tk.Toplevel(self.widget)
        tw.wm_overrideredirect(True)
        tw.wm_geometry(f"+{x}+{y}")
        label = tk.Label(
            tw,
            text=self.text,
            justify="left",
            bg="#FFF8DB",
            fg=TEXT,
            relief="solid",
            borderwidth=1,
            padx=8,
            pady=5,
            wraplength=340,
            font=("Arial", 9),
        )
        label.pack()

    def _hide(self, _event=None) -> None:
        self._cancel()
        if self._tip_window:
            self._tip_window.destroy()
            self._tip_window = None


class TabWorkspace(RoundedPanel):
    """Flat application tabs matching the approved visual layout."""

    def __init__(self, parent) -> None:
        super().__init__(
            parent,
            fill=PANEL,
            outer_bg=BG,
            radius=8,
            border=BORDER,
            padx=0,
            pady=0,
        )
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self._tabs: list[tuple[tk.Widget, tk.Button, tk.Frame]] = []
        self._current: tk.Widget | None = None
        self._icons = {}
        self.shell_body = tk.Frame(self, bg=PANEL, borderwidth=0, highlightthickness=0)
        self.shell_body.grid(row=0, column=0, sticky="nsew", padx=3, pady=3)
        self.shell_body.columnconfigure(0, weight=1)
        self.shell_body.rowconfigure(1, weight=1)
        self.navigation_shell, self.navigation = create_rounded_card(
            self.shell_body,
            fill=NAV_BG,
            outer_bg=PANEL,
            radius=8,
            border=BORDER,
            padding=4,
            height=42,
        )
        self.navigation_shell.grid(row=0, column=0, sticky="ew")
        self.navigation_shell.grid_propagate(False)
        self.navigation.rowconfigure(0, weight=1)
        self.navigation.bind("<Configure>", lambda _event: self.after_idle(self._fit_tab_icons))
        self.content = tk.Frame(self.shell_body, bg=PANEL)
        self.content.grid(row=1, column=0, sticky="nsew", pady=(4, 0))
        self.content.columnconfigure(0, weight=1)
        self.content.rowconfigure(0, weight=1)

    def add(self, child: tk.Widget, text: str) -> None:
        index = len(self._tabs)
        self.navigation.columnconfigure(index, weight=1, uniform="tabs")
        holder = tk.Frame(self.navigation, bg=NAV_BG)
        holder.grid(row=0, column=index, sticky="nsew")
        holder.rowconfigure(0, weight=1)
        holder.columnconfigure(0, weight=1)
        kind = ("pencil", "image", "document", "folder", "layers", "info")[index % 6]
        factory = create_editor_tool_icon if kind == "pencil" else create_action_icon
        self._icons[index] = (factory(kind, TEXT, 20), factory(kind, "#FFFFFF", 20))
        button = tk.Button(
            holder,
            text=text,
            image=self._icons[index][0],
            compound="left",
            padx=6,
            command=lambda target=child: self.select(target),
            bg=NAV_BG,
            fg="#425878",
            activebackground="#E7EBF2",
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=NAV_BG,
            highlightcolor=BLUE,
            takefocus=True,
            font=(FONT_FAMILY, 10),
            cursor="hand2",
        )
        button.grid(row=0, column=0, sticky="nsew", padx=3, pady=(2, 0))
        button.bind("<Return>", lambda _event, target=child: self.select(target))
        line = tk.Frame(holder, bg=NAV_BG, height=3)
        line.grid(row=1, column=0, sticky="ew", padx=12, pady=(0, 2))
        child.grid(row=0, column=0, sticky="nsew")
        child.grid_remove()
        self._tabs.append((child, button, line))
        if self._current is None:
            self.select(child)

    def select(self, target=None):
        if target is None:
            return str(self._current) if self._current is not None else ""
        if isinstance(target, int):
            target = self._tabs[target][0]
        for index, (child, button, line) in enumerate(self._tabs):
            active = child is target
            if active:
                child.grid()
                button.configure(bg=BLUE, fg="white", activebackground=HOVER_BLUE,
                                 activeforeground="white", image=self._icons[index][1],
                                 font=(FONT_FAMILY, 10, "bold"))
                line.configure(bg=NAV_BG)
                self._current = child
            else:
                child.grid_remove()
                button.configure(bg=NAV_BG, fg=TEXT, activebackground="#DCE8FA", activeforeground=TEXT,
                                 image=self._icons[index][0], font=(FONT_FAMILY, 10))
                line.configure(bg=NAV_BG)
        return str(self._current) if self._current is not None else ""

    def _fit_tab_icons(self, _event=None):
        for index, (child, button, _) in enumerate(self._tabs):
            text_width = int(self.tk.call("font", "measure", button.cget("font"), button.cget("text")))
            image = self._icons[index][int(child is self._current)] if button.winfo_width() >= text_width + 34 else ""
            if self.tk.splitlist(button.cget("image")) != ((str(image),) if image else ()):
                button.configure(image=image)


class BlankPageDialog(simpledialog.Dialog):
    def body(self, master):
        self.title("Пустая страница")
        self.resizable(False, False)
        self.page_size = tk.StringVar(value="A4")
        self.orientation = tk.StringVar(value="portrait")
        ttk.Label(master, text="Размер").grid(row=0, column=0, sticky="w", padx=6, pady=6)
        ttk.Combobox(master, textvariable=self.page_size, values=("A5", "A4", "A3"), state="readonly", width=12).grid(
            row=0, column=1, sticky="ew", padx=6, pady=6
        )
        ttk.Label(master, text="Ориентация").grid(row=1, column=0, sticky="w", padx=6, pady=6)
        orientation = ttk.Frame(master)
        orientation.grid(row=1, column=1, sticky="w", padx=6, pady=6)
        ttk.Radiobutton(orientation, text="Книжная", variable=self.orientation, value="portrait").pack(side="left")
        ttk.Radiobutton(orientation, text="Альбомная", variable=self.orientation, value="landscape").pack(
            side="left", padx=(10, 0)
        )
        return master

    def buttonbox(self) -> None:
        box = ttk.Frame(self)
        ttk.Button(box, text="Создать", command=self.ok, style="Accent.TButton").pack(side="left", padx=5)
        ttk.Button(box, text="Отмена", command=self.cancel).pack(side="left", padx=5)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        box.pack(pady=(4, 10))

    def apply(self) -> None:
        self.result = self.page_size.get(), self.orientation.get()


class WordReplacementDialog(simpledialog.Dialog):
    def __init__(
        self,
        parent,
        original_text: str,
        font_family: str,
        font_size: float,
        bold: bool,
        italic: bool,
        scan_effect: bool = True,
        underline: bool = False,
    ) -> None:
        self.original_text = original_text
        self.detected_font_family = font_family
        self.detected_font_size = font_size
        self.replacement_text = tk.StringVar(value=original_text)
        self.bold = tk.BooleanVar(value=bold)
        self.italic = tk.BooleanVar(value=italic)
        self.scan_effect = tk.BooleanVar(value=scan_effect)
        self.underline = tk.BooleanVar(value=underline)
        self.result = None
        super().__init__(parent, "Заменить слово")

    def body(self, master):
        self.resizable(False, False)
        ttk.Label(master, text=f"Исходное слово: {self.original_text}").grid(
            row=0, column=0, columnspan=3, sticky="w", padx=6, pady=(6, 3)
        )
        ttk.Label(master, text="Новое слово").grid(row=1, column=0, sticky="w", padx=6, pady=5)
        entry = ttk.Entry(master, textvariable=self.replacement_text, width=32)
        entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=6, pady=5)
        ttk.Label(
            master,
            text=f"Подобрано: {self.detected_font_family}, {self.detected_font_size:.1f} пт; цвет: чёрный",
        ).grid(row=2, column=0, columnspan=3, sticky="w", padx=6, pady=(3, 6))
        ttk.Checkbutton(master, text="Жирный", variable=self.bold).grid(row=3, column=1, sticky="w", padx=6, pady=5)
        ttk.Checkbutton(master, text="Курсив", variable=self.italic).grid(row=3, column=2, sticky="w", padx=6, pady=5)
        ttk.Checkbutton(master, text="Подчёркнутый", variable=self.underline).grid(row=3, column=0, sticky="w", padx=6, pady=5)
        ttk.Checkbutton(master, text="Эффект скана", variable=self.scan_effect).grid(
            row=4, column=1, columnspan=2, sticky="w", padx=6, pady=(1, 5)
        )
        master.columnconfigure(1, weight=1)
        return entry

    def validate(self):
        return bool(self.replacement_text.get().strip())

    def apply(self):
        self.result = (self.replacement_text.get().strip(), self.bold.get(), self.italic.get(),
                       self.scan_effect.get(), self.underline.get())


class PrintDialog(simpledialog.Dialog):
    def __init__(self, parent, page_count: int) -> None:
        self.page_count = max(1, int(page_count))
        self.printers = available_printers()
        self.printer_display = tk.StringVar()
        self.copies = tk.IntVar(value=1)
        self.page_range = tk.StringVar()
        self.duplex = tk.BooleanVar(value=False)
        self.grayscale = tk.BooleanVar(value=False)
        super().__init__(parent, title="Печать")

    def body(self, master):
        default = next((printer.name for printer in self.printers if printer.is_default), "")
        values = ["Системный принтер по умолчанию"] + [printer.name for printer in self.printers]
        self.printer_display.set(default or values[0])
        ttk.Label(master, text="Принтер").grid(row=0, column=0, sticky="w", padx=7, pady=6)
        ttk.Combobox(
            master,
            textvariable=self.printer_display,
            values=values,
            state="readonly",
            width=38,
        ).grid(row=0, column=1, sticky="ew", padx=7, pady=6)
        ttk.Label(master, text="Копии").grid(row=1, column=0, sticky="w", padx=7, pady=6)
        tk.Spinbox(master, from_=1, to=999, textvariable=self.copies, width=8).grid(
            row=1, column=1, sticky="w", padx=7, pady=6
        )
        ttk.Label(master, text="Страницы").grid(row=2, column=0, sticky="w", padx=7, pady=6)
        page_entry = ttk.Entry(master, textvariable=self.page_range, width=24)
        page_entry.grid(row=2, column=1, sticky="ew", padx=7, pady=6)
        ttk.Label(
            master,
            text=f"Пустое поле — все {self.page_count}; пример: 1-3,5",
            foreground=MUTED,
        ).grid(row=3, column=1, sticky="w", padx=7, pady=(0, 6))
        ttk.Checkbutton(master, text="Двусторонняя печать", variable=self.duplex).grid(
            row=4, column=1, sticky="w", padx=7, pady=4
        )
        ttk.Checkbutton(master, text="Оттенки серого", variable=self.grayscale).grid(
            row=5, column=1, sticky="w", padx=7, pady=4
        )
        master.columnconfigure(1, weight=1)
        return page_entry

    def validate(self) -> bool:
        try:
            copies = int(self.copies.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Печать", "Введите количество копий от 1 до 999", parent=self)
            return False
        if not 1 <= copies <= 999:
            messagebox.showerror("Печать", "Введите количество копий от 1 до 999", parent=self)
            return False
        value = self.page_range.get().replace(" ", "")
        if value:
            import re

            if not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", value):
                messagebox.showerror("Печать", "Диапазон задаётся, например, так: 1-3,5,8", parent=self)
                return False
            for item in value.split(","):
                bounds = [int(part) for part in item.split("-")]
                if min(bounds) < 1 or max(bounds) > self.page_count or len(bounds) == 2 and bounds[0] > bounds[1]:
                    messagebox.showerror("Печать", f"Допустимы страницы от 1 до {self.page_count}", parent=self)
                    return False
        return True

    def buttonbox(self) -> None:
        box = ttk.Frame(self)
        ttk.Button(box, text="Печатать", command=self.ok, style="Accent.TButton").pack(side="left", padx=5)
        ttk.Button(box, text="Отмена", command=self.cancel).pack(side="left", padx=5)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        box.pack(pady=(5, 11))

    def apply(self) -> None:
        selected = self.printer_display.get()
        printer = "" if selected == "Системный принтер по умолчанию" else selected
        self.result = PrintOptions(
            printer=printer,
            copies=int(self.copies.get()),
            page_range=self.page_range.get().replace(" ", ""),
            duplex=bool(self.duplex.get()),
            grayscale=bool(self.grayscale.get()),
        )


class PageAlignmentDialog(simpledialog.Dialog):
    def __init__(self, parent, current_angle: float, detected_angle: float, preview) -> None:
        self.current_angle = float(current_angle)
        self.detected_angle = float(detected_angle)
        self.preview = preview
        self.angle = tk.DoubleVar(master=parent, value=self.current_angle)
        super().__init__(parent, title="Выравнивание страницы")

    def body(self, master):
        ttk.Label(
            master,
            text=f"Автоматически определённая поправка: {self.detected_angle:+.2f}°",
        ).grid(row=0, column=0, columnspan=3, sticky="w", padx=8, pady=(8, 5))
        ttk.Label(master, text="Угол").grid(row=1, column=0, sticky="w", padx=8, pady=6)
        scale = tk.Scale(
            master,
            variable=self.angle,
            from_=-15.0,
            to=15.0,
            resolution=0.1,
            orient="horizontal",
            length=290,
            showvalue=False,
            command=self._preview_angle,
        )
        scale.grid(row=1, column=1, sticky="ew", padx=5, pady=6)
        spin = ttk.Spinbox(master, from_=-15.0, to=15.0, increment=0.1, textvariable=self.angle, width=7)
        spin.grid(row=1, column=2, padx=8, pady=6)
        spin.bind("<KeyRelease>", self._preview_angle)
        actions = ttk.Frame(master)
        actions.grid(row=2, column=1, sticky="w", padx=5, pady=(0, 8))
        ttk.Button(actions, text="Авто", command=lambda: self._set_angle(self.detected_angle)).pack(side="left")
        ttk.Button(actions, text="Сбросить", command=lambda: self._set_angle(0.0)).pack(side="left", padx=(5, 0))
        ttk.Label(
            master,
            text="Положительный угол поворачивает страницу против часовой стрелки.",
            foreground=MUTED,
        ).grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=(0, 8))
        master.columnconfigure(1, weight=1)
        return spin

    def _set_angle(self, value: float) -> None:
        self.angle.set(round(float(value), 2))
        self._preview_angle()

    def _preview_angle(self, _event=None) -> None:
        try:
            value = max(-15.0, min(15.0, float(self.angle.get())))
        except (tk.TclError, ValueError):
            return
        self.preview(value)

    def validate(self) -> bool:
        try:
            value = float(self.angle.get())
        except (tk.TclError, ValueError):
            messagebox.showerror("Выравнивание", "Введите угол от -15 до 15 градусов", parent=self)
            return False
        if not -15.0 <= value <= 15.0:
            messagebox.showerror("Выравнивание", "Допустимый угол: от -15 до 15 градусов", parent=self)
            return False
        return True

    def apply(self) -> None:
        self.result = round(float(self.angle.get()), 2)

    def cancel(self, event=None) -> None:
        self.preview(None)
        super().cancel(event)


class PdfCombinerApp(BaseTk):
    def __init__(self, initially_hidden: bool = False) -> None:
        super().__init__()
        if initially_hidden:
            self.withdraw()
        self.title(APP_TITLE)
        self._uses_override_chrome = os.name == "nt"
        if self._uses_override_chrome:
            self.overrideredirect(True)
        self.geometry("1460x920")
        self.minsize(1080, 700)
        self.configure(bg=BG)
        self._window_drag_origin: tuple[int, int] | None = None
        self._window_restore_geometry = "1460x920"
        self._window_maximized = False
        self._restore_override_after_map = False
        self._header_render_after = None
        self._window_rounding_after = None
        self._x11_client_window_id = None
        self.preferences = load_preferences()


        self.pages: list[PageRef] = []
        self.selected: set[int] = set()
        self.last_selected_index: int | None = None
        self.thumbnails: list[ImageTk.PhotoImage | None] = []
        self.page_tiles: list[dict[str, object]] = []
        self.image_files: list[str] = []
        self.docx_files: list[str] = []
        self.pdf_docx_files: list[str] = []
        self.compress_files: list[str] = []
        self.autosave_after_id = None
        self.autosave_pending_id = None
        self._last_recovery_payload: dict | None = None
        self.restoring_session = False
        self.drag_source_index: int | None = None
        self.drag_start_xy: tuple[int, int] | None = None
        self.page_reflow_after = None
        self.pdf_image_input = tk.StringVar()
        self.pdf_image_output_dir = tk.StringVar()
        image_format = self.preferences.get("pdf_image_format", "jpg")
        self.pdf_image_format = tk.StringVar(value=image_format if image_format in {"jpg", "png", "webp", "tiff"} else "jpg")
        self.pdf_image_dpi = tk.IntVar(value=150)
        self.compress_output_dir = tk.StringVar()
        saved_compress_preset = self.preferences.get("compress_preset", "medium")
        self.compress_mode = tk.StringVar(value="preset")
        self.compress_preset = tk.StringVar(value=saved_compress_preset if saved_compress_preset in {"maximum", "medium"} else "medium")
        self.compress_target_mb = tk.StringVar(value="10")
        self.compress_color_mode = tk.StringVar(value="color")
        color_labels = {"color": "Цветной", "grayscale": "Оттенки серого", "monochrome": "Чёрно-белый (минимальный размер)"}
        self.compress_color_display = tk.StringVar(value=color_labels.get(self.compress_color_mode.get(), "Цветной"))
        self.compress_estimate = tk.StringVar(value="Добавьте PDF")
        self.pdf_workspace_summary = tk.StringVar(value="Рабочая область пуста")
        self.image_files_summary = tk.StringVar(value="Файлы не добавлены")
        self.docx_files_summary = tk.StringVar(value="Файлы не добавлены")
        self.pdf_docx_files_summary = tk.StringVar(value="Файлы не добавлены")
        self.compress_files_summary = tk.StringVar(value="Файлы не добавлены")
        self.editor_pdf_path: Path | None = None
        self.editor_source_kind: str | None = None
        self.editor_page_index = 0
        self.editor_page_count = 0
        self.editor_page_source_names: list[str] = []
        self.editor_scale = 1.0
        self.editor_offset = (0.0, 0.0)
        self.editor_page_size = (0.0, 0.0)
        self.editor_text_edits: list[TextEdit] = []
        self.editor_signature_edits: list[SignatureEdit] = []
        self.editor_region_edits: list[RegionCopyEdit] = []
        self.editor_stroke_edits: list[StrokeEdit] = []
        self.editor_crop_edits: list[CropEdit] = []
        self.editor_alignment_edits: list[AlignmentEdit] = []
        self.editor_alignment_preview: tuple[int, float] | None = None
        self.editor_undo_stack: list[
            tuple[list[TextEdit], list[SignatureEdit], list[RegionCopyEdit], list[StrokeEdit], list[CropEdit], list[AlignmentEdit], tuple[str, int] | None]
        ] = []
        self.editor_redo_stack: list[
            tuple[list[TextEdit], list[SignatureEdit], list[RegionCopyEdit], list[StrokeEdit], list[CropEdit], list[AlignmentEdit], tuple[str, int] | None]
        ] = []
        self.editor_selected: tuple[str, int] | None = None
        self.editor_clipboard: dict | None = None
        self.editor_region_selection: tuple[int, tuple[float, float, float, float]] | None = None
        self.editor_recognized_text_selection: tuple[int, tuple[tuple[float, float, float, float], ...], str] | None = None
        self.editor_dirty = False
        self.editor_signature_paths: list[Path] = []
        self.editor_overlay_images: list[ImageTk.PhotoImage] = []
        self.editor_placing_signature: Path | None = None
        self.editor_place_mode: str | None = None
        self.editor_pending_crop: tuple[float, float, float, float] | None = None
        self.editor_split_page_size = tk.StringVar(value="A4")
        self.editor_drag_start: tuple[float, float] | None = None
        self.editor_stroke_points: list[tuple[float, float]] = []
        self.editor_object_drag: tuple[str, int, str, tuple[float, float, float, float]] | None = None
        self.editor_zoom_percent = tk.DoubleVar(value=100.0)
        self.editor_zoom_label = tk.StringVar(value="100%")
        self.editor_zoom_mode = "page"
        self.editor_page_refit_after_id = None
        self.editor_replace_text = tk.StringVar()
        self.editor_font_size = tk.DoubleVar(value=11.0)
        self.editor_font_family = tk.StringVar(value=self.preferences.get("editor_font_family", "Times New Roman"))
        self.editor_text_bold = tk.BooleanVar(value=False)
        self.editor_text_italic = tk.BooleanVar(value=False)
        self.editor_text_underline = tk.BooleanVar(value=False)
        self.editor_text_scan_effect = tk.BooleanVar(value=False)
        self.editor_word_offset_x = tk.DoubleVar(value=0.0)
        self.editor_word_offset_y = tk.DoubleVar(value=0.0)
        self.editor_word_scan_effect = tk.BooleanVar(value=True)
        self.editor_word_after_ocr = False
        self.editor_word_background_ocr = False
        self.editor_word_region_uses_ocr = True
        self.editor_word_busy_dialog = None
        self.editor_text_color = tk.StringVar(value="#000000")
        self.editor_draw_color = tk.StringVar(value="#000000")
        self.editor_pencil_width = tk.DoubleVar(value=2.5)
        self.editor_eraser_width = tk.DoubleVar(value=14.0)
        self.editor_page_label = tk.StringVar(value="Страница —")
        self.editor_reader_page_label = tk.StringVar(value="— / —")
        self.editor_warning = tk.StringVar(value="")
        self.editor_new_document = False
        self.editor_last_save_path: Path | None = None
        self.editor_context_canvas_point: tuple[float, float] | None = None
        self.editor_context_menu: tk.Menu | None = None
        self.pdf_context_menu: tk.Menu | None = None
        self.editor_tool_buttons: dict[str, ttk.Button] = {}
        self.editor_property_frames: dict[str, tk.Frame] = {}
        self.editor_thumbnail_images: list[ImageTk.PhotoImage | None] = []
        self.editor_thumbnail_widgets: list[dict[str, object]] = []
        self.editor_thumbnail_generation = 0
        self.editor_grid_mode = False
        self.editor_page_selection = set()
        self.editor_page_anchor = None
        self.editor_page_drag = None
        self.editor_thumbnail_drop_target: int | None = None
        self.editor_thumbnail_drop_ghost = None
        self.editor_pages_busy = False
        self.editor_best_ocr_pages = set()
        self.editor_index_busy = False
        self.editor_edit_list_items: list[tuple[str, int]] = []
        self.editor_text_index: list[PageText] = []
        self.editor_text_index_mode = ""
        self.editor_text_index_language = ""
        self.editor_search_hits: list[SearchHit] = []
        self.editor_search_hit_index = -1
        self.editor_search_generation = 0
        self.editor_search_query = tk.StringVar()
        self.editor_search_summary = tk.StringVar(value="")
        self.editor_reading_mode = False
        self.editor_reading_restore_window = False
        saved_ocr_language = self.preferences.get("ocr_language", "rus")
        self.editor_ocr_language = tk.StringVar(
            value=saved_ocr_language if saved_ocr_language in {"rus+eng", "rus", "eng"} else "rus"
        )
        self.pdf_docx_pre_ocr = tk.BooleanVar(value=bool(self.preferences.get("pdf_docx_pre_ocr", False)))
        saved_document_ocr_language = self.preferences.get("document_ocr_language", "rus")
        self.pdf_docx_ocr_language = tk.StringVar(
            value=saved_document_ocr_language
            if saved_document_ocr_language in {"rus+eng", "rus", "eng"}
            else "rus"
        )
        self.pdf_docx_preserve_graphics = tk.BooleanVar(
            value=bool(self.preferences.get("document_ocr_preserve_graphics", False))
        )
        self.pdf_docx_detect_tables = tk.BooleanVar(
            value=bool(self.preferences.get("document_ocr_detect_tables", True))
        )
        self.documents_progress_value = tk.DoubleVar(value=0.0)
        self.documents_progress_text = tk.StringVar(value="Готово к преобразованию")
        self._global_progress_active = False
        self._drop_zone_icon_cache: dict[int, ImageTk.PhotoImage] = {}
        self.signature_remove_background = tk.BooleanVar(value=True)
        self.signature_enhance = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="Готово")

        self._set_window_icon()
        self._configure_style()
        self._build_ui()
        self._build_app_menu()
        self._bind_preference_traces()
        self._install_tooltips()
        self._enable_drop(self)
        self.bind("<Control-z>", self._editor_undo_shortcut)
        self.bind("<Control-y>", self._editor_redo_shortcut)
        self.bind("<Control-c>", self._editor_copy_shortcut)
        self.bind("<Control-v>", self._editor_paste_shortcut)
        self.bind("<Control-KeyPress>", self._editor_clipboard_keypress, add="+")
        self.bind("<Delete>", self._editor_delete_shortcut)
        self.bind("<Control-s>", self._editor_save_shortcut)
        self.bind("<Control-p>", self._print_current_section)
        self.bind_all("<Escape>", self._editor_escape, add="+")
        self.bind_all("<Left>", self._editor_reading_page_key, add="+")
        self.bind_all("<Right>", self._editor_reading_page_key, add="+")
        self.bind_all("<Button-1>", self._dismiss_context_menus, add="+")
        self.bind("<Map>", self._restore_custom_chrome_after_map, add="+")
        self.bind("<Configure>", self._schedule_window_rounding, add="+")
        self.bind("<Alt-F4>", lambda _event: self._on_app_close())
        self.protocol("WM_DELETE_WINDOW", self._on_app_close)
        self._schedule_autosave()

    def _set_window_icon(self) -> None:
        if not APP_ICON_PATH.exists():
            return
        try:
            self.app_icon_image = tk.PhotoImage(file=str(APP_ICON_PATH))
            self.iconphoto(True, self.app_icon_image)
        except tk.TclError:
            pass

    def _configure_style(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("TFrame", background=BG)
        style.configure("Panel.TFrame", background=PANEL)
        style.configure("TNotebook", background=PANEL, borderwidth=0, tabmargins=(18, 0, 18, 0))
        style.configure(
            "TNotebook.Tab",
            padding=(20, 16),
            font=(FONT_FAMILY, 11),
            background=PANEL,
            foreground="#425878",
            borderwidth=0,
            lightcolor=PANEL,
            darkcolor=PANEL,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", PANEL), ("active", "#F4F8FD")],
            foreground=[("selected", BLUE), ("active", TEXT)],
        )
        style.configure("TLabel", background=BG, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.configure("Muted.TLabel", background=BG, foreground=MUTED)
        style.configure("Panel.TLabel", background=PANEL, foreground=TEXT)
        style.configure("Header.TLabel", background=HEADER_BLUE, foreground=TEXT, font=(FONT_FAMILY, 18, "bold"))
        style.configure("SubHeader.TLabel", background=HEADER_BLUE, foreground=MUTED, font=(FONT_FAMILY, 10))
        style.configure(
            "Accent.TButton",
            font=(FONT_FAMILY, 10, "bold"),
            padding=(12, 6),
            background="#E8F1FF",
            foreground="#173F7A",
            bordercolor="#BCD1F1",
            lightcolor="#E8F1FF",
            darkcolor="#E8F1FF",
            borderwidth=1,
        )
        style.map(
            "Accent.TButton",
            background=[("disabled", "#F0F3F8"), ("pressed", "#C8DDFA"), ("active", "#D9E9FF")],
            foreground=[("disabled", MUTED), ("active", "#173F7A")],
        )
        style.configure("Primary.TButton", font=(FONT_FAMILY, 10, "bold"), padding=(12, 6),
                        background=BLUE, foreground="white", borderwidth=1, bordercolor=BLUE)
        style.map("Primary.TButton", background=[("pressed", "#04499B"), ("active", HOVER_BLUE)],
                  foreground=[("disabled", "#D6E2EA"), ("active", "white")])
        style.configure(
            "PrimaryLarge.TButton",
            font=(FONT_FAMILY, 11, "bold"),
            padding=(19, 12),
            background=BLUE,
            foreground="white",
            borderwidth=0,
        )
        style.map(
            "PrimaryLarge.TButton",
            background=[("active", HOVER_BLUE), ("pressed", "#04499B")],
            foreground=[("active", "white")],
        )
        style.configure(
            "SecondaryLarge.TButton",
            font=(FONT_FAMILY, 11),
            padding=(18, 11),
            background="#F5F7FA",
            foreground="#536985",
            bordercolor=BORDER,
        )
        style.configure(
            "TButton",
            font=(FONT_FAMILY, 10),
            padding=(9, 5),
            background="#F8FAFD",
            foreground="#536985",
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            borderwidth=1,
        )
        style.map("TButton", background=[("active", "#EEF4FB"), ("pressed", "#E3ECF7")])
        style.configure("Tool.TButton", padding=(6, 5), background=PANEL, foreground=TEXT)
        style.configure("WordNudge.TButton", padding=(3, 6))
        style.configure("WordTool.TButton", padding=(3, 6))
        style.configure("Sidebar.Tool.TButton", padding=(1, 3))
        style.configure("WordTool.TMenubutton", padding=(3, 6))
        style.configure("WordApply.Accent.TButton", padding=(4, 10))
        style.map("Tool.TButton", background=[("active", "#EAF3FF"), ("pressed", "#DBEAFE")])
        style.configure("Active.Tool.TButton", padding=(6, 5), background="#CDE3FF", foreground=TEXT, bordercolor=BLUE)
        style.map("Active.Tool.TButton", background=[("active", "#BCD9FF")], foreground=[("active", TEXT)])
        style.configure("Header.TButton", padding=(10, 6), background=HEADER_BLUE, foreground="white")
        style.map("Header.TButton", background=[("active", BLUE)], foreground=[("active", "white")])
        style.configure("TLabelframe", background=PANEL, bordercolor=BORDER, relief="solid")
        style.configure("TLabelframe.Label", background=PANEL, foreground=DEEP_BLUE, font=(FONT_FAMILY, 10, "bold"))
        style.map("TButton", foreground=[("disabled", MUTED)], bordercolor=[("focus", BLUE)])
        style.configure("TEntry", padding=(6, 4), fieldbackground=PANEL, bordercolor=BORDER)
        style.configure("TCombobox", padding=(6, 4), fieldbackground=PANEL, bordercolor=BORDER)
        style.configure("TSpinbox", padding=(6, 4), fieldbackground=PANEL, bordercolor=BORDER)
        style.configure("TCheckbutton", background=PANEL, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.map("TCheckbutton", background=[("active", PANEL)])
        style.configure("TRadiobutton", background=PANEL, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.configure("Card.TCheckbutton", background=DROP_ZONE_BG, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.map("Card.TCheckbutton", background=[("active", DROP_ZONE_BG)])
        style.configure("Card.TRadiobutton", background=DROP_ZONE_BG, foreground=TEXT, font=(FONT_FAMILY, 10))
        style.map("Card.TRadiobutton", background=[("active", DROP_ZONE_BG)])
        style.configure("Vertical.TScrollbar", background="#E6EDF6", troughcolor="#F7F9FC", borderwidth=0)
        style.configure("Horizontal.TScrollbar", background="#E6EDF6", troughcolor="#F7F9FC", borderwidth=0)

    def _build_custom_header(self) -> tk.Canvas:
        header = tk.Canvas(self, height=44, bg=HEADER_BLUE, highlightthickness=0, borderwidth=0)
        header.grid(row=0, column=0, sticky="ew")
        header.grid_propagate(False)
        if APP_ICON_PATH.exists():
            with Image.open(APP_ICON_PATH) as icon:
                small_icon = icon.convert("RGBA")
                small_icon.thumbnail((24, 24), LANCZOS)
                self.window_title_icon_image = ImageTk.PhotoImage(small_icon)
                main_icon = icon.convert("RGBA")
                main_icon.thumbnail((64, 64), LANCZOS)
                self.header_icon_image = ImageTk.PhotoImage(main_icon)
        header.bind("<Configure>", self._schedule_header_render)
        header.bind("<ButtonPress-1>", self._start_window_drag)
        header.bind("<B1-Motion>", self._drag_window)
        header.bind("<ButtonRelease-1>", self._stop_window_drag)
        header.bind("<Double-Button-1>", self._header_double_click)
        return header

    def _schedule_header_render(self, event=None) -> None:
        if self._header_render_after:
            self.after_cancel(self._header_render_after)
        self._header_render_after = self.after(20, self._render_custom_header)

    def _render_custom_header(self) -> None:
        self._header_render_after = None
        if not hasattr(self, "header_canvas"):
            return
        canvas = self.header_canvas
        width = max(720, canvas.winfo_width())
        canvas.delete("all")

        if hasattr(self, "window_title_icon_image"):
            canvas.create_image(18, 22, image=self.window_title_icon_image)
        canvas.create_text(42, 22, text=APP_TITLE, anchor="w", fill=TEXT, font=(FONT_FAMILY, 11, "bold"))

        controls = (
            ("window_minimize", width - 144, "−"),
            ("window_maximize", width - 96, "□"),
            ("window_close", width - 48, "×"),
        )
        for tag, x1, symbol in controls:
            background_tag = f"{tag}_background"
            canvas.create_rectangle(
                x1,
                0,
                x1 + 48,
                44,
                fill="",
                outline="",
                tags=(tag, background_tag),
            )
            canvas.create_text(
                x1 + 24,
                21,
                text=symbol,
                fill=TEXT,
                font=(FONT_FAMILY, 17 if tag != "window_maximize" else 13),
                tags=(tag, f"{tag}_symbol"),
            )
            canvas.tag_bind(tag, "<Enter>", lambda _event, item=tag: self._hover_window_control(item, True))
            canvas.tag_bind(tag, "<Leave>", lambda _event, item=tag: self._hover_window_control(item, False))
        canvas.tag_bind("window_minimize", "<Button-1>", lambda _event: self._minimize_window())
        canvas.tag_bind("window_maximize", "<Button-1>", lambda _event: self._toggle_maximize_window())
        canvas.tag_bind("window_close", "<Button-1>", lambda _event: self._on_app_close())

    def _hover_window_control(self, tag: str, hovered: bool) -> None:
        if not hasattr(self, "header_canvas"):
            return
        if tag == "window_close" and hovered:
            color = "#D94B4B"
        else:
            color = "#CDDEF7" if hovered else ""
        self.header_canvas.itemconfigure(f"{tag}_background", fill=color)
        self.header_canvas.itemconfigure(f"{tag}_symbol", fill="white" if tag == "window_close" and hovered else TEXT)

    def _start_window_drag(self, event) -> None:
        if event.y > 44 or event.x >= self.header_canvas.winfo_width() - 144:
            return
        if self._window_maximized:
            return
        self._window_drag_origin = (event.x_root - self.winfo_x(), event.y_root - self.winfo_y())

    def _drag_window(self, event) -> None:
        if not self._window_drag_origin:
            return
        offset_x, offset_y = self._window_drag_origin
        self.geometry(f"+{event.x_root - offset_x}+{event.y_root - offset_y}")

    def _stop_window_drag(self, _event=None) -> None:
        self._window_drag_origin = None

    def _header_double_click(self, event) -> None:
        if event.y <= 44 and event.x < self.header_canvas.winfo_width() - 144:
            self._toggle_maximize_window()

    def _minimize_window(self) -> None:
        if self._uses_override_chrome:
            self._restore_override_after_map = True
            self.overrideredirect(False)
            self.update_idletasks()
            self._apply_windows_taskbar_style()
        self.iconify()

    def _restore_custom_chrome_after_map(self, _event=None) -> None:
        if not self._restore_override_after_map or self.state() != "normal":
            return
        self._restore_override_after_map = False
        self.after_idle(self._restore_windows_custom_chrome)

    def _restore_windows_custom_chrome(self) -> None:
        self.overrideredirect(True)
        self.update_idletasks()
        self._apply_windows_taskbar_style()

    def _activate_custom_chrome(self) -> None:
        if self._uses_override_chrome:
            self._apply_windows_taskbar_style()
            self._apply_window_rounding()
            return
        self._apply_x11_borderless_style()

    def _schedule_window_rounding(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        if self._window_rounding_after:
            self.after_cancel(self._window_rounding_after)
        self._window_rounding_after = self.after(80, self._apply_window_rounding)

    def _apply_window_rounding(self) -> None:
        self._window_rounding_after = None
        self.update_idletasks()
        width = max(1, self.winfo_width())
        height = max(1, self.winfo_height())
        # Windows 11 geometry uses an 8 px radius for top-level windows.
        radius = 0 if self._window_maximized else 8
        if os.name == "nt":
            self._apply_windows_window_rounding(width, height, radius)
        elif self._x11_client_window_id:
            self._apply_x11_window_rounding(self._x11_client_window_id, width, height, radius)

    def _apply_windows_window_rounding(self, width: int, height: int, radius: int) -> None:
        try:
            import ctypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32
            user32.GetParent.argtypes = [ctypes.c_void_p]
            user32.GetParent.restype = ctypes.c_void_p
            user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
            user32.SetWindowRgn.restype = ctypes.c_int
            gdi32.CreateRoundRectRgn.argtypes = [
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
            ]
            gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
            gdi32.CreateRectRgn.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int]
            gdi32.CreateRectRgn.restype = ctypes.c_void_p
            hwnd = user32.GetParent(self.winfo_id()) or self.winfo_id()
            try:
                dwmapi = ctypes.windll.dwmapi
                dwmapi.DwmSetWindowAttribute.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint,
                    ctypes.c_void_p,
                    ctypes.c_uint,
                ]
                dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
                preference = ctypes.c_int(1 if radius <= 0 else 2)
                result = dwmapi.DwmSetWindowAttribute(
                    hwnd,
                    33,
                    ctypes.byref(preference),
                    ctypes.sizeof(preference),
                )
                if result == 0:
                    user32.SetWindowRgn(hwnd, None, True)
                    return
            except (AttributeError, OSError):
                pass
            if radius:
                region = gdi32.CreateRoundRectRgn(0, 0, width + 1, height + 1, radius * 2, radius * 2)
            else:
                region = gdi32.CreateRectRgn(0, 0, width + 1, height + 1)
            if region:
                user32.SetWindowRgn(hwnd, region, True)
        except (AttributeError, OSError):
            pass

    def _apply_x11_window_rounding(self, window_id: int, width: int, height: int, radius: int) -> None:
        try:
            import ctypes

            x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            xext = ctypes.cdll.LoadLibrary("libXext.so.6")
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XCreatePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint]
            x11.XCreatePixmap.restype = ctypes.c_ulong
            x11.XCreateGC.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_void_p]
            x11.XCreateGC.restype = ctypes.c_void_p
            x11.XSetForeground.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_ulong]
            x11.XFillRectangle.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
            ]
            x11.XFillArc.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_uint,
                ctypes.c_uint,
                ctypes.c_int,
                ctypes.c_int,
            ]
            x11.XFreeGC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            x11.XFreePixmap.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
            x11.XFlush.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            xext.XShapeCombineMask.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.c_ulong,
                ctypes.c_int,
            ]
            display = x11.XOpenDisplay(None)
            if not display:
                return
            pixmap = x11.XCreatePixmap(display, ctypes.c_ulong(window_id), width, height, 1)
            gc = x11.XCreateGC(display, pixmap, 0, None)
            if not pixmap or not gc:
                if pixmap:
                    x11.XFreePixmap(display, pixmap)
                x11.XCloseDisplay(display)
                return
            x11.XSetForeground(display, gc, 0)
            x11.XFillRectangle(display, pixmap, gc, 0, 0, width, height)
            x11.XSetForeground(display, gc, 1)
            if radius <= 0:
                x11.XFillRectangle(display, pixmap, gc, 0, 0, width, height)
            else:
                diameter = radius * 2
                x11.XFillRectangle(display, pixmap, gc, radius, 0, max(1, width - diameter), height)
                x11.XFillRectangle(display, pixmap, gc, 0, radius, width, max(1, height - diameter))
                for x, y in ((0, 0), (width - diameter, 0), (0, height - diameter), (width - diameter, height - diameter)):
                    x11.XFillArc(display, pixmap, gc, x, y, diameter, diameter, 0, 360 * 64)
            xext.XShapeCombineMask(display, ctypes.c_ulong(window_id), 0, 0, 0, pixmap, 0)
            x11.XFreeGC(display, gc)
            x11.XFreePixmap(display, pixmap)
            x11.XFlush(display)
            x11.XCloseDisplay(display)
        except (AttributeError, OSError):
            pass

    def _apply_x11_borderless_style(self) -> None:
        try:
            import ctypes

            x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
            x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
            x11.XOpenDisplay.restype = ctypes.c_void_p
            x11.XQueryTree.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.c_ulong),
                ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
                ctypes.POINTER(ctypes.c_uint),
            ]
            x11.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            x11.XInternAtom.restype = ctypes.c_ulong
            x11.XChangeProperty.argtypes = [
                ctypes.c_void_p,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_ulong,
                ctypes.c_int,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_ubyte),
                ctypes.c_int,
            ]
            x11.XFlush.argtypes = [ctypes.c_void_p]
            x11.XFlush.restype = ctypes.c_int
            x11.XFree.argtypes = [ctypes.c_void_p]
            x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
            display = x11.XOpenDisplay(None)
            if not display:
                return
            atom = x11.XInternAtom(display, b"_MOTIF_WM_HINTS", False)
            hints = (ctypes.c_ulong * 5)(2, 0, 0, 0, 0)
            window_id = int(self.winfo_id())
            visited = set()
            client_window_id = None
            for _ in range(8):
                if not window_id or window_id in visited:
                    break
                visited.add(window_id)
                root = ctypes.c_ulong()
                parent = ctypes.c_ulong()
                children = ctypes.POINTER(ctypes.c_ulong)()
                child_count = ctypes.c_uint()
                status = x11.XQueryTree(
                    display,
                    ctypes.c_ulong(window_id),
                    ctypes.byref(root),
                    ctypes.byref(parent),
                    ctypes.byref(children),
                    ctypes.byref(child_count),
                )
                if children:
                    x11.XFree(children)
                if not status or not parent.value or parent.value == root.value:
                    break
                x11.XChangeProperty(
                    display,
                    ctypes.c_ulong(window_id),
                    atom,
                    atom,
                    32,
                    0,
                    ctypes.cast(hints, ctypes.POINTER(ctypes.c_ubyte)),
                    5,
                )
                client_window_id = window_id
                window_id = int(parent.value)
            x11.XFlush(display)
            x11.XCloseDisplay(display)
            if client_window_id:
                self._x11_client_window_id = client_window_id
                self.after(80, self._apply_window_rounding)
        except (AttributeError, OSError):
            pass

    def _apply_windows_taskbar_style(self) -> None:
        if os.name != "nt":
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            hwnd = user32.GetParent(self.winfo_id())
            extended_style = user32.GetWindowLongW(hwnd, -20)
            extended_style = (extended_style & ~0x00000080) | 0x00040000
            user32.SetWindowLongW(hwnd, -20, extended_style)
            user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0027)
        except (AttributeError, OSError):
            pass

    def _toggle_maximize_window(self) -> None:
        if self._window_maximized:
            self.geometry(self._window_restore_geometry)
            self._window_maximized = False
        else:
            self._window_restore_geometry = self.geometry()
            self.update_idletasks()
            center_x = self.winfo_rootx() + max(1, self.winfo_width()) // 2
            center_y = self.winfo_rooty() + max(1, self.winfo_height()) // 2
            x, y, width, height = _monitor_geometry_for_point(self, center_x, center_y)
            if sys.platform.startswith("linux"):
                height = max(700, height - 42)
            self.geometry(f"{width}x{height}{x:+d}{y:+d}")
            self._window_maximized = True
        self.after_idle(self._render_custom_header)

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        self.header_canvas = self._build_custom_header()

        self.notebook = TabWorkspace(self)
        self.notebook.grid(row=1, column=0, sticky="nsew", padx=6, pady=(4, 4))
        self.editor_tab = self._editor_tab(self.notebook.content)
        self.notebook.add(self.editor_tab, text="PDF Редактор")
        self.notebook.add(self._images_tab(self.notebook.content), text="Изображения → PDF")
        self.notebook.add(self._pdf_images_tab(self.notebook.content), text="PDF → изображения")
        self.documents_tab = self._documents_tab(self.notebook.content)
        self.notebook.add(self.documents_tab, text="Документы")
        self.notebook.add(self._compress_tab(self.notebook.content), text="Сжатие")
        self.notebook.add(self._about_tab(self.notebook.content), text="О программе")
        self._decorate_workspace_actions(self.notebook.content)

        self.status_bar = tk.Frame(self, bg="#F8FAFD", highlightthickness=1, highlightbackground=BORDER)
        self.status_bar.grid(row=2, column=0, sticky="ew")
        self.status_bar.columnconfigure(1, weight=1)
        tk.Label(self.status_bar, text="●", bg="#F8FAFD", fg=BLUE, padx=14, pady=9).grid(row=0, column=0)
        tk.Label(
            self.status_bar,
            textvariable=self.status,
            anchor="w",
            bg="#F8FAFD",
            fg=MUTED,
            font=(FONT_FAMILY, 9),
            pady=9,
        ).grid(row=0, column=1, sticky="ew")
        self.global_progress_bar = ttk.Progressbar(self.status_bar, mode="indeterminate", length=180)
        self.global_progress_bar.grid(row=0, column=2, padx=(12, 6), pady=9)
        self.global_progress_bar.grid_remove()
        tk.Label(
            self.status_bar,
            text=f"pdf bars {__version__}",
            anchor="e",
            bg="#F8FAFD",
            fg=MUTED,
            font=(FONT_FAMILY, 9),
            padx=22,
            pady=9,
        ).grid(row=0, column=3)
        self.window_sizegrip = ttk.Sizegrip(self.status_bar)
        self.window_sizegrip.grid(row=0, column=4, sticky="se", padx=(0, 3), pady=(0, 3))

    def _decorate_workspace_actions(self, parent) -> None:
        if not hasattr(self, "workspace_action_icons"):
            self.workspace_action_icons = {}
        for widget in parent.winfo_children():
            if isinstance(widget, ttk.Button) and not widget.cget("image"):
                text = str(widget.cget("text"))
                primary = text.startswith("Преобразовать") or text in {"Создать PDF", "Сжать PDF"}
                kind = ("document" if primary else "folder" if text.startswith(("Добавить", "Выбрать", "Папка"))
                        else "clear" if text.startswith(("Удалить", "Очистить"))
                        else "info" if text.startswith(("Открыть руководство", "Собрать отладочную")) else None)
                if kind:
                    key = (kind, primary)
                    if key not in self.workspace_action_icons:
                        self.workspace_action_icons[key] = create_action_icon(kind, "#FFFFFF" if primary else DEEP_BLUE, 20)
                    widget.configure(image=self.workspace_action_icons[key], compound="left")
                    if primary:
                        widget.configure(style="Primary.TButton")
            self._decorate_workspace_actions(widget)

    def _start_global_progress(self, text: str, *, determinate: bool = False, maximum: int = 100) -> None:
        self._global_progress_active = True
        self.status.set(text)
        mode = "determinate" if determinate else "indeterminate"
        self.global_progress_bar.stop()
        self.global_progress_bar.configure(mode=mode, maximum=max(1, int(maximum)), value=0)
        self.global_progress_bar.grid()
        if not determinate:
            self.global_progress_bar.start(12)
        if hasattr(self, "editor_reader_progress") and self.editor_reading_mode:
            self.editor_reader_search_summary_label.grid_remove()
            self.editor_reader_progress.stop()
            self.editor_reader_progress.configure(mode=mode, maximum=max(1, int(maximum)), value=0)
            self.editor_reader_progress.grid()
            if not determinate:
                self.editor_reader_progress.start(12)
        self.configure(cursor="watch")
        self.update_idletasks()

    def _update_global_progress(self, current: int, total: int, text: str | None = None) -> None:
        if not self._global_progress_active:
            self._start_global_progress(text or "Выполняется…", determinate=True, maximum=total)
        maximum = max(1, int(total))
        value = max(0, min(int(current), maximum))
        self.global_progress_bar.stop()
        self.global_progress_bar.configure(mode="determinate", maximum=maximum, value=value)
        if hasattr(self, "editor_reader_progress") and self.editor_reading_mode:
            self.editor_reader_progress.stop()
            self.editor_reader_progress.configure(mode="determinate", maximum=maximum, value=value)
        if text:
            self.status.set(text)
        self.update_idletasks()

    def _finish_global_progress(self) -> None:
        self._global_progress_active = False
        self.global_progress_bar.stop()
        self.global_progress_bar.grid_remove()
        if hasattr(self, "editor_reader_progress"):
            self.editor_reader_progress.stop()
            self.editor_reader_progress.grid_remove()
            if self.editor_reading_mode:
                self.editor_reader_search_summary_label.grid()
        self.configure(cursor="")

    def _build_app_menu(self) -> None:
        self.bind("<Control-o>", lambda _event: self._choose_editor_file())

    def _refresh_recent_menu(self) -> None:
        if not hasattr(self, "recent_menu"):
            return
        self.recent_menu.delete(0, tk.END)
        recent = existing_recent_files(self.preferences)
        if not recent:
            self.recent_menu.add_command(label="Список пока пуст", state="disabled")
            return
        for path in recent:
            label = path.name if len(path.name) <= 52 else f"{path.name[:49]}…"
            self.recent_menu.add_command(label=label, command=lambda value=path: self._open_recent_file(value))
        self.recent_menu.add_separator()
        self.recent_menu.add_command(label="Очистить список", command=self._clear_recent_files)

    def _clear_recent_files(self) -> None:
        self.preferences["recent_files"] = []
        try:
            save_preferences(self.preferences)
        except OSError:
            pass
        self._refresh_recent_menu()

    def _open_recent_file(self, path: Path) -> None:
        if not path.is_file():
            remember_recent_files(self.preferences, ())
            self._refresh_recent_menu()
            messagebox.showinfo("Недавние документы", "Файл больше не найден")
            return
        self._remember_dialog_selection(path)
        if path.suffix.lower() in {".docx", ".doc"}:
            self.notebook.select(self.documents_tab)
            self._append_docx([str(path)])
            return
        if path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES:
            self.notebook.select(self.editor_tab)
            self._load_editor_file(path)


    def _pdf_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(24, 14, 24, 20), style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        toolbar_shell, toolbar = create_rounded_card(
            frame, fill=PANEL, outer_bg=PANEL, radius=16, border=BORDER, padding=10
        )
        toolbar_shell.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(4, weight=1)
        self.pdf_toolbar_icons = {
            "add": create_action_icon("add", "#FFFFFF"),
            "save": create_action_icon("save", "#FFFFFF"),
            "print": create_action_icon("print", "#FFFFFF"),
            "clear": create_action_icon("clear", MUTED),
        }
        ttk.Button(
            toolbar,
            text="Добавить файл",
            image=self.pdf_toolbar_icons["add"],
            compound="left",
            command=self._choose_pdf_files,
            style="PrimaryLarge.TButton",
        ).grid(
            row=0,
            column=0,
            padx=(0, 12),
        )
        ttk.Button(
            toolbar,
            text="Сохранить PDF",
            image=self.pdf_toolbar_icons["save"],
            compound="left",
            command=self._save_pdf_document,
            style="PrimaryLarge.TButton",
        ).grid(
            row=0,
            column=1,
            padx=(0, 12),
        )
        ttk.Button(
            toolbar,
            text="Печать",
            image=self.pdf_toolbar_icons["print"],
            compound="left",
            command=self._print_pdf_document,
            style="PrimaryLarge.TButton",
        ).grid(
            row=0,
            column=2,
            padx=(0, 12),
        )
        ttk.Button(
            toolbar,
            text="Очистить",
            image=self.pdf_toolbar_icons["clear"],
            compound="left",
            command=self._clear_pdf_workspace,
            style="SecondaryLarge.TButton",
        ).grid(row=0, column=3)
        tk.Label(
            toolbar,
            textvariable=self.pdf_workspace_summary,
            bg=PANEL,
            fg=MUTED,
            font=(FONT_FAMILY, 10),
        ).grid(row=0, column=4, sticky="e", padx=(18, 2))

        self.drop_label = tk.Label(
            frame,
            text="Перетащите PDF или изображения · Shift — диапазон · ПКМ по миниатюре — дополнительные действия",
            bg=PANEL,
            fg=MUTED,
            padx=14,
            pady=10,
            font=(FONT_FAMILY, 9),
            relief="flat",
        )
        self.drop_label.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        self._enable_drop(self.drop_label, self._handle_pdf_workspace_drop)

        self.page_canvas = tk.Canvas(frame, bg=PANEL, highlightthickness=0, borderwidth=0)
        self.page_scrollbar = ttk.Scrollbar(frame, orient="vertical", command=self.page_canvas.yview)
        self.page_canvas.configure(yscrollcommand=self.page_scrollbar.set)
        self.page_canvas.grid(row=2, column=0, sticky="nsew")
        self.page_scrollbar.grid(row=2, column=1, sticky="ns")
        self.page_scrollbar.grid_remove()

        self.page_grid = tk.Frame(self.page_canvas, bg=PANEL)
        self.page_canvas_window = self.page_canvas.create_window((0, 0), window=self.page_grid, anchor="nw")
        self.page_grid.bind("<Configure>", lambda _event: self.page_canvas.configure(scrollregion=self.page_canvas.bbox("all")))
        self.page_grid.bind("<Button-1>", self._clear_page_selection_from_background)
        self.page_grid.bind("<Button-3>", lambda event: self._show_workspace_context_menu(event))
        self.page_canvas.bind("<Configure>", self._on_canvas_resize)
        self.page_canvas.bind("<Button-1>", self._clear_page_selection_from_background)
        self.page_canvas.bind("<Button-3>", lambda event: self._show_workspace_context_menu(event))
        self._bind_canvas_wheel(self.page_canvas, self.page_canvas)
        self._bind_canvas_wheel(self.page_grid, self.page_canvas)
        self._enable_drop(self.page_canvas, self._handle_pdf_workspace_drop)
        if DROP_ZONE_ICON_PATH.exists():
            with Image.open(DROP_ZONE_ICON_PATH) as icon:
                drop_icon = icon.convert("RGBA")
                drop_icon.thumbnail((180, 180), LANCZOS)
                self.drop_zone_icon_image = ImageTk.PhotoImage(drop_icon)
        self._draw_pdf_empty_state()
        return frame


    def _editor_tab(self, parent) -> ttk.Frame:
        if DROP_ZONE_ICON_PATH.exists():
            with Image.open(DROP_ZONE_ICON_PATH) as icon:
                drop_icon = icon.convert("RGBA")
                drop_icon.thumbnail((180, 180), LANCZOS)
                self.drop_zone_icon_image = ImageTk.PhotoImage(drop_icon)
        frame = ttk.Frame(parent, padding=(8, 6, 8, 8), style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)

        toolbar_shell, toolbar = create_rounded_card(
            frame, fill=PANEL, outer_bg=PANEL, radius=8, border=BORDER, padding=6
        )
        self.editor_toolbar_shell = toolbar_shell
        toolbar_shell.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(7, weight=1)
        self.editor_command_icons = {kind: create_action_icon(kind, "#FFFFFF" if kind == "folder" else "#173F7A", 20)
                                     for kind in ("folder", "save", "grid", "print", "undo", "redo")}
        ttk.Button(toolbar, text="Открыть", image=self.editor_command_icons["folder"], compound="left",
                   command=self._choose_editor_file, style="Primary.TButton").grid(row=0, column=0, padx=(0, 3))
        ttk.Button(toolbar, text="Сохранить как…", image=self.editor_command_icons["save"], compound="left",
                   command=self._save_editor_pdf, style="Accent.TButton").grid(row=0, column=1, padx=3)
        self.editor_view_button = ttk.Button(toolbar, text="Все страницы", image=self.editor_command_icons["grid"],
                                            compound="left", command=self._toggle_editor_grid, style="Accent.TButton")
        self.editor_view_button.grid(row=0, column=2, padx=3)
        self._tip(self.editor_view_button, "Сетка миниатюр: объединение PDF, перестановка, сохранение и удаление страниц")
        self.editor_print_icon = self.editor_command_icons["print"]
        ttk.Button(
            toolbar,
            text="Печать",
            image=self.editor_print_icon,
            compound="left",
            command=self._print_editor_document,
            style="Accent.TButton",
        ).grid(row=0, column=3, padx=3)
        ttk.Separator(toolbar, orient="vertical").grid(row=0, column=4, sticky="ns", padx=5)
        undo_button = ttk.Button(toolbar, image=self.editor_command_icons["undo"], width=3, command=self._undo_editor_action)
        undo_button.grid(row=0, column=5, padx=1)
        redo_button = ttk.Button(toolbar, image=self.editor_command_icons["redo"], width=3, command=self._redo_editor_action)
        redo_button.grid(row=0, column=6, padx=1)
        self._tip(undo_button, "Отменить действие")
        self._tip(redo_button, "Повторить действие")
        reading_mode_button = ttk.Button(toolbar, text="Чтение", command=self._enter_editor_reading_mode)
        reading_mode_button.grid(
            row=0,
            column=8,
            sticky="e",
            padx=(4, 3),
        )
        self._tip(reading_mode_button, "Открыть документ без инструментов редактирования")
        document_commands = tk.Frame(toolbar, bg=PANEL)
        document_commands.grid(row=0, column=7, sticky="w", padx=8)
        self.editor_close_button = ttk.Button(document_commands, text="Закрыть документ", command=self._clear_editor_document)
        self.editor_close_button.pack(side="left", padx=3)
        self.editor_edits_visible = tk.BooleanVar(value=False)
        ttk.Checkbutton(document_commands, text="Правки", variable=self.editor_edits_visible,
                        command=self._sync_editor_edits_visibility).pack(side="left", padx=8)
        self.editor_document_commands = document_commands
        def fit_commands(event):
            fixed = [widget for widget in toolbar.grid_slaves(row=0) if widget is not document_commands]
            required = sum(widget.winfo_reqwidth() + 10 for widget in fixed) + document_commands.winfo_reqwidth() + 16
            wrapped = event.width < required
            if getattr(self, "_editor_commands_wrapped", None) == wrapped:
                return
            self._editor_commands_wrapped = wrapped
            document_commands.grid_configure(row=3 if wrapped else 0, column=0 if wrapped else 7,
                                            columnspan=18 if wrapped else 1,
                                            padx=0 if wrapped else 8, pady=(4, 0) if wrapped else 0)
        toolbar.bind("<Configure>", fit_commands)
        navigation_row = tk.Frame(toolbar, bg=PANEL)
        navigation_row.grid(row=1, column=0, columnspan=18, sticky="ew", pady=(7, 0))
        self.editor_navigation_row = navigation_row
        navigation = tk.Frame(navigation_row, bg=PANEL)
        navigation.pack(side="right")
        ttk.Button(navigation, text="‹", width=3, command=lambda: self._change_editor_page(-1)).grid(row=0, column=8, padx=1)
        tk.Label(navigation, textvariable=self.editor_page_label, bg=PANEL, fg=TEXT, width=9).grid(row=0, column=9, padx=1)
        ttk.Button(navigation, text="›", width=3, command=lambda: self._change_editor_page(1)).grid(row=0, column=10, padx=1)
        ttk.Separator(navigation, orient="vertical").grid(row=0, column=11, sticky="ns", padx=3)
        ttk.Button(navigation, text="−", width=3, command=lambda: self._change_editor_zoom(-10)).grid(row=0, column=12, padx=1)
        tk.Label(navigation, textvariable=self.editor_zoom_label, bg=PANEL, fg=TEXT, width=6).grid(row=0, column=13)
        ttk.Button(navigation, text="+", width=3, command=lambda: self._change_editor_zoom(10)).grid(row=0, column=14, padx=1)
        ttk.Button(navigation, text="По ширине", command=self._fit_editor_width).grid(row=0, column=15, padx=(2, 1))
        ttk.Button(navigation, text="Страница", command=self._fit_editor_page).grid(row=0, column=16, padx=1)
        ttk.Button(navigation, text="100%", width=5, command=lambda: self._set_editor_zoom(100.0)).grid(
            row=0, column=17, padx=(1, 0)
        )

        tools = tk.Frame(navigation_row, bg=PANEL)
        tools.pack(side="left")
        self.editor_tools_row = tools
        def fit_navigation(event):
            stacked = event.width < tools.winfo_reqwidth() + navigation.winfo_reqwidth() + 12
            if getattr(self, "_editor_navigation_stacked", None) == stacked:
                return
            self._editor_navigation_stacked = stacked
            tools.pack_forget()
            navigation.pack_forget()
            if stacked:
                tools.pack(anchor="w")
                navigation.pack(anchor="e", pady=(4, 0))
            else:
                navigation.pack(side="right")
                tools.pack(side="left")
        navigation_row.bind("<Configure>", fit_navigation)
        tk.Label(tools, text="Инструменты", bg=PANEL, fg=MUTED, font=("Arial", 9, "bold")).pack(side="left", padx=(1, 7))
        tool_specs = (
            ("select", "Выбор", "Выбрать, переместить или изменить размер объекта", self._start_object_selection),
            ("text", "Текст", "Добавить надпись на страницу", self._start_text_placement),
            ("replace_word", "Заменить слово", "Заменить распознанное слово с восстановлением фона", self._start_word_replacement),
            ("erase", "Закрыть область", "Закрыть прямоугольную область выбранным цветом", self._start_erase_placement),
            ("copy_region", "Выделить область", "Выделить фрагмент страницы для Ctrl+C", self._start_region_copy),
            ("crop", "Обрезка", "Оставить только выбранную часть страницы", self._start_crop_selection),
            ("align", "Выравнивание", "Автоматически определить и вручную поправить наклон", self._start_page_alignment),
            ("pencil", "Карандаш", "Рисовать произвольные линии", self._start_pencil),
            ("line", "Линия", "Нарисовать прямую линию", self._start_line),
            ("arrow", "Стрелка", "Нарисовать стрелку", self._start_arrow),
            ("free_erase", "Ластик", "Закрыть фрагмент свободным штрихом", self._start_free_eraser),
            ("signature", "Подпись", "Открыть библиотеку подписей и отметок", self._open_signature_library),
        )
        self.editor_tool_icons = {
            mode: create_editor_tool_icon(mode, DEEP_BLUE, 22) for mode, _label, _tip_text, _command in tool_specs
        }
        self.editor_tool_buttons.clear()
        for mode, label, tip_text, command in tool_specs:
            if mode in {"pencil", "signature"}:
                ttk.Separator(tools, orient="vertical").pack(side="left", fill="y", padx=5)
            button = ttk.Button(
                tools,
                image=self.editor_tool_icons[mode],
                command=command,
                style="Tool.TButton",
                width=3,
                takefocus=True,
            )
            button.pack(side="left", padx=2)
            self._tip(button, f"{label}. {tip_text}")
            self.editor_tool_buttons[mode] = button

        search_bar = tk.Frame(toolbar, bg=PANEL)
        search_bar.grid(row=2, column=0, columnspan=18, sticky="ew", pady=(7, 0))
        search_bar.columnconfigure(1, weight=1)
        tk.Label(search_bar, text="Поиск", bg=PANEL, fg=MUTED, font=("Arial", 9, "bold")).grid(row=0, column=0, padx=(1, 7))
        self.editor_search_entry = ttk.Entry(search_bar, textvariable=self.editor_search_query)
        self.editor_search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 4))
        self.editor_search_entry.bind("<Return>", lambda _event: self._start_editor_search())
        self.editor_search_button = ttk.Button(search_bar, text="Найти", command=self._start_editor_search)
        self.editor_search_button.grid(row=0, column=2, padx=1)
        self.editor_search_previous_button = ttk.Button(search_bar, text="‹", width=3, command=lambda: self._move_editor_search_hit(-1))
        self.editor_search_previous_button.grid(row=0, column=3, padx=(5, 1))
        self.editor_search_next_button = ttk.Button(search_bar, text="›", width=3, command=lambda: self._move_editor_search_hit(1))
        self.editor_search_next_button.grid(row=0, column=4, padx=1)
        self.editor_ocr_button = ttk.Button(search_bar, text="Распознать текст", command=self._start_editor_ocr)
        self.editor_ocr_button.grid(row=0, column=5, padx=(7, 1))
        self.editor_ocr_language_box = ttk.Combobox(
            search_bar,
            textvariable=self.editor_ocr_language,
            values=("rus+eng", "rus", "eng"),
            state="readonly",
            width=8,
        )
        self.editor_ocr_language_box.grid(row=0, column=6, padx=(4, 1))
        self._tip(
            self.editor_ocr_language_box,
            "Язык OCR: rus+eng для смешанных документов, rus только для русского, eng только для английского.",
        )
        self.editor_ocr_progress = ttk.Progressbar(search_bar, mode="determinate", length=90)
        self.editor_ocr_progress.grid(row=0, column=7, padx=(6, 1))
        self.editor_ocr_progress.grid_remove()
        tk.Label(search_bar, textvariable=self.editor_search_summary, bg=PANEL, fg=MUTED, width=15, anchor="w").grid(
            row=0,
            column=8,
            padx=(8, 1),
        )

        reader_shell, reader_toolbar = create_rounded_card(
            frame, fill=PANEL, outer_bg=PANEL, radius=16, border=BORDER, padding=10
        )
        self.editor_reader_toolbar_shell = reader_shell
        reader_shell.grid(row=0, column=0, sticky="ew")
        reader_shell.grid_remove()
        tk.Label(reader_toolbar, text="Поиск", bg=PANEL, fg=MUTED).grid(row=0, column=0, padx=(0, 6))
        reader_toolbar.columnconfigure(1, weight=1, minsize=80)
        self.editor_reader_search_entry = ttk.Entry(reader_toolbar, textvariable=self.editor_search_query, width=8)
        self.editor_reader_search_entry.grid(row=0, column=1, sticky="ew", padx=(0, 3))
        self.editor_reader_search_entry.bind("<Return>", lambda _event: self._start_editor_search())
        self.editor_reader_search_button = ttk.Button(reader_toolbar, text="Найти", width=6, command=self._start_editor_search)
        self.editor_reader_search_button.grid(row=0, column=2, padx=1)
        ttk.Button(reader_toolbar, text="‹", width=3, command=lambda: self._move_editor_search_hit(-1)).grid(row=0, column=3, padx=(4, 1))
        ttk.Button(reader_toolbar, text="›", width=3, command=lambda: self._move_editor_search_hit(1)).grid(row=0, column=4, padx=1)
        self.editor_reader_search_summary_label = tk.Label(
            reader_toolbar, textvariable=self.editor_search_summary, bg=PANEL, fg=MUTED, width=7, anchor="w"
        )
        self.editor_reader_search_summary_label.grid(
            row=0,
            column=5,
            padx=(5, 5),
        )
        self.editor_reader_progress = ttk.Progressbar(reader_toolbar, mode="indeterminate", length=72)
        self.editor_reader_progress.grid(row=0, column=5, padx=(5, 5))
        self.editor_reader_progress.grid_remove()
        ttk.Separator(reader_toolbar, orient="vertical").grid(row=0, column=6, sticky="ns", padx=5)
        ttk.Button(reader_toolbar, text="‹", width=3, command=lambda: self._change_editor_page(-1)).grid(
            row=0, column=7, padx=1
        )
        tk.Label(reader_toolbar, textvariable=self.editor_reader_page_label, bg=PANEL, fg=TEXT, width=7).grid(
            row=0, column=8, padx=1
        )
        ttk.Button(reader_toolbar, text="›", width=3, command=lambda: self._change_editor_page(1)).grid(
            row=0, column=9, padx=1
        )
        ttk.Separator(reader_toolbar, orient="vertical").grid(row=0, column=10, sticky="ns", padx=5)
        ttk.Button(reader_toolbar, text="−", width=3, command=lambda: self._change_editor_zoom(-10)).grid(row=0, column=11)
        tk.Label(reader_toolbar, textvariable=self.editor_zoom_label, bg=PANEL, fg=TEXT, width=6).grid(row=0, column=12)
        ttk.Button(reader_toolbar, text="+", width=3, command=lambda: self._change_editor_zoom(10)).grid(row=0, column=13)
        ttk.Button(reader_toolbar, text="По ширине", command=self._fit_editor_width).grid(row=0, column=14, padx=(3, 1))
        ttk.Button(reader_toolbar, text="Страница", command=self._fit_editor_page).grid(row=0, column=15, padx=1)
        ttk.Button(reader_toolbar, text="100%", width=5, command=lambda: self._set_editor_zoom(100.0)).grid(
            row=0, column=16, padx=(1, 3)
        )
        ttk.Separator(reader_toolbar, orient="vertical").grid(row=0, column=17, sticky="ns", padx=3)
        reader_toolbar.columnconfigure(18, minsize=112, uniform="reader_actions")
        reader_toolbar.columnconfigure(19, minsize=112, uniform="reader_actions")
        reader_print_button = ttk.Button(
            reader_toolbar,
            text="Печать",
            width=6,
            image=self.editor_print_icon,
            compound="left",
            command=self._print_editor_document,
            style="Accent.TButton",
        )
        reader_print_button.grid(row=0, column=18, sticky="nsew", padx=(2, 2))
        self._tip(reader_print_button, "Напечатать открытый документ")
        reader_exit_button = ttk.Button(
            reader_toolbar,
            text="Выйти",
            width=6,
            command=self._exit_editor_reading_mode,
            style="Accent.TButton",
        )
        reader_exit_button.grid(row=0, column=19, sticky="nsew", padx=(2, 0))
        self._tip(reader_exit_button, "Выйти из режима чтения")
        # Leave the search field flexible across Windows and RED OS font metrics.
        for button in reader_toolbar.winfo_children():
            if isinstance(button, ttk.Button) and button not in (reader_print_button, reader_exit_button):
                button.configure(padding=(8, 8))

        properties_shell, self.editor_properties_host = create_rounded_card(
            frame,
            fill=SURFACE_ALT,
            outer_bg=PANEL,
            radius=12,
            border=BORDER,
            padding=9,
        )
        self.editor_properties_shell = properties_shell
        properties_shell.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        self.editor_property_frames.clear()

        selection_properties = tk.Frame(self.editor_properties_host, bg=SURFACE_ALT)
        tk.Label(
            selection_properties,
            text="Выберите объект для перемещения и изменения размера. Правый клик открывает действия для указанного места.",
            bg=SURFACE_ALT,
            fg=MUTED,
            font=("Arial", 9),
        ).pack(side="left")
        self.editor_property_frames["select"] = selection_properties

        text_panel, text_properties = self._editor_scrollable_property_row(self.editor_properties_host)
        tk.Label(text_properties, text="Текст", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 5))
        self.editor_text_entry = ttk.Entry(text_properties, textvariable=self.editor_replace_text, width=28)
        self.editor_text_entry.pack(side="left", padx=(0, 6))
        self.editor_text_entry.bind("<Return>", lambda _event: self._start_text_placement())
        self.editor_font_box = ttk.Combobox(
            text_properties,
            textvariable=self.editor_font_family,
            values=("Times New Roman", "PT Sans", "DejaVu Sans", "Liberation Serif", "Liberation Sans", "Arial", "Noto Sans"),
            state="readonly",
            width=18,
        )
        self.editor_font_box.pack(side="left", padx=(0, 5))
        ttk.Spinbox(text_properties, from_=6, to=72, increment=0.5, textvariable=self.editor_font_size, width=5).pack(side="left", padx=(0, 5))
        bold_button = ttk.Checkbutton(text_properties, text="B", variable=self.editor_text_bold, width=2)
        bold_button.pack(side="left", padx=1)
        italic_button = ttk.Checkbutton(text_properties, text="I", variable=self.editor_text_italic, width=2)
        italic_button.pack(side="left", padx=1)
        underline_button = ttk.Checkbutton(text_properties, text="U", variable=self.editor_text_underline, width=2)
        underline_button.pack(side="left", padx=(1, 5))
        self._tip(bold_button, "Жирный текст")
        self._tip(italic_button, "Курсив")
        self._tip(underline_button, "Подчёркнутый текст")
        text_scan_effect = ttk.Checkbutton(text_properties, text="Скан", variable=self.editor_text_scan_effect)
        text_scan_effect.pack(side="left", padx=(1, 4))
        self._tip(text_scan_effect, "Эффект отсканированного текста")
        tk.Label(text_properties, text="Цвет", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 3))
        self.editor_color_button = self._build_editor_color_palette(
            text_properties,
            self.editor_text_color,
            self._set_editor_text_color,
            self._choose_editor_text_color,
        )
        ttk.Button(text_properties, text="Разместить", command=self._start_text_placement, style="Accent.TButton").pack(side="left", padx=1)
        ttk.Button(text_properties, text="Применить", command=self._apply_selected_text_properties).pack(side="left", padx=1)
        self.editor_property_frames["text"] = text_panel

        word_panel, word_properties = self._editor_scrollable_property_row(self.editor_properties_host)
        ttk.Button(
            word_properties, text="Применить", command=self._apply_selected_word_properties,
            style="WordApply.Accent.TButton",
        ).pack(side="right", padx=1)
        tk.Label(word_properties, text="Слово", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 4))
        self.editor_word_entry = ttk.Entry(word_properties, textvariable=self.editor_replace_text, width=11)
        self.editor_word_entry.pack(side="left", padx=(0, 5))
        self.editor_word_font_box = ttk.Combobox(
            word_properties,
            textvariable=self.editor_font_family,
            values=("Times New Roman", "PT Sans", "DejaVu Sans", "Liberation Serif", "Liberation Sans", "Arial", "Noto Sans"),
            state="readonly",
            width=14,
        )
        self.editor_word_font_box.pack(side="left", padx=(0, 4))
        ttk.Spinbox(word_properties, from_=6, to=72, increment=0.5, textvariable=self.editor_font_size, width=5).pack(side="left", padx=(0, 3))
        word_bold_button = ttk.Checkbutton(word_properties, text="B", variable=self.editor_text_bold, width=1)
        word_bold_button.pack(side="left", padx=1)
        word_italic_button = ttk.Checkbutton(word_properties, text="I", variable=self.editor_text_italic, width=1)
        word_italic_button.pack(side="left", padx=(1, 4))
        word_underline_button = ttk.Checkbutton(word_properties, text="U", variable=self.editor_text_underline, width=1)
        word_underline_button.pack(side="left", padx=1)
        self._tip(word_underline_button, "Подчёркнутый текст")
        word_scan_effect = ttk.Checkbutton(
            word_properties,
            text="Скан",
            variable=self.editor_word_scan_effect,
        )
        word_scan_effect.pack(side="left", padx=(1, 4))
        self._tip(word_bold_button, "Жирный текст")
        self._tip(word_italic_button, "Курсив")
        self._tip(word_scan_effect, "Эффект отсканированного текста")
        tk.Label(word_properties, text="X", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 2))
        ttk.Button(word_properties, text="←", width=2, style="WordNudge.TButton", command=lambda: self._nudge_selected_word(-0.1, 0.0)).pack(side="left", padx=1)
        ttk.Spinbox(
            word_properties,
            from_=-150,
            to=150,
            increment=0.1,
            textvariable=self.editor_word_offset_x,
            width=5,
        ).pack(side="left", padx=1)
        ttk.Button(word_properties, text="→", width=2, style="WordNudge.TButton", command=lambda: self._nudge_selected_word(0.1, 0.0)).pack(side="left", padx=(1, 4))
        tk.Label(word_properties, text="Y", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 2))
        ttk.Button(word_properties, text="↑", width=2, style="WordNudge.TButton", command=lambda: self._nudge_selected_word(0.0, -0.1)).pack(side="left", padx=1)
        ttk.Spinbox(
            word_properties,
            from_=-150,
            to=150,
            increment=0.1,
            textvariable=self.editor_word_offset_y,
            width=5,
        ).pack(side="left", padx=1)
        ttk.Button(word_properties, text="↓", width=2, style="WordNudge.TButton", command=lambda: self._nudge_selected_word(0.0, 0.1)).pack(side="left", padx=(1, 4))
        tk.Label(word_properties, text="Цвет", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 3))
        self.editor_word_color_button = self._build_editor_color_palette(
            word_properties,
            self.editor_text_color,
            self._set_editor_text_color,
            self._choose_editor_text_color,
            compact=True,
        )
        self.editor_word_extra_icons = {
            "pick": create_editor_tool_icon("eyedropper", DEEP_BLUE, 20),
            "save": create_action_icon("save", DEEP_BLUE, 20),
            "load": create_editor_tool_icon("preset_load", DEEP_BLUE, 20),
        }
        for key, command, tooltip in (
            ("pick", self._start_word_color_pick, "Пипетка: выбрать цвет текста на документе"),
            ("save", self._save_word_style, "Сохранить настройки слова"),
        ):
            button = ttk.Button(word_properties, image=self.editor_word_extra_icons[key],
                                command=command, style="WordTool.TButton", width=3)
            button.pack(side="left", padx=2)
            self._tip(button, tooltip)
        presets = ttk.Menubutton(word_properties, image=self.editor_word_extra_icons["load"],
                                 style="WordTool.TMenubutton", width=3)
        self.editor_word_styles_menu = tk.Menu(presets, tearoff=False,
                                               postcommand=self._refresh_word_styles_menu)
        presets.configure(menu=self.editor_word_styles_menu)
        presets.pack(side="left", padx=2)
        self._tip(presets, "Загрузить сохранённые настройки слова")
        self.editor_property_frames["word"] = word_panel

        draw_properties = tk.Frame(self.editor_properties_host, bg=SURFACE_ALT)
        tk.Label(draw_properties, text="Цвет", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 3))
        self.editor_draw_color_button = self._build_editor_color_palette(
            draw_properties,
            self.editor_draw_color,
            self._set_editor_draw_color,
            self._choose_editor_draw_color,
        )
        tk.Label(draw_properties, text="Толщина карандаша", bg=SURFACE_ALT, fg=TEXT).pack(side="left")
        ttk.Spinbox(draw_properties, from_=1, to=20, increment=0.5, textvariable=self.editor_pencil_width, width=5).pack(side="left", padx=(6, 16))
        tk.Label(draw_properties, text="Размер ластика", bg=SURFACE_ALT, fg=TEXT).pack(side="left")
        ttk.Spinbox(draw_properties, from_=4, to=80, increment=1, textvariable=self.editor_eraser_width, width=5).pack(side="left", padx=(6, 12))
        tk.Label(draw_properties, text="Рисуйте с зажатой левой кнопкой мыши", bg=SURFACE_ALT, fg=MUTED, font=("Arial", 9)).pack(side="left")
        self.editor_property_frames["draw"] = draw_properties

        area_properties = tk.Frame(self.editor_properties_host, bg=SURFACE_ALT)
        tk.Label(area_properties, text="Цвет области", bg=SURFACE_ALT, fg=TEXT).pack(side="left", padx=(0, 8))
        self.editor_area_color_button = self._build_editor_color_palette(
            area_properties, self.editor_draw_color, self._set_editor_draw_color, self._choose_editor_draw_color)
        self.editor_property_frames["area"] = area_properties

        crop_properties = tk.Frame(self.editor_properties_host, bg=SURFACE_ALT)
        tk.Label(
            crop_properties,
            text="После применения размер страницы станет равен выбранной области",
            bg=SURFACE_ALT,
            fg=MUTED,
        ).pack(side="left")
        self.editor_crop_apply_button = ttk.Button(
            crop_properties,
            text="Применить обрезку",
            command=self._apply_pending_crop,
            style="Accent.TButton",
        )
        self.editor_crop_apply_button.pack(side="left", padx=(12, 4))
        ttk.Button(crop_properties, text="Отмена", command=self._cancel_pending_crop).pack(side="left")
        self.editor_property_frames["crop"] = crop_properties

        signature_properties = tk.Frame(self.editor_properties_host, bg=SURFACE_ALT)
        tk.Label(signature_properties, text="Подписи и отметки хранятся локально на этом компьютере", bg=SURFACE_ALT, fg=MUTED).pack(side="left")
        ttk.Button(signature_properties, text="Открыть библиотеку", command=self._open_signature_library, style="Accent.TButton").pack(side="left", padx=(12, 0))
        self.editor_property_frames["signature"] = signature_properties
        self._show_editor_properties("select")

        content = tk.PanedWindow(
            frame,
            orient=tk.HORIZONTAL,
            bg=BORDER,
            borderwidth=0,
            sashwidth=5,
            sashrelief=tk.FLAT,
            showhandle=False,
        )
        content.grid(row=2, column=0, sticky="nsew", pady=(6, 0))
        self.editor_content_panes = content

        thumbnail_shell, thumbnail_panel = create_rounded_card(
            content,
            fill=PANEL,
            outer_bg=PANEL,
            radius=14,
            border=BORDER,
            padding=6,
            width=170,
        )
        self.editor_thumbnail_shell = thumbnail_shell
        thumbnail_shell.grid_propagate(False)
        thumbnail_panel.columnconfigure(0, weight=1)
        thumbnail_panel.rowconfigure(1, weight=1)
        thumbnail_heading = tk.Frame(thumbnail_panel, bg=PANEL)
        thumbnail_heading.columnconfigure(0, weight=1)
        tk.Label(thumbnail_heading, text="Страницы", bg=PANEL, fg=TEXT, font=("Arial", 10, "bold"), anchor="w").grid(row=0, column=0, sticky="w")
        self.editor_page_action_icons = {kind: create_action_icon(kind, DEEP_BLUE, 18) for kind in ("add", "clear")}
        for column, (kind, tip, command) in enumerate((
                ("add", "Добавить файлы в документ", self._choose_editor_file),
                ("clear", "Удалить выбранные страницы", self._delete_editor_selected_pages)), start=1):
            button = ttk.Button(thumbnail_heading, image=self.editor_page_action_icons[kind],
                                style="Sidebar.Tool.TButton", command=command)
            button.grid(row=0, column=column, padx=1)
            self._tip(button, tip)
        thumbnail_heading.grid(
            row=0,
            column=0,
            columnspan=2,
            sticky="ew",
            padx=3,
            pady=(8, 5),
        )
        self.editor_thumbnail_canvas = tk.Canvas(thumbnail_panel, bg=BG, highlightthickness=0, width=158)
        self.editor_thumbnail_canvas.grid(row=1, column=0, sticky="nsew")
        thumbnail_scroll = ttk.Scrollbar(thumbnail_panel, orient="vertical", command=self.editor_thumbnail_canvas.yview)
        thumbnail_scroll.grid(row=1, column=1, sticky="ns")
        def thumbnail_scrolled(first, last):
            thumbnail_scroll.set(first, last)
            self._queue_visible_editor_thumbnails()
        self.editor_thumbnail_canvas.configure(yscrollcommand=thumbnail_scrolled)
        self.editor_thumbnail_grid = tk.Frame(self.editor_thumbnail_canvas, bg=BG)
        self.editor_thumbnail_window = self.editor_thumbnail_canvas.create_window(
            (0, 0),
            window=self.editor_thumbnail_grid,
            anchor="nw",
        )
        self.editor_thumbnail_grid.bind(
            "<Configure>",
            lambda _event: self.editor_thumbnail_canvas.configure(scrollregion=self.editor_thumbnail_canvas.bbox("all")),
        )
        self.editor_thumbnail_canvas.bind(
            "<Configure>",
            lambda event: self.editor_thumbnail_canvas.itemconfigure(self.editor_thumbnail_window, width=event.width),
        )
        self._bind_canvas_wheel(self.editor_thumbnail_canvas, self.editor_thumbnail_canvas)
        self._bind_canvas_wheel(self.editor_thumbnail_grid, self.editor_thumbnail_canvas)

        canvas_shell, canvas_holder = create_rounded_card(
            content,
            fill="#E8EDF4",
            outer_bg=PANEL,
            radius=14,
            border=BORDER,
            padding=7,
        )
        self.editor_canvas_shell = canvas_shell
        canvas_holder.columnconfigure(0, weight=1)
        canvas_holder.rowconfigure(0, weight=1)
        self.editor_canvas = tk.Canvas(canvas_holder, bg="#E8EDF4", highlightthickness=0, width=760, height=470)
        self.editor_canvas.grid(row=0, column=0, sticky="nsew")
        editor_vscroll = ttk.Scrollbar(canvas_holder, orient="vertical", command=self.editor_canvas.yview)
        editor_vscroll.grid(row=0, column=1, sticky="ns")
        editor_hscroll = ttk.Scrollbar(canvas_holder, orient="horizontal", command=self.editor_canvas.xview)
        editor_hscroll.grid(row=1, column=0, sticky="ew")
        self.editor_canvas.configure(xscrollcommand=editor_hscroll.set, yscrollcommand=editor_vscroll.set)
        self._draw_editor_empty_state()
        self.editor_canvas.bind("<Configure>", self._on_editor_canvas_resize)
        self.editor_canvas.bind("<ButtonPress-1>", self._editor_canvas_press)
        self.editor_canvas.bind("<Double-Button-1>", self._editor_canvas_double_click)
        self.editor_canvas.bind("<B1-Motion>", self._editor_canvas_drag)
        self.editor_canvas.bind("<Motion>", self._editor_eraser_preview)
        self.editor_canvas.bind("<Leave>", lambda event: self.editor_canvas.delete("eraser-preview"))
        self.editor_canvas.bind("<ButtonRelease-1>", self._editor_canvas_release)
        self.editor_canvas.bind("<Control-MouseWheel>", self._editor_mousewheel_zoom)
        self.editor_canvas.bind("<Control-Button-4>", self._editor_mousewheel_zoom)
        self.editor_canvas.bind("<Control-Button-5>", self._editor_mousewheel_zoom)
        self._bind_canvas_wheel(self.editor_canvas, self.editor_canvas)
        self.editor_canvas.bind("<Button-3>", self._show_editor_context_menu)
        self.editor_canvas.bind("<Delete>", lambda _event: self._delete_selected_editor_object())
        self.editor_canvas.bind("<Control-d>", lambda _event: self._duplicate_selected_editor_object())
        self.editor_canvas.bind("<<Copy>>", self._editor_copy_shortcut)
        self.editor_canvas.bind("<<Paste>>", self._editor_paste_shortcut)
        self._enable_drop(self.editor_canvas, self._handle_editor_drop)

        edits_shell, edits_panel = create_rounded_card(
            content,
            fill=PANEL,
            outer_bg=PANEL,
            radius=14,
            border=BORDER,
            padding=6,
            width=185,
        )
        self.editor_edits_shell = edits_shell
        self.editor_edits_panel = edits_panel
        edits_shell.grid_propagate(False)
        edits_panel.columnconfigure(0, weight=1)
        edits_panel.rowconfigure(1, weight=1)
        tk.Label(edits_panel, text="Правки страницы", bg=PANEL, fg=DEEP_BLUE, font=("Arial", 10, "bold"), anchor="w").grid(
            row=0,
            column=0,
            sticky="ew",
            padx=9,
            pady=(8, 5),
        )
        self.editor_edits_list = tk.Listbox(
            edits_panel,
            exportselection=False,
            bg=PANEL,
            fg=TEXT,
            selectbackground=BLUE,
            selectforeground="white",
            relief="flat",
            borderwidth=0,
            activestyle="none",
            font=("Arial", 9),
        )
        self.editor_edits_list.grid(row=1, column=0, sticky="nsew", padx=(6, 0), pady=(0, 6))
        edits_scroll = ttk.Scrollbar(edits_panel, orient="vertical", command=self.editor_edits_list.yview)
        edits_scroll.grid(row=1, column=1, sticky="ns", pady=(0, 6))
        self.editor_edits_list.configure(yscrollcommand=edits_scroll.set)
        self.editor_edits_list.bind("<<ListboxSelect>>", self._select_editor_edit_from_list)

        content.add(thumbnail_shell, minsize=145, width=170, stretch="never")
        content.add(canvas_shell, minsize=470, stretch="always")
        self._sync_editor_edits_visibility()
        self._tip(self.editor_search_entry, "Введите слово или фразу. Поиск не учитывает регистр и различает страницы документа.")
        self._tip(self.editor_search_previous_button, "Перейти к предыдущему найденному совпадению.")
        self._tip(self.editor_search_next_button, "Перейти к следующему найденному совпадению.")
        self._tip(
            self.editor_ocr_button,
            "Распознаёт текст в документе, после чего его можно выделять мышью, копировать и искать по словам.",
        )
        self._tip(self.editor_thumbnail_canvas, "Перетаскивайте страницы для перестановки. Ctrl: несколько страниц; Shift: диапазон; ПКМ: команды страницы.")
        self.editor_thumbnail_canvas.bind("<Button-1>", self._clear_editor_page_selection)
        self.editor_thumbnail_grid.bind("<Button-1>", self._clear_editor_page_selection)
        self.editor_thumbnail_canvas.bind("<Button-3>", lambda event: self._show_editor_thumbnail_context_menu(event, None))
        self.editor_thumbnail_grid.bind("<Button-3>", lambda event: self._show_editor_thumbnail_context_menu(event, None))
        self.editor_thumbnail_canvas.bind("<Configure>", self._layout_editor_thumbnails, add="+")
        # The dragged tile is temporarily unmapped; its own release binding
        # would never see a drop over the preview tile.
        self.bind("<B1-Motion>", self._drag_editor_thumbnail, add="+")
        self.bind("<ButtonRelease-1>", self._release_editor_thumbnail, add="+")
        self._enable_drop(self.editor_thumbnail_canvas, self._handle_editor_drop)
        self._enable_drop(self.editor_thumbnail_grid, self._handle_editor_drop)
        self._tip(self.editor_edits_list, "Выберите правку, чтобы быстро перейти к ней и выделить её на странице.")
        self._refresh_signature_list()
        self._enable_drop(frame, self._handle_editor_drop)
        return frame

    def _images_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(10, 8, 10, 10), style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)
        self.image_output_dir = tk.StringVar()
        self.image_dpi = tk.IntVar(value=150)
        self.image_one_per_file = tk.BooleanVar(value=False)

        heading = tk.Frame(frame, bg=PANEL)
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        heading.columnconfigure(0, weight=1)
        tk.Label(heading, text="Создание PDF из изображений", bg=PANEL, fg=DEEP_BLUE, font=("Arial", 15, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(heading, textvariable=self.image_files_summary, bg=PANEL, fg=MUTED).grid(row=0, column=1, sticky="e")

        list_shell, list_holder = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=12
        )
        list_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        list_holder.columnconfigure(0, weight=1)
        list_holder.rowconfigure(0, weight=1)
        list_surface, list_body = create_rounded_card(
            list_holder, fill=PANEL, outer_bg=DROP_ZONE_BG, radius=14, border=BORDER, padding=5
        )
        list_surface.grid(row=0, column=0, sticky="nsew")
        list_body.columnconfigure(0, weight=1)
        list_body.rowconfigure(0, weight=1)
        self.image_list = tk.Listbox(
            list_body,
            selectmode=tk.EXTENDED,
            bg=PANEL,
            fg=TEXT,
            selectbackground=BLUE,
            selectforeground="white",
            relief="flat",
            borderwidth=0,
            activestyle="none",
        )
        self.image_list.grid(row=0, column=0, sticky="nsew")
        self._enable_drop(self.image_list, self._handle_images_drop)
        self.image_empty_state = self._create_file_drop_state(
            list_body,
            "Перетащите изображения",
            "JPG, PNG, WEBP, TIFF, BMP или GIF\nили нажмите здесь для выбора файлов",
            self._handle_images_drop,
            self._add_image_files,
        )
        list_actions = tk.Frame(list_holder, bg=DROP_ZONE_BG)
        list_actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(list_actions, text="Добавить изображения", command=self._add_image_files, style="Accent.TButton").pack(side="left")
        ttk.Button(list_actions, text="Удалить выбранные", command=self._remove_image_files).pack(side="left", padx=5)

        panel_shell, panel = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=16, width=330
        )
        panel_shell.grid(row=1, column=1, sticky="ns")
        panel_shell.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        tk.Label(panel, text="Параметры результата", bg=DROP_ZONE_BG, fg=DEEP_BLUE, font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(panel, text="Качество (DPI)", bg=DROP_ZONE_BG, fg=TEXT).grid(row=1, column=0, sticky="w", pady=(16, 3))
        ttk.Spinbox(panel, from_=36, to=1200, textvariable=self.image_dpi, width=12).grid(row=2, column=0, sticky="ew")
        ttk.Checkbutton(panel, text="Создавать отдельный PDF для каждого файла", variable=self.image_one_per_file, style="Card.TCheckbutton").grid(row=3, column=0, sticky="w", pady=(14, 6))
        tk.Label(panel, text="Папка для отдельных PDF", bg=DROP_ZONE_BG, fg=TEXT).grid(row=4, column=0, sticky="w", pady=(8, 3))
        ttk.Entry(panel, textvariable=self.image_output_dir).grid(row=5, column=0, sticky="ew")
        ttk.Button(panel, text="Выбрать папку…", command=lambda: self._choose_dir(self.image_output_dir)).grid(row=6, column=0, sticky="ew", pady=(5, 0))
        tk.Label(panel, text="Если папка не выбрана, результат сохранится рядом с первым изображением.", bg=DROP_ZONE_BG, fg=MUTED, wraplength=290, justify="left", font=("Arial", 9)).grid(row=7, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(panel, text="Создать PDF", command=self._run_images, style="Accent.TButton").grid(row=8, column=0, sticky="ew", pady=(22, 0))
        return frame

    def _pdf_images_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(10, 8, 10, 10), style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        tk.Label(frame, text="Экспорт страниц PDF в изображения", bg=PANEL, fg=DEEP_BLUE, font=("Arial", 15, "bold")).grid(row=0, column=0, sticky="w", pady=(0, 8))

        card_shell, card = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=20
        )
        card_shell.grid(row=1, column=0, sticky="new")
        card.columnconfigure(1, weight=1)
        tk.Label(card, text="Исходный PDF", bg=DROP_ZONE_BG, fg=TEXT).grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(card, textvariable=self.pdf_image_input).grid(row=0, column=1, sticky="ew", padx=10, pady=6)
        ttk.Button(card, text="Выбрать…", command=lambda: self._open_pdf(self.pdf_image_input), style="Accent.TButton").grid(row=0, column=2, pady=6)

        drop_shell, drop = create_rounded_card(
            card,
            fill=DROP_ZONE_BG,
            outer_bg=DROP_ZONE_BG,
            radius=22,
            border="#BCD2F1",
            border_width=2,
            padding=10,
            dash=(6, 5),
        )
        drop_shell.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(6, 12))
        drop_icon = self._drop_zone_icon(68)
        if drop_icon:
            tk.Label(drop, image=drop_icon, bg=DROP_ZONE_BG).pack(side="left", padx=(2, 12))
        drop_text = tk.Frame(drop, bg=DROP_ZONE_BG)
        drop_text.pack(side="left", fill="x", expand=True)
        tk.Label(
            drop_text,
            text="Перетащите PDF сюда",
            bg=DROP_ZONE_BG,
            fg=DEEP_BLUE,
            font=(FONT_FAMILY, 11, "bold"),
        ).pack(anchor="w")
        tk.Label(
            drop_text,
            text="Страницы будут сохранены как JPG, PNG, WEBP или TIFF",
            bg=DROP_ZONE_BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
        ).pack(anchor="w", pady=(3, 0))

        tk.Label(card, text="Папка результата", bg=DROP_ZONE_BG, fg=TEXT).grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(card, textvariable=self.pdf_image_output_dir).grid(row=2, column=1, sticky="ew", padx=10, pady=6)
        ttk.Button(card, text="Выбрать…", command=lambda: self._choose_dir(self.pdf_image_output_dir)).grid(row=2, column=2, pady=6)

        options = tk.Frame(card, bg=DROP_ZONE_BG)
        options.grid(row=3, column=1, sticky="w", padx=10, pady=(8, 4))
        tk.Label(options, text="Формат", bg=DROP_ZONE_BG, fg=TEXT).pack(side="left")
        format_box = ttk.Combobox(options, textvariable=self.pdf_image_format, values=("jpg", "png", "webp", "tiff"), state="readonly", width=10)
        format_box.pack(side="left", padx=(6, 18))
        tk.Label(options, text="DPI", bg=DROP_ZONE_BG, fg=TEXT).pack(side="left")
        ttk.Spinbox(options, from_=36, to=1200, textvariable=self.pdf_image_dpi, width=8).pack(side="left", padx=(6, 0))
        tk.Label(card, text="Пустая папка результата означает сохранение рядом с исходным PDF.", bg=DROP_ZONE_BG, fg=MUTED, font=("Arial", 9)).grid(row=4, column=1, sticky="w", padx=10, pady=(2, 14))
        ttk.Button(card, text="Преобразовать", command=self._run_pdf_to_images, style="Accent.TButton").grid(row=5, column=1, sticky="w", padx=10)
        self._enable_drop(frame, self._handle_pdf_images_drop)
        self._enable_drop(drop_shell, self._handle_pdf_images_drop)
        self._enable_drop(drop, self._handle_pdf_images_drop)
        return frame

    def _documents_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(10, 8, 10, 10), style="Panel.TFrame")
        frame.columnconfigure((0, 1), weight=1, uniform="documents")
        frame.rowconfigure(1, weight=1)
        self.docx_output_dir = tk.StringVar()
        self.pdf_docx_output_dir = tk.StringVar()

        tk.Label(frame, text="Преобразование документов", bg=PANEL, fg=DEEP_BLUE, font=("Arial", 15, "bold")).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))

        docx_shell, docx_panel = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=14
        )
        docx_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 5))
        docx_panel.columnconfigure(0, weight=1)
        docx_panel.rowconfigure(2, weight=1)
        title_row = tk.Frame(docx_panel, bg=DROP_ZONE_BG)
        title_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        title_row.columnconfigure(0, weight=1)
        tk.Label(title_row, text="DOCX / DOC → PDF", bg=DROP_ZONE_BG, fg=DEEP_BLUE, font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(title_row, textvariable=self.docx_files_summary, bg=DROP_ZONE_BG, fg=MUTED).grid(row=0, column=1, sticky="e")
        doc_actions = tk.Frame(docx_panel, bg=DROP_ZONE_BG)
        doc_actions.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(doc_actions, text="Добавить документы", command=self._add_docx_files, style="Accent.TButton").pack(side="left")
        ttk.Button(doc_actions, text="Удалить", command=self._remove_docx_files).pack(side="left", padx=5)
        docx_list_shell, docx_list_body = create_rounded_card(
            docx_panel, fill=PANEL, outer_bg=DROP_ZONE_BG, radius=13, border=BORDER, padding=5
        )
        docx_list_shell.grid(row=2, column=0, sticky="nsew")
        docx_list_body.columnconfigure(0, weight=1)
        docx_list_body.rowconfigure(0, weight=1)
        self.docx_list = tk.Listbox(docx_list_body, selectmode=tk.EXTENDED, bg=PANEL, fg=TEXT, selectbackground=BLUE, selectforeground="white", relief="flat", borderwidth=0, highlightthickness=0, activestyle="none")
        self.docx_list.grid(row=0, column=0, sticky="nsew")
        self._enable_drop(self.docx_list, self._handle_docx_drop)
        self.docx_empty_state = self._create_file_drop_state(
            docx_list_body,
            "Перетащите документы",
            "DOCX или DOC для преобразования в PDF\nили нажмите здесь для выбора файлов",
            self._handle_docx_drop,
            self._add_docx_files,
        )
        doc_output = tk.Frame(docx_panel, bg=DROP_ZONE_BG)
        doc_output.grid(row=3, column=0, sticky="ew", pady=(9, 0))
        doc_output.columnconfigure(0, weight=1)
        ttk.Entry(doc_output, textvariable=self.docx_output_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(doc_output, text="Папка…", command=lambda: self._choose_dir(self.docx_output_dir)).grid(row=0, column=1, padx=(5, 0))
        self.docx_convert_button = ttk.Button(
            docx_panel, text="Преобразовать в PDF", command=self._run_docx, style="Accent.TButton"
        )
        self.docx_convert_button.grid(row=4, column=0, sticky="ew", pady=(8, 0))

        pdf_shell, pdf_panel = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=14
        )
        pdf_shell.grid(row=1, column=1, sticky="nsew", padx=(5, 0))
        pdf_panel.columnconfigure(0, weight=1)
        pdf_panel.rowconfigure(2, weight=1)
        pdf_title_row = tk.Frame(pdf_panel, bg=DROP_ZONE_BG)
        pdf_title_row.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        pdf_title_row.columnconfigure(0, weight=1)
        tk.Label(pdf_title_row, text="PDF → DOCX", bg=DROP_ZONE_BG, fg=DEEP_BLUE, font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(pdf_title_row, textvariable=self.pdf_docx_files_summary, bg=DROP_ZONE_BG, fg=MUTED).grid(row=0, column=1, sticky="e")
        pdf_actions = tk.Frame(pdf_panel, bg=DROP_ZONE_BG)
        pdf_actions.grid(row=1, column=0, sticky="ew", pady=(0, 7))
        ttk.Button(pdf_actions, text="Добавить PDF", command=self._add_pdf_docx_files, style="Accent.TButton").pack(side="left")
        ttk.Button(pdf_actions, text="Удалить", command=self._remove_pdf_docx_files).pack(side="left", padx=5)
        pdf_list_shell, pdf_list_body = create_rounded_card(
            pdf_panel, fill=PANEL, outer_bg=DROP_ZONE_BG, radius=13, border=BORDER, padding=5
        )
        pdf_list_shell.grid(row=2, column=0, sticky="nsew")
        pdf_list_body.columnconfigure(0, weight=1)
        pdf_list_body.rowconfigure(0, weight=1)
        self.pdf_docx_list = tk.Listbox(pdf_list_body, selectmode=tk.EXTENDED, bg=PANEL, fg=TEXT, selectbackground=BLUE, selectforeground="white", relief="flat", borderwidth=0, highlightthickness=0, activestyle="none")
        self.pdf_docx_list.grid(row=0, column=0, sticky="nsew")
        self._enable_drop(self.pdf_docx_list, self._handle_pdf_docx_drop)
        self.pdf_docx_empty_state = self._create_file_drop_state(
            pdf_list_body,
            "Перетащите PDF",
            f"Преобразование в DOCX через {'Microsoft Word' if os.name == 'nt' else 'R7-Офис'} или встроенное OCR\nили нажмите здесь для выбора файлов",
            self._handle_pdf_docx_drop,
            self._add_pdf_docx_files,
        )
        pdf_mode = tk.Frame(pdf_panel, bg=DROP_ZONE_BG)
        pdf_mode.grid(row=3, column=0, sticky="ew", pady=(8, 0))
        ttk.Checkbutton(
            pdf_mode,
            text="Предварительно распознать текст",
            variable=self.pdf_docx_pre_ocr,
            style="Card.TCheckbutton",
        ).grid(row=0, column=0, sticky="w")
        language_row = tk.Frame(pdf_mode, bg=DROP_ZONE_BG)
        language_row.grid(row=0, column=1, sticky="w", padx=(12, 0))
        tk.Label(language_row, text="Язык текста", bg=DROP_ZONE_BG, fg=TEXT).pack(side="left", padx=(0, 5))
        self.pdf_docx_ocr_language_box = ttk.Combobox(
            language_row,
            textvariable=self.pdf_docx_ocr_language,
            values=("rus", "rus+eng", "eng"),
            state="readonly",
            width=8,
        )
        self.pdf_docx_ocr_language_box.pack(side="left")
        self._tip(
            self.pdf_docx_ocr_language_box,
            "rus — только русский текст и меньше латинских артефактов; rus+eng — смешанный документ; eng — английский.",
        )
        self.pdf_docx_preserve_graphics_check = ttk.Checkbutton(
            pdf_mode,
            text="Сохранить графику",
            variable=self.pdf_docx_preserve_graphics,
            style="Card.TCheckbutton",
        )
        self.pdf_docx_preserve_graphics_check.grid(row=1, column=0, sticky="w", pady=(6, 0))
        self._tip(
            self.pdf_docx_preserve_graphics_check,
            "Дополнительный режим: переносит логотипы, линии, печати и другую графику. Отключите для чистого DOCX только с редактируемым текстом и таблицами.",
        )
        self.pdf_docx_detect_tables_check = ttk.Checkbutton(
            pdf_mode,
            text="Распознавать таблицы",
            variable=self.pdf_docx_detect_tables,
            style="Card.TCheckbutton",
        )
        self.pdf_docx_detect_tables_check.grid(row=1, column=1, sticky="w", padx=(12, 0), pady=(6, 0))
        self._tip(
            self.pdf_docx_detect_tables_check,
            "Создавать редактируемые таблицы по сетке документа. Отключите, если нужен только связный текст без ячеек.",
        )
        tk.Label(
            pdf_panel,
            text="По умолчанию создаётся чистый DOCX: редактируемый текст и таблицы без графики. Включите «Сохранить графику», если важнее визуальное сходство с PDF. Точность зависит от качества скана.",
            bg="#FFF7D6",
            fg="#735C00",
            wraplength=500,
            justify="left",
            padx=8,
            pady=6,
        ).grid(row=4, column=0, sticky="ew", pady=(7, 0))
        pdf_output = tk.Frame(pdf_panel, bg=DROP_ZONE_BG)
        pdf_output.grid(row=5, column=0, sticky="ew", pady=(9, 0))
        pdf_output.columnconfigure(0, weight=1)
        ttk.Entry(pdf_output, textvariable=self.pdf_docx_output_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(pdf_output, text="Папка…", command=lambda: self._choose_dir(self.pdf_docx_output_dir)).grid(row=0, column=1, padx=(5, 0))
        self.pdf_docx_convert_button = ttk.Button(
            pdf_panel, text="Преобразовать в DOCX", command=self._run_pdf_docx, style="Accent.TButton"
        )
        self.pdf_docx_convert_button.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        progress_shell, progress_panel = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=12, border=BORDER, padding=8
        )
        progress_shell.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        progress_panel.columnconfigure(0, weight=1)
        self.documents_progress_bar = ttk.Progressbar(
            progress_panel,
            variable=self.documents_progress_value,
            maximum=1,
            mode="determinate",
        )
        self.documents_progress_bar.grid(row=0, column=0, sticky="ew")
        tk.Label(
            progress_panel,
            textvariable=self.documents_progress_text,
            bg=DROP_ZONE_BG,
            fg=MUTED,
            width=34,
            anchor="e",
        ).grid(row=0, column=1, padx=(10, 0))
        return frame

    def _compress_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=(10, 8, 10, 10), style="Panel.TFrame")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(1, weight=1)

        heading = tk.Frame(frame, bg=PANEL)
        heading.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 7))
        heading.columnconfigure(0, weight=1)
        tk.Label(heading, text="Сжатие PDF", bg=PANEL, fg=DEEP_BLUE, font=("Arial", 15, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(heading, textvariable=self.compress_files_summary, bg=PANEL, fg=MUTED).grid(row=0, column=1, sticky="e")

        list_shell, list_holder = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=12
        )
        list_shell.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        list_holder.columnconfigure(0, weight=1)
        list_holder.rowconfigure(0, weight=1)
        compress_surface, compress_body = create_rounded_card(
            list_holder, fill=PANEL, outer_bg=DROP_ZONE_BG, radius=14, border=BORDER, padding=5
        )
        compress_surface.grid(row=0, column=0, sticky="nsew")
        compress_body.columnconfigure(0, weight=1)
        compress_body.rowconfigure(0, weight=1)
        self.compress_list = tk.Listbox(compress_body, selectmode=tk.EXTENDED, bg=PANEL, fg=TEXT, selectbackground=BLUE, selectforeground="white", relief="flat", borderwidth=0, highlightthickness=0, activestyle="none")
        self.compress_list.grid(row=0, column=0, sticky="nsew")
        self._enable_drop(self.compress_list, self._handle_compress_drop)
        self.compress_empty_state = self._create_file_drop_state(
            compress_body,
            "Перетащите PDF для сжатия",
            "Можно добавить один документ или целую партию\nили нажмите здесь для выбора файлов",
            self._handle_compress_drop,
            self._add_compress_files,
        )
        compress_actions = tk.Frame(list_holder, bg=DROP_ZONE_BG)
        compress_actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(compress_actions, text="Добавить PDF", command=self._add_compress_files, style="Accent.TButton").pack(side="left")
        ttk.Button(compress_actions, text="Удалить", command=self._remove_compress_files).pack(side="left", padx=5)
        ttk.Button(compress_actions, text="Очистить список", command=self._clear_compress_files).pack(side="left")

        panel_shell, panel = create_rounded_card(
            frame, fill=DROP_ZONE_BG, outer_bg=PANEL, radius=18, border=BORDER, padding=12, width=370
        )
        panel_shell.grid(row=1, column=1, sticky="ns")
        panel_shell.grid_propagate(False)
        panel.columnconfigure(0, weight=1)
        tk.Label(panel, text="Параметры сжатия", bg=DROP_ZONE_BG, fg=DEEP_BLUE, font=("Arial", 12, "bold")).grid(row=0, column=0, sticky="w")
        tk.Label(panel, text="Папка результата", bg=DROP_ZONE_BG, fg=TEXT).grid(row=1, column=0, sticky="w", pady=(10, 3))
        output_row = tk.Frame(panel, bg=DROP_ZONE_BG)
        output_row.grid(row=2, column=0, sticky="ew")
        output_row.columnconfigure(0, weight=1)
        ttk.Entry(output_row, textvariable=self.compress_output_dir).grid(row=0, column=0, sticky="ew")
        ttk.Button(output_row, text="Выбрать…", command=lambda: self._choose_dir(self.compress_output_dir)).grid(
            row=0, column=1, padx=(5, 0)
        )
        tk.Label(panel, text="Сжатие", bg=DROP_ZONE_BG, fg=TEXT, font=("Arial", 10, "bold")).grid(row=4, column=0, sticky="w", pady=(11, 3))
        options = (
            ("medium", "Оптимальное"),
            ("maximum", "Максимальное"),
        )
        self.compress_preset_buttons = []
        for row, (value, title) in enumerate(options, start=5):
            button = ttk.Radiobutton(panel, text=title, variable=self.compress_preset, value=value, style="Card.TRadiobutton")
            button.grid(row=row, column=0, sticky="w", pady=1)
            self.compress_preset_buttons.append(button)
        tk.Label(panel, textvariable=self.compress_estimate, bg=SURFACE_ALT, fg=DEEP_BLUE, wraplength=330, justify="left", padx=8, pady=4, font=("Arial", 9, "bold")).grid(row=8, column=0, sticky="ew", pady=(12, 0))
        ttk.Button(panel, text="Сжать PDF", command=self._run_compress, style="Accent.TButton").grid(row=9, column=0, sticky="ew", pady=(12, 0))
        self._update_compress_mode_controls()
        self._enable_drop(frame, self._handle_compress_drop)
        self._enable_drop(panel_shell, self._handle_compress_drop)
        self._enable_drop(panel, self._handle_compress_drop)
        return frame

    def _update_compress_mode_controls(self) -> None:
        target_mode = self.compress_mode.get() == "target"
        if hasattr(self, "compress_target_entry"):
            self.compress_target_entry.configure(state="normal" if target_mode else "disabled")
        for button in getattr(self, "compress_preset_buttons", []):
            button.configure(state="disabled" if target_mode else "normal")
        self._update_compression_estimate()

    @staticmethod
    def _format_file_size(size: int) -> str:
        if size < 1024 * 1024:
            return f"{size / 1024:.1f} КБ"
        return f"{size / (1024 * 1024):.2f} МБ"

    def _update_compression_estimate(self) -> None:
        if not hasattr(self, "compress_estimate"):
            return
        paths = [Path(value) for value in self.compress_files if Path(value).is_file()]
        if not paths:
            self.compress_estimate.set("Добавьте PDF")
            return
        try:
            source_size = sum(path.stat().st_size for path in paths)
        except OSError:
            self.compress_estimate.set("Исходный объём недоступен")
            return
        self.compress_estimate.set(f"Исходный объём: {self._format_file_size(source_size)}")

    def _about_tab(self, parent) -> ttk.Frame:
        frame = ttk.Frame(parent, padding=20, style="Panel.TFrame")
        ttk.Label(frame, text=APP_TITLE, font=("Arial", 22, "bold")).pack(anchor="w")
        ttk.Label(frame, text=f"Версия {__version__}").pack(anchor="w", pady=(8, 16))
        ttk.Label(frame, text="Редактор PDF: страницы, текст, подписи, OCR, конвертация и сжатие.\nДокументы обрабатываются локально. Сбор и отправка статистики отсутствуют.",
                  wraplength=700, justify="left").pack(anchor="w")
        ttk.Button(frame, text="Открыть руководство пользователя", command=self._open_user_guide).pack(anchor="w", pady=16)
        ttk.Button(frame, text="Собрать отладочную информацию", command=self._export_ocr_diagnostics).pack(anchor="w")
        return frame

    def _enable_drop(self, widget, handler=None) -> None:
        if not DND_FILES:
            return
        try:
            widget.drop_target_register(DND_FILES)
            widget.dnd_bind("<<Drop>>", handler or self._handle_drop)
        except Exception:
            pass

    def _drop_zone_icon(self, size: int = 96) -> ImageTk.PhotoImage | None:
        if size in self._drop_zone_icon_cache:
            return self._drop_zone_icon_cache[size]
        if not DROP_ZONE_ICON_PATH.exists():
            return None
        with Image.open(DROP_ZONE_ICON_PATH) as source:
            icon = source.convert("RGBA")
            icon.thumbnail((size, size), LANCZOS)
        photo = ImageTk.PhotoImage(icon)
        self._drop_zone_icon_cache[size] = photo
        return photo

    def _create_file_drop_state(
        self,
        parent,
        title: str,
        subtitle: str,
        drop_handler,
        choose_command,
    ) -> tk.Frame:
        empty, content = create_rounded_card(
            parent,
            fill=DROP_ZONE_BG,
            outer_bg=PANEL,
            radius=22,
            border="#BCD2F1",
            border_width=2,
            padding=7,
            dash=(6, 5),
            cursor="hand2",
        )
        empty.grid(row=0, column=0, sticky="nsew")
        content.configure(cursor="hand2")
        content.columnconfigure(0, weight=1)
        content.rowconfigure((0, 4), weight=1)
        icon = self._drop_zone_icon(104)
        icon_label = tk.Label(content, image=icon, bg=DROP_ZONE_BG, cursor="hand2") if icon else tk.Label(content, bg=DROP_ZONE_BG)
        icon_label.grid(row=1, column=0, pady=(10, 8))
        title_label = tk.Label(
            content,
            text=title,
            bg=DROP_ZONE_BG,
            fg=TEXT,
            font=(FONT_FAMILY, 13, "bold"),
            cursor="hand2",
        )
        title_label.grid(row=2, column=0)
        subtitle_label = tk.Label(
            content,
            text=subtitle,
            bg=DROP_ZONE_BG,
            fg=MUTED,
            font=(FONT_FAMILY, 9),
            justify="center",
            wraplength=430,
            cursor="hand2",
        )
        subtitle_label.grid(row=3, column=0, padx=18, pady=(7, 10))
        for widget in (empty, content, icon_label, title_label, subtitle_label):
            self._enable_drop(widget, drop_handler)
            widget.bind("<Button-1>", lambda _event, command=choose_command: command())
        return empty

    def _sync_file_drop_states(self) -> None:
        states = (
            (getattr(self, "image_empty_state", None), self.image_files),
            (getattr(self, "docx_empty_state", None), self.docx_files),
            (getattr(self, "pdf_docx_empty_state", None), self.pdf_docx_files),
            (getattr(self, "compress_empty_state", None), self.compress_files),
        )
        for widget, files in states:
            if widget is None:
                continue
            if files:
                widget.grid_remove()
            else:
                widget.grid()
                widget.tkraise()

    @staticmethod
    def _wheel_direction(event) -> int:
        number = getattr(event, "num", 0)
        delta = getattr(event, "delta", 0)
        if number == 4 or delta > 0:
            return -1
        if number == 5 or delta < 0:
            return 1
        return 0

    def _scroll_canvas_with_wheel(self, canvas: tk.Canvas, event) -> str | None:
        if getattr(event, "state", 0) & 0x0004:
            return None
        direction = self._wheel_direction(event)
        if not direction:
            return None
        if getattr(event, "state", 0) & 0x0001:
            canvas.xview_scroll(direction * 3, "units")
        else:
            canvas.yview_scroll(direction * 3, "units")
        return "break"

    def _bind_canvas_wheel(self, widget, canvas: tk.Canvas) -> None:
        widget.bind("<MouseWheel>", lambda event, target=canvas: self._scroll_canvas_with_wheel(target, event), add="+")
        widget.bind("<Button-4>", lambda event, target=canvas: self._scroll_canvas_with_wheel(target, event), add="+")
        widget.bind("<Button-5>", lambda event, target=canvas: self._scroll_canvas_with_wheel(target, event), add="+")

    def _open_user_guide(self) -> None:
        if not GUIDE_PATH.exists():
            messagebox.showerror("Руководство", f"Файл руководства не найден:\n{GUIDE_PATH}")
            return
        try:
            if os.name == "nt":
                os.startfile(str(GUIDE_PATH))
            else:
                subprocess.Popen(["xdg-open", str(GUIDE_PATH)])
        except (OSError, subprocess.SubprocessError):
            messagebox.showinfo("Руководство", f"Откройте PDF-файл:\n{GUIDE_PATH}")

    def _bind_preference_traces(self) -> None:
        for variable in (
            self.pdf_image_format,
            self.editor_font_family,
            self.compress_mode,
            self.compress_preset,
            self.compress_color_mode,
            self.editor_ocr_language,
            self.pdf_docx_pre_ocr,
            self.pdf_docx_ocr_language,
            self.pdf_docx_preserve_graphics,
            self.pdf_docx_detect_tables,
        ):
            variable.trace_add("write", self._preferences_changed)
        for variable in (self.compress_mode, self.compress_preset, self.compress_color_mode, self.compress_target_mb):
            variable.trace_add("write", lambda *_args: self._update_compression_estimate())

    def _preferences_changed(self, *_args) -> None:
        self.preferences.update(
            {
                "pdf_image_format": self.pdf_image_format.get(),
                "editor_font_family": self.editor_font_family.get(),
                "compress_mode": self.compress_mode.get(),
                "compress_preset": self.compress_preset.get(),
                "compress_color_mode": self.compress_color_mode.get(),
                "ocr_language": self.editor_ocr_language.get(),
                "pdf_docx_pre_ocr": bool(self.pdf_docx_pre_ocr.get()),
                "document_ocr_language": self.pdf_docx_ocr_language.get(),
                "document_ocr_preserve_graphics": bool(self.pdf_docx_preserve_graphics.get()),
                "document_ocr_detect_tables": bool(self.pdf_docx_detect_tables.get()),
            }
        )
        try:
            save_preferences(self.preferences)
        except OSError:
            pass

    def _dialog_initialdir(self, fallback: Path | None = None) -> str:
        return str(usable_last_folder(self.preferences, fallback))

    def _remember_dialog_selection(self, selected) -> None:
        if not selected:
            return
        value = selected[0] if isinstance(selected, (tuple, list)) else selected
        remember_folder(self.preferences, value)
        remember_recent_files(self.preferences, selected)
        try:
            save_preferences(self.preferences)
        except OSError:
            pass
        self._refresh_recent_menu()

    def _schedule_autosave(self) -> None:
        if self.autosave_after_id is not None:
            self.after_cancel(self.autosave_after_id)
        self.autosave_after_id = self.after(20000, self._autosave_tick)

    def _queue_autosave(self) -> None:
        if self.restoring_session or not hasattr(self, "autosave_pending_id"):
            return
        if self.autosave_pending_id is not None:
            self.after_cancel(self.autosave_pending_id)
        self.autosave_pending_id = self.after(1500, self._autosave_once)

    def _autosave_tick(self) -> None:
        self.autosave_after_id = None
        self._autosave_once()
        self._schedule_autosave()

    def _autosave_once(self) -> None:
        self.autosave_pending_id = None
        payload = self._recovery_payload()
        if payload == self._last_recovery_payload:
            self._prune_editor_session_files(payload)
            return
        try:
            if has_recoverable_content(payload):
                save_recovery(payload)
            else:
                clear_recovery()
        except OSError:
            return
        self._last_recovery_payload = payload
        self._prune_editor_session_files(payload)

    def _editor_cache_file(self, name):
        if not hasattr(self, "_editor_session_files"):
            self._editor_session_files = EditorSessionFiles(app_cache_dir("editor-sessions"))
        return self._editor_session_files.path(name)

    def _prune_editor_session_files(self, recovery):
        owner = getattr(self, "_editor_session_files", None)
        if (owner is None or self.editor_pages_busy or self.editor_index_busy or self._global_progress_active
                or getattr(self, "_background_task_count", 0)):
            return
        # Retain at least the most recent undo/redo even for an oversized source.
        def history_bytes():
            refs = owner.references((self._editor_state_snapshot(), self.editor_undo_stack, self.editor_redo_stack, recovery))
            return sum(path.stat().st_size for path in refs if path.is_file())
        try:
            for history in (self.editor_undo_stack, self.editor_redo_stack):
                while len(history) > 1 and history_bytes() > 512 * 1024 * 1024:
                    del history[0]
        except OSError:
            return
        live = (self._editor_state_snapshot(), self.editor_undo_stack, self.editor_redo_stack, recovery)
        try:
            owner.prune(live)
        except OSError:
            return
        paths = {str(self.editor_pdf_path)}
        paths.update(str(state[7]) for state in self.editor_undo_stack + self.editor_redo_stack)
        if hasattr(self, "_editor_ocr_cache"):
            self._editor_ocr_cache = {key: value for key, value in self._editor_ocr_cache.items() if key[0] in paths}

    def _recovery_payload(self) -> dict:
        workspace = [
            {
                "source": str(ref.source),
                "page_index": ref.page_index,
                "page_count": ref.page_count,
                "rotation": ref.rotation,
                "label": ref.label,
                "cached_source": str(ref.cached_source) if ref.cached_source else "",
            }
            for ref in self.pages
            if resolve_page_ref_source(ref) is not None
        ]
        editor = None
        if self.editor_dirty and self.editor_pdf_path and self.editor_pdf_path.is_file():
            editor = {
                "source": str(self.editor_pdf_path),
                "source_kind": self.editor_source_kind,
                "new_document": self.editor_new_document,
                "last_save_path": str(self.editor_last_save_path) if self.editor_last_save_path else "",
                "page_index": self.editor_page_index,
                "text_edits": [asdict(item) for item in self.editor_text_edits],
                "signature_edits": [asdict(item) for item in self.editor_signature_edits],
                "region_edits": [asdict(item) for item in self.editor_region_edits],
                "stroke_edits": [asdict(item) for item in self.editor_stroke_edits],
                "crop_edits": [asdict(item) for item in self.editor_crop_edits],
                "alignment_edits": [asdict(item) for item in self.editor_alignment_edits],
            }
        return {"workspace": workspace, "editor": editor}

    def _offer_recovery(self) -> None:
        payload = load_recovery()
        if not has_recoverable_content(payload):
            return
        workspace_count = len(payload.get("workspace", []))
        editor_text = " и несохранённые правки редактора" if payload.get("editor") else ""
        if not messagebox.askyesno(
            "Восстановление работы",
            f"Найдено автосохранение: страниц в разделе PDF — {workspace_count}{editor_text}.\n\nВосстановить предыдущую работу?",
            parent=self,
        ):
            clear_recovery()
            return
        self._restore_recovery(payload)

    def _restore_recovery(self, payload: dict) -> None:
        self.restoring_session = True
        try:
            workspace = []
            for item in payload.get("workspace", []):
                source = Path(str(item.get("source", "")))
                cached_value = str(item.get("cached_source", ""))
                cached_source = Path(cached_value) if cached_value else None
                if available_workspace_source(source, cached_source) is not None:
                    workspace.append(
                        PageRef(
                            source,
                            int(item.get("page_index", 0)),
                            max(1, int(item.get("page_count", 1))),
                            int(item.get("rotation", 0)) % 360,
                            str(item.get("label", "")),
                            cached_source,
                        )
                    )
            self.pages = workspace
            self.selected.clear()
            self.last_selected_index = None
            # Migrate the old independent PDF workspace into the unified editor.
            if workspace and not payload.get("editor"):
                restored = app_cache_dir("editor-documents") / f"recovered-{time.time_ns()}.pdf"
                self._write_page_refs(workspace, restored)
                self._load_editor_pdf(restored)
                self.editor_dirty = True
                self.editor_new_document = True
            self.pages = []

            editor = payload.get("editor")
            if isinstance(editor, dict):
                source = Path(str(editor.get("source", "")))
                if source.is_file():
                    if source.suffix.lower() == ".pdf":
                        self._load_editor_pdf(source)
                    elif source.suffix.lower() in IMAGE_SUFFIXES:
                        self._load_editor_image(source)
                    self.editor_text_edits = [
                        TextEdit(
                            int(item["page_index"]),
                            tuple(item["rect"]),
                            str(item.get("text", "")),
                            float(item.get("font_size", 11.0)),
                            str(item.get("font_family", "Times New Roman")),
                            item.get("font_file"),
                            tuple(item.get("color", (0.0, 0.0, 0.0))),
                            bool(item.get("opaque_background", False)),
                            tuple(item.get("background_color", (1.0, 1.0, 1.0))),
                            bool(item.get("bold", False)),
                            bool(item.get("italic", False)),
                            bool(item.get("underline", False)),
                            item.get("replacement_background"),
                            tuple(item["replacement_background_rect"])
                            if item.get("replacement_background_rect")
                            else None,
                            tuple(item["replacement_source_rect"])
                            if item.get("replacement_source_rect")
                            else None,
                            str(item.get("original_text", "")),
                            float(item.get("style_confidence", 0.0)),
                            float(item.get("scan_blur", 0.0)),
                            bool(item.get("scan_effect", True)),
                        )
                        for item in editor.get("text_edits", [])
                    ]
                    self.editor_signature_edits = [
                        SignatureEdit(
                            int(item["page_index"]),
                            tuple(item["rect"]),
                            str(item["image_path"]),
                            bool(item.get("keep_proportion", True)),
                        )
                        for item in editor.get("signature_edits", [])
                        if Path(str(item.get("image_path", ""))).is_file()
                    ]
                    self.editor_region_edits = [
                        RegionCopyEdit(int(item["page_index"]), tuple(item["source_rect"]), tuple(item["rect"]))
                        for item in editor.get("region_edits", [])
                    ]
                    self.editor_stroke_edits = [
                        StrokeEdit(
                            int(item["page_index"]),
                            tuple(tuple(point) for point in item["points"]),
                            float(item.get("width", 2.0)),
                            tuple(item.get("color", (0.0, 0.0, 0.0))),
                            str(item.get("style", "free")),
                        )
                        for item in editor.get("stroke_edits", [])
                    ]
                    self.editor_crop_edits = [
                        CropEdit(int(item["page_index"]), tuple(item["rect"])) for item in editor.get("crop_edits", [])
                    ]
                    self.editor_alignment_edits = [
                        AlignmentEdit(int(item["page_index"]), float(item.get("angle", 0.0)))
                        for item in editor.get("alignment_edits", [])
                    ]
                    self.editor_page_index = min(
                        max(0, int(editor.get("page_index", 0))),
                        max(0, self.editor_page_count - 1),
                    )
                    self.editor_new_document = bool(editor.get("new_document", False))
                    saved_path = Path(str(editor.get("last_save_path", "")))
                    self.editor_last_save_path = saved_path if saved_path.is_file() else None
                    self.editor_dirty = True
                    self.notebook.select(self.editor_tab)
                    self.after_idle(self._render_editor_page)
                    self.after_idle(self._refresh_editor_edits_panel)
            if workspace and payload.get("editor"):
                restored = app_cache_dir("editor-documents") / f"recovered-{time.time_ns()}.pdf"
                self._write_page_refs(workspace, restored)
                if self.editor_pdf_path:
                    self._append_editor_files([restored])
                else:
                    self._load_editor_pdf(restored)
                    self.editor_dirty = True
                    self.editor_new_document = True
            self.status.set("Предыдущая работа восстановлена из автосохранения")
        except Exception as exc:
            clear_recovery()
            self._show_error(exc)
        finally:
            self.restoring_session = False

    def _editor_shortcuts_enabled(self) -> bool:
        if not hasattr(self, "notebook") or self.notebook.select() != str(self.editor_tab):
            return False
        focus = self.focus_get()
        return not isinstance(focus, (tk.Entry, tk.Text, tk.Listbox, ttk.Entry, ttk.Combobox))

    def _editor_undo_shortcut(self, _event=None):
        if self._editor_shortcuts_enabled() and not self.editor_reading_mode:
            self._undo_editor_action()
            return "break"
        return None

    def _editor_redo_shortcut(self, _event=None):
        if self._editor_shortcuts_enabled() and not self.editor_reading_mode:
            self._redo_editor_action()
            return "break"
        return None

    def _editor_copy_shortcut(self, _event=None):
        if self._editor_shortcuts_enabled() and (
            self.editor_recognized_text_selection or self.editor_selected or self.editor_region_selection
        ):
            if self.editor_recognized_text_selection:
                self._copy_recognized_text()
            else:
                self._copy_selected_editor_object()
            return "break"
        return None

    def _editor_clipboard_keypress(self, event):
        # Windows virtual key codes follow the physical shortcut in any layout.
        if int(event.state) & (0x0008 | 0x20000):
            return None
        key = str(event.keysym).lower()
        code = int(event.keycode) if os.name == "nt" else None
        if code == 67 or key in {"c", "cyrillic_es", "с"}:
            return self._editor_copy_shortcut(event)
        if code == 86 or key in {"v", "cyrillic_em", "м"}:
            return self._editor_paste_shortcut(event)
        return None

    def _editor_paste_shortcut(self, _event=None):
        if self._editor_shortcuts_enabled() and not self.editor_reading_mode:
            self._paste_editor_object()
            return "break"
        return None

    def _editor_delete_shortcut(self, _event=None):
        if self.editor_grid_mode and self._editor_shortcuts_enabled() and self.editor_page_selection:
            self._delete_editor_selected_pages()
            return "break"
        if not self.editor_reading_mode and self._editor_shortcuts_enabled() and (
            self.editor_recognized_text_selection or self.editor_selected or self.editor_region_selection
        ):
            if self.editor_selected:
                self._delete_selected_editor_object()
            elif self.editor_recognized_text_selection:
                self.editor_recognized_text_selection = None
                self.status.set("Выделение распознанного текста снято")
                self._render_editor_page()
            else:
                self.editor_region_selection = None
                self.status.set("Выделение области снято")
                self._render_editor_page()
            return "break"
        return None

    def _editor_save_shortcut(self, _event=None):
        if not self.editor_reading_mode and self._editor_shortcuts_enabled() and self.editor_pdf_path:
            self._save_editor_pdf()
            return "break"
        return None

    def _confirm_discard_editor_changes(self) -> bool:
        if not self.editor_dirty:
            return True
        return messagebox.askyesno(
            "Несохранённые изменения",
            "В PDF Редакторе есть несохранённые изменения. Закрыть документ без сохранения?",
            parent=self,
        )








    def _on_app_close(self) -> None:
        if self._confirm_discard_editor_changes():
            for after_id in (self.autosave_after_id, self.autosave_pending_id):
                if after_id is not None:
                    try:
                        self.after_cancel(after_id)
                    except tk.TclError:
                        pass
            clear_recovery()
            self.destroy()

    def _tip(self, widget, text: str):
        widget._pdfbars_tooltip = ToolTip(widget, text)
        return widget

    def _install_tooltips(self) -> None:
        for widget in self._walk_widgets(self):
            if hasattr(widget, "_pdfbars_tooltip"):
                continue
            text = ""
            try:
                text = str(widget.cget("text"))
            except tk.TclError:
                text = ""
            tooltip = self._default_tooltip(widget, text)
            if tooltip:
                self._tip(widget, tooltip)

    def _walk_widgets(self, widget):
        for child in widget.winfo_children():
            yield child
            yield from self._walk_widgets(child)

    def _default_tooltip(self, widget, text: str) -> str:
        tips = {
            "Открыть руководство пользователя": "Открыть подробную локальную инструкцию в формате PDF.",
            "Добавить файлы": "Добавить PDF и изображения в общую рабочую область страниц.",
            "Добавить PDF": "Выберите PDF-файлы. В разделах PDF и сжатия можно также перетаскивать файлы мышью.",
            "Сохранить PDF": "Собрать все страницы рабочей области в новый PDF.",
            "Печать": "Открыть параметры печати и отправить текущий PDF или документ редактора на выбранный принтер. Ctrl+P.",
            "Сохранить документ": "Собрать все страницы в один новый PDF. Исходные файлы не меняются.",
            "Сохранить выбранные": "Сохранить только выделенные страницы в отдельный PDF.",
            "Удалить выбранные": "Убрать выбранные страницы из рабочей области без удаления исходных файлов.",
            "Дублировать": "Создать копии выделенных страниц непосредственно рядом с оригиналами.",
            "Повернуть влево": "Повернуть выбранные страницы на 90 градусов против часовой стрелки.",
            "Повернуть вправо": "Повернуть выбранные страницы на 90 градусов по часовой стрелке.",
            "↶ 90°": "Повернуть выделенные страницы на 90 градусов против часовой стрелки.",
            "↷ 90°": "Повернуть выделенные страницы на 90 градусов по часовой стрелке.",
            "Очистить": "Очистить список в текущем разделе.",
            "Открыть": "Открыть PDF или изображение для визуального редактирования.",
            "Новый лист": "Создать пустой PDF-лист A5, A4 или A3 и сразу открыть его в PDF Редакторе.",
            "‹": "Перейти на предыдущую страницу PDF.",
            "›": "Перейти на следующую страницу PDF.",
            "Сохранить как…": "Выберите имя и формат: PDF, DOCX с распознанным текстом, PNG, JPG, WEBP, TIFF или BMP.",
            "Добавить файл": "Добавить один или несколько PDF и изображений в рабочую область.",
            "Выбор": "Выберите уже добавленный объект, чтобы переместить, изменить размер или удалить его.",
            "Отменить": "Отменить последнее действие в PDF Редакторе.",
            "Повторить": "Вернуть отменённое действие.",
            "Удалить": "Удалить выбранный элемент из списка или выбранный объект редактора.",
            "Копия": "Создать копию выбранного объекта и разместить её рядом.",
            "Объект": "Операции с выбранным объектом и сброс всех правок.",
            "↶": "Отменить последнее действие редактора.",
            "↷": "Повторить отменённое действие редактора.",
            "−": "Уменьшить масштаб просмотра.",
            "+": "Увеличить масштаб просмотра.",
            "Вписать": "Подогнать страницу или изображение под размер окна.",
            "По ширине": "Установить масштаб так, чтобы страница заняла всю ширину области просмотра.",
            "Страница": "Показать страницу целиком в доступной области просмотра.",
            "100%": "Показать страницу в масштабе 100%.",
            "Определить стиль": "Кликните по текстовому слою PDF, чтобы подобрать похожий шрифт, размер и цвет.",
            "Определить стиль по клику": "Кликните по текстовому слою PDF, чтобы подобрать похожий шрифт, размер и цвет.",
            "Текст": "Введите текст и щёлкните место вставки либо протяните рамку нужного размера.",
            "Закрыть": "Закрыть выбранную область белым прямоугольником.",
            "Копия области": "Выделить фрагмент страницы; для копирования нажмите Ctrl+C, для вставки — Ctrl+V.",
            "Копировать область": "Выделить фрагмент страницы; для копирования нажмите Ctrl+C, для вставки — Ctrl+V.",
            "Выделить область": "Обведите фрагмент страницы, затем нажмите Ctrl+C. Вставить его можно в другом окне pdf bars.",
            "Карандаш": "Рисовать произвольные линии поверх страницы или изображения.",
            "Ластик": "Рисовать белой линией, чтобы быстро закрывать мелкие элементы.",
            "Обрезка": "Выделите рамкой область, которая останется после сохранения.",
            "Обрезать": "Выделите рамкой область, которая останется после сохранения.",
            "Выравнивание": "Автоматически определить наклон текста и при необходимости вручную поправить угол страницы.",
            "Стиль": "Определить шрифт, размер и цвет по текстовому слою PDF.",
            "Подпись": "Открыть локальную библиотеку подписей и отметок.",
            "Разместить": "Разместить выбранную подпись или подготовленный текст.",
            "Применить": "Применить параметры к выбранному текстовому объекту.",
            "Сбросить правки": "Удалить все добавленные объекты редактора.",
            "Применить к выбранному тексту": "Обновить текст, шрифт, размер и цвет выбранного текстового объекта.",
            "Загрузить": "Добавить изображение подписи или отметки в локальную библиотеку.",
            "Добавить файл…": "Импортировать изображение подписи или отметки в локальную библиотеку.",
            "Открыть библиотеку": "Открыть сохранённые на этом компьютере подписи и отметки.",
            "Автоматически удалять фон при импорте": "Сделать однородный фон прозрачным и обрезать пустые края изображения.",
            "Разместить отметку": "Выберите сохранённую отметку и протяните рамку на документе.",
            "Очистить все правки": "Удалить все добавленные объекты редактора в текущем файле.",
            "Добавить изображения": "Выберите изображения для создания PDF. WEBP поддерживается.",
            "Создавать отдельный PDF для каждого файла": "Создать для каждого выбранного изображения самостоятельный PDF.",
            "Создать PDF": "Создать один PDF из изображений или отдельный PDF для каждого файла.",
            "Преобразовать": "Выполнить преобразование с выбранными параметрами.",
            "Добавить DOCX/DOC": f"Добавить офисные документы для преобразования через {'Microsoft Word' if os.name == 'nt' else 'R7-Office'}.",
            "Добавить документы": "Добавить DOCX или DOC для пакетного преобразования в PDF.",
            "Преобразовать в PDF": "Пакетно создать PDF из выбранных DOCX/DOC.",
            "Преобразовать в DOCX": f"Создать DOCX через {'Microsoft Word' if os.name == 'nt' else 'R7-Office'}. Включите предварительное распознавание для сканов.",
            "Предварительно распознать текст": "Распознать каждую страницу целиком и создать DOCX с редактируемым текстом, абзацами и таблицами.",
            "Сохранить графику": "Дополнительно перенести логотипы, линии, печати и другую графику. Отключите для чистого текстового DOCX.",
            "Сохранить макет через R7-Office": f"Использовать {'Microsoft Word' if os.name == 'nt' else 'R7-Office'} для максимально возможного сохранения исходного оформления PDF.",
            "Распознанный текст (OCR)": "Локально распознать русский и английский текст и создать редактируемый DOCX без зависимости от R7-Office.",
            "По качеству": "Использовать один из трёх готовых профилей качества.",
            "До размера": "Автоматически распределить указанный общий объём между всеми добавленными PDF.",
            "Сжать PDF": "Сжать один или несколько PDF по профилю качества или до заданного размера.",
            "Найти": "Найти слово или фразу в текстовом слое и автоматически распознать страницы без текста.",
            "Распознать текст": "Подготавливает скан, распознаёт весь документ локально и позволяет искать, выделять и копировать текст.",
            "Максимальное — для почты": "Наименьший читаемый файл; подходит для отправки по почте.",
            "Среднее — баланс качества": "Умеренное уменьшение размера с хорошей читаемостью.",
            "Минимальное — без потерь": "Оптимизировать структуру PDF без ухудшения изображений.",
            "Копировать техническую информацию": "Скопировать сведения об ошибке, версии и системе для отправки разработчику.",
            "Копировать отчёт": "Скопировать список обработанных файлов, размеры и пути сохранения.",
            "Выбрать папку": "Указать папку для пакетного сохранения. Если не указана, файлы сохраняются рядом с исходными.",
            "Выбрать папку…": "Указать папку для пакетного сохранения. Если не указана, файлы сохраняются рядом с исходными.",
            "Папка…": "Указать папку результата. Пустое поле сохраняет файлы рядом с исходными.",
            "Папка сохранения": "Выбрать папку для результата.",
            "Выбрать…": "Выбрать исходный файл через стандартное окно открытия.",
            "Очистить список": "Удалить все записи из текущего списка без удаления исходных файлов.",
        }
        if text in tips:
            return tips[text]
        class_name = widget.winfo_class()
        if class_name in {"Listbox"}:
            return "Список поддерживает выбор нескольких строк; файлы можно перетаскивать мышью."
        if class_name in {"TCombobox"}:
            return "Выберите вариант из списка."
        if class_name in {"TEntry", "Entry"}:
            return "Поле можно заполнить вручную или через кнопку выбора рядом."
        if text:
            return f"Нажмите, чтобы выполнить действие: {text}"
        return ""

    def _handle_editor_drop(self, event) -> str:
        files = [Path(item).expanduser() for item in self.tk.splitlist(event.data)]
        self._remember_dialog_selection(files)
        supported = [path for path in files if path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES]
        if supported:
            self._append_editor_files(supported)
        else:
            self.status.set("В редактор можно перетащить PDF или изображение")
        return "break"

    def _handle_pdf_docx_drop(self, event) -> str:
        files = [str(Path(item).expanduser()) for item in self.tk.splitlist(event.data) if Path(item).suffix.lower() == ".pdf"]
        self._remember_dialog_selection(files)
        self._append_pdf_docx(files)
        return "break"

    def _handle_compress_drop(self, event) -> str:
        files = [Path(item).expanduser() for item in self.tk.splitlist(event.data)]
        self._remember_dialog_selection(files)
        pdfs = [str(path) for path in files if path.suffix.lower() == ".pdf" and path.exists()]
        if pdfs:
            self._append_compress_files(pdfs)
        else:
            self.status.set("Для сжатия перетащите PDF-файлы")
        return "break"

    def _handle_pdf_workspace_drop(self, event) -> str:
        files = [Path(item).expanduser() for item in self.tk.splitlist(event.data)]
        self._remember_dialog_selection(files)
        self._add_workspace_files(files)
        return "break"

    def _handle_pdf_images_drop(self, event) -> str:
        files = [Path(item).expanduser() for item in self.tk.splitlist(event.data)]
        self._remember_dialog_selection(files)
        pdfs = [path for path in files if path.suffix.lower() == ".pdf" and path.exists()]
        if pdfs:
            self.pdf_image_input.set(str(pdfs[0]))
            self.status.set(f"Выбран для преобразования в изображения: {pdfs[0].name}")
        else:
            self.status.set("Для преобразования в изображения перетащите PDF-файл")
        return "break"

    def _handle_images_drop(self, event) -> str:
        files = [str(Path(item).expanduser()) for item in self.tk.splitlist(event.data) if Path(item).suffix.lower() in IMAGE_SUFFIXES]
        self._remember_dialog_selection(files)
        if files:
            self._append_images(files)
        else:
            self.status.set("Для этого раздела перетащите изображения")
        return "break"

    def _handle_docx_drop(self, event) -> str:
        files = [str(Path(item).expanduser()) for item in self.tk.splitlist(event.data) if Path(item).suffix.lower() in {".docx", ".doc"}]
        self._remember_dialog_selection(files)
        if files:
            self._append_docx(files)
        else:
            self.status.set("Для этого раздела перетащите DOCX/DOC")
        return "break"

    def _handle_drop(self, event) -> str:
        files = [Path(item).expanduser() for item in self.tk.splitlist(event.data)]
        self._remember_dialog_selection(files)
        supported = [path for path in files if path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES]
        added = len(supported)
        if supported:
            self.notebook.select(self.editor_tab)
            self._append_editor_files(supported)
        docs = [str(path) for path in files if path.suffix.lower() in {".docx", ".doc"}]
        if docs:
            self._append_docx(docs)
        if not added and not docs:
            self.status.set("Поддерживаются PDF, изображения, DOCX/DOC")
        return "break"

    def _choose_pdf_files(self) -> None:
        files = filedialog.askopenfilenames(
            initialdir=self._dialog_initialdir(),
            filetypes=[
                ("PDF и изображения", "*.pdf *.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp"),
                ("PDF", "*.pdf"),
                ("Images", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp"),
            ]
        )
        self._remember_dialog_selection(files)
        self._add_workspace_files([Path(file) for file in files])

    def _add_workspace_files(self, files) -> int:
        pdfs = [str(Path(path)) for path in files if Path(path).suffix.lower() == ".pdf"]
        images = [str(Path(path)) for path in files if Path(path).suffix.lower() in IMAGE_SUFFIXES]
        added = 0
        if pdfs:
            added += self._add_pdf_files(pdfs)
        if images:
            added += self._add_workspace_images(images)
        return added

    def _add_pdf_files(self, files) -> int:
        added = 0
        for file_path in files:
            path = Path(file_path)
            if not path.exists() or path.suffix.lower() != ".pdf":
                continue
            try:
                reader = PdfReader(str(path))
                if reader.is_encrypted:
                    raise ValueError("PDF защищён паролем")
                count = len(reader.pages)
            except Exception as exc:
                messagebox.showwarning("PDF", f"Не удалось открыть {path.name}: {exc}")
                continue
            cached_source = snapshot_workspace_source(path)
            self.pages.extend(PageRef(path, index, count, cached_source=cached_source) for index in range(count))
            added += count
        if added:
            self._rebuild_page_grid()
            self.status.set(f"Добавлено страниц: {added}")
        return added

    def _add_workspace_images(self, files) -> int:
        added = 0
        for file_path in files:
            path = Path(file_path)
            if not path.exists() or path.suffix.lower() not in IMAGE_SUFFIXES:
                continue
            try:
                with Image.open(path) as image:
                    image.verify()
            except Exception as exc:
                messagebox.showwarning("Изображение", f"Не удалось открыть {path.name}: {exc}")
                continue
            cached_source = snapshot_workspace_source(path)
            self.pages.append(PageRef(path, 0, 1, cached_source=cached_source))
            added += 1
        if added:
            self._rebuild_page_grid()
            self.status.set(f"Добавлено страниц/изображений: {added}")
        return added

    def _rebuild_page_grid(self) -> None:
        tracks_progress = len(self.pages) >= 4 and not self._global_progress_active
        if tracks_progress:
            self._start_global_progress(
                f"Создание миниатюр: 0/{len(self.pages)}",
                determinate=True,
                maximum=len(self.pages),
            )
        for child in self.page_grid.winfo_children():
            child.destroy()
        self.thumbnails = []
        self.page_tiles = []
        columns = max(1, self.page_canvas.winfo_width() // 190)
        for index, page_ref in enumerate(self.pages):
            self.thumbnails.append(self._render_thumbnail(page_ref))
            tile = RoundedPanel(
                self.page_grid,
                fill=PANEL,
                outer_bg=PANEL,
                radius=11,
                border=CYAN if index in self.selected else "#D7E5EF",
                border_width=2,
                padx=8,
                pady=8,
            )
            row, col = divmod(index, columns)
            tile.grid(row=row, column=col, padx=8, pady=8, sticky="n")
            image_label = tk.Label(tile, image=self.thumbnails[-1], bg=PANEL, width=138, height=178)
            image_label.pack()
            label = self._page_label(index, page_ref)
            text_label = tk.Label(tile, text=label, bg=PANEL, fg=TEXT, justify="center", wraplength=150, font=("Arial", 9))
            text_label.pack(pady=(6, 0))
            bundle = {"tile": tile, "image": image_label, "label": text_label}
            self.page_tiles.append(bundle)
            self._bind_page_tile(bundle, index)
            if tracks_progress:
                self._update_global_progress(
                    index + 1,
                    len(self.pages),
                    f"Создание миниатюр: {index + 1}/{len(self.pages)}",
                )
        if tracks_progress:
            self._finish_global_progress()
        self._refresh_pdf_workspace_summary()
        self._queue_autosave()

    def _bind_page_tile(self, bundle: dict[str, object], index: int) -> None:
        for widget in (bundle["tile"], bundle["image"], bundle["label"]):
            widget.page_index = index
            widget.bind("<ButtonPress-1>", lambda event, i=index: self._start_page_drag(event, i))
            widget.bind("<ButtonRelease-1>", self._finish_page_drag)
            widget.bind("<Button-3>", lambda event, i=index: self._show_page_context_menu(event, i))
            self._bind_canvas_wheel(widget, self.page_canvas)

    def _reflow_page_tiles(self, flash_index: int | None = None) -> None:
        columns = max(1, self.page_canvas.winfo_width() // 190)
        for index, (page_ref, bundle) in enumerate(zip(self.pages, self.page_tiles)):
            tile = bundle["tile"]
            image_label = bundle["image"]
            text_label = bundle["label"]
            row, col = divmod(index, columns)
            tile.grid_configure(row=row, column=col)
            tile.set_panel_border(CYAN if index in self.selected else "#D7E5EF")
            image_label.configure(image=self.thumbnails[index])
            text_label.configure(text=self._page_label(index, page_ref))
            self._bind_page_tile(bundle, index)
        self._refresh_pdf_workspace_summary()
        if flash_index is not None and 0 <= flash_index < len(self.page_tiles):
            tile = self.page_tiles[flash_index]["tile"]
            tile.set_panel_border("#58C6E7")
            self.after(180, lambda: self._refresh_page_tile_highlight(flash_index))

    def _refresh_page_tile_highlight(self, index: int) -> None:
        if 0 <= index < len(self.page_tiles):
            self.page_tiles[index]["tile"].set_panel_border(CYAN if index in self.selected else "#D7E5EF")

    def _refresh_pdf_workspace_summary(self) -> None:
        selected_count = len(self.selected)
        self.pdf_workspace_summary.set(
            f"Страниц: {len(self.pages)}" + (f" · Выбрано: {selected_count}" if selected_count else "")
            if self.pages
            else "ⓘ  Рабочая область пуста"
        )
        if hasattr(self, "drop_label"):
            if self.pages:
                self.drop_label.grid()
            else:
                self.drop_label.grid_remove()
        if hasattr(self, "page_scrollbar"):
            if self.pages:
                self.page_scrollbar.grid()
            else:
                self.page_scrollbar.grid_remove()
        self._draw_pdf_empty_state()

    def _draw_pdf_empty_state(self) -> None:
        if not hasattr(self, "page_canvas"):
            return
        canvas = self.page_canvas
        canvas.delete("pdf_empty")
        if self.pages:
            return
        width = max(640, canvas.winfo_width())
        height = max(410, canvas.winfo_height())
        margin_x = 24
        margin_y = 22
        create_rounded_rectangle(
            canvas,
            margin_x,
            margin_y,
            width - margin_x,
            height - margin_y,
            radius=22,
            fill=DROP_ZONE_BG,
            outline="#BCD2F1",
            width=2,
            dash=(6, 5),
            tags="pdf_empty",
        )
        center_x = width / 2
        center_y = height / 2
        if hasattr(self, "drop_zone_icon_image"):
            canvas.create_image(
                center_x,
                center_y - 82,
                image=self.drop_zone_icon_image,
                tags="pdf_empty",
            )
        canvas.create_text(
            center_x,
            center_y + 22,
            text="Перетащите PDF или изображения",
            fill=TEXT,
            font=(FONT_FAMILY, 17, "bold"),
            tags="pdf_empty",
        )
        canvas.create_text(
            center_x,
            center_y + 62,
            text="Перетащите файлы сюда или нажмите «Добавить файл»\nдля выбора на компьютере",
            fill=MUTED,
            font=(FONT_FAMILY, 10),
            justify="center",
            tags="pdf_empty",
        )

    def _render_thumbnail(self, page_ref: PageRef) -> ImageTk.PhotoImage | None:
        try:
            if self._is_image_ref(page_ref):
                with Image.open(page_ref.source) as source_image:
                    image = ImageOps.exif_transpose(source_image).convert("RGB")
                    if page_ref.rotation:
                        image = image.rotate(-page_ref.rotation, expand=True)
                    image.thumbnail((150, 190))
                    canvas = Image.new("RGB", (150, 190), "white")
                    canvas.paste(image, ((150 - image.width) // 2, (190 - image.height) // 2))
                    return ImageTk.PhotoImage(canvas)
            import fitz

            with fitz.open(str(page_ref.source)) as doc:
                page = doc.load_page(page_ref.page_index)
                zoom = min(150 / page.rect.width, 190 / page.rect.height)
                pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
                image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
                if page_ref.rotation:
                    image = image.rotate(-page_ref.rotation, expand=True)
                canvas = Image.new("RGB", (150, 190), "white")
                image.thumbnail((150, 190))
                canvas.paste(image, ((150 - image.width) // 2, (190 - image.height) // 2))
                return ImageTk.PhotoImage(canvas)
        except Exception:
            image = Image.new("RGB", (150, 190), "#F8FBFD")
            return ImageTk.PhotoImage(image)

    def _is_image_ref(self, page_ref: PageRef) -> bool:
        return page_ref.source.suffix.lower() in IMAGE_SUFFIXES

    def _page_label(self, index: int, page_ref: PageRef) -> str:
        if page_ref.label:
            label = f"{index + 1}. {page_ref.label}"
        elif self._is_image_ref(page_ref):
            label = f"{index + 1}. {page_ref.source.name}\nизображение"
        else:
            label = f"{index + 1}. {page_ref.source.name}\nстр. {page_ref.page_index + 1}/{page_ref.page_count}"
        if page_ref.rotation:
            label += f"\nповорот {page_ref.rotation}°"
        return label

    def _toggle_page(self, index: int, extend: bool = False, toggle: bool = False) -> None:
        self.selected = select_page_indexes(
            index,
            self.last_selected_index,
            extend,
            toggle,
            self.selected,
        )
        if not extend:
            self.last_selected_index = index
        self._reflow_page_tiles()

    def _clear_page_selection_from_background(self, event=None) -> None:
        self._dismiss_pdf_context_menu()
        if event is not None and hasattr(event.widget, "page_index"):
            return
        if self.selected:
            self.selected.clear()
            self.last_selected_index = None
            self._reflow_page_tiles()
            self.status.set("Выбор страниц снят")

    def _is_shift_event(self, event) -> bool:
        return bool(getattr(event, "state", 0) & 0x0001)

    def _is_control_event(self, event) -> bool:
        return bool(getattr(event, "state", 0) & 0x0004)

    def _start_page_drag(self, event, index: int) -> None:
        self.drag_source_index = index
        self.drag_start_xy = (event.x_root, event.y_root)

    def _finish_page_drag(self, event) -> None:
        source = self.drag_source_index
        start_xy = self.drag_start_xy
        self.drag_source_index = None
        self.drag_start_xy = None
        if source is None:
            return
        moved = bool(start_xy and (abs(event.x_root - start_xy[0]) > 8 or abs(event.y_root - start_xy[1]) > 8))
        target = self._page_index_at(event.x_root, event.y_root)
        if moved and target is not None and target != source:
            self._move_page(source, target)
            return
        self._toggle_page(
            source,
            extend=self._is_shift_event(event),
            toggle=self._is_control_event(event),
        )

    def _page_index_at(self, x_root: int, y_root: int) -> int | None:
        widget = self.winfo_containing(x_root, y_root)
        while widget is not None:
            if hasattr(widget, "page_index"):
                return getattr(widget, "page_index")
            widget = getattr(widget, "master", None)
        return None

    def _move_page(self, source: int, target: int) -> None:
        if source < 0 or source >= len(self.pages) or target < 0 or target >= len(self.pages):
            return
        old_order = list(range(len(self.pages)))
        item = old_order.pop(source)
        old_order.insert(target, item)
        old_pages = list(self.pages)
        old_last_selected_index = self.last_selected_index
        self.pages = [old_pages[index] for index in old_order]
        old_tiles = list(self.page_tiles)
        old_thumbnails = list(self.thumbnails)
        self.page_tiles = [old_tiles[index] for index in old_order]
        self.thumbnails = [old_thumbnails[index] for index in old_order]
        self.selected = {new_index for new_index, old_index in enumerate(old_order) if old_index in self.selected}
        if old_last_selected_index is not None:
            self.last_selected_index = next(
                (new_index for new_index, old_index in enumerate(old_order) if old_index == old_last_selected_index),
                None,
            )
        self._reflow_page_tiles(flash_index=target)
        self._queue_autosave()
        self.status.set("Порядок страниц изменён")

    def _rotate_selected_pages(self, degrees: int) -> None:
        if not self.selected:
            messagebox.showinfo("PDF", "Выберите страницы для поворота")
            return
        for index in list(self.selected):
            ref = self.pages[index]
            self.pages[index] = replace(ref, rotation=(ref.rotation + degrees) % 360)
        self._rebuild_page_grid()
        self.status.set("Выбранные страницы повернуты")

    def _duplicate_selected_pages(self) -> None:
        if not self.selected:
            messagebox.showinfo("PDF", "Выберите страницы для дублирования")
            return
        self.pages, duplicate_indexes = duplicate_selected_page_refs(self.pages, self.selected)
        self.selected = duplicate_indexes
        self.last_selected_index = max(duplicate_indexes) if duplicate_indexes else None
        self._rebuild_page_grid()
        self.status.set(f"Создано копий страниц: {len(duplicate_indexes)}")

    def _show_page_context_menu(self, event, index: int) -> None:
        self._dismiss_pdf_context_menu()
        had_selection = bool(self.selected)
        if index not in self.selected:
            self.selected = {index}
            self.last_selected_index = index
            self._rebuild_page_grid()
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(
            label="Открыть в PDF Редакторе",
            command=lambda selected_only=had_selection: self._send_pages_to_visual_editor(selected_only),
        )
        menu.add_command(label="Сохранить выделенные страницы…", command=self._save_selected_pages)
        menu.add_command(label="Печатать документ…", command=self._print_pdf_document)
        menu.add_command(label="Печатать выделенные страницы…", command=self._print_selected_pdf_pages)
        menu.add_separator()
        menu.add_command(label="Дублировать выбранные", command=self._duplicate_selected_pages)
        menu.add_command(label="Повернуть влево", command=lambda: self._rotate_selected_pages(-90))
        menu.add_command(label="Повернуть вправо", command=lambda: self._rotate_selected_pages(90))
        blank_menu = tk.Menu(menu, tearoff=False)
        blank_menu.add_command(label="Перед выбранной", command=lambda: self._insert_blank_page(index))
        blank_menu.add_command(label="После выбранной", command=lambda: self._insert_blank_page(index + 1))
        menu.add_cascade(label="Вставить пустую страницу", menu=blank_menu)
        menu.add_separator()
        menu.add_command(label="Удалить выбранные", command=self._delete_selected_pages)
        self.pdf_context_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _show_workspace_context_menu(self, event) -> None:
        if self._page_index_at(event.x_root, event.y_root) is not None:
            return
        self._dismiss_pdf_context_menu()
        menu = tk.Menu(self, tearoff=False)
        menu.add_command(label="Добавить пустую страницу…", command=lambda: self._insert_blank_page(len(self.pages)))
        if self.pages:
            menu.add_separator()
            menu.add_command(label="Открыть в PDF Редакторе", command=self._send_pages_to_visual_editor)
            menu.add_command(label="Печатать документ…", command=self._print_pdf_document)
        self.pdf_context_menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _insert_blank_page(self, index: int) -> None:
        dialog = BlankPageDialog(self)
        if not dialog.result:
            return
        page_size, orientation = dialog.result
        try:
            source = create_blank_pdf(app_cache_dir("blank-pages"), page_size, orientation)
        except Exception as exc:
            self._show_error(exc)
            return
        orientation_label = "книжная" if orientation == "portrait" else "альбомная"
        index = max(0, min(index, len(self.pages)))
        self.pages.insert(index, PageRef(source, 0, 1, label=f"Пустая страница · {page_size} · {orientation_label}"))
        self.selected = {index}
        self.last_selected_index = index
        self._rebuild_page_grid()
        self.status.set(f"Добавлена пустая страница {page_size}, {orientation_label}")

    def _on_canvas_resize(self, event) -> None:
        self.page_canvas.itemconfigure(self.page_canvas_window, width=event.width)
        self._draw_pdf_empty_state()
        if self.page_reflow_after:
            self.after_cancel(self.page_reflow_after)
        self.page_reflow_after = self.after(80, self._reflow_page_tiles)

    def _selected_refs(self) -> list[PageRef]:
        return [self.pages[index] for index in sorted(self.selected)]

    def _save_pdf_document(self) -> None:
        if not self.pages:
            messagebox.showinfo("PDF", "Добавьте PDF-файлы или изображения")
            return
        default_output = self._default_pdf_output()
        selected = filedialog.asksaveasfilename(
            initialfile=default_output.name,
            initialdir=str(default_output.parent),
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
        )
        if not selected:
            return
        output = Path(selected)
        self._remember_dialog_selection(output)
        self._run_task(lambda: self._write_page_refs(self.pages, output), 'Документ сохранён')

    def _print_current_section(self, _event=None) -> str:
        if getattr(self.notebook, "_current", None) is self.editor_tab:
            self._print_editor_document()
        return "break"

    def _print_pdf_document(self) -> None:
        self._print_page_refs(list(self.pages), "документ")

    def _print_selected_pdf_pages(self) -> None:
        self._print_page_refs(self._selected_refs(), "выбранные-страницы")

    def _print_page_refs(self, refs: list[PageRef], label: str) -> None:
        if not refs:
            messagebox.showinfo("Печать", "Добавьте документ или выберите страницы для печати")
            return
        dialog = PrintDialog(self, len(refs))
        if not dialog.result:
            return
        options = dialog.result

        def print_job() -> str:
            output = temporary_print_path(label)
            try:
                self._write_page_refs(refs, output)
                return submit_pdf_print_job(output, options)
            finally:
                if os.name != "nt":
                    remove_temporary_print_file(output)

        self._run_task(print_job, 'Документ отправлен на печать')

    def _save_selected_pages(self) -> None:
        refs = self._selected_refs()
        if not refs:
            messagebox.showinfo("PDF", "Выберите одну или несколько страниц")
            return
        output = self._default_selected_output(refs)
        output = filedialog.asksaveasfilename(initialfile=output.name, initialdir=str(output.parent), defaultextension=".pdf", filetypes=[("PDF", "*.pdf")])
        if output:
            self._remember_dialog_selection(output)
            self._run_task(lambda: self._write_page_refs(refs, Path(output)), 'Выбранные страницы сохранены')

    def _send_pages_to_visual_editor(self, selected_only: bool | None = None) -> None:
        refs = page_refs_for_editor(self.pages, self.selected, selected_only)
        if not refs:
            messagebox.showinfo("PDF", "Сначала добавьте PDF или изображения")
            return
        transfer_dir = app_cache_dir("editor-transfer")
        cleanup_cache_files(
            transfer_dir,
            ("pdf-pages-to-editor-*.pdf",),
            max_age_seconds=7 * 86400,
            max_total_bytes=512 * 1024 * 1024,
            protect_recent_seconds=3600,
        )
        destination = transfer_dir / f"pdf-pages-to-editor-{time.time_ns()}.pdf"

        def open_in_editor(result) -> None:
            path = Path(result)
            self.selected.clear()
            self.last_selected_index = None
            self._rebuild_page_grid()
            self.notebook.select(self.editor_tab)
            self._append_editor_files([path])

        self._run_task(lambda: self._write_page_refs(refs, destination), 'Страницы переданы в PDF Редактор', on_success=open_in_editor)

    def _delete_selected_pages(self) -> None:
        if not self.selected:
            return
        self.pages = [page for index, page in enumerate(self.pages) if index not in self.selected]
        self.selected.clear()
        self.last_selected_index = None
        self._rebuild_page_grid()
        self.status.set("Выбранные страницы удалены из рабочей области")

    def _clear_pdf_workspace(self) -> None:
        self.pages.clear()
        self.selected.clear()
        self.last_selected_index = None
        self.thumbnails.clear()
        self._rebuild_page_grid()
        self.status.set("Рабочая область очищена")

    def _write_page_refs(self, refs: list[PageRef], output: Path) -> Path:
        resolved_sources: dict[PageRef, Path] = {}
        missing_sources: list[Path] = []
        for ref in refs:
            source = resolve_page_ref_source(ref)
            if source is None:
                missing_sources.append(ref.source)
            else:
                resolved_sources[ref] = source
        if missing_sources:
            raise FileNotFoundError(missing_workspace_sources_message(missing_sources))

        writer = PdfWriter()
        readers: dict[Path, PdfReader] = {}
        for ref in refs:
            source = resolved_sources[ref]
            if self._is_image_ref(ref):
                reader = PdfReader(self._image_ref_to_pdf_stream(ref, source))
                writer.add_page(reader.pages[0])
            else:
                if source not in readers:
                    readers[source] = PdfReader(str(source))
                writer.add_page(readers[source].pages[ref.page_index])
            if ref.rotation:
                writer.pages[-1].rotate(ref.rotation)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("wb") as handle:
            writer.write(handle)
        return output

    def _image_ref_to_pdf_stream(self, ref: PageRef, source_path: Path | None = None) -> BytesIO:
        buffer = BytesIO()
        with Image.open(source_path or ref.source) as source_image:
            image = ImageOps.exif_transpose(source_image)
            if image.mode == "RGB":
                page = image.copy()
            elif image.mode in {"RGBA", "LA"} or "transparency" in image.info:
                canvas = image.convert("RGBA")
                base = Image.new("RGBA", canvas.size, "white")
                base.alpha_composite(canvas)
                page = base.convert("RGB")
            else:
                page = image.convert("RGB")
            page.save(buffer, "PDF", resolution=150)
            page.close()
        buffer.seek(0)
        return buffer

    def _default_pdf_output(self) -> Path:
        source = next((ref.source for ref in self.pages if not ref.label), None)
        return (source.parent if source else usable_last_folder(self.preferences)) / "pdf_combined.pdf"

    def _default_selected_output(self, refs: list[PageRef]) -> Path:
        if len(refs) == 1:
            ref = refs[0]
            if not ref.label:
                return ref.source.parent / f"{ref.source.stem}_page_{ref.page_index + 1}.pdf"
        source = next((ref.source for ref in refs if not ref.label), None)
        return (source.parent if source else usable_last_folder(self.preferences)) / "selected_pages.pdf"

    def _append_images(self, files: list[str]) -> None:
        added = 0
        for file_path in files:
            file_path = str(Path(file_path).expanduser())
            if file_path not in self.image_files:
                self.image_files.append(file_path)
                if hasattr(self, "image_list"):
                    self.image_list.insert(tk.END, file_path)
                added += 1
        self.image_files_summary.set(f"Файлов: {len(self.image_files)}" if self.image_files else "Файлы не добавлены")
        self._sync_file_drop_states()
        if added:
            self.status.set(f"Добавлено изображений: {added}")

    def _append_docx(self, files: list[str]) -> None:
        added = 0
        for file_path in files:
            if file_path not in self.docx_files:
                self.docx_files.append(file_path)
                if hasattr(self, "docx_list"):
                    self.docx_list.insert(tk.END, file_path)
                added += 1
        self.docx_files_summary.set(f"Файлов: {len(self.docx_files)}" if self.docx_files else "Файлы не добавлены")
        self._sync_file_drop_states()
        if added:
            self.status.set(f"Добавлено документов: {added}")

    def _append_pdf_docx(self, files: list[str]) -> None:
        added = 0
        for file_path in files:
            path = Path(file_path)
            value = str(path)
            if path.suffix.lower() == ".pdf" and path.exists() and value not in self.pdf_docx_files:
                self.pdf_docx_files.append(value)
                if hasattr(self, "pdf_docx_list"):
                    self.pdf_docx_list.insert(tk.END, value)
                added += 1
        self.pdf_docx_files_summary.set(f"Файлов: {len(self.pdf_docx_files)}" if self.pdf_docx_files else "Файлы не добавлены")
        self._sync_file_drop_states()
        if added:
            self.status.set(f"Добавлено PDF: {added}")

    def _append_compress_files(self, files: list[str]) -> None:
        added = 0
        for file_path in files:
            path = Path(file_path)
            if path.suffix.lower() != ".pdf" or not path.exists():
                continue
            value = str(path)
            if value not in self.compress_files:
                self.compress_files.append(value)
                if hasattr(self, "compress_list"):
                    self.compress_list.insert(tk.END, value)
                added += 1
        if added:
            self.status.set(f"Добавлено PDF для сжатия: {added}")
        self.compress_files_summary.set(f"Файлов: {len(self.compress_files)}" if self.compress_files else "Файлы не добавлены")
        self._sync_file_drop_states()
        self._update_compression_estimate()

    def _add_image_files(self) -> None:
        files = filedialog.askopenfilenames(initialdir=self._dialog_initialdir(), filetypes=[("Images", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp")])
        self._remember_dialog_selection(files)
        self._append_images(list(files))

    def _remove_image_files(self) -> None:
        for index in reversed(self.image_list.curselection()):
            self.image_list.delete(index)
            del self.image_files[index]
        self.image_files_summary.set(f"Файлов: {len(self.image_files)}" if self.image_files else "Файлы не добавлены")
        self._sync_file_drop_states()

    def _run_images(self) -> None:
        if not self.image_files:
            messagebox.showinfo("Изображения", "Добавьте изображения")
            return
        sources = [str(Path(value).expanduser()) for value in self.image_files]
        one_per_file = self.image_one_per_file.get()
        dpi = self.image_dpi.get()
        first_dir = Path(sources[0]).parent
        output_file: Path | None = None
        output_dir: Path | None = None
        if one_per_file:
            output_dir = Path(self.image_output_dir.get().strip()).expanduser() if self.image_output_dir.get().strip() else first_dir
        else:
            default_name = f"{Path(self.image_files[0]).stem}_images.pdf" if len(self.image_files) > 1 else f"{Path(self.image_files[0]).stem}.pdf"
            selected = filedialog.asksaveasfilename(
                initialfile=default_name,
                initialdir=str(first_dir),
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf")],
            )
            if not selected:
                return
            output_file = Path(selected)
            self._remember_dialog_selection(output_file)

        def task():
            if one_per_file:
                return images_to_individual_pdfs(sources, output_dir or first_dir, dpi)
            return images_to_pdf(sources, output_file or (first_dir / "images.pdf"), dpi)

        self._run_task(task, 'PDF из изображений создан', source_paths=list(self.image_files))

    def _run_pdf_to_images(self) -> None:
        input_path = Path(self.pdf_image_input.get().strip())
        if not input_path.exists():
            messagebox.showinfo("PDF в изображения", "Выберите PDF")
            return
        output_dir = Path(self.pdf_image_output_dir.get().strip()) if self.pdf_image_output_dir.get().strip() else input_path.parent
        self._run_task(lambda: pdf_to_images(input_path, output_dir, self.pdf_image_format.get(), self.pdf_image_dpi.get()), 'PDF преобразован в изображения', source_paths=[input_path])

    def _add_docx_files(self) -> None:
        files = filedialog.askopenfilenames(initialdir=self._dialog_initialdir(), filetypes=[("Word documents", "*.docx *.doc")])
        self._remember_dialog_selection(files)
        self._append_docx(list(files))

    def _remove_docx_files(self) -> None:
        for index in reversed(self.docx_list.curselection()):
            self.docx_list.delete(index)
            del self.docx_files[index]
        self.docx_files_summary.set(f"Файлов: {len(self.docx_files)}" if self.docx_files else "Файлы не добавлены")
        self._sync_file_drop_states()

    def _add_pdf_docx_files(self) -> None:
        files = filedialog.askopenfilenames(initialdir=self._dialog_initialdir(), filetypes=[("PDF", "*.pdf")])
        self._remember_dialog_selection(files)
        self._append_pdf_docx(list(files))

    def _remove_pdf_docx_files(self) -> None:
        for index in reversed(self.pdf_docx_list.curselection()):
            self.pdf_docx_list.delete(index)
            del self.pdf_docx_files[index]
        self.pdf_docx_files_summary.set(f"Файлов: {len(self.pdf_docx_files)}" if self.pdf_docx_files else "Файлы не добавлены")
        self._sync_file_drop_states()

    def _add_compress_files(self) -> None:
        files = filedialog.askopenfilenames(initialdir=self._dialog_initialdir(), filetypes=[("PDF", "*.pdf")])
        self._remember_dialog_selection(files)
        self._append_compress_files(list(files))

    def _remove_compress_files(self) -> None:
        for index in reversed(self.compress_list.curselection()):
            self.compress_list.delete(index)
            del self.compress_files[index]
        self.compress_files_summary.set(f"Файлов: {len(self.compress_files)}" if self.compress_files else "Файлы не добавлены")
        self._sync_file_drop_states()
        self._update_compression_estimate()

    def _clear_compress_files(self) -> None:
        self.compress_files.clear()
        if hasattr(self, "compress_list"):
            self.compress_list.delete(0, tk.END)
        self.compress_files_summary.set("Файлы не добавлены")
        self._sync_file_drop_states()
        self.status.set("Список сжатия очищен")
        self._update_compression_estimate()

    def _start_documents_progress(self, text: str) -> None:
        self.documents_progress_value.set(0)
        self.documents_progress_bar.configure(maximum=1)
        self.documents_progress_text.set(text)
        self._start_global_progress(text, determinate=True, maximum=1)
        self.docx_convert_button.configure(state="disabled")
        self.pdf_docx_convert_button.configure(state="disabled")

    def _update_documents_progress(self, current: int, total: int, text: str) -> None:
        def update() -> None:
            self.documents_progress_bar.configure(maximum=max(1, int(total)))
            self.documents_progress_value.set(max(0, min(int(current), max(1, int(total)))))
            self.documents_progress_text.set(text)
            self._update_global_progress(current, total, text)

        self.after(0, update)

    def _finish_documents_progress(self) -> None:
        maximum = max(1.0, float(self.documents_progress_bar.cget("maximum")))
        self.documents_progress_value.set(maximum)
        self.documents_progress_text.set("Преобразование завершено")
        self._finish_global_progress()
        self.docx_convert_button.configure(state="normal")
        self.pdf_docx_convert_button.configure(state="normal")

    def _run_docx(self) -> None:
        if not self.docx_files:
            messagebox.showinfo("DOCX", "Добавьте DOCX/DOC")
            return
        output_dir = Path(self.docx_output_dir.get().strip()) if self.docx_output_dir.get().strip() else Path(self.docx_files[0]).parent
        self._start_documents_progress("Подготовка DOCX / DOC")
        self._run_task(lambda: docx_to_pdf(self.docx_files, output_dir, progress=self._update_documents_progress), 'DOCX сконвертированы', source_paths=list(self.docx_files), on_complete=self._finish_documents_progress)

    def _run_pdf_docx(self) -> None:
        if not self.pdf_docx_files:
            messagebox.showinfo("PDF в DOCX", "Добавьте PDF")
            return
        output_dir = Path(self.pdf_docx_output_dir.get().strip()) if self.pdf_docx_output_dir.get().strip() else Path(self.pdf_docx_files[0]).parent
        self._start_documents_progress("Подготовка PDF")
        if self.pdf_docx_pre_ocr.get():
            self._run_task(lambda: pdfs_to_layout_ocr_docx(self.pdf_docx_files, output_dir, language=self.pdf_docx_ocr_language.get(), preserve_graphics=self.pdf_docx_preserve_graphics.get(), detect_tables=self.pdf_docx_detect_tables.get(), progress=self._update_documents_progress), 'Распознанный текст сохранён в DOCX', source_paths=list(self.pdf_docx_files), on_complete=self._finish_documents_progress)
            return
        self._run_task(lambda: pdf_to_docx(self.pdf_docx_files, output_dir, progress=self._update_documents_progress), f"PDF преобразованы в DOCX через {('Microsoft Word' if os.name == 'nt' else 'R7-Office')}", source_paths=list(self.pdf_docx_files), on_complete=self._finish_documents_progress)

    def _run_compress(self) -> None:
        if not self.compress_files:
            messagebox.showinfo("Сжатие", "Добавьте PDF для сжатия")
            return
        preset = self.compress_preset.get()
        color_mode = "color"
        sources = [Path(value) for value in self.compress_files]
        output_dir_value = self.compress_output_dir.get().strip()
        if len(self.compress_files) == 1 and not output_dir_value:
            input_path = Path(self.compress_files[0])
            default_output = available_output_path(input_path.with_name(f"{input_path.stem}_compressed.pdf"))
            selected = filedialog.asksaveasfilename(
                initialfile=default_output.name,
                initialdir=str(default_output.parent),
                defaultextension=".pdf",
                filetypes=[("PDF", "*.pdf")],
            )
            if not selected:
                return
            output = Path(selected)
            self._remember_dialog_selection(output)
            self._run_task(lambda: compress_pdf_preset(input_path, output, preset, color_mode), 'Сжатие завершено', source_paths=sources, on_success=lambda result: self._show_compression_result(sources, [result]), quiet=True)
            return

        output_dir = Path(output_dir_value) if output_dir_value else None
        self._run_task(lambda: self._compress_files_batch(output_dir, preset, color_mode, input_paths=sources), 'Сжатие завершено', source_paths=sources, on_success=lambda result: self._show_compression_result(sources, result), quiet=True)

    def _compress_files_batch(
        self,
        output_dir: Path | None,
        preset: str,
        color_mode: str = "color",
        target_mb: float | None = None,
        input_paths: list[Path] | None = None,
    ) -> list[Path] | BatchTargetCompressionResult:
        input_paths = input_paths if input_paths is not None else [Path(file_path) for file_path in self.compress_files]
        if target_mb is not None:
            output_paths = [
                (output_dir or input_path.parent) / f"{input_path.stem}_to_{target_mb:g}MB.pdf"
                for input_path in input_paths
            ]
            return compress_pdfs_to_total_size(input_paths, output_paths, target_mb, color_mode)

        outputs: list[Path] = []
        for input_path in input_paths:
            target_dir = output_dir or input_path.parent
            output = available_output_path(target_dir / f"{input_path.stem}_compressed.pdf")
            outputs.append(compress_pdf_preset(input_path, output, preset, color_mode))
        return outputs

    def _show_compression_result(self, sources: list[Path], outputs: list[Path]) -> None:
        unchanged = [source.name for source, output in zip(sources, outputs)
                     if Path(output).stat().st_size >= source.stat().st_size]
        note = ""
        title = "Сжатие завершено"
        if unchanged:
            title = "Сжатие завершено с предупреждением"
            note = ("Выбранный режим не уменьшил размер следующих файлов: " + ", ".join(unchanged) +
                    ". Дальнейшее сжатие этими настройками невозможно; сохранены копии исходных файлов "
                    "без ухудшения качества.")
        self.status.set(title)
        self._show_batch_report(sources, [Path(output) for output in outputs], title, note)

    def _run_task(
        self,
        callback,
        success_text: str,
        source_paths=None,
        on_success=None,
        on_complete=None,
        *,
        quiet: bool = False,
    ) -> None:
        self._background_task_count = getattr(self, "_background_task_count", 0) + 1
        owns_progress = not self._global_progress_active
        if owns_progress:
            self._start_global_progress("Выполняется…")
        else:
            self.status.set("Выполняется…")
        sources = [Path(value) for value in (source_paths or [])]
        started_at = time.perf_counter()

        def worker() -> None:
            try:
                result = callback()
            except Exception as exc:
                error = exc
                technical_details = traceback.format_exc()
                notice = user_notice(exc, sources)
                if owns_progress:
                    self.after(0, self._finish_global_progress)
                if on_complete is not None:
                    self.after(0, on_complete)
                self.after(0, lambda: self._show_error(notice or error, technical_details))
            else:
                if owns_progress:
                    self.after(0, self._finish_global_progress)
                if on_complete is not None:
                    self.after(0, on_complete)
                self.after(0, lambda: self._task_succeeded(result, success_text, sources, on_success, quiet))
            self.after(0, self._background_task_finished)

        threading.Thread(target=worker, daemon=True).start()

    def _background_task_finished(self):
        self._background_task_count = max(0, getattr(self, "_background_task_count", 0) - 1)


    def _task_succeeded(
        self,
        result,
        success_text: str,
        source_paths: list[Path],
        on_success=None,
        quiet: bool = False,
    ) -> None:
        if on_success is not None:
            on_success(result)
        if quiet:
            return
        self._show_success(result, success_text, source_paths)

    def _show_error(self, exc: Exception, technical_details: str | None = None) -> None:
        notice = user_notice(exc)
        if notice is not None:
            self.status.set("Запись в выбранную папку невозможна" if is_permission_denied(exc) else notice.title)
            messagebox.showwarning(notice.title, str(notice), parent=self)
            return
        self.status.set("Ошибка")
        trace_text = technical_details or traceback.format_exc()
        if trace_text.strip() == "NoneType: None":
            trace_text = "Техническая трассировка недоступна."
        details = (
            f"pdf bars {__version__}\n"
            f"Python {platform.python_version()}\n"
            f"Система: {platform.platform()}\n"
            f"Ошибка: {type(exc).__name__}: {exc}\n\n"
            f"{trace_text}"
        )
        window = tk.Toplevel(self)
        window.title("Ошибка")
        window.geometry("700x430")
        window.minsize(540, 320)
        window.transient(self)
        window.configure(bg=BG)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        tk.Label(window, text=str(exc), bg=BG, fg="#9D1C1C", font=("Arial", 11, "bold"), wraplength=640, justify="left").grid(
            row=0, column=0, sticky="ew", padx=18, pady=(16, 8)
        )
        holder = tk.Frame(window, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        holder.grid(row=1, column=0, sticky="nsew", padx=18)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        text = tk.Text(holder, wrap="word", bg=PANEL, fg=TEXT, relief="flat", font=("DejaVu Sans Mono", 9))
        text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", details)
        text.configure(state="disabled")
        buttons = tk.Frame(window, bg=BG)
        buttons.grid(row=2, column=0, sticky="ew", padx=18, pady=14)
        ttk.Button(buttons, text="Копировать техническую информацию", command=lambda: self._copy_text(details)).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=window.destroy, style="Accent.TButton").pack(side="right")
        self._install_tooltips()

    def _copy_text(self, value: str) -> None:
        self.clipboard_clear()
        self.clipboard_append(value)
        self.update_idletasks()
        self.status.set("Техническая информация скопирована")

    def _show_success(self, result, message: str, source_paths: list[Path] | None = None) -> None:
        sources = source_paths or []
        if isinstance(result, TargetCompressionResult):
            if sources:
                self.status.set("PDF сжат" if result.achieved else "Сжатие завершено, лимит не достигнут")
                note = "" if result.achieved else "Заданный размер недостижим без дальнейшего ухудшения читаемости."
                self._show_batch_report(sources, [result.path], message, note)
            else:
                self._show_target_compression_result(result)
            return
        if isinstance(result, BatchTargetCompressionResult):
            if result.achieved:
                self.status.set("PDF сжаты до общего заданного размера")
                note = f"Общий лимит: {result.target_mb:.2f} МБ · результат: {result.actual_mb:.2f} МБ"
            else:
                self.status.set("Сжатие завершено, общий лимит не достигнут")
                note = (
                    f"Общий лимит: {result.target_mb:.2f} МБ · результат: {result.actual_mb:.2f} МБ. "
                    "Сохранены самые компактные читаемые варианты."
                )
            self._show_batch_report(sources, [item.path for item in result.results], message, note)
            return
        if isinstance(result, list):
            target_results = [item for item in result if isinstance(item, TargetCompressionResult)]
            if target_results:
                achieved = sum(item.achieved for item in target_results)
                missed = len(target_results) - achieved
                self.status.set("Сжатие завершено" if not missed else "Сжатие завершено с предупреждениями")
                note = f"В заданный размер уложились: {achieved} из {len(target_results)}"
                if missed:
                    note += ". Для остальных файлов сохранён самый компактный читаемый вариант."
                self._show_batch_report(sources, [item.path for item in target_results], message, note)
                return
            self.status.set(message)
            if sources:
                self._show_batch_report(sources, [Path(item) for item in result], message)
            else:
                messagebox.showinfo("Готово", f"{message}\nСоздано файлов: {len(result)}")
        else:
            self.status.set(message)
            outputs = [Path(result)] if isinstance(result, (str, os.PathLike)) else []
            if sources and outputs:
                self._show_batch_report(sources, outputs, message)
            else:
                messagebox.showinfo("Готово", f"{message}\n{result}")

    def _show_batch_report(self, source_paths: list[Path], output_paths: list[Path], title: str, note: str = "") -> None:
        sources = [Path(path) for path in source_paths]
        outputs = [Path(path) for path in output_paths]
        lines = [title, f"Обработано результатов: {len(outputs)}"]
        if note:
            lines.append(note)
        lines.append("")

        if len(sources) == len(outputs):
            pairs = list(zip(sources, outputs))
        elif len(sources) == 1:
            pairs = [(sources[0], output) for output in outputs]
        elif len(outputs) == 1:
            source_text = ", ".join(path.name for path in sources)
            source_size = sum(path.stat().st_size for path in sources if path.exists())
            output = outputs[0]
            output_size = output.stat().st_size if output.exists() else 0
            lines.extend(
                (
                    f"Исходные файлы: {source_text}",
                    f"Исходный объём: {self._format_file_size(source_size)}",
                    f"Результат: {output}",
                    f"Новый размер: {self._format_file_size(output_size)}",
                )
            )
            pairs = []
        else:
            pairs = [(None, output) for output in outputs]

        for index, (source, output) in enumerate(pairs, start=1):
            source_size = source.stat().st_size if source is not None and source.exists() else 0
            output_size = output.stat().st_size if output.exists() else 0
            saved = source_size - output_size
            lines.append(f"{index}. {source.name if source else 'Результат'}")
            if source_size:
                lines.append(f"   Исходный размер: {self._format_file_size(source_size)}")
            lines.append(f"   Новый размер: {self._format_file_size(output_size)}")
            if source_size:
                percent = saved / source_size * 100.0
                lines.append(f"   Изменение: {percent:+.1f}%")
            lines.append(f"   Сохранено: {output}")
            lines.append("")

        report = "\n".join(lines).rstrip()
        window = tk.Toplevel(self)
        window.title("Отчёт обработки")
        window.geometry("760x520")
        window.minsize(560, 340)
        window.transient(self)
        window.configure(bg=BG)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        tk.Label(window, text=title, bg=BG, fg=DEEP_BLUE, font=("Arial", 15, "bold")).grid(row=0, column=0, sticky="w", padx=18, pady=(16, 8))
        holder = tk.Frame(window, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        holder.grid(row=1, column=0, sticky="nsew", padx=18)
        holder.columnconfigure(0, weight=1)
        holder.rowconfigure(0, weight=1)
        text = tk.Text(holder, wrap="word", bg=PANEL, fg=TEXT, relief="flat", font=("DejaVu Sans Mono", 9))
        text.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        scrollbar = ttk.Scrollbar(holder, orient="vertical", command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", report)
        text.configure(state="disabled")
        buttons = tk.Frame(window, bg=BG)
        buttons.grid(row=2, column=0, sticky="ew", padx=18, pady=14)
        ttk.Button(buttons, text="Копировать отчёт", command=lambda: self._copy_text(report)).pack(side="left")
        ttk.Button(buttons, text="Закрыть", command=window.destroy, style="Accent.TButton").pack(side="right")
        self._install_tooltips()

    def _show_target_compression_result(self, result: TargetCompressionResult) -> None:
        details = (
            f"Целевой размер: {result.target_mb:.2f} МБ\n"
            f"Фактический размер: {result.actual_mb:.2f} МБ\n"
            f"Файл: {result.path}"
        )
        if result.achieved:
            self.status.set("PDF сжат до заданного размера")
            messagebox.showinfo("Готово", details)
        else:
            self.status.set("Сжатие завершено, лимит не достигнут")
            messagebox.showwarning(
                "Минимальный читаемый размер",
                f"{details}\n\nЗаданный объём недостижим с доступными настройками без дальнейшего ухудшения читаемости.",
            )


    def _choose_editor_file(self) -> None:
        values = filedialog.askopenfilenames(
            initialdir=self._dialog_initialdir(),
            filetypes=[
                ("PDF и изображения", "*.pdf *.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp"),
                ("PDF", "*.pdf"),
                ("Изображения", "*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp"),
            ]
        )
        if values:
            self._remember_dialog_selection(values)
            self._append_editor_files([Path(value) for value in values])

    def _choose_editor_pdf(self) -> None:
        self._choose_editor_file()

    def _create_blank_editor_document(self) -> None:
        dialog = BlankPageDialog(self)
        if not dialog.result:
            return
        page_size, orientation = dialog.result
        try:
            source = create_blank_pdf(app_cache_dir("blank-pages"), page_size, orientation)
        except Exception as exc:
            self._show_error(exc)
            return
        if self.editor_pdf_path:
            self._append_editor_files([source])
            return
        self._load_editor_pdf(source)
        if self.editor_pdf_path != source:
            return
        self.editor_page_source_names = ["Пустая страница"]
        self.editor_new_document = True
        self.editor_last_save_path = None
        self.editor_dirty = True
        self.editor_warning.set("")
        orientation_label = "книжная" if orientation == "portrait" else "альбомная"
        self.status.set(f"Создан новый лист {page_size}, {orientation_label}. Сохраните его под нужным именем.")
        self._queue_autosave()

    def _load_editor_file(self, path: Path) -> None:
        if self.editor_pdf_path and not self._confirm_discard_editor_changes():
            return
        original_name = path.name
        previous = self.editor_pdf_path
        try:
            path = self._retain_editor_source(input_path(path))
        except Exception as exc:
            self._show_error(exc)
            return
        suffix = path.suffix.lower()
        if suffix == ".pdf":
            self._load_editor_pdf(path)
        elif suffix in IMAGE_SUFFIXES:
            self._load_editor_image(path)
        else:
            messagebox.showinfo("PDF Редактор", "Поддерживаются PDF и основные форматы изображений")
        if self.editor_pdf_path == path or self.editor_pdf_path != previous:
            self.editor_page_source_names = [original_name] * self.editor_page_count

    def _retain_editor_source(self, path: Path) -> Path:
        import shutil

        temporary_roots = (Path(tempfile.gettempdir()).resolve(), Path("/tmp"), Path("/var/tmp"))
        resolved = path.resolve()
        if not any(directory == resolved.parent or directory in resolved.parents for directory in temporary_roots):
            return path
        destination = self._editor_cache_file(f"imported-{time.time_ns()}{path.suffix.lower()}")
        shutil.copyfile(path, destination)
        return destination

    def _append_editor_files(self, paths: list[Path]) -> None:
        if self.editor_pages_busy or self.editor_index_busy:
            self.status.set("Дождитесь завершения текущей операции")
            return
        supported = [
            Path(path).expanduser()
            for path in paths
            if Path(path).suffix.lower() == ".pdf" or Path(path).suffix.lower() in IMAGE_SUFFIXES
        ]
        if not supported:
            self.status.set("В редактор можно добавить PDF или изображение")
            return
        try:
            supported = [input_path(path) for path in supported]
        except Exception as exc:
            self._show_error(exc)
            return
        if not self.editor_pdf_path and len(supported) == 1:
            self._load_editor_file(supported[0])
            return

        previous_kind = self.editor_source_kind
        current_source = Path(self.editor_pdf_path) if self.editor_pdf_path else None
        destination = self._editor_cache_file(f"editor-combined-{time.time_ns()}.pdf")
        source_paths = ([current_source] if current_source else []) + supported
        current_page_names = list(self.editor_page_source_names)
        if current_source and len(current_page_names) != self.editor_page_count:
            current_page_names = [current_source.name] * self.editor_page_count
        appended_page_names = self._editor_page_names_for_sources(supported)
        try:
            supported = [self._retain_editor_source(path) for path in supported]
        except Exception as exc:
            self._show_error(exc)
            return
        source_paths = ([current_source] if current_source else []) + supported
        text_edits = list(self.editor_text_edits)
        signature_edits = list(self.editor_signature_edits)
        region_edits = list(self.editor_region_edits)
        stroke_edits = list(self.editor_stroke_edits)
        crop_rect = self._current_crop_rect()
        alignment_edits = list(self.editor_alignment_edits)

        def combine() -> Path:
            inputs = list(source_paths)
            if current_source is not None and previous_kind == "image":
                baked = destination.with_name(f"{destination.stem}-current.png")
                apply_image_edits(
                    current_source,
                    baked,
                    text_edits,
                    signature_edits,
                    region_edits,
                    stroke_edits,
                    crop_rect,
                    alignment_edits=alignment_edits,
                )
                inputs[0] = baked
            return combine_editor_sources(inputs, destination)

        def added(result: Path) -> None:
            result_path = Path(result)
            if current_source is not None and previous_kind == "pdf":
                self._remember_editor_state()
                self._adopt_appended_editor_pdf(
                    result_path,
                    len(supported),
                    current_page_names + appended_page_names,
                )
            else:
                self._load_editor_pdf(
                    result_path,
                    (current_page_names if current_source else []) + appended_page_names,
                )
                self.editor_dirty = True
                self.editor_new_document = True
                self.editor_last_save_path = None
                self._queue_autosave()
            self.status.set(
                f"Добавлено файлов: {len(supported)}; страниц в документе: {self.editor_page_count}"
            )

        self.editor_pages_busy = True
        self._run_task(combine, 'Файлы добавлены в PDF Редактор', source_paths=source_paths, on_success=added, on_complete=lambda: setattr(self, 'editor_pages_busy', False), quiet=True)

    @staticmethod
    def _editor_page_names_for_sources(paths: list[Path]) -> list[str]:
        """Keep the original filename on every editor thumbnail after merging."""
        import fitz

        names: list[str] = []
        for path in paths:
            try:
                if path.suffix.lower() == ".pdf":
                    with fitz.open(str(path)) as document:
                        page_count = document.page_count
                else:
                    page_count = 1
            except Exception:
                page_count = 1
            names.extend([path.name] * max(1, page_count))
        return names

    def _adopt_appended_editor_pdf(
        self,
        path: Path,
        added_file_count: int,
        page_source_names: list[str] | None = None,
    ) -> None:
        import fitz

        with fitz.open(str(path)) as document:
            self.editor_page_count = document.page_count
        self.editor_pdf_path = path
        self.editor_page_source_names = (
            list(page_source_names)
            if page_source_names and len(page_source_names) == self.editor_page_count
            else [path.name] * self.editor_page_count
        )
        if self.editor_text_index_language == self.editor_ocr_language.get():
            self._remember_editor_ocr()
        self.editor_source_kind = "pdf"
        self.editor_new_document = True
        self.editor_last_save_path = None
        self.editor_dirty = True
        self.editor_pending_crop = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self._reset_editor_search()
        self.editor_thumbnail_generation += 1
        self.after_idle(self._refresh_editor_thumbnails)
        self.after_idle(self._render_editor_page)
        self._refresh_editor_edits_panel()
        self._queue_autosave()
        self.status.set(f"Добавлено файлов: {added_file_count}")

    def _load_editor_pdf(self, path: Path, page_source_names: list[str] | None = None) -> None:
        import fitz

        try:
            path = input_path(path)
            with open_pdf_input(path) as document:
                # A mislabeled image must become a real PDF before editing.
                with fitz.open(str(path)) as original:
                    is_pdf = original.is_pdf
                if not is_pdf:
                    converted = self._editor_cache_file(f"imported-{time.time_ns()}.pdf")
                    document.save(str(converted))
                    page_source_names = [path.name] * document.page_count
                    path = converted
                self.editor_page_count = document.page_count
        except Exception as exc:
            self._show_error(exc)
            return
        self.editor_pdf_path = path
        self.editor_page_source_names = (
            list(page_source_names)
            if page_source_names and len(page_source_names) == self.editor_page_count
            else [path.name] * self.editor_page_count
        )
        self.editor_source_kind = "pdf"
        self.editor_new_document = False
        self.editor_last_save_path = None
        self.editor_page_index = 0
        self.editor_text_edits.clear()
        self.editor_signature_edits.clear()
        self.editor_region_edits.clear()
        self.editor_stroke_edits.clear()
        self.editor_crop_edits.clear()
        self.editor_alignment_edits.clear()
        self.editor_alignment_preview = None
        self.editor_pending_crop = None
        self.editor_undo_stack.clear()
        self.editor_redo_stack.clear()
        self.editor_selected = None
        self.editor_clipboard = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self._reset_editor_search()
        self.editor_dirty = False
        self.editor_place_mode = "select"
        self._set_editor_tool_visual("select", "select")
        if pdf_has_signatures(path):
            self.editor_warning.set("Обнаружена электронная подпись. Правки будут отмечены рамкой, подпись станет недействительной; результат сохранится отдельно.")
        else:
            self.editor_warning.set("Корректировки отмечаются рамкой. Исходный PDF не изменяется; визуальная подпись не является электронной подписью.")
        with fitz.open(str(path)) as document:
            standard = declared_pdfa(document)
        if standard:
            self.editor_warning.set(f"{standard}: исходник не изменяется. Результат правок сохраняется как обычный PDF без подтверждения архивного соответствия.")
        self.after_idle(self._fit_editor_page)
        self.after_idle(self._refresh_editor_thumbnails)
        self._refresh_editor_edits_panel()
        self.status.set(f"Открыт в редакторе: {path.name}")

    def _load_editor_image(self, path: Path) -> None:
        try:
            path = input_path(path)
            with Image.open(path) as image:
                image.verify()
        except Exception as exc:
            self._show_error(exc)
            return
        self.editor_pdf_path = path
        self.editor_source_kind = "image"
        self.editor_new_document = False
        self.editor_last_save_path = None
        self.editor_page_index = 0
        self.editor_page_count = 1
        self.editor_page_source_names = [path.name]
        self.editor_text_edits.clear()
        self.editor_signature_edits.clear()
        self.editor_region_edits.clear()
        self.editor_stroke_edits.clear()
        self.editor_crop_edits.clear()
        self.editor_alignment_edits.clear()
        self.editor_alignment_preview = None
        self.editor_pending_crop = None
        self.editor_undo_stack.clear()
        self.editor_redo_stack.clear()
        self.editor_selected = None
        self.editor_clipboard = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self._reset_editor_search()
        self.editor_dirty = False
        self.editor_place_mode = "select"
        self._set_editor_tool_visual("select", "select")
        self.editor_warning.set("")
        self.after_idle(self._fit_editor_page)
        self.after_idle(self._refresh_editor_thumbnails)
        self._refresh_editor_edits_panel()
        self.status.set(f"Открыто изображение: {path.name}")

    def _change_editor_page(self, delta: int) -> None:
        if not self.editor_pdf_path or self.editor_source_kind != "pdf":
            return
        new_index = min(max(self.editor_page_index + delta, 0), self.editor_page_count - 1)
        self._go_to_editor_page(new_index)

    def _go_to_editor_page(self, page_index: int, focus_rect=None) -> None:
        if self.editor_word_busy_dialog is not None:
            return
        if not self.editor_pdf_path or not 0 <= page_index < self.editor_page_count:
            return
        changed = page_index != self.editor_page_index
        self.editor_page_index = page_index
        if changed:
            self.editor_selected = None
            self.editor_region_selection = None
            self.editor_recognized_text_selection = None
            self.editor_pending_crop = None
        if not self.editor_grid_mode:
            self._render_editor_page()
        else:
            self.editor_page_label.set(f"{page_index + 1} / {self.editor_page_count}")
        if changed and self.editor_place_mode == "replace_word" and not self.editor_grid_mode:
            self._start_word_replacement()
        self._refresh_editor_thumbnail_state(scroll_to_current=True)
        if focus_rect is not None:
            self.after_idle(lambda: self._center_editor_rect(focus_rect))

    def _refresh_editor_thumbnails(self) -> None:
        if not hasattr(self, "editor_thumbnail_grid"):
            return
        self._cancel_editor_thumbnail_drag()
        self._clear_editor_thumbnail_drop_ghost(restore_layout=False)
        self.editor_thumbnail_generation += 1
        generation = self.editor_thumbnail_generation
        for child in self.editor_thumbnail_grid.winfo_children():
            child.destroy()
        self.editor_thumbnail_images = []
        self.editor_thumbnail_widgets = []
        self._editor_loaded_thumbnails = set()
        self._editor_thumbnail_stale = set()
        self.editor_page_selection = {i for i in self.editor_page_selection if i < self.editor_page_count}
        if not self.editor_pdf_path:
            return

        self._editor_thumbnail_size = self._editor_thumbnail_target_size()
        thumb_width, thumb_height = self._editor_thumbnail_size
        self._editor_thumbnail_placeholder = ImageTk.PhotoImage(Image.new("RGB", self._editor_thumbnail_size, "#F2F6F9"))
        for page_index in range(self.editor_page_count):
            frame = RoundedPanel(
                self.editor_thumbnail_grid,
                fill=PANEL,
                outer_bg=BG,
                radius=10,
                border=BLUE if page_index == self.editor_page_index else BORDER,
                border_width=3 if page_index == self.editor_page_index else 1,
                padx=5,
                pady=5,
            )
            frame.grid(row=page_index, column=0, padx=7, pady=6)
            placeholder = self._editor_thumbnail_placeholder
            self.editor_thumbnail_images.append(placeholder)
            image_label = tk.Label(frame, image=placeholder, bg=PANEL, width=thumb_width, height=thumb_height)
            image_label.pack()
            footer = tk.Frame(frame, bg=PANEL)
            footer.pack(fill="x", pady=(4, 0))
            source_name = (
                self.editor_page_source_names[page_index]
                if page_index < len(self.editor_page_source_names)
                else Path(self.editor_pdf_path).name
            )
            page_label = tk.Label(
                footer,
                text=f"{source_name}\nСтраница {page_index + 1}",
                bg=PANEL,
                fg=TEXT,
                font=("Arial", 8),
                anchor="w",
                justify="left",
                wraplength=thumb_width,
            )
            page_label.pack(side="left")
            badge = tk.Label(footer, text="", bg=CYAN, fg="white", font=("Arial", 8, "bold"), padx=4)
            bundle = {"frame": frame, "image": image_label, "badge": badge, "label": page_label, "footer": footer}
            self.editor_thumbnail_widgets.append(bundle)
            for widget in (frame, image_label, footer, page_label):
                widget.bind("<Button-1>", lambda event, index=page_index: self._press_editor_thumbnail(event, index))
                widget.bind("<Double-Button-1>", lambda event, index=page_index: self._open_editor_thumbnail(index))
                widget.bind(
                    "<Button-3>",
                    lambda event, index=page_index: self._show_editor_thumbnail_context_menu(event, index),
                )
                self._bind_canvas_wheel(widget, self.editor_thumbnail_canvas)
        self._refresh_editor_thumbnail_state()
        self._layout_editor_thumbnails()
        self._queue_visible_editor_thumbnails()

    def _editor_thumbnail_target_size(self) -> tuple[int, int]:
        if self.editor_grid_mode:
            return 116, 150
        width = max(96, min(1200, self.editor_thumbnail_canvas.winfo_width() - 30))
        return width, round(width * 150 / 116)

    def _schedule_editor_thumbnail_resize(self) -> None:
        timer = getattr(self, "_thumbnail_resize_after", None)
        if timer is not None:
            self.after_cancel(timer)
            self._thumbnail_resize_after = None
        if self._editor_thumbnail_target_size() == getattr(self, "_editor_thumbnail_size", None):
            return
        render_timer = getattr(self, "_thumbnail_render_after", None)
        if render_timer is not None:
            self.after_cancel(render_timer)
            self._thumbnail_render_after = None
        self._thumbnail_resize_after = self.after(120, self._resize_editor_thumbnails)

    def _resize_editor_thumbnails(self) -> None:
        self._thumbnail_resize_after = None
        size = self._editor_thumbnail_target_size()
        if size == getattr(self, "_editor_thumbnail_size", None) or not self.editor_thumbnail_widgets:
            return
        canvas = self.editor_thumbnail_canvas
        top = canvas.canvasy(0)
        anchor = next((b["frame"] for b in self.editor_thumbnail_widgets
                       if b["frame"].winfo_y() + b["frame"].winfo_height() > top), None)
        offset = ((top - anchor.winfo_y()) / max(1, anchor.winfo_height())) if anchor else 0
        self._editor_thumbnail_size = size
        self.editor_thumbnail_generation += 1
        self._editor_thumbnail_stale = getattr(self, "_editor_thumbnail_stale", set()) | self._editor_loaded_thumbnails
        self._editor_loaded_thumbnails = set()
        self._editor_thumbnail_placeholder = ImageTk.PhotoImage(Image.new("RGB", size, "#F2F6F9"))
        for index, bundle in enumerate(self.editor_thumbnail_widgets):
            if index not in self._editor_thumbnail_stale:
                self.editor_thumbnail_images[index] = self._editor_thumbnail_placeholder
                bundle["image"].configure(image=self._editor_thumbnail_placeholder)
            bundle["image"].configure(width=size[0], height=size[1])
            bundle["label"].configure(wraplength=size[0])
        # Resize existing tiles, preserving selection and the visible page.
        self._layout_editor_thumbnails()
        canvas.update_idletasks()
        bbox = canvas.bbox("all")
        if anchor is not None and bbox and bbox[3] > 0:
            canvas.yview_moveto(max(0, (anchor.winfo_y() + offset * anchor.winfo_height()) / bbox[3]))
        self._queue_visible_editor_thumbnails()

    def _queue_visible_editor_thumbnails(self):
        if getattr(self, "_thumbnail_resize_after", None) is not None:
            return
        timer = getattr(self, "_thumbnail_render_after", None)
        if timer is not None:
            self.after_cancel(timer)
        generation = getattr(self, "editor_thumbnail_generation", 0)
        self._thumbnail_render_after = self.after(30, lambda: self._render_next_editor_thumbnail(generation, 0))

    def _render_next_editor_thumbnail(self, generation: int, page_index: int) -> None:
        self._thumbnail_render_after = None
        if generation != self.editor_thumbnail_generation or not self.editor_pdf_path or page_index >= self.editor_page_count:
            return
        canvas = self.editor_thumbnail_canvas
        top = canvas.canvasy(0)
        bottom = top + max(1, canvas.winfo_height())
        candidates = [i for i, bundle in enumerate(self.editor_thumbnail_widgets)
                      if bundle["frame"].winfo_y() + bundle["frame"].winfo_height() >= top - 220
                      and bundle["frame"].winfo_y() <= bottom + 220]
        candidates.sort(key=lambda i: abs(self.editor_thumbnail_widgets[i]["frame"].winfo_y() - (top + bottom) / 2))
        wanted = set(candidates[:64])
        loaded = self._editor_loaded_thumbnails
        stale = getattr(self, "_editor_thumbnail_stale", set())
        for index in (loaded | stale) - wanted:
            self.editor_thumbnail_images[index] = self._editor_thumbnail_placeholder
            self.editor_thumbnail_widgets[index]["image"].configure(image=self._editor_thumbnail_placeholder)
        loaded.intersection_update(wanted)
        stale.intersection_update(wanted)
        pending = [i for i in candidates[:64] if i not in loaded]
        if not pending:
            return
        page_index = pending[0]
        try:
            photo = self._render_editor_thumbnail_image(page_index)
            self.editor_thumbnail_images[page_index] = photo
            self.editor_thumbnail_widgets[page_index]["image"].configure(image=photo)
        except Exception:
            pass
        loaded.add(page_index)
        stale.discard(page_index)
        self._thumbnail_render_after = self.after(1, lambda: self._render_next_editor_thumbnail(generation, 0))

    def _render_editor_thumbnail_image(self, page_index: int) -> ImageTk.PhotoImage:
        thumb_width, thumb_height = getattr(self, "_editor_thumbnail_size", (116, 150))
        if self._editor_is_pdf():
            import fitz

            with fitz.open(str(self.editor_pdf_path)) as document:
                page = document[page_index]
                zoom = min(thumb_width / page.rect.width, thumb_height / page.rect.height)
            crop = next((edit.rect for edit in reversed(self.editor_crop_edits) if edit.page_index == page_index), None)
            image = render_pdf_page_image(self.editor_pdf_path, page_index,
                self.editor_text_edits, self.editor_signature_edits, self.editor_region_edits,
                self.editor_stroke_edits, crop, dpi=zoom * 72, alignment_edits=self.editor_alignment_edits)
        else:
            with Image.open(self.editor_pdf_path) as source:
                image = ImageOps.exif_transpose(source).convert("RGB")
        angle = self._editor_alignment_angle(page_index)
        if abs(angle) >= 0.01 and not self._editor_is_pdf():
            rotated = rotate_raster_image(image, angle)
            image.close()
            image = rotated
        image.thumbnail((thumb_width, thumb_height), LANCZOS)
        canvas = Image.new("RGB", (thumb_width, thumb_height), "white")
        canvas.paste(image, ((thumb_width - image.width) // 2, (thumb_height - image.height) // 2))
        image.close()
        photo = ImageTk.PhotoImage(canvas)
        canvas.close()
        return photo

    def _refresh_editor_thumbnail_state(self, scroll_to_current: bool = False) -> None:
        hit_counts: dict[int, int] = {}
        for hit in self.editor_search_hits:
            hit_counts[hit.page_index] = hit_counts.get(hit.page_index, 0) + 1
        for page_index, bundle in enumerate(self.editor_thumbnail_widgets):
            current = page_index in self.editor_page_selection if self.editor_grid_mode else page_index == self.editor_page_index or page_index in self.editor_page_selection
            bundle["frame"].set_panel_border(BLUE if current else BORDER, 3 if current else 1)
            fill = "#E1EDFF" if current else PANEL
            bundle["frame"].set_panel_fill(fill)
            for name in ("footer", "label", "image"):
                bundle[name].configure(bg=fill)
            count = hit_counts.get(page_index, 0)
            badge = bundle["badge"]
            if count:
                badge.configure(text=str(count))
                badge.pack(side="right")
            else:
                badge.pack_forget()
        if scroll_to_current and self.editor_thumbnail_widgets:
            self.editor_thumbnail_canvas.update_idletasks()
            bbox = self.editor_thumbnail_canvas.bbox("all")
            if bbox and bbox[3] > 0:
                frame = self.editor_thumbnail_widgets[self.editor_page_index]["frame"]
                self.editor_thumbnail_canvas.yview_moveto(max(0.0, frame.winfo_y() / bbox[3] - 0.05))

    def _show_editor_thumbnail_context_menu(self, event, page_index: int) -> str:
        self._dismiss_context_menus()
        if page_index is not None:
            if page_index not in self.editor_page_selection:
                self.editor_page_selection = {page_index}
            self._go_to_editor_page(page_index)
        menu = tk.Menu(
            self,
            tearoff=False,
            bg=PANEL,
            fg=TEXT,
            activebackground=BLUE,
            activeforeground="white",
            relief="solid",
            borderwidth=1,
            font=("Arial", 10),
        )
        self.editor_context_menu = menu
        menu.add_command(label="Добавить файлы…", command=self._choose_editor_file)
        if page_index is not None:
            menu.add_command(label="Открыть страницу", command=lambda: self._open_editor_thumbnail(page_index))
            menu.add_command(label="Сохранить как…", command=lambda: self._export_editor_pages([page_index], choose_format=True))
            menu.add_command(label="Сохранить выделенные страницы…", command=lambda: self._export_editor_pages(sorted(self.editor_page_selection)))
            menu.add_command(label="Печатать выделенные страницы…", command=lambda: self._export_editor_pages(sorted(self.editor_page_selection), printing=True))
            menu.add_separator()
            menu.add_command(label="Выделить все страницы", command=self._select_all_editor_pages)
            menu.add_command(label="Дублировать выбранные", command=self._duplicate_editor_pages)
            menu.add_command(label="Повернуть влево", command=lambda: self._rotate_editor_pages(-90))
            menu.add_command(label="Повернуть вправо", command=lambda: self._rotate_editor_pages(90))
            menu.add_command(label="Удалить выбранные", command=self._delete_editor_selected_pages)
            menu.add_command(label="Выровнять страницу…", command=lambda: (self._open_editor_thumbnail(page_index), self._start_page_alignment()))
            blank_menu = tk.Menu(menu, tearoff=False)
            blank_menu.add_command(label="Перед выбранной", command=lambda: self._insert_editor_blank(page_index))
            blank_menu.add_command(label="После выбранной", command=lambda: self._insert_editor_blank(page_index + 1))
            menu.add_cascade(label="Вставить пустую страницу", menu=blank_menu)
        else:
            menu.add_command(label="Добавить пустую страницу…", command=self._create_blank_editor_document)
        menu.add_command(label="Печатать документ…", command=self._print_editor_document)
        split_menu = tk.Menu(menu, tearoff=False, bg=PANEL, fg=TEXT, activebackground=BLUE, activeforeground="white")
        for page_size in ("A4", "A3", "A5"):
            split_menu.add_command(
                label=f"На {page_size}" + (" (по умолчанию)" if page_size == "A4" else ""),
                command=lambda size=page_size: self._split_editor_document(size),
            )
        menu.add_cascade(label="Разрезать длинные страницы", menu=split_menu)
        menu.add_separator()
        menu.add_command(label="Закрыть документ", command=self._clear_editor_document)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()
        return "break"

    def _toggle_editor_grid(self) -> None:
        if self.editor_reading_mode:
            self._exit_editor_reading_mode()
        self.editor_grid_mode = not self.editor_grid_mode
        self._dismiss_context_menus()
        panes = self.editor_content_panes
        if self.editor_grid_mode:
            panes.forget(self.editor_canvas_shell)
            panes.forget(self.editor_edits_shell)
            panes.paneconfigure(self.editor_thumbnail_shell, stretch="always")
            self.editor_properties_shell.grid_remove()
            self.editor_navigation_row.grid_remove()
            self._refresh_editor_thumbnails()
        else:
            panes.paneconfigure(self.editor_thumbnail_shell, width=170, stretch="never")
            panes.add(self.editor_canvas_shell, minsize=470, stretch="always")
            self._sync_editor_edits_visibility()
            self._show_editor_properties("select")
            self.editor_navigation_row.grid()
            self.after_idle(self._fit_editor_page)
        self.editor_view_button.configure(text="К странице" if self.editor_grid_mode else "Все страницы")
        self._layout_editor_thumbnails()
        self._refresh_editor_thumbnail_state()

    def _layout_editor_thumbnails(self, event=None, drop_target: int | None = None) -> None:
        width = self.editor_thumbnail_canvas.winfo_width()
        self._schedule_editor_thumbnail_resize()
        if drop_target is None and self.editor_thumbnail_drop_target is not None:
            drop_target = self.editor_thumbnail_drop_target
        self.editor_thumbnail_canvas.delete("grid-empty")
        if self.editor_grid_mode and not self.editor_pdf_path:
            canvas = self.editor_thumbnail_canvas
            height = max(300, canvas.winfo_height())
            create_rounded_rectangle(canvas, 18, 18, max(100, width - 18), height - 18,
                radius=22, fill=DROP_ZONE_BG, outline="#BCD2F1", width=2, dash=(6, 5), tags="grid-empty")
            if hasattr(self, "drop_zone_icon_image"):
                canvas.create_image(width / 2, height / 2 - 50, image=self.drop_zone_icon_image, tags="grid-empty")
            canvas.create_text(width / 2, height / 2 + 64, text="Перетащите PDF или изображения",
                fill=TEXT, font=(FONT_FAMILY, 15, "bold"), tags="grid-empty")
            canvas.configure(scrollregion=(0, 0, width, height))
        columns = max(1, width // 155) if self.editor_grid_mode else 1
        selected = {
            index for index in self.editor_page_selection
            if 0 <= index < len(self.editor_thumbnail_widgets)
        }
        order = list(range(len(self.editor_thumbnail_widgets)))
        show_drop_preview = (
            drop_target is not None
            and selected
            and drop_target not in selected
        )
        if show_drop_preview:
            order = move_page_selection(len(order), selected, drop_target)

        positions: dict[int, tuple[int, int]] = {}
        ghost_positions: list[tuple[int, int]] = []
        for layout_index, source_index in enumerate(order):
            row, column = divmod(layout_index, columns)
            if show_drop_preview and source_index in selected:
                ghost_positions.append((row, column))
            else:
                positions[source_index] = (row, column)

        # Keep rendered thumbnails alive while dragging. Re-gridding only the
        # moved tiles avoids placeholder flashes and needless thumbnail renders.
        for source_index, bundle in enumerate(self.editor_thumbnail_widgets):
            frame = bundle["frame"]
            position = positions.get(source_index)
            if position is None:
                if frame.winfo_manager() == "grid":
                    frame.grid_remove()
                continue
            row, column = position
            if frame.winfo_manager() == "grid":
                frame.grid_configure(row=row, column=column, padx=7, pady=6)
            else:
                frame.grid(row=row, column=column, padx=7, pady=6)

        previous_ghosts = self.editor_thumbnail_drop_ghost or []
        for ghost in previous_ghosts:
            try:
                ghost.destroy()
            except tk.TclError:
                pass
        ghosts = []
        for row, column in ghost_positions:
            ghost = self._create_editor_thumbnail_drop_ghost(show_label=not ghosts)
            ghost.grid(row=row, column=column, padx=7, pady=6)
            ghosts.append(ghost)
        self.editor_thumbnail_drop_ghost = ghosts or None
        if not show_drop_preview:
            self._queue_visible_editor_thumbnails()

    def _clear_editor_page_selection(self, event=None):
        self._dismiss_context_menus()
        self._cancel_editor_thumbnail_drag()
        self._clear_editor_thumbnail_drop_ghost()
        self.editor_page_selection.clear()
        self.editor_page_anchor = None
        self._refresh_editor_thumbnail_state()

    def _select_all_editor_pages(self):
        self.editor_page_selection = set(range(self.editor_page_count))
        self._refresh_editor_thumbnail_state()

    def _open_editor_thumbnail(self, index):
        if self.editor_grid_mode:
            self._toggle_editor_grid()
        self._go_to_editor_page(index)

    def _press_editor_thumbnail(self, event, index):
        self._dismiss_context_menus()
        toggle, extend = bool(event.state & 0x4), bool(event.state & 0x1)
        previous = set(self.editor_page_selection)
        # Keep a selected group until release so it can be dragged together.
        if toggle or extend or index not in previous:
            self.editor_page_selection = select_page_indexes(index, anchor=self.editor_page_anchor,
                extend=extend, toggle=toggle, selected=previous)
        if not extend:
            self.editor_page_anchor = index
        self.editor_page_drag = (index, event.x_root, event.y_root, False, not (toggle or extend))
        self._go_to_editor_page(index)
        self.editor_thumbnail_canvas.focus_set()
        return "break"

    def _editor_thumbnail_drop_index(self, x_root: int, y_root: int) -> int | None:
        canvas = self.editor_thumbnail_canvas
        if not (
            canvas.winfo_rootx() <= x_root <= canvas.winfo_rootx() + canvas.winfo_width()
            and canvas.winfo_rooty() <= y_root <= canvas.winfo_rooty() + canvas.winfo_height()
        ):
            return None
        for ghost in self.editor_thumbnail_drop_ghost or []:
            if (
                ghost.winfo_rootx() <= x_root < ghost.winfo_rootx() + ghost.winfo_width()
                and ghost.winfo_rooty() <= y_root < ghost.winfo_rooty() + ghost.winfo_height()
            ):
                return self.editor_thumbnail_drop_target
        for index, bundle in enumerate(self.editor_thumbnail_widgets):
            widget = bundle["frame"]
            if (
                widget.winfo_rootx() <= x_root < widget.winfo_rootx() + widget.winfo_width()
                and widget.winfo_rooty() <= y_root < widget.winfo_rooty() + widget.winfo_height()
            ):
                after = (
                    x_root > widget.winfo_rootx() + widget.winfo_width() / 2
                    if self.editor_grid_mode
                    else y_root > widget.winfo_rooty() + widget.winfo_height() / 2
                )
                return index + int(after)
        return self.editor_page_count

    def _clear_editor_thumbnail_drop_ghost(self, restore_layout: bool = True) -> None:
        ghosts = self.editor_thumbnail_drop_ghost or []
        if not isinstance(ghosts, (tuple, list)):
            ghosts = [ghosts]
        for ghost in ghosts:
            try:
                ghost.destroy()
            except tk.TclError:
                pass
        self.editor_thumbnail_drop_ghost = None
        self.editor_thumbnail_drop_target = None
        if restore_layout and hasattr(self, "editor_thumbnail_grid"):
            self._layout_editor_thumbnails()

    def _create_editor_thumbnail_drop_ghost(self, show_label: bool) -> RoundedPanel:
        """Reserve a real grid cell so surrounding thumbnails reflow during a drag."""
        reference = self.editor_thumbnail_widgets[0]["frame"]
        ghost = RoundedPanel(
            self.editor_thumbnail_grid,
            fill="#D9EAFF",
            outer_bg=BG,
            radius=10,
            border=BLUE,
            border_width=3,
            padx=5,
            pady=5,
        )
        ghost.configure(
            width=max(128, reference.winfo_reqwidth()),
            height=max(190, reference.winfo_reqheight()),
        )
        ghost.grid_propagate(False)
        label = "Новая\nпозиция" if show_label else ""
        tk.Label(
            ghost,
            text=label,
            bg="#D9EAFF",
            fg=DEEP_BLUE,
            font=("Arial", 8, "bold"),
            justify="center",
        ).place(relx=0.5, rely=0.5, anchor="center")
        return ghost

    def _show_editor_thumbnail_drop_ghost(self, target: int | None) -> None:
        if target is None or target == self.editor_thumbnail_drop_target:
            return
        if not self.editor_thumbnail_widgets:
            return
        self._clear_editor_thumbnail_drop_ghost(restore_layout=False)
        self.editor_thumbnail_drop_target = target
        self._layout_editor_thumbnails(drop_target=target)

    def _drag_editor_thumbnail(self, event):
        if not self.editor_page_drag or self.editor_reading_mode:
            return
        index, x, y, moving, plain = self.editor_page_drag
        if abs(event.x_root - x) + abs(event.y_root - y) >= 7:
            if not moving:
                try:
                    self.editor_thumbnail_canvas.grab_set()
                except tk.TclError:
                    pass
            self.editor_page_drag = (index, x, y, True, plain)
            self.editor_thumbnail_canvas.configure(cursor="fleur")
            self._show_editor_thumbnail_drop_ghost(
                self._editor_thumbnail_drop_index(event.x_root, event.y_root)
            )
            top = self.editor_thumbnail_canvas.winfo_rooty()
            if event.y_root < top + 24:
                self.editor_thumbnail_canvas.yview_scroll(-1, "units")
            elif event.y_root > top + self.editor_thumbnail_canvas.winfo_height() - 24:
                self.editor_thumbnail_canvas.yview_scroll(1, "units")

    def _release_editor_thumbnail(self, event):
        if not self.editor_page_drag:
            return
        self.editor_thumbnail_canvas.configure(cursor="arrow")
        drag, self.editor_page_drag = self.editor_page_drag, None
        target = self._editor_thumbnail_drop_index(event.x_root, event.y_root)
        self._cancel_editor_thumbnail_drag()
        self._clear_editor_thumbnail_drop_ghost()
        if not drag:
            return
        index, _x, _y, moving, plain = drag
        if moving:
            if target is None:
                self._refresh_editor_thumbnail_state()
                return
            order = move_page_selection(self.editor_page_count, self.editor_page_selection, target)
            self._arrange_editor_pages(order, "Порядок страниц изменён")
        elif plain:
            self.editor_page_selection = {index}
        self._refresh_editor_thumbnail_state()

    def _cancel_editor_thumbnail_drag(self) -> None:
        self.editor_page_drag = None
        if not hasattr(self, "editor_thumbnail_canvas"):
            return
        try:
            if self.editor_thumbnail_canvas.grab_current() == self.editor_thumbnail_canvas:
                self.editor_thumbnail_canvas.grab_release()
            self.editor_thumbnail_canvas.configure(cursor="arrow")
        except tk.TclError:
            pass

    def _duplicate_editor_pages(self):
        order = [i for i in range(self.editor_page_count) for _ in range(2 if i in self.editor_page_selection else 1)]
        self._arrange_editor_pages(order, "Страницы продублированы")

    def _delete_editor_selected_pages(self):
        order = [i for i in range(self.editor_page_count) if i not in self.editor_page_selection]
        if not order:
            self._clear_editor_document()
        else:
            self._arrange_editor_pages(order, "Выбранные страницы удалены")

    def _rotate_editor_pages(self, angle):
        self._arrange_editor_pages(list(range(self.editor_page_count)), "Страницы повёрнуты",
                                   rotations={i: angle for i in self.editor_page_selection})

    def _editor_pdf_snapshot_job(self):
        source = self.editor_pdf_path
        is_pdf = self._editor_is_pdf()
        edits = [list(items) for items in (self.editor_text_edits, self.editor_signature_edits,
                 self.editor_region_edits, self.editor_stroke_edits, self.editor_crop_edits, self.editor_alignment_edits)]
        crop = self._current_crop_rect()
        def snapshot(output):
            if is_pdf:
                return apply_pdf_edits(source, output, *edits)
            return apply_image_edits(source, output, *edits[:4], crop_rect=crop, alignment_edits=edits[5])
        return snapshot

    def _arrange_editor_pages(self, order, message, rotations=None, blank=None):
        if self.editor_pages_busy or self.editor_index_busy or not self.editor_pdf_path:
            return
        if not blank and not rotations and order == list(range(self.editor_page_count)):
            return
        source = self.editor_pdf_path
        was_pdf = self._editor_is_pdf()
        output = self._editor_cache_file(f"arranged-{time.time_ns()}.pdf")
        snapshot = self._editor_pdf_snapshot_job()
        collections = [list(items) for items in (self.editor_text_edits, self.editor_signature_edits,
            self.editor_region_edits, self.editor_stroke_edits, self.editor_crop_edits, self.editor_alignment_edits)]
        indexed_pages = list(self.editor_text_index) if self.editor_text_index_language == self.editor_ocr_language.get() else []
        best_pages = set(self.editor_best_ocr_pages)
        selected = set(self.editor_page_selection)
        current = self.editor_page_index
        page_source_names = list(self.editor_page_source_names)
        def job():
            base = source
            if rotations or not was_pdf:
                base = snapshot(output.with_name(output.stem + "-edited.pdf"))
            if blank:
                base = combine_editor_sources([base, blank], output.with_name(output.stem + "-blank.pdf"))
            return arrange_pdf_pages(base, output, order, rotations)
        def ready(result):
            if source != self.editor_pdf_path:
                return
            self._remember_editor_state()
            self.editor_pdf_path = Path(result)
            self.editor_source_kind = "pdf"
            self.editor_page_count = len(order)
            all_source_names = page_source_names + (["Пустая страница"] if blank else [])
            self.editor_page_source_names = [
                all_source_names[old] if old < len(all_source_names) else "Пустая страница"
                for old in order
            ]
            self.editor_page_index = order.index(current) if current in order else 0
            mapped = [remap_page_edits(items, order) for items in collections] if was_pdf and not rotations else [[] for _ in collections]
            (self.editor_text_edits, self.editor_signature_edits, self.editor_region_edits,
             self.editor_stroke_edits, self.editor_crop_edits, self.editor_alignment_edits) = mapped
            self.editor_page_selection = {i for i, old in enumerate(order) if old in selected}
            self.editor_selected = None
            self.editor_pending_crop = None
            self.editor_region_selection = None
            self.editor_new_document = True
            if was_pdf and not rotations:
                self.editor_text_index = [replace(page, page_index=new) for new, old in enumerate(order)
                                          for page in indexed_pages if page.page_index == old]
                language = self.editor_ocr_language.get()
                self.editor_best_ocr_pages = {(new, language) for new, old in enumerate(order) if (old, language) in best_pages}
                self._remember_editor_ocr()
            self._reset_editor_search()
            self._refresh_editor_thumbnails()
            self._render_editor_page()
            self._queue_autosave()
        self.editor_pages_busy = True
        self._run_task(job, message, on_success=ready, on_complete=lambda: setattr(self, 'editor_pages_busy', False), quiet=True)

    def _insert_editor_blank(self, position):
        dialog = BlankPageDialog(self)
        if not dialog.result:
            return
        blank = create_blank_pdf(app_cache_dir("blank-pages"), *dialog.result)
        order = list(range(self.editor_page_count))
        order.insert(position, self.editor_page_count)
        self._arrange_editor_pages(order, "Пустая страница добавлена", blank=blank)

    def _export_editor_pages(self, indexes, choose_format=False, printing=False):
        if not indexes or not self.editor_pdf_path or self.editor_pages_busy:
            return
        source = self.editor_pdf_path
        selected_type = tk.StringVar(value="PDF")
        options = None
        if printing:
            dialog = PrintDialog(self, len(indexes))
            if not dialog.result:
                return
            options = dialog.result
            output = temporary_print_path("pages")
        else:
            formats = [("PDF", "*.pdf")]
            if choose_format:
                formats += [("PNG", "*.png"), ("JPEG", "*.jpg"), ("Word DOCX с распознанным текстом", "*.docx")]
            value = filedialog.asksaveasfilename(parent=self, initialdir=self._dialog_initialdir(),
                initialfile=f"Страницы_{indexes[0] + 1}.pdf", defaultextension=".pdf", filetypes=formats, typevariable=selected_type)
            if not value:
                return
            output = editor_save_path_for_type(Path(value), selected_type.get(), ".pdf")
            if editor_output_is_source(source, output):
                messagebox.showinfo("Сохранение страницы", "Выберите новое имя, чтобы сохранить исходный документ")
                return
            self._remember_dialog_selection(output)
        snapshot = self._editor_pdf_snapshot_job()
        language = self.editor_ocr_language.get()
        def job():
            import tempfile
            with tempfile.TemporaryDirectory(prefix="pdf-bars-export-", dir=app_cache_dir("editor-pages")) as folder:
                prepared = snapshot(Path(folder) / "edited.pdf")
                selected = arrange_pdf_pages(prepared, Path(folder) / "selected.pdf", indexes)
                if printing:
                    import shutil
                    shutil.copyfile(selected, output)
                    try:
                        return submit_pdf_print_job(output, options)
                    finally:
                        remove_temporary_print_file(output)
                if output.suffix.lower() == ".pdf":
                    import shutil
                    shutil.copyfile(selected, output)
                    return output
                if output.suffix.lower() == ".docx":
                    return source_to_layout_ocr_docx(selected, output, dpi=350, language=language)
                return render_pdf_page_edits_to_image(selected, output, 0, [], [])
        self._run_task(job, 'Страницы отправлены на печать' if printing else 'Страницы сохранены')

    def _delete_editor_page(self, page_index: int) -> None:
        if not self._editor_is_pdf() or self.editor_page_count <= 1:
            messagebox.showinfo("Удаление страницы", "В документе должна остаться хотя бы одна страница")
            return
        if not messagebox.askyesno("Удаление страницы", f"Удалить страницу {page_index + 1} из документа?"):
            return
        source = Path(self.editor_pdf_path)
        destination = self._editor_cache_file(f"delete-{time.time_ns()}.pdf")
        edited = self._editor_cache_file(f"before-delete-{time.time_ns()}.pdf")
        text_edits = list(self.editor_text_edits)
        signature_edits = list(self.editor_signature_edits)
        region_edits = list(self.editor_region_edits)
        stroke_edits = list(self.editor_stroke_edits)
        crop_edits = list(self.editor_crop_edits)
        alignment_edits = list(self.editor_alignment_edits)

        def job() -> Path:
            apply_pdf_edits(source, edited, text_edits, signature_edits, region_edits, stroke_edits, crop_edits, alignment_edits)
            return delete_pdf_page(edited, destination, page_index)

        self._run_task(job, 'Страница удалена', on_success=lambda result: self._replace_editor_source_after_page_operation(Path(result), source, 'Страница удалена. Сохраните документ под нужным именем.'))

    def _split_editor_document(self, page_size: str = "A4") -> None:
        if self.editor_pages_busy or self.editor_index_busy:
            return
        if not self._editor_is_pdf():
            messagebox.showinfo("Разрезание страниц", "Откройте PDF-документ")
            return
        source = Path(self.editor_pdf_path)
        destination = self._editor_cache_file(f"split-{page_size}-{time.time_ns()}.pdf")
        edited = self._editor_cache_file(f"before-split-{time.time_ns()}.pdf")
        text_edits = list(self.editor_text_edits)
        signature_edits = list(self.editor_signature_edits)
        region_edits = list(self.editor_region_edits)
        stroke_edits = list(self.editor_stroke_edits)
        crop_edits = list(self.editor_crop_edits)
        alignment_edits = list(self.editor_alignment_edits)
        selected_pages = set(self.editor_page_selection) or None

        def job() -> Path:
            apply_pdf_edits(source, edited, text_edits, signature_edits, region_edits, stroke_edits, crop_edits, alignment_edits)
            return split_long_pdf_pages(edited, destination, page_size, page_indexes=selected_pages)

        self.editor_split_page_size.set(page_size)
        self.editor_pages_busy = True
        self._run_task(job, f'Длинные страницы разрезаны на {page_size}', on_success=lambda result: self._replace_editor_source_after_page_operation(Path(result), source, f'Страницы разрезаны на {page_size}. Сохраните документ под нужным именем.'), on_complete=lambda: setattr(self, 'editor_pages_busy', False))

    def _replace_editor_source_after_page_operation(self, result: Path, original: Path, status_text: str) -> None:
        previous = self._editor_state_snapshot()
        history = list(self.editor_undo_stack)
        self._load_editor_pdf(result)
        self.editor_undo_stack = (history + [previous])[-100:]
        self.editor_new_document = True
        self.editor_last_save_path = original.with_name(f"{original.stem}_edited.pdf")
        self.editor_dirty = True
        self.status.set(status_text)
        self._queue_autosave()

    def _refresh_editor_edits_panel(self) -> None:
        if not hasattr(self, "editor_edits_list"):
            return
        self.editor_edits_list.delete(0, tk.END)
        self.editor_edit_list_items = []
        entries = (
            ("text", self.editor_text_edits),
            ("signature", self.editor_signature_edits),
            ("region", self.editor_region_edits),
            ("stroke", self.editor_stroke_edits),
            ("crop", self.editor_crop_edits),
            ("alignment", self.editor_alignment_edits),
        )
        for kind, collection in entries:
            for index, edit in enumerate(collection):
                if edit.page_index != self.editor_page_index:
                    continue
                self.editor_edit_list_items.append((kind, index))
                self.editor_edits_list.insert(tk.END, self._editor_edit_label(kind, edit))
                if self.editor_selected == (kind, index):
                    self.editor_edits_list.selection_set(tk.END)
        if not self.editor_edit_list_items:
            self.editor_edits_list.insert(tk.END, "Правок пока нет")
            self.editor_edits_list.itemconfigure(0, fg=MUTED)

    def _editor_edit_label(self, kind: str, edit) -> str:
        if kind == "text":
            if edit.opaque_background and not edit.text:
                return "Закрытая область"
            if edit.replacement_source_rect:
                before = " ".join(edit.original_text.split())
                after = " ".join(edit.text.split())
                return f"Замена: {before[:13]} -> {after[:13]}"
            value = " ".join(edit.text.split())
            return f"Текст: {value[:28]}" + ("…" if len(value) > 28 else "")
        if kind == "signature":
            if not edit.keep_proportion:
                return "Вставленная область"
            return f"Изображение: {Path(edit.image_path).stem}"
        if kind == "region":
            return "Вставленная область"
        if kind == "stroke":
            if edit.style == "eraser":
                return "Ластик"
            if edit.style == "line":
                return "Линия"
            if edit.style == "arrow":
                return "Стрелка"
            return "Карандаш"
        if kind == "alignment":
            return f"Выравнивание: {edit.angle:+.2f}°"
        return "Обрезка страницы"

    def _select_editor_edit_from_list(self, _event=None) -> None:
        selection = self.editor_edits_list.curselection()
        if not selection or selection[0] >= len(self.editor_edit_list_items):
            return
        self.editor_selected = self.editor_edit_list_items[selection[0]]
        self.editor_region_selection = None
        self._load_selected_editor_properties()
        self._render_editor_page()
        edit = self._selected_editor_object()
        rect = self._editor_object_rect(self.editor_selected[0], edit)
        if rect:
            self.after_idle(lambda: self._center_editor_rect(rect))

    def _reset_editor_search(self) -> None:
        self.editor_search_generation += 1
        self._editor_index_request_key = None
        self.editor_best_ocr_pages.clear()
        self.editor_index_busy = False
        self.editor_word_background_ocr = False
        self.editor_text_index = []
        self.editor_text_index_mode = ""
        self.editor_text_index_language = ""
        self.editor_search_hits = []
        self.editor_search_hit_index = -1
        self.editor_recognized_text_selection = None
        self.editor_search_summary.set("")
        key = self._editor_ocr_cache_key()
        self._editor_text_index_key = key
        cached = getattr(self, "_editor_ocr_cache", {}).get(key)
        if cached:
            self.editor_text_index = list(cached[0])
            self.editor_best_ocr_pages = set(cached[1])
            self.editor_text_index_language = self.editor_ocr_language.get()
            self.editor_text_index_mode = f"ocr:{self.editor_text_index_language}"
        if self.editor_pdf_path is None:
            self._editor_ocr_cache = {}
        if self.editor_reading_mode:
            self.editor_place_mode = None
            if hasattr(self, "editor_canvas"):
                self.editor_canvas.configure(cursor="arrow")

    def _editor_ocr_cache_key(self):
        if not self.editor_pdf_path:
            return None
        try:
            stat = self.editor_pdf_path.stat()
            return (str(self.editor_pdf_path), stat.st_size, stat.st_mtime_ns, self.editor_ocr_language.get())
        except OSError:
            return None

    def _remember_editor_ocr(self):
        key = self._editor_ocr_cache_key()
        self._editor_text_index_key = key
        if key is not None:
            if not hasattr(self, "_editor_ocr_cache"):
                self._editor_ocr_cache = {}
            self._editor_ocr_cache[key] = (list(self.editor_text_index), set(self.editor_best_ocr_pages))

    def _ensure_editor_ocr_current(self):
        if getattr(self, "_editor_text_index_key", None) != self._editor_ocr_cache_key():
            self._reset_editor_search()

    def _start_editor_search(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("Поиск", "Сначала откройте PDF или изображение")
            return
        self._ensure_editor_ocr_current()
        if not self.editor_search_query.get().strip():
            (self.editor_reader_search_entry if self.editor_reading_mode else self.editor_search_entry).focus_set()
            self.status.set("Введите слово или фразу для поиска")
            return
        if len(self.editor_text_index) == self.editor_page_count and self.editor_text_index_language == self.editor_ocr_language.get():
            self._apply_editor_search_query()
            return
        self._build_editor_text_index(force_ocr=False)

    def _start_editor_ocr(self) -> None:
        if self.editor_index_busy or self.editor_pages_busy:
            return
        if not self.editor_pdf_path:
            messagebox.showinfo("Распознавание текста", "Сначала откройте PDF или изображение")
            return
        self._ensure_editor_ocr_current()
        if (len(self.editor_text_index) == self.editor_page_count
                and self.editor_text_index_language == self.editor_ocr_language.get()):
            self._activate_recognized_text_selection()
            return
        if not ocr_models_available(language=self.editor_ocr_language.get()):
            messagebox.showerror("Распознавание текста", "Не найдена встроенная модель выбранного языка")
            return
        self._build_editor_text_index(force_ocr=True)

    def _start_word_replacement(self) -> None:
        if ((self.editor_index_busy and not self.editor_word_background_ocr)
                or self.editor_pages_busy or self.editor_word_busy_dialog is not None):
            return
        if not self.editor_pdf_path:
            messagebox.showinfo("Замена слова", "Сначала откройте PDF или изображение")
            return
        self._ensure_editor_ocr_current()
        if not ocr_models_available(language=self.editor_ocr_language.get()):
            messagebox.showerror("Замена слова", "Не найдена встроенная модель выбранного языка")
            return
        self._activate_word_replacement_mode()

    def _activate_word_replacement_mode(self) -> None:
        self.editor_place_mode = "replace_word"
        self.editor_selected = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self.editor_pending_crop = None
        self.editor_canvas.delete("editor-draft")
        self.editor_canvas.configure(cursor="hand2")
        self._set_editor_tool_visual("replace_word", "word")
        self.status.set("Щёлкните по распознанному слову, которое нужно заменить")
        self._render_editor_page()

    def _replace_word_at_canvas_point(self, canvas_x: float, canvas_y: float) -> None:
        if self.editor_word_busy_dialog is not None:
            return
        pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((canvas_x, canvas_y, canvas_x, canvas_y))
        page = next(
            (item for item in self.editor_text_index if item.page_index == self.editor_page_index),
            None,
        )
        language = self.editor_ocr_language.get()
        if page is not None and (page.page_index, language) in self.editor_best_ocr_pages:
            word = self._word_at_editor_point(page, pdf_x, pdf_y)
            if word is not None:
                self._prepare_editor_replacement_word(page, word)
                return
        source = Path(self.editor_pdf_path)
        page_index = self.editor_page_index
        angle = self._editor_alignment_angle(page_index)
        source_point = self._rotate_editor_rect((pdf_x, pdf_y, pdf_x, pdf_y),
                                                 -angle, *self.editor_page_size)[:2]
        self._show_word_busy("Распознавание выбранного слова…")
        self._run_task(lambda: recognize_word_region(source, page_index, source_point, language=language), 'Выбранное слово распознано', [source], on_success=lambda result: self._word_region_ready(result, pdf_x, pdf_y, source), on_complete=self._finish_word_busy, quiet=True)

    def _word_at_editor_point(self, page: PageText, pdf_x: float, pdf_y: float):
        for word in page.words:
            rect = self._indexed_rect_to_editor(page.page_index, word.rect, index_page=page)
            if rect[0] - 4 <= pdf_x <= rect[2] + 4 and rect[1] - 4 <= pdf_y <= rect[3] + 4:
                return word
        return None

    def _word_region_ready(self, page: PageText, pdf_x: float, pdf_y: float, source: Path) -> None:
        if source != self.editor_pdf_path or page.page_index != self.editor_page_index:
            return
        word = self._word_at_editor_point(page, pdf_x, pdf_y)
        if word is None:
            self.status.set("Слово не найдено. Щёлкните точнее внутри букв")
            return
        self.editor_word_region_uses_ocr = page.used_ocr
        self._prepare_editor_replacement_word(page, word)

    def _prepare_editor_replacement_word(self, page: PageText, word) -> None:
        source_rect = self._indexed_rect_to_editor(page.page_index, word.rect, index_page=page)
        destination = app_cache_dir("word-backgrounds") / f"word-{time.time_ns()}.png"
        source = Path(self.editor_pdf_path)
        page_index = self.editor_page_index
        alignment_angle = self._editor_alignment_angle(page_index)
        self._show_word_busy("Определение шрифта и подготовка фона…")
        self._run_task(lambda: prepare_word_replacement(source, destination, page_index, source_rect, word.text, alignment_angle=alignment_angle), 'Стиль слова определён', [source], on_success=lambda prepared: self._prompt_word_replacement(prepared, word.text), on_complete=self._finish_word_busy, quiet=True)

    def _show_word_busy(self, text: str, title: str = "Замена слова") -> None:
        if self.editor_word_busy_dialog is not None:
            return
        self.editor_drag_start = None
        self.editor_object_drag = None
        self.editor_canvas.delete("editor-draft", "eraser-preview")
        dialog = tk.Toplevel(self)
        self.editor_word_busy_dialog = dialog
        dialog.title(title)
        dialog.transient(self)
        dialog.resizable(False, False)
        dialog.protocol("WM_DELETE_WINDOW", lambda: None)
        # Do not propagate keyboard shortcuts to the editor while processing.
        dialog.bindtags((str(dialog), "Toplevel"))
        ttk.Label(dialog, text=text, padding=20).pack()
        progress = ttk.Progressbar(dialog, mode="indeterminate", length=340)
        dialog.progress = progress
        progress.pack(padx=20, pady=(0, 20))
        progress.start(60)
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()
        dialog.focus_set()

    def _finish_word_busy(self) -> None:
        dialog = self.editor_word_busy_dialog
        self.editor_word_busy_dialog = None
        if dialog is not None:
            dialog.progress.stop()
            dialog.grab_release()
            dialog.destroy()
            self.editor_canvas.focus_set()

    def _prompt_word_replacement(self, prepared, original_text: str) -> None:
        if not self.editor_index_busy and self.editor_pdf_path:
            language = self.editor_ocr_language.get()
            if (self.editor_page_index, language) not in self.editor_best_ocr_pages:
                needs_ocr = self.editor_word_region_uses_ocr
                self._build_editor_text_index(force_ocr=needs_ocr, page_indexes=[self.editor_page_index],
                                              quality="word" if needs_ocr else "fast", background_only=True)
        dialog = WordReplacementDialog(
            self,
            original_text,
            prepared.font_family,
            prepared.font_size,
            prepared.bold,
            prepared.italic,
            scan_effect=True,
        )
        if not dialog.result:
            self.status.set("Замена слова отменена")
            return
        new_text, bold, italic, scan_effect, underline = dialog.result
        self._complete_word_replacement(prepared, original_text, new_text, bold, italic, scan_effect, underline)

    def _complete_word_replacement(
        self,
        prepared,
        original_text: str,
        new_text: str,
        bold: bool | None = None,
        italic: bool | None = None,
        scan_effect: bool = True,
        underline: bool = False,
    ) -> None:
        bold = prepared.bold if bold is None else bool(bold)
        italic = prepared.italic if italic is None else bool(italic)
        page_width, page_height = self.editor_page_size
        source_x0, source_y0, source_x1, source_y1 = prepared.source_rect
        width = measure_editor_text(
            new_text,
            prepared.font_family,
            prepared.font_size,
            bold,
            italic,
        ) + max(2.0, prepared.font_size * 0.18)
        height = max(source_y1 - source_y0, prepared.font_size * 1.3)
        text_y0 = max(0.0, min(page_height - height, source_y0 - prepared.font_size * 0.14))
        text_x0 = max(0.0, min(page_width - width, source_x0))
        self._remember_editor_state()
        edit = TextEdit(
            page_index=self.editor_page_index,
            rect=(text_x0, text_y0, min(page_width, text_x0 + width), min(page_height, text_y0 + height)),
            text=new_text,
            font_size=prepared.font_size,
            font_family=prepared.font_family,
            font_file=resolve_font_file(prepared.font_family, bold, italic),
            color=(0.0, 0.0, 0.0),
            background_color=prepared.background_color,
            bold=bold,
            italic=italic,
            underline=underline,
            replacement_background=prepared.background_path,
            replacement_background_rect=prepared.background_rect,
            replacement_source_rect=prepared.source_rect,
            original_text=original_text,
            style_confidence=prepared.confidence,
            scan_blur=prepared.scan_blur,
            scan_effect=scan_effect,
        )
        self.editor_text_edits.append(edit)
        self.editor_selected = ("text", len(self.editor_text_edits) - 1)
        self.editor_place_mode = "select"
        self.editor_canvas.configure(cursor="arrow")
        self._load_selected_editor_properties()
        self.status.set(
            f"Слово заменено. Совпадение стиля: {round(prepared.confidence * 100)}%. "
            "Положение можно уточнить стрелками по горизонтали и вертикали"
        )
        self._render_editor_page()

    def _build_editor_text_index(self, force_ocr: bool, page_indexes=None, quality="fast",
                                 background_only: bool = False) -> None:
        source = self.editor_pdf_path
        if source is None or self.editor_index_busy or self.editor_pages_busy:
            return
        self._ensure_editor_ocr_current()
        ocr_language = self.editor_ocr_language.get()
        if self.editor_text_index_language != ocr_language:
            self.editor_text_index = []
            self.editor_best_ocr_pages.clear()
            cached = getattr(self, "_editor_ocr_cache", {}).get(self._editor_ocr_cache_key())
            if cached:
                self.editor_text_index = list(cached[0])
                self.editor_best_ocr_pages = set(cached[1])
            self.editor_text_index_language = ocr_language
        known = {page.page_index for page in self.editor_text_index}
        requested = range(self.editor_page_count) if page_indexes is None else page_indexes
        page_indexes = [index for index in requested if background_only or index not in known]
        if not page_indexes:
            if self.editor_word_after_ocr:
                self.editor_word_after_ocr = False
                self._activate_word_replacement_mode()
            elif force_ocr:
                self._activate_recognized_text_selection()
            else:
                self._apply_editor_search_query()
            return
        self.editor_index_busy = True
        self.editor_word_background_ocr = background_only
        if self.editor_word_after_ocr:
            self._show_word_busy("Распознавание текущей страницы…")
        self.editor_search_generation += 1
        generation = self.editor_search_generation
        self._editor_index_request_key = self._editor_ocr_cache_key()
        self.editor_search_button.configure(state="disabled")
        self.editor_ocr_button.configure(state="disabled")
        self.editor_reader_search_button.configure(state="disabled")
        self.editor_ocr_language_box.configure(state="disabled")
        self.editor_ocr_progress.configure(value=0, maximum=max(1, self.editor_page_count))
        self.editor_ocr_progress.grid()
        self.editor_search_summary.set("Распознавание…" if force_ocr else "Индексация…")
        progress_text = "Распознавание текста в документе…" if force_ocr else "Подготовка поиска по документу…"
        self._start_global_progress(progress_text, determinate=True, maximum=max(1, self.editor_page_count))
        events: queue.Queue = queue.Queue()
        started_at = time.perf_counter()
        page_count = self.editor_page_count

        def progress(current: int, total: int, used_ocr: bool) -> None:
            label = "OCR" if used_ocr or force_ocr else "Индекс"
            events.put(("progress", label, current, total))

        def worker() -> None:
            try:
                index = build_text_index(
                    source,
                    force_ocr=force_ocr,
                    dpi=350 if quality in {"best", "word"} else 220,
                    language=ocr_language,
                    enhance=True,
                    progress=progress,
                    page_indexes=page_indexes,
                    quality=quality,
                )
            except Exception as exc:
                events.put(("error", exc, traceback.format_exc()))
            else:
                mode = (f"page-best:{ocr_language}" if quality == "best" or background_only
                        else (f"ocr:{ocr_language}" if force_ocr else "auto"))
                events.put(("ready", index, mode))

        threading.Thread(target=worker, daemon=True).start()
        self.after(50, lambda: self._poll_editor_index_events(generation, events))

    def _poll_editor_index_events(self, generation: int, events: queue.Queue) -> None:
        if generation != self.editor_search_generation:
            return
        finished = False
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                break
            if event[0] == "progress":
                _event_type, label, current, total = event
                self.editor_search_summary.set(f"{label}: {current}/{total}")
                self.editor_ocr_progress.configure(value=current, maximum=max(1, total))
                self._update_global_progress(current, total, f"{label}: {current}/{total}")
            elif event[0] == "error":
                _event_type, exc, details = event
                self._editor_index_failed(generation, exc, details)
                finished = True
            elif event[0] == "ready":
                _event_type, index, mode = event
                self._editor_index_ready(generation, index, mode)
                finished = True
        if not finished:
            self.after(50, lambda: self._poll_editor_index_events(generation, events))

    def _editor_index_failed(self, generation: int, exc: Exception, details: str) -> None:
        if generation != self.editor_search_generation:
            return
        self._editor_index_request_key = None
        background_only = self.editor_word_background_ocr
        if not background_only:
            self._finish_word_busy()
        self.editor_index_busy = False
        self.editor_word_background_ocr = False
        self.editor_search_button.configure(state="normal")
        self.editor_ocr_button.configure(state="normal")
        self.editor_reader_search_button.configure(state="normal")
        self.editor_ocr_language_box.configure(state="readonly")
        self.editor_ocr_progress.grid_remove()
        self.editor_search_summary.set("")
        self.editor_word_after_ocr = False
        self._finish_global_progress()
        if background_only:
            self.status.set(f"Фоновое распознавание не завершено: {exc}")
        else:
            self._show_error(exc, details)

    def _editor_index_ready(self, generation: int, index: list[PageText], mode: str) -> None:
        if generation != self.editor_search_generation:
            return
        request_key = getattr(self, "_editor_index_request_key", None)
        self._editor_index_request_key = None
        if request_key is not None and request_key != self._editor_ocr_cache_key():
            self._editor_index_failed(generation, ValueError(
                "Исходный документ изменился во время распознавания. Откройте его заново."), "")
            self._reset_editor_search()
            return
        if not self.editor_word_background_ocr:
            self._finish_word_busy()
        self.editor_index_busy = False
        self.editor_word_background_ocr = False
        merged = {page.page_index: page for page in self.editor_text_index}
        merged.update({page.page_index: page for page in index})
        self.editor_text_index = [merged[key] for key in sorted(merged)]
        if mode.startswith("page-best:"):
            language = mode.split(":", 1)[1]
            self.editor_best_ocr_pages.update((page.page_index, language) for page in index)
        else:
            self.editor_text_index_mode = mode
        self.editor_text_index_language = self.editor_ocr_language.get()
        self._remember_editor_ocr()
        self.editor_search_button.configure(state="normal")
        self.editor_ocr_button.configure(state="normal")
        self.editor_reader_search_button.configure(state="normal")
        self.editor_ocr_language_box.configure(state="readonly")
        self.editor_ocr_progress.grid_remove()
        self._finish_global_progress()
        ocr_pages = sum(page.used_ocr for page in index)
        self.status.set(
            f"Распознавание завершено: страниц {ocr_pages}. Текст можно выделять мышью и копировать"
            if mode.startswith("ocr:")
            else "Документ проиндексирован для поиска"
        )
        if self.editor_search_query.get().strip() and not mode.startswith("page-best:"):
            self._apply_editor_search_query()
        else:
            self.editor_search_summary.set(f"Страниц: {len(index)}")
        if self.editor_word_after_ocr:
            self.editor_word_after_ocr = False
            if (self.editor_page_index, self.editor_ocr_language.get()) in self.editor_best_ocr_pages:
                self._activate_word_replacement_mode()
            else:
                self._start_word_replacement()
        elif mode.startswith("ocr:") or self.editor_reading_mode:
            self._activate_recognized_text_selection(render=True)

    def _activate_recognized_text_selection(self, render: bool = True) -> None:
        if not self.editor_text_index:
            self.status.set("Сначала нажмите «Распознать текст»")
            return
        self.editor_place_mode = "recognized_text"
        self.editor_selected = None
        self.editor_region_selection = None
        self.editor_canvas.configure(cursor="xterm")
        self._set_editor_tool_visual("", "select")
        self.status.set("Протяните мышью по тексту; затем нажмите Ctrl+C или используйте правую кнопку мыши")
        if render:
            self._render_editor_page()

    def _copy_recognized_text(self) -> None:
        if not self.editor_recognized_text_selection:
            return
        text = self.editor_recognized_text_selection[2]
        try:
            self.clipboard_clear()
            self.clipboard_append(text)
            self.update_idletasks()
        except tk.TclError:
            return
        self.editor_clipboard = None
        self.status.set(f"Распознанный текст скопирован: {len(text)} симв.")

    def _apply_editor_search_query(self) -> None:
        self.editor_search_hits = search_text_index(self.editor_text_index, self.editor_search_query.get())
        self.editor_search_hit_index = 0 if self.editor_search_hits else -1
        self._refresh_editor_thumbnail_state()
        if not self.editor_search_hits:
            self.editor_search_summary.set("Не найдено")
            self.status.set("Совпадений не найдено")
            self._render_editor_page()
            return
        self.editor_search_summary.set(f"1 из {len(self.editor_search_hits)}")
        hit = self.editor_search_hits[0]
        rect = self._search_hit_editor_rect(hit)
        self._go_to_editor_page(hit.page_index, rect)
        self.status.set(f"Найдено совпадений: {len(self.editor_search_hits)}")

    def _move_editor_search_hit(self, delta: int) -> None:
        if not self.editor_search_hits:
            self._start_editor_search()
            return
        self.editor_search_hit_index = (self.editor_search_hit_index + delta) % len(self.editor_search_hits)
        hit = self.editor_search_hits[self.editor_search_hit_index]
        self.editor_search_summary.set(f"{self.editor_search_hit_index + 1} из {len(self.editor_search_hits)}")
        self._go_to_editor_page(hit.page_index, self._search_hit_editor_rect(hit))

    def _search_hit_editor_rect(self, hit: SearchHit) -> tuple[float, float, float, float]:
        return self._indexed_rect_to_editor(hit.page_index, hit.rect)

    def _indexed_rect_to_editor(
        self,
        page_index: int,
        rect: tuple[float, float, float, float],
        index_page: PageText | None = None,
    ) -> tuple[float, float, float, float]:
        page = index_page or next((item for item in self.editor_text_index if item.page_index == page_index), None)
        if page is None or page.width <= 0 or page.height <= 0:
            return rect
        if page_index == self.editor_page_index and self.editor_page_size[0] > 0 and self.editor_page_size[1] > 0:
            source_width, source_height = self.editor_page_size
        else:
            source_width, source_height = self._editor_source_dimensions_for_page(page_index)
        scale_x = source_width / page.width
        scale_y = source_height / page.height
        scaled = (
            rect[0] * scale_x,
            rect[1] * scale_y,
            rect[2] * scale_x,
            rect[3] * scale_y,
        )
        return self._rotate_editor_rect(scaled, self._editor_alignment_angle(page_index), source_width, source_height)

    def _editor_rect_to_index(
        self,
        page: PageText,
        rect: tuple[float, float, float, float],
    ) -> tuple[float, float, float, float]:
        if self.editor_page_size[0] <= 0 or self.editor_page_size[1] <= 0:
            return rect
        rect = self._rotate_editor_rect(
            rect,
            -self._editor_alignment_angle(page.page_index),
            self.editor_page_size[0],
            self.editor_page_size[1],
        )
        scale_x = page.width / self.editor_page_size[0]
        scale_y = page.height / self.editor_page_size[1]
        return rect[0] * scale_x, rect[1] * scale_y, rect[2] * scale_x, rect[3] * scale_y

    @staticmethod
    def _rotate_editor_rect(rect, angle: float, width: float, height: float):
        if abs(angle) < 0.01:
            return tuple(rect)
        radians = math.radians(float(angle))
        cosine = math.cos(radians)
        sine = math.sin(radians)
        center_x, center_y = width / 2.0, height / 2.0
        corners = ((rect[0], rect[1]), (rect[2], rect[1]), (rect[2], rect[3]), (rect[0], rect[3]))
        rotated = []
        for x, y in corners:
            delta_x, delta_y = x - center_x, y - center_y
            rotated.append(
                (
                    center_x + cosine * delta_x + sine * delta_y,
                    center_y - sine * delta_x + cosine * delta_y,
                )
            )
        xs = [point[0] for point in rotated]
        ys = [point[1] for point in rotated]
        return min(xs), min(ys), max(xs), max(ys)

    def _editor_source_dimensions_for_page(self, page_index: int) -> tuple[float, float]:
        if self._editor_is_pdf():
            import fitz

            with fitz.open(str(self.editor_pdf_path)) as document:
                rect = document[page_index].rect
            return float(rect.width), float(rect.height)
        with Image.open(self.editor_pdf_path) as image:
            source = ImageOps.exif_transpose(image)
            return float(source.width), float(source.height)

    def _center_editor_rect(self, rect) -> None:
        if not rect or not self.editor_pdf_path:
            return
        x0, y0, x1, y1 = self._pdf_rect_to_canvas(rect)
        self.editor_canvas.update_idletasks()
        scrollregion = self.editor_canvas.cget("scrollregion").split()
        if len(scrollregion) != 4:
            return
        sx0, sy0, sx1, sy1 = (float(value) for value in scrollregion)
        total_width = max(1.0, sx1 - sx0)
        total_height = max(1.0, sy1 - sy0)
        view_width = self.editor_canvas.winfo_width()
        view_height = self.editor_canvas.winfo_height()
        if total_width > view_width:
            self.editor_canvas.xview_moveto(max(0.0, min(1.0, ((x0 + x1) / 2 - view_width / 2) / (total_width - view_width))))
        if total_height > view_height:
            self.editor_canvas.yview_moveto(max(0.0, min(1.0, ((y0 + y1) / 2 - view_height / 2) / (total_height - view_height))))

    def _editor_is_pdf(self) -> bool:
        return self.editor_pdf_path is not None and self.editor_source_kind == "pdf"

    def _editor_is_image(self) -> bool:
        return self.editor_pdf_path is not None and self.editor_source_kind == "image"

    def _change_editor_zoom(self, delta: float) -> None:
        if not self.editor_pdf_path:
            return
        self._set_editor_zoom(self.editor_zoom_percent.get() + delta)

    def _set_editor_zoom(self, percent: float) -> None:
        if not self.editor_pdf_path:
            return
        self.editor_zoom_mode = "manual"
        self.editor_zoom_percent.set(max(25.0, min(400.0, percent)))
        self._render_editor_page()

    def _editor_source_dimensions(self) -> tuple[float, float]:
        if self._editor_is_pdf():
            import fitz

            with fitz.open(str(self.editor_pdf_path)) as document:
                rect = document[self.editor_page_index].rect
            return rect.width, rect.height
        with Image.open(self.editor_pdf_path) as image:
            source = ImageOps.exif_transpose(image)
            return float(source.width), float(source.height)

    def _fit_editor_page(self) -> None:
        if not self.editor_pdf_path or not hasattr(self, "editor_canvas"):
            return

        self.editor_zoom_mode = "page"
        self.editor_canvas.update_idletasks()
        width = max(200, self.editor_canvas.winfo_width() - 56)
        height = max(200, self.editor_canvas.winfo_height() - 56)
        source_width, source_height = self._editor_source_dimensions()
        self.editor_zoom_percent.set(max(25.0, min(400.0, min(width / source_width, height / source_height) * 100.0)))
        self._render_editor_page()
        self.editor_canvas.xview_moveto(0)
        self.editor_canvas.yview_moveto(0)

    def _finish_entering_editor_reading_mode(self) -> None:
        if not self.editor_reading_mode:
            return
        self.update_idletasks()
        self._fit_editor_page()
        self.after_idle(self.editor_canvas.focus_set)

    def _fit_editor_width(self) -> None:
        if not self.editor_pdf_path or not hasattr(self, "editor_canvas"):
            return
        self.editor_zoom_mode = "width"
        self.editor_canvas.update_idletasks()
        width = max(200, self.editor_canvas.winfo_width() - 24)
        source_width, _source_height = self._editor_source_dimensions()
        self.editor_zoom_percent.set(max(25.0, min(400.0, width / source_width * 100.0)))
        self._render_editor_page()
        self.editor_canvas.xview_moveto(0)
        self.editor_canvas.yview_moveto(0)

    def _enter_editor_reading_mode(self) -> None:
        if self.editor_reading_mode:
            return
        if not self.editor_pdf_path:
            messagebox.showinfo("Режим чтения", "Сначала откройте PDF или изображение")
            return
        if self.editor_grid_mode:
            self._toggle_editor_grid()
        self.editor_reading_restore_window = not self._window_maximized
        if self.editor_reading_restore_window:
            self._toggle_maximize_window()
        self.editor_reading_mode = True
        self.editor_toolbar_shell.grid_remove()
        self.editor_properties_shell.grid_remove()
        self.editor_reader_toolbar_shell.grid()
        self.notebook.navigation_shell.grid_remove()
        self.status_bar.grid_remove()
        self.notebook.grid_configure(padx=8, pady=(0, 8))
        self.editor_tab.configure(padding=(12, 8, 12, 12))
        panes = {str(pane) for pane in self.editor_content_panes.panes()}
        if str(self.editor_edits_shell) in panes:
            self.editor_content_panes.forget(self.editor_edits_shell)
        self.editor_selected = None
        self.editor_object_drag = None
        self.editor_region_selection = None
        self.editor_place_mode = "recognized_text" if self.editor_text_index else None
        self.editor_canvas.configure(cursor="xterm" if self.editor_text_index else "arrow")
        self.status.set("Режим чтения: Esc возвращает обычный интерфейс")
        self.after_idle(self._finish_entering_editor_reading_mode)
        self.after(260, self._finish_entering_editor_reading_mode)

    def _exit_editor_reading_mode(self) -> None:
        if not self.editor_reading_mode:
            return
        self.editor_reading_mode = False
        self.editor_reader_toolbar_shell.grid_remove()
        self.editor_toolbar_shell.grid()
        self._show_editor_properties("select")
        self.notebook.navigation_shell.grid()
        self.status_bar.grid()
        self.notebook.grid_configure(padx=6, pady=(4, 4))
        self.editor_tab.configure(padding=(8, 6, 8, 8))
        self._sync_editor_edits_visibility()
        self.editor_place_mode = "select"
        self.editor_canvas.configure(cursor="arrow")
        self.status.set("Режим чтения закрыт")
        if self.editor_reading_restore_window and self._window_maximized:
            self._toggle_maximize_window()
        self.editor_reading_restore_window = False
        self.after_idle(self._render_editor_page)

    def _editor_escape(self, _event=None):
        if self.editor_reading_mode:
            self._exit_editor_reading_mode()
            return "break"
        self._dismiss_context_menus()
        self._clear_editor_page_selection()
        self.editor_selected = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self.editor_pending_crop = None
        self.editor_drag_start = None
        self.editor_stroke_points = []
        self.editor_object_drag = None
        self.editor_placing_signature = None
        self.editor_place_mode = "select" if self.editor_pdf_path else None
        if hasattr(self, "editor_canvas"):
            self.editor_canvas.delete("editor-draft")
            self.editor_canvas.configure(cursor="arrow")
        self._set_editor_tool_visual("select", "select")
        self.status.set("Выбор и выделенные области сброшены")
        self._render_editor_page()
        return "break"

    def _editor_reading_escape(self, event=None):
        return self._editor_escape(event)

    def _editor_reading_page_key(self, event):
        if not self.editor_reading_mode:
            return None
        if isinstance(event.widget, (tk.Entry, ttk.Entry, ttk.Combobox, tk.Text)):
            return None
        self._change_editor_page(-1 if event.keysym == "Left" else 1)
        return "break"

    def _editor_mousewheel_zoom(self, event) -> str:
        wheel_number = getattr(event, "num", 0)
        wheel_delta = getattr(event, "delta", 0)
        if wheel_number == 4 or wheel_delta > 0:
            self._change_editor_zoom(10)
        elif wheel_number == 5 or wheel_delta < 0:
            self._change_editor_zoom(-10)
        return "break"

    def _sync_editor_edits_visibility(self) -> None:
        panes = self.editor_content_panes
        visible = self.editor_edits_visible.get() and not self.editor_grid_mode and not self.editor_reading_mode
        attached = str(self.editor_edits_shell) in {str(pane) for pane in panes.panes()}
        if visible and not attached:
            panes.add(self.editor_edits_shell, minsize=165, width=185, stretch="never")
        elif not visible and attached:
            panes.forget(self.editor_edits_shell)

    def _show_editor_properties(self, name: str) -> None:
        if not hasattr(self, "editor_property_frames"):
            return
        for frame in self.editor_property_frames.values():
            frame.pack_forget()
        if name == "select" or self.editor_reading_mode or self.editor_grid_mode:
            self.editor_properties_shell.grid_remove()
            return
        self.editor_properties_shell.grid()
        frame = self.editor_property_frames.get(name) or self.editor_property_frames.get("select")
        if frame:
            frame.pack(fill="x")
            if hasattr(frame, "property_layout"):
                frame.after_idle(frame.property_layout)

    def _set_editor_tool_visual(self, mode: str, properties: str | None = None) -> None:
        self.editor_canvas.delete("eraser-preview")
        for name, button in self.editor_tool_buttons.items():
            button.configure(style="Active.Tool.TButton" if name == mode else "Tool.TButton")
        if properties:
            self._show_editor_properties(properties)

    def _show_editor_context_menu(self, event) -> str:
        self._dismiss_context_menus()
        menu = tk.Menu(
            self,
            tearoff=False,
            bg=PANEL,
            fg=TEXT,
            activebackground=BLUE,
            activeforeground="white",
            relief="solid",
            borderwidth=1,
            font=("Arial", 10),
        )
        self.editor_context_menu = menu
        if not self.editor_pdf_path:
            menu.add_command(label="Создать пустой лист…", command=self._create_blank_editor_document)
            menu.add_command(label="Открыть PDF или изображение…", command=self._choose_editor_file)
            menu.tk_popup(event.x_root, event.y_root)
            return "break"

        if self.editor_reading_mode:
            if self.editor_recognized_text_selection:
                menu.add_command(
                    label="Копировать выделенный текст    Ctrl+C",
                    command=self._copy_recognized_text,
                )
                menu.add_separator()
            if not self.editor_text_index:
                menu.add_command(label="Распознать текст", command=self._start_editor_ocr)
            menu.add_command(label="По ширине", command=self._fit_editor_width)
            menu.add_command(label="Страница целиком", command=self._fit_editor_page)
            menu.add_command(label="Масштаб 100%", command=lambda: self._set_editor_zoom(100.0))
            menu.add_separator()
            menu.add_command(label="Печать…", command=self._print_editor_document)
            menu.add_command(label="Выйти из режима чтения", command=self._exit_editor_reading_mode)
            try:
                menu.tk_popup(event.x_root, event.y_root)
            finally:
                menu.grab_release()
            return "break"

        canvas_x = self.editor_canvas.canvasx(event.x)
        canvas_y = self.editor_canvas.canvasy(event.y)
        page_x0, page_y0 = self.editor_offset
        page_x1 = page_x0 + self.editor_page_size[0] * self.editor_scale
        page_y1 = page_y0 + self.editor_page_size[1] * self.editor_scale
        if not (page_x0 <= canvas_x <= page_x1 and page_y0 <= canvas_y <= page_y1):
            menu.add_command(label="Добавить пустой лист…", command=self._create_blank_editor_document)
            menu.add_command(label="Очистить все открытые файлы", command=self._clear_editor_document)
            menu.add_separator()
            menu.add_command(label="Вписать страницу в окно", command=self._fit_editor_page)
            menu.add_command(label="Печать…", command=self._print_editor_document)
            menu.add_command(label="Сохранить как…", command=self._save_editor_pdf)
            menu.tk_popup(event.x_root, event.y_root)
            return "break"

        self.editor_context_canvas_point = (canvas_x, canvas_y)
        pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((canvas_x, canvas_y, canvas_x, canvas_y))
        selected = self._find_editor_object_at(pdf_x, pdf_y)
        self.editor_selected = selected
        self.editor_object_drag = None
        if selected:
            self._load_selected_editor_properties()
            if selected[0] != "text":
                self._show_editor_properties("select")
            self._render_editor_page()
            if selected[0] == "text":
                menu.add_command(label="Изменить текст…", command=self._edit_context_text)
            menu.add_command(label="Копировать объект    Ctrl+C", command=self._copy_selected_editor_object)
            menu.add_command(label="Создать копию объекта", command=self._duplicate_selected_editor_object)
            menu.add_command(label="Удалить объект", command=self._delete_selected_editor_object)
            menu.add_separator()

        if (
            self.editor_recognized_text_selection
            and self.editor_recognized_text_selection[0] == self.editor_page_index
        ):
            menu.add_command(
                label="Копировать распознанный текст    Ctrl+C",
                command=self._copy_recognized_text,
            )
        if self.editor_text_index:
            menu.add_command(
                label="Выделять распознанный текст",
                command=self._activate_recognized_text_selection,
            )
            menu.add_command(label="Заменить слово здесь…", command=self._replace_context_word)
        menu.add_command(label="Добавить текст здесь…", command=self._add_context_text)
        menu.add_command(label="Закрыть область…", command=self._start_erase_placement)
        menu.add_command(label="Выделить область копирования…", command=self._start_region_copy)
        if self.editor_region_selection and self.editor_region_selection[0] == self.editor_page_index:
            menu.add_command(label="Копировать выделенную область    Ctrl+C", command=self._copy_selected_editor_object)
        menu.add_command(label="Вставить    Ctrl+V", command=self._paste_editor_object)
        menu.add_command(label="Обрезать страницу…", command=self._start_crop_selection)
        menu.add_command(label="Выровнять страницу…", command=self._start_page_alignment)
        menu.add_separator()
        menu.add_command(label="Карандаш", command=self._start_pencil)
        menu.add_command(label="Линия", command=self._start_line)
        menu.add_command(label="Стрелка", command=self._start_arrow)
        menu.add_command(label="Ластик", command=self._start_free_eraser)
        if self._editor_is_pdf():
            split_menu = tk.Menu(menu, tearoff=False, bg=PANEL, fg=TEXT, activebackground=BLUE, activeforeground="white")
            for page_size in ("A4", "A3", "A5"):
                split_menu.add_command(
                    label=f"На {page_size}" + (" (по умолчанию)" if page_size == "A4" else ""),
                    command=lambda size=page_size: self._split_editor_document(size),
                )
            menu.add_cascade(label="Разрезать длинные страницы", menu=split_menu)
        if self._editor_is_pdf():
            menu.add_command(label="Определить стиль в этой точке", command=lambda: self._detect_style_at_canvas_point(canvas_x, canvas_y))

        signature_menu = tk.Menu(menu, tearoff=False, bg=PANEL, fg=TEXT, activebackground=BLUE, activeforeground="white")
        self.editor_signature_paths = list_signatures()
        if self.editor_signature_paths:
            for path in self.editor_signature_paths:
                signature_menu.add_command(label=path.stem, command=lambda item=path: self._add_context_signature(item))
            signature_menu.add_separator()
        signature_menu.add_command(label="Открыть библиотеку…", command=self._open_signature_library)
        menu.add_cascade(label="Добавить подпись или отметку", menu=signature_menu)
        menu.add_separator()
        menu.add_command(label="Добавить пустой лист…", command=self._create_blank_editor_document)
        menu.add_command(label="Очистить все открытые файлы", command=self._clear_editor_document)
        menu.add_separator()
        menu.add_command(label="Печать…", command=self._print_editor_document)
        menu.add_command(label="Сохранить как…", command=self._save_editor_pdf)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _dismiss_context_menus(self, event=None) -> str | None:
        if event is not None and getattr(event.widget, "winfo_class", lambda: "")() == "Menu":
            return
        self._dismiss_pdf_context_menu()
        self._dismiss_editor_context_menu()
        return "break"

    def _dismiss_pdf_context_menu(self) -> None:
        menu = self.pdf_context_menu
        if menu is not None:
            try:
                menu.unpost()
            except tk.TclError:
                pass
        self.pdf_context_menu = None

    def _dismiss_editor_context_menu(self, event=None) -> None:
        if event is not None and getattr(event.widget, "winfo_class", lambda: "")() == "Menu":
            return
        menu = self.editor_context_menu
        if menu is not None:
            try:
                menu.unpost()
            except tk.TclError:
                pass
        self.editor_context_menu = None
        return "break"

    def _context_pdf_point(self) -> tuple[float, float] | None:
        if not self.editor_context_canvas_point:
            return None
        canvas_x, canvas_y = self.editor_context_canvas_point
        pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((canvas_x, canvas_y, canvas_x, canvas_y))
        return pdf_x, pdf_y

    def _add_context_text(self) -> None:
        point = self._context_pdf_point()
        if point is None:
            return
        text = simpledialog.askstring(
            "Добавить текст",
            "Введите надпись:",
            initialvalue=self.editor_replace_text.get(),
            parent=self,
        )
        if not text:
            return
        self.editor_replace_text.set(text)
        font_size = float(self.editor_font_size.get())
        page_width, page_height = self.editor_page_size
        width = min(max(90.0, len(text) * font_size * 0.62 + 18.0), max(20.0, page_width))
        height = min(max(22.0, font_size * 1.8), max(10.0, page_height))
        x0 = min(max(0.0, point[0]), max(0.0, page_width - width))
        y0 = min(max(0.0, point[1]), max(0.0, page_height - height))
        color = self.editor_text_color.get().lstrip("#")
        rgb = tuple(int(color[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
        family = self.editor_font_family.get()
        self._remember_editor_state()
        self.editor_text_edits.append(
            TextEdit(
                self.editor_page_index,
                (x0, y0, x0 + width, y0 + height),
                text,
                font_size,
                family,
                resolve_font_file(family, self.editor_text_bold.get(), self.editor_text_italic.get()),
                rgb,
                bold=self.editor_text_bold.get(),
                italic=self.editor_text_italic.get(),
                underline=self.editor_text_underline.get(),
                scan_blur=self._editor_text_scan_blur(font_size),
            )
        )
        self.editor_selected = ("text", len(self.editor_text_edits) - 1)
        self._set_editor_tool_visual("select", "text")
        self.editor_place_mode = "select"
        self.status.set("Надпись добавлена; её можно перемещать и менять размер")
        self._render_editor_page()

    def _replace_context_word(self) -> None:
        if not self.editor_context_canvas_point:
            return
        self._replace_word_at_canvas_point(*self.editor_context_canvas_point)

    def _edit_context_text(self) -> None:
        edit = self._selected_editor_object()
        if not isinstance(edit, TextEdit):
            return
        value = simpledialog.askstring("Изменить текст", "Введите новый текст:", initialvalue=edit.text, parent=self)
        if value is None:
            return
        if edit.replacement_source_rect:
            self.editor_replace_text.set(value)
            self._apply_selected_word_properties()
            return
        self._remember_editor_state()
        index = self.editor_selected[1]
        self.editor_text_edits[index] = replace(edit, text=value)
        self.editor_replace_text.set(value)
        self._show_editor_properties("text")
        self.status.set("Текст изменён")
        self._render_editor_page()

    def _add_context_signature(self, path: Path) -> None:
        point = self._context_pdf_point()
        if point is None:
            return
        page_width, page_height = self.editor_page_size
        width = min(170.0, max(20.0, page_width))
        height = min(75.0, max(20.0, page_height))
        x0 = min(max(0.0, point[0]), max(0.0, page_width - width))
        y0 = min(max(0.0, point[1]), max(0.0, page_height - height))
        self._remember_editor_state()
        self.editor_signature_edits.append(SignatureEdit(self.editor_page_index, (x0, y0, x0 + width, y0 + height), str(path)))
        self.editor_selected = None
        self.editor_place_mode = "select"
        self._set_editor_tool_visual("select", "select")
        self.status.set("Подпись или отметка добавлена")
        self._render_editor_page()

    def _start_object_selection(self) -> None:
        self.editor_place_mode = "select"
        self.editor_placing_signature = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self.editor_pending_crop = None
        self.editor_drag_start = None
        self.editor_object_drag = None
        self.editor_canvas.delete("editor-draft")
        self.editor_canvas.configure(cursor="arrow")
        self._set_editor_tool_visual("select", "select")
        self.status.set("Выберите объект; перетащите его или потяните синий маркер для изменения размера")
        self._render_editor_page()

    def _find_editor_object_at(self, pdf_x: float, pdf_y: float) -> tuple[str, int] | None:
        for index in range(len(self.editor_text_edits) - 1, -1, -1):
            edit = self.editor_text_edits[index]
            if (
                edit.page_index == self.editor_page_index
                and not edit.opaque_background
                and self._point_in_rect(pdf_x, pdf_y, edit.rect)
            ):
                return "text", index
        for index in range(len(self.editor_stroke_edits) - 1, -1, -1):
            edit = self.editor_stroke_edits[index]
            rect = self._editor_object_rect("stroke", edit)
            if edit.page_index == self.editor_page_index and rect and self._point_in_rect(pdf_x, pdf_y, rect):
                return "stroke", index
        for index in range(len(self.editor_region_edits) - 1, -1, -1):
            edit = self.editor_region_edits[index]
            if edit.page_index == self.editor_page_index and self._point_in_rect(pdf_x, pdf_y, edit.rect):
                return "region", index
        for index in range(len(self.editor_signature_edits) - 1, -1, -1):
            edit = self.editor_signature_edits[index]
            if edit.page_index == self.editor_page_index and self._point_in_rect(pdf_x, pdf_y, edit.rect):
                return "signature", index
        for index in range(len(self.editor_text_edits) - 1, -1, -1):
            edit = self.editor_text_edits[index]
            if (
                edit.page_index == self.editor_page_index
                and edit.opaque_background
                and self._point_in_rect(pdf_x, pdf_y, edit.rect)
            ):
                return "text", index
        return None

    @staticmethod
    def _point_in_rect(x: float, y: float, rect) -> bool:
        return rect[0] <= x <= rect[2] and rect[1] <= y <= rect[3]

    def _selected_editor_object(self):
        if not self.editor_selected:
            return None
        kind, index = self.editor_selected
        collection = self._editor_collection(kind)
        if index >= len(collection):
            self.editor_selected = None
            return None
        return collection[index]

    def _editor_collection(self, kind: str):
        if kind == "text":
            return self.editor_text_edits
        if kind == "signature":
            return self.editor_signature_edits
        if kind == "region":
            return self.editor_region_edits
        if kind == "stroke":
            return self.editor_stroke_edits
        if kind == "crop":
            return self.editor_crop_edits
        if kind == "alignment":
            return self.editor_alignment_edits
        raise ValueError(f"Неизвестный тип объекта редактора: {kind}")

    def _editor_object_rect(self, kind: str, edit):
        if edit is None:
            return None
        if hasattr(edit, "rect"):
            return tuple(edit.rect)
        if kind == "stroke" and edit.points:
            padding = max(2.0, float(edit.width))
            xs = [point[0] for point in edit.points]
            ys = [point[1] for point in edit.points]
            return min(xs) - padding, min(ys) - padding, max(xs) + padding, max(ys) + padding
        return None

    def _load_selected_editor_properties(self) -> None:
        edit = self._selected_editor_object()
        if not isinstance(edit, TextEdit):
            return
        if edit.opaque_background and not edit.text:
            color = "#%02x%02x%02x" % tuple(round(value * 255) for value in edit.background_color)
            self._set_editor_draw_color(color, apply_to_selection=False)
            self._show_editor_properties("area")
            return
        self.editor_replace_text.set(edit.text)
        self.editor_font_size.set(edit.font_size)
        self.editor_font_family.set(edit.font_family)
        self.editor_text_bold.set(edit.bold)
        self.editor_text_italic.set(edit.italic)
        self.editor_text_underline.set(edit.underline)
        self.editor_word_scan_effect.set(edit.scan_effect)
        self.editor_text_scan_effect.set(edit.scan_blur > 0 and
                                         (not edit.replacement_source_rect or edit.scan_effect))
        color = "#%02x%02x%02x" % tuple(round(value * 255) for value in edit.color)
        self._set_editor_text_color(color)
        if edit.replacement_source_rect:
            self.editor_word_offset_x.set(round(edit.rect[0] - edit.replacement_source_rect[0], 1))
            self.editor_word_offset_y.set(round(edit.rect[1] - edit.replacement_source_rect[1], 1))
            self._show_editor_properties("word")
        else:
            self._show_editor_properties("text")

    def _on_editor_canvas_resize(self, _event=None) -> None:
        if not self.editor_pdf_path:
            self._draw_editor_empty_state()
            return
        if self.editor_zoom_mode != "page" or self.state() == "iconic":
            return
        if self.editor_page_refit_after_id is not None:
            try:
                self.after_cancel(self.editor_page_refit_after_id)
            except tk.TclError:
                pass
        self.editor_page_refit_after_id = self.after(90, self._refit_editor_page_after_resize)

    def _refit_editor_page_after_resize(self) -> None:
        self.editor_page_refit_after_id = None
        if self.editor_pdf_path and self.editor_zoom_mode == "page" and self.state() != "iconic":
            self._fit_editor_page()

    def _draw_editor_empty_state(self) -> None:
        if not hasattr(self, "editor_canvas") or self.editor_pdf_path:
            return
        canvas = self.editor_canvas
        canvas.delete("all")
        width = max(620, canvas.winfo_width())
        height = max(390, canvas.winfo_height())
        margin_x = 24
        margin_y = 22
        create_rounded_rectangle(
            canvas,
            margin_x,
            margin_y,
            width - margin_x,
            height - margin_y,
            radius=22,
            fill=DROP_ZONE_BG,
            outline="#BCD2F1",
            width=2,
            dash=(6, 5),
            tags="editor_empty",
        )
        center_x = width / 2
        center_y = height / 2
        if hasattr(self, "drop_zone_icon_image"):
            canvas.create_image(
                center_x,
                center_y - 76,
                image=self.drop_zone_icon_image,
                tags="editor_empty",
            )
        canvas.create_text(
            center_x,
            center_y + 28,
            text="Откройте или перетащите PDF/изображение",
            fill=TEXT,
            font=(FONT_FAMILY, 15, "bold"),
            tags="editor_empty",
        )
        canvas.create_text(
            center_x,
            center_y + 64,
            text="Файл откроется в рабочей области для просмотра и редактирования",
            fill=MUTED,
            font=(FONT_FAMILY, 9),
            tags="editor_empty",
        )
        canvas.configure(scrollregion=(0, 0, width, height))

    def _render_editor_page(self) -> None:
        if not self.editor_pdf_path:
            self._draw_editor_empty_state()
            return

        self.editor_canvas.update_idletasks()
        width = max(1, self.editor_canvas.winfo_width())
        height = max(1, self.editor_canvas.winfo_height())
        scale = max(0.001, min(4.0, self.editor_zoom_percent.get() / 100.0))
        angle = self._editor_alignment_angle()
        stat = self.editor_pdf_path.stat()
        key = (str(self.editor_pdf_path), stat.st_size, stat.st_mtime_ns, self.editor_page_index, scale, angle)
        cache = getattr(self, "_editor_preview_cache", None)
        if cache and cache[0] == key:
            _, self.editor_page_image, page_width, page_height, pix_width, pix_height, scale = cache
        else:
            self._render_editor_base_preview(key, scale, angle)
            _, self.editor_page_image, page_width, page_height, pix_width, pix_height, scale = self._editor_preview_cache
        offset_x = max(10.0, (width - pix_width) / 2)
        offset_y = max(10.0, (height - pix_height) / 2)
        self.editor_scale = scale
        self.editor_offset = (offset_x, offset_y)
        self.editor_page_size = (page_width, page_height)
        self.editor_canvas.delete("all")
        self.editor_canvas.create_image(offset_x, offset_y, image=self.editor_page_image, anchor="nw")
        self.editor_canvas.configure(
            scrollregion=(0, 0, max(width, pix_width + 20), max(height, pix_height + 20)),
        )
        self._render_editor_overlays(scale)

    def _render_editor_base_preview(self, key, scale, angle):
        requested = scale
        if self._editor_is_pdf():
            import fitz

            with fitz.open(str(self.editor_pdf_path)) as document:
                page = document[self.editor_page_index]
                page_width, page_height = page.rect.width, page.rect.height
                scale = bounded_preview_scale(page_width, page_height, scale)
                pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
            image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        else:
            with Image.open(self.editor_pdf_path) as source_image:
                source_image = ImageOps.exif_transpose(source_image).convert("RGB")
                page_width, page_height = source_image.size
                scale = bounded_preview_scale(page_width, page_height, scale)
                pix_width = max(1, int(page_width * scale))
                pix_height = max(1, int(page_height * scale))
                image = source_image.resize((pix_width, pix_height), LANCZOS)
        if abs(angle) >= 0.01:
            rotated = rotate_raster_image(image, angle)
            image.close()
            image = rotated
        self.editor_page_image = ImageTk.PhotoImage(image)
        pix_width, pix_height = image.size
        image.close()
        if scale < requested:
            self.editor_zoom_percent.set(scale * 100)
            self.status.set("Масштаб ограничен для экономии памяти на длинной странице")
            key = (*key[:4], scale, angle)
        self._editor_preview_cache = (key, self.editor_page_image, page_width, page_height, pix_width, pix_height, scale)

    def _render_editor_overlays(self, scale):
        for hit_index, hit in enumerate(self.editor_search_hits):
            if hit.page_index != self.editor_page_index:
                continue
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(self._search_hit_editor_rect(hit))
            current = hit_index == self.editor_search_hit_index
            self.editor_canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                fill="#FFE36E",
                stipple="gray25",
                outline="#F28C00" if current else "#D6A600",
                width=3 if current else 1,
                tags="search-hit",
            )
        if (
            self.editor_recognized_text_selection
            and self.editor_recognized_text_selection[0] == self.editor_page_index
        ):
            for indexed_rect in self.editor_recognized_text_selection[1]:
                source_rect = self._indexed_rect_to_editor(self.editor_page_index, indexed_rect)
                x0, y0, x1, y1 = self._pdf_rect_to_canvas(source_rect)
                self.editor_canvas.create_rectangle(
                    x0,
                    y0,
                    x1,
                    y1,
                    fill="#87CEFA",
                    stipple="gray25",
                    outline="#0079C2",
                    width=1,
                    tags="recognized-text-selection",
                )
        for index, edit in enumerate(self.editor_text_edits):
            if edit.page_index != self.editor_page_index or not edit.opaque_background:
                continue
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.rect)
            fill = "#%02x%02x%02x" % tuple(round(value * 255) for value in edit.background_color)
            self.editor_canvas.create_rectangle(x0, y0, x1, y1, fill=fill, outline="")
        self.editor_overlay_images = []
        for edit in self.editor_text_edits:
            if (
                edit.page_index != self.editor_page_index
                or not edit.replacement_background
                or not edit.replacement_background_rect
                or not Path(edit.replacement_background).is_file()
            ):
                continue
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.replacement_background_rect)
            with Image.open(edit.replacement_background) as opened:
                preview = opened.convert("RGBA")
            resized = preview.resize((max(1, int(x1 - x0)), max(1, int(y1 - y0))), LANCZOS)
            preview.close()
            photo = ImageTk.PhotoImage(resized)
            resized.close()
            self.editor_overlay_images.append(photo)
            self.editor_canvas.create_image(x0, y0, image=photo, anchor="nw")
        for index, edit in enumerate(self.editor_signature_edits):
            if edit.page_index != self.editor_page_index:
                continue
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.rect)
            selected = self.editor_selected == ("signature", index)
            with Image.open(edit.image_path) as signature:
                preview = signature.convert("RGBA")
                target_size = (max(1, int(x1 - x0)), max(1, int(y1 - y0)))
                if edit.keep_proportion:
                    preview.thumbnail(target_size)
                else:
                    resized = preview.resize(target_size, LANCZOS)
                    preview.close()
                    preview = resized
                photo = ImageTk.PhotoImage(preview)
                preview.close()
            self.editor_overlay_images.append(photo)
            self.editor_canvas.create_image((x0 + x1) / 2, (y0 + y1) / 2, image=photo)
            if selected:
                self.editor_canvas.create_rectangle(x0, y0, x1, y1, outline=BLUE, width=2, dash=(4, 2))
                self._draw_editor_resize_handle(x1, y1)
        visible_regions = [(index, edit) for index, edit in enumerate(self.editor_region_edits) if edit.page_index == self.editor_page_index]
        if visible_regions:
            if self._editor_is_pdf():
                import fitz

                with fitz.open(str(self.editor_pdf_path)) as original:
                    source_page = original[self.editor_page_index]
                    aligned_source = None
                    angle = self._editor_alignment_angle()
                    if abs(angle) >= 0.01:
                        full_pixmap = source_page.get_pixmap(matrix=fitz.Matrix(scale, scale), alpha=False)
                        aligned_source = Image.frombytes(
                            "RGB", (full_pixmap.width, full_pixmap.height), full_pixmap.samples
                        )
                        rotated = rotate_raster_image(aligned_source, angle)
                        aligned_source.close()
                        aligned_source = rotated
                    for index, edit in visible_regions:
                        x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.rect)
                        if aligned_source is not None:
                            source_box = tuple(int(round(value * scale)) for value in edit.source_rect)
                            preview = aligned_source.crop(source_box)
                        else:
                            pixmap = source_page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=fitz.Rect(edit.source_rect), alpha=False)
                            preview = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
                        preview = preview.resize((max(1, int(x1 - x0)), max(1, int(y1 - y0))), LANCZOS)
                        photo = ImageTk.PhotoImage(preview)
                        preview.close()
                        self.editor_overlay_images.append(photo)
                        self.editor_canvas.create_image((x0 + x1) / 2, (y0 + y1) / 2, image=photo)
                        selected = self.editor_selected == ("region", index)
                        if selected:
                            self.editor_canvas.create_rectangle(x0, y0, x1, y1, outline=BLUE, width=3)
                            self._draw_editor_resize_handle(x1, y1)
                    if aligned_source is not None:
                        aligned_source.close()
            else:
                with Image.open(self.editor_pdf_path) as original_image:
                    original_image = ImageOps.exif_transpose(original_image).convert("RGB")
                    angle = self._editor_alignment_angle()
                    if abs(angle) >= 0.01:
                        rotated = rotate_raster_image(original_image, angle)
                        original_image.close()
                        original_image = rotated
                    for index, edit in visible_regions:
                        x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.rect)
                        sx0, sy0, sx1, sy1 = (int(round(value)) for value in edit.source_rect)
                        preview = original_image.crop((sx0, sy0, sx1, sy1))
                        preview = preview.resize((max(1, int(x1 - x0)), max(1, int(y1 - y0))), LANCZOS)
                        photo = ImageTk.PhotoImage(preview)
                        preview.close()
                        self.editor_overlay_images.append(photo)
                        self.editor_canvas.create_image((x0 + x1) / 2, (y0 + y1) / 2, image=photo)
                        selected = self.editor_selected == ("region", index)
                        if selected:
                            self.editor_canvas.create_rectangle(x0, y0, x1, y1, outline=BLUE, width=3)
                            self._draw_editor_resize_handle(x1, y1)
        for index, edit in enumerate(self.editor_stroke_edits):
            if edit.page_index != self.editor_page_index or not edit.points:
                continue
            points = []
            for point in edit.points:
                cx, cy, _, _ = self._pdf_rect_to_canvas((point[0], point[1], point[0], point[1]))
                points.extend((cx, cy))
            color = "#%02x%02x%02x" % tuple(round(value * 255) for value in edit.color)
            if len(points) == 2:
                x, y = points
                radius = max(1, int(edit.width * scale / 2))
                if edit.style == "eraser":
                    self.editor_canvas.create_rectangle(
                        x - radius, y - radius, x + radius, y + radius, fill=color, outline=color
                    )
                else:
                    self.editor_canvas.create_oval(
                        x - radius, y - radius, x + radius, y + radius, fill=color, outline=color
                    )
            else:
                self.editor_canvas.create_line(
                    *points,
                    fill=color,
                    width=max(1, int(edit.width * scale)),
                    capstyle=tk.PROJECTING if edit.style == "eraser" else tk.ROUND,
                    joinstyle=tk.MITER if edit.style == "eraser" else tk.ROUND,
                    smooth=edit.style == "free",
                    arrow=tk.LAST if edit.style == "arrow" else tk.NONE,
                )
            if self.editor_selected == ("stroke", index):
                rect = self._editor_object_rect("stroke", edit)
                x0, y0, x1, y1 = self._pdf_rect_to_canvas(rect)
                self.editor_canvas.create_rectangle(x0, y0, x1, y1, outline=BLUE, width=2, dash=(4, 2))
        for index, edit in enumerate(self.editor_text_edits):
            if edit.page_index != self.editor_page_index:
                continue
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.rect)
            selected = self.editor_selected == ("text", index)
            if edit.text and (
                edit.replacement_source_rect
                or (not edit.replacement_source_rect and edit.scan_blur > 0)
            ):
                overlay, bounds = word_text_overlay(edit)
                left, top, right, bottom = self._pdf_rect_to_canvas(bounds)
                preview = overlay.resize((max(1, round(right-left)), max(1, round(bottom-top))), LANCZOS)
                overlay.close()
                photo = ImageTk.PhotoImage(preview)
                preview.close()
                self.editor_overlay_images.append(photo)
                self.editor_canvas.create_image(left, top, image=photo, anchor="nw")
            elif edit.text:
                color = "#%02x%02x%02x" % tuple(round(value * 255) for value in edit.color)
                self.editor_canvas.create_text(
                    x0,
                    y0,
                    text=edit.text,
                    fill=color,
                    anchor="nw",
                    justify="left",
                    font=editor_preview_font_spec(edit, scale),
                )
            if selected:
                self.editor_canvas.create_rectangle(x0, y0, x1, y1, outline=BLUE, width=2, dash=(4, 2))
                if not edit.replacement_source_rect:
                    self._draw_editor_resize_handle(x1, y1)
        crop_previews = list(self.editor_crop_edits)
        if self.editor_pending_crop is not None:
            crop_previews = [edit for edit in crop_previews if edit.page_index != self.editor_page_index]
            crop_previews.append(CropEdit(self.editor_page_index, self.editor_pending_crop))
        for index, edit in enumerate(crop_previews):
            if edit.page_index != self.editor_page_index:
                continue
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(edit.rect)
            page_x0, page_y0 = self.editor_offset
            page_x1 = page_x0 + self.editor_page_size[0] * self.editor_scale
            page_y1 = page_y0 + self.editor_page_size[1] * self.editor_scale
            self.editor_canvas.create_rectangle(page_x0, page_y0, page_x1, y0, fill="#2F3B45", stipple="gray25", outline="")
            self.editor_canvas.create_rectangle(page_x0, y1, page_x1, page_y1, fill="#2F3B45", stipple="gray25", outline="")
            self.editor_canvas.create_rectangle(page_x0, y0, x0, y1, fill="#2F3B45", stipple="gray25", outline="")
            self.editor_canvas.create_rectangle(x1, y0, page_x1, y1, fill="#2F3B45", stipple="gray25", outline="")
            selected = self.editor_selected == ("crop", index)
            self.editor_canvas.create_rectangle(
                x0,
                y0,
                x1,
                y1,
                outline=BLUE if selected else CYAN,
                width=3 if selected else 2,
                dash=(5, 3),
            )
        if self.editor_region_selection and self.editor_region_selection[0] == self.editor_page_index:
            x0, y0, x1, y1 = self._pdf_rect_to_canvas(self.editor_region_selection[1])
            self.editor_canvas.create_rectangle(x0, y0, x1, y1, outline="#00B4D8", width=3, dash=(6, 3))
        self.editor_page_label.set(f"{self.editor_page_index + 1} / {self.editor_page_count}")
        self.editor_reader_page_label.set(f"{self.editor_page_index + 1} / {self.editor_page_count}")
        self.editor_zoom_label.set(f"{int(round(self.editor_zoom_percent.get()))}%")
        self._refresh_editor_edits_panel()
        self._refresh_editor_thumbnail_state()

    def _draw_editor_resize_handle(self, x: float, y: float) -> None:
        size = 6
        self.editor_canvas.create_rectangle(x - size, y - size, x + size, y + size, fill=BLUE, outline="white", width=1)

    def _pdf_rect_to_canvas(self, rect) -> tuple[float, float, float, float]:
        ox, oy = self.editor_offset
        return tuple(value * self.editor_scale + (ox if index % 2 == 0 else oy) for index, value in enumerate(rect))

    def _canvas_rect_to_pdf(self, rect) -> tuple[float, float, float, float]:
        ox, oy = self.editor_offset
        page_width, page_height = self.editor_page_size
        x0, y0, x1, y1 = rect
        values = (
            max(0.0, min(page_width, (x0 - ox) / self.editor_scale)),
            max(0.0, min(page_height, (y0 - oy) / self.editor_scale)),
            max(0.0, min(page_width, (x1 - ox) / self.editor_scale)),
            max(0.0, min(page_height, (y1 - oy) / self.editor_scale)),
        )
        return values

    def _editor_scrollable_property_row(self, parent):
        shell = tk.Frame(parent, bg=SURFACE_ALT)
        canvas = tk.Canvas(shell, bg=SURFACE_ALT, highlightthickness=0, width=1, height=1)
        canvas.pack(fill="x")
        scrollbar = ttk.Scrollbar(shell, orient="horizontal", command=canvas.xview)
        canvas.configure(xscrollcommand=scrollbar.set)
        row = tk.Frame(canvas, bg=SURFACE_ALT)
        canvas.create_window(0, 0, window=row, anchor="nw")
        shell.property_row = row
        shell.property_canvas = canvas

        def layout(_event=None):
            needed = row.winfo_reqwidth()
            available = canvas.winfo_width()
            height = row.winfo_reqheight()
            canvas.configure(height=height, scrollregion=(0, 0, max(needed, available), height))
            if needed > available:
                if not scrollbar.winfo_manager():
                    scrollbar.pack(fill="x")
            else:
                scrollbar.pack_forget()
                canvas.xview_moveto(0)

        canvas.bind("<Configure>", layout)
        row.bind("<Configure>", layout)
        shell.property_layout = layout
        return shell, row

    def _build_editor_color_palette(self, parent, variable, setter, custom_command, compact=False) -> tk.Button:
        palette = tk.Frame(parent, bg=SURFACE_ALT)
        palette.pack(side="left", padx=(0, 5))
        for color, name in EDITOR_BASIC_COLORS:
            button = tk.Button(
                palette,
                text="",
                command=lambda value=color: setter(value),
                bg=color,
                activebackground=color,
                relief="solid",
                borderwidth=1,
                highlightthickness=0,
                width=1 if compact else 2,
                height=1,
                padx=2 if compact else 0,
                pady=0,
            )
            button.pack(side="left", padx=1)
            self._tip(button, name)
        custom = tk.Button(
            palette,
            text="…" if compact else "Свой…",
            command=custom_command,
            bg=variable.get(),
            fg=self._editor_color_foreground(variable.get()),
            activebackground=variable.get(),
            relief="solid",
            borderwidth=1,
            padx=5,
            pady=2,
        )
        custom.pack(side="left", padx=(4, 0))
        self._tip(custom, "Выбрать свой цвет")
        return custom

    @staticmethod
    def _editor_color_foreground(color: str) -> str:
        value = color.lstrip("#")
        try:
            red, green, blue = (int(value[index:index + 2], 16) for index in (0, 2, 4))
        except (TypeError, ValueError):
            return "white"
        return TEXT if red * 299 + green * 587 + blue * 114 > 160000 else "white"

    def _set_editor_text_color(self, color: str) -> None:
        self.editor_text_color.set(color)
        for name in ("editor_color_button", "editor_word_color_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(
                    bg=color,
                    fg=self._editor_color_foreground(color),
                    activebackground=color,
                )

    def _set_editor_draw_color(self, color: str, *, apply_to_selection: bool = True) -> None:
        self.editor_draw_color.set(color)
        for name in ("editor_draw_color_button", "editor_area_color_button"):
            button = getattr(self, name, None)
            if button is not None:
                button.configure(bg=color, fg=self._editor_color_foreground(color), activebackground=color)
        edit = self._selected_editor_object()
        if (apply_to_selection and self.editor_place_mode in {None, "select"}
                and isinstance(edit, TextEdit) and edit.opaque_background and not edit.text):
            rgb = tuple(int(color.lstrip("#")[i:i + 2], 16) / 255.0 for i in (0, 2, 4))
            if edit.background_color != rgb:
                self._remember_editor_state()
                self.editor_text_edits[self.editor_selected[1]] = replace(edit, background_color=rgb, color=rgb)
                self.status.set("Цвет закрытой области изменён")
                self._render_editor_page()

    def _choose_editor_text_color(self) -> None:
        selected = colorchooser.askcolor(self.editor_text_color.get(), title="Цвет текста", parent=self)
        if selected[1]:
            self._set_editor_text_color(selected[1])

    def _start_word_color_pick(self) -> None:
        if not self.editor_pdf_path:
            return
        self.editor_place_mode = "word_color_pick"
        self.editor_drag_start = None
        self.editor_object_drag = None
        self.editor_canvas.configure(cursor="crosshair")
        self.status.set("Пипетка: щёлкните по буквам нужного цвета; Esc — отмена")

    def _pick_word_color(self, canvas_x, canvas_y) -> None:
        x = (canvas_x - self.editor_offset[0]) / self.editor_scale
        y = (canvas_y - self.editor_offset[1]) / self.editor_scale
        if not 0 <= x < self.editor_page_size[0] or not 0 <= y < self.editor_page_size[1]:
            return
        image, scale = self._render_editor_composite(self.editor_page_index)
        try:
            px, py = round(x * scale), round(y * scale)
            colors = [image.getpixel((xx, yy))[:3]
                      for yy in range(max(0, py-1), min(image.height, py+2))
                      for xx in range(max(0, px-1), min(image.width, px+2))]
            color = min(colors, key=lambda c: c[0]*299 + c[1]*587 + c[2]*114)
            self._set_editor_text_color("#%02x%02x%02x" % color)
        finally:
            image.close()
        self.editor_place_mode = "select"
        self.editor_canvas.configure(cursor="")
        self.status.set("Цвет выбран; нажмите «Применить» для изменения слова")

    def _save_word_style(self) -> None:
        styles = normalize_word_styles(self.preferences.get("word_style_presets"))
        name = simpledialog.askstring("Настройки слова", "Название набора настроек:", parent=self)
        if not name or not name.strip():
            return
        name = name.strip()[:60]
        if name not in styles and len(styles) >= 20:
            messagebox.showinfo("Настройки слова", "Сохранено 20 наборов. Удалите ненужный набор.", parent=self)
            return
        if name in styles and not messagebox.askyesno("Настройки слова", "Заменить сохранённый набор?", parent=self):
            return
        try:
            style = {"font_family": self.editor_font_family.get(), "font_size": self.editor_font_size.get(),
                     "color": self.editor_text_color.get(), "bold": self.editor_text_bold.get(),
                     "italic": self.editor_text_italic.get(), "underline": self.editor_text_underline.get(),
                     "scan_effect": self.editor_word_scan_effect.get()}
            valid = normalize_word_styles({name: style})
            if not valid:
                raise ValueError("Проверьте шрифт, размер (6–72 пт) и цвет текста")
            styles.update(valid)
            self.preferences["word_style_presets"] = styles
            save_preferences(self.preferences)
        except (OSError, ValueError, tk.TclError) as exc:
            self._show_error(exc)
            return
        self.status.set(f"Настройки слова «{name}» сохранены")

    def _load_word_style(self, name: str) -> None:
        style = normalize_word_styles(self.preferences.get("word_style_presets")).get(name)
        if not style:
            return
        self.editor_font_family.set(style["font_family"])
        self.editor_font_size.set(style["font_size"])
        self._set_editor_text_color(style["color"])
        for key, variable in (("bold", self.editor_text_bold), ("italic", self.editor_text_italic),
                              ("underline", self.editor_text_underline), ("scan_effect", self.editor_word_scan_effect)):
            variable.set(style[key])
        self.status.set(f"Настройки «{name}» загружены; нажмите «Применить»")

    def _delete_word_style(self, name: str) -> None:
        styles = normalize_word_styles(self.preferences.get("word_style_presets"))
        styles.pop(name, None)
        self.preferences["word_style_presets"] = styles
        try:
            save_preferences(self.preferences)
        except OSError as exc:
            self._show_error(exc)

    def _refresh_word_styles_menu(self) -> None:
        menu = self.editor_word_styles_menu
        menu.delete(0, "end")
        for child in menu.winfo_children():
            child.destroy()
        styles = normalize_word_styles(self.preferences.get("word_style_presets"))
        for name in styles:
            menu.add_command(label=name, command=lambda n=name: self._load_word_style(n))
        if styles:
            menu.add_separator()
            delete_menu = tk.Menu(menu, tearoff=False)
            for name in styles:
                delete_menu.add_command(label=name, command=lambda n=name: self._delete_word_style(n))
            menu.add_cascade(label="Удалить набор", menu=delete_menu)
        else:
            menu.add_command(label="Нет сохранённых настроек", state="disabled")

    def _choose_editor_draw_color(self) -> None:
        selected = colorchooser.askcolor(self.editor_draw_color.get(), title="Цвет рисования и закрытия", parent=self)
        if selected[1]:
            self._set_editor_draw_color(selected[1])

    def _start_text_placement(self) -> None:
        self._set_editor_tool_visual("text", "text")
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        if not self.editor_replace_text.get():
            self.editor_place_mode = "select"
            self.editor_text_entry.focus_set()
            self.status.set("Введите текст в строке параметров и нажмите «Разместить»")
            return
        self.editor_place_mode = "text"
        self.editor_canvas.configure(cursor="crosshair")
        self.status.set("Щёлкните место вставки текста или протяните рамку нужного размера")

    def _start_erase_placement(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        self.editor_place_mode = "erase"
        self.editor_canvas.configure(cursor="crosshair")
        self._set_editor_tool_visual("erase", "area")
        self.status.set("Выделите область, которую нужно закрыть")

    def _start_region_copy(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        if self._editor_is_pdf() and pdf_has_signatures(self.editor_pdf_path):
            messagebox.showwarning(
                "Копирование недоступно",
                "Документ содержит криптографическую подпись. Копирование областей для него отключено.",
            )
            return
        self.editor_place_mode = "copy_region"
        self.editor_selected = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self.editor_pending_crop = None
        self.editor_canvas.delete("editor-draft")
        self.editor_canvas.configure(cursor="crosshair")
        self._set_editor_tool_visual("copy_region", "select")
        self.status.set("Выделите область, затем нажмите Ctrl+C или используйте меню правой кнопки мыши")
        self._render_editor_page()

    def _start_style_detection(self) -> None:
        self._set_editor_tool_visual("detect", "text")
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF")
            return
        if not self._editor_is_pdf():
            messagebox.showinfo("Определение стиля", "Стиль можно определить только в PDF с текстовым слоем")
            return
        self.editor_place_mode = "detect"
        self.editor_canvas.configure(cursor="crosshair")
        self.status.set("Кликните по тексту в PDF для определения стиля")

    def _start_pencil(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        self.editor_place_mode = "pencil"
        self.editor_canvas.configure(cursor="pencil")
        self._set_editor_tool_visual("pencil", "draw")
        self.status.set("Рисуйте мышью поверх страницы или изображения")

    def _start_line(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        self.editor_place_mode = "line"
        self.editor_canvas.configure(cursor="crosshair")
        self._set_editor_tool_visual("line", "draw")
        self.status.set("Протяните линию от начальной до конечной точки")

    def _start_arrow(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        self.editor_place_mode = "arrow"
        self.editor_canvas.configure(cursor="crosshair")
        self._set_editor_tool_visual("arrow", "draw")
        self.status.set("Протяните стрелку от начальной до конечной точки")

    def _start_free_eraser(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        self.editor_place_mode = "free_erase"
        self.editor_canvas.configure(cursor="dotbox")
        self._set_editor_tool_visual("free_erase", "draw")
        self.status.set("Проведите ластиком по области, которую нужно закрыть")

    def _start_crop_selection(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        self.editor_place_mode = "crop"
        self.editor_pending_crop = None
        self.editor_canvas.configure(cursor="crosshair")
        self._set_editor_tool_visual("crop", "crop")
        self.status.set("Выделите область, затем нажмите «Применить обрезку»")

    def _editor_alignment_angle(self, page_index: int | None = None) -> float:
        index = self.editor_page_index if page_index is None else int(page_index)
        if self.editor_alignment_preview and self.editor_alignment_preview[0] == index:
            return float(self.editor_alignment_preview[1])
        for edit in reversed(self.editor_alignment_edits):
            if edit.page_index == index:
                return float(edit.angle)
        return 0.0

    def _start_page_alignment(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("Выравнивание", "Сначала откройте PDF или изображение")
            return
        page_index = self.editor_page_index
        source = Path(self.editor_pdf_path)
        self._set_editor_tool_visual("align", "select")
        self.status.set("Определяю наклон текста на странице…")

        def show_dialog(detected: float) -> None:
            current = self._editor_alignment_angle(page_index)
            initial = current if abs(current) >= 0.01 else float(detected)

            def preview(value) -> None:
                self.editor_alignment_preview = None if value is None else (page_index, float(value))
                if self.editor_page_index == page_index:
                    self._render_editor_page()

            self.editor_alignment_preview = (page_index, initial)
            self._render_editor_page()
            dialog = PageAlignmentDialog(self, initial, float(detected), preview)
            self.editor_alignment_preview = None
            if dialog.result is None:
                self._render_editor_page()
                self.status.set("Выравнивание отменено")
                return
            value = float(dialog.result)
            if abs(value - current) < 0.01:
                self._render_editor_page()
                return
            self._remember_editor_state()
            self.editor_alignment_edits = [
                edit for edit in self.editor_alignment_edits if edit.page_index != page_index
            ]
            if abs(value) >= 0.01:
                self.editor_alignment_edits.append(AlignmentEdit(page_index, value))
            self.editor_selected = None
            self.editor_thumbnail_generation += 1
            self._refresh_editor_thumbnails()
            self.status.set(f"Страница выровнена: {value:+.2f}°")
            self._render_editor_page()

        self._run_task(lambda: estimate_page_alignment(source, page_index), 'Наклон страницы определён', on_success=show_dialog)

    def _cancel_pending_crop(self) -> None:
        self.editor_pending_crop = None
        self.editor_place_mode = "select" if self.editor_pdf_path else None
        if hasattr(self, "editor_canvas"):
            self.editor_canvas.delete("editor-draft")
            self.editor_canvas.configure(cursor="arrow")
        self._set_editor_tool_visual("select", "select")
        self.status.set("Обрезка отменена")
        self._render_editor_page()

    def _apply_pending_crop(self) -> None:
        if self.editor_index_busy or self.editor_pages_busy or self.editor_word_busy_dialog is not None:
            return
        if not self.editor_pdf_path or self.editor_pending_crop is None:
            messagebox.showinfo("Обрезка", "Сначала выделите область, которая должна остаться")
            return
        source = Path(self.editor_pdf_path)
        snapshot = self._editor_state_snapshot()
        history = list(self.editor_undo_stack)
        crop = CropEdit(self.editor_page_index, self.editor_pending_crop)
        destination = self._editor_cache_file(f"crop-{time.time_ns()}{'.pdf' if self._editor_is_pdf() else '.png'}")
        text_edits = list(self.editor_text_edits)
        signature_edits = list(self.editor_signature_edits)
        region_edits = list(self.editor_region_edits)
        stroke_edits = list(self.editor_stroke_edits)
        alignment_edits = list(self.editor_alignment_edits)

        if self._editor_is_pdf():
            callback = lambda: apply_pdf_edits(
                source,
                destination,
                text_edits,
                signature_edits,
                region_edits,
                stroke_edits,
                [crop],
                alignment_edits,
            )
        else:
            callback = lambda: apply_image_edits(
                source,
                destination,
                text_edits,
                signature_edits,
                region_edits,
                stroke_edits,
                crop.rect,
                alignment_edits=alignment_edits,
            )
        self._show_word_busy("Применение обрезки…", title="Обрезка страницы")
        self._run_task(callback, 'Обрезка применена', on_success=lambda result: self._editor_crop_applied(Path(result), source, snapshot, history), on_complete=self._finish_word_busy, quiet=True)

    def _editor_crop_applied(self, result: Path, original: Path, snapshot, history) -> None:
        if result.suffix.lower() == ".pdf":
            self._load_editor_pdf(result)
            suggested_suffix = ".pdf"
        else:
            self._load_editor_image(result)
            suggested_suffix = original.suffix if original.suffix.lower() in IMAGE_SUFFIXES else ".png"
        self.editor_new_document = True
        self.editor_undo_stack = (history + [snapshot])[-100:]
        self.editor_redo_stack.clear()
        self.editor_page_index = min(snapshot[11], self.editor_page_count - 1)
        self.editor_last_save_path = original.with_name(f"{original.stem}_cropped{suggested_suffix}")
        self.editor_dirty = True
        self.editor_pending_crop = None
        self.status.set("Обрезка применена: размер страницы уменьшен до выбранной области")
        self._queue_autosave()

    def _detect_style_at_canvas_point(self, x: float, y: float) -> None:
        if not self.editor_pdf_path:
            return
        pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((x, y, x, y))
        style = detect_text_style(self.editor_pdf_path, self.editor_page_index, pdf_x, pdf_y)
        self.editor_place_mode = None
        self.editor_canvas.configure(cursor="")
        self._set_editor_tool_visual("text", "text")
        if not style:
            messagebox.showinfo(
                "Определение стиля",
                "В этой точке нет текстового слоя. Для скана выберите кириллический шрифт и размер вручную.",
            )
            return
        family = style["font_family"]
        known_fonts = list(self.editor_font_box.cget("values"))
        resolved = style.get("font_file")
        display_family = family
        for known in known_fonts:
            if known.casefold() in family.replace("-", " ").casefold():
                display_family = known
                break
        if display_family not in known_fonts and resolved:
            known_fonts.append(display_family)
            self.editor_font_box.configure(values=known_fonts)
        self.editor_font_family.set(display_family if resolved else "DejaVu Sans")
        self.editor_font_size.set(round(style["font_size"], 1))
        self.editor_text_bold.set(bool(style.get("bold", False)))
        self.editor_text_italic.set(bool(style.get("italic", False)))
        self.editor_text_underline.set(False)
        red, green, blue = (round(value * 255) for value in style["color"])
        color = f"#{red:02x}{green:02x}{blue:02x}"
        self.editor_text_color.set(color)
        self.editor_color_button.configure(bg=color)
        self.status.set(f"Стиль определён: {family}, {style['font_size']:.1f} пт")

    def _refresh_signature_list(self) -> None:
        self.editor_signature_paths = list_signatures()
        if not hasattr(self, "signature_list") or not self.signature_list.winfo_exists():
            return
        self.signature_list.delete(0, tk.END)
        for path in self.editor_signature_paths:
            self.signature_list.insert(tk.END, path.stem)

    def _open_signature_library(self) -> None:
        self._set_editor_tool_visual("signature", "signature")
        existing = getattr(self, "signature_window", None)
        if existing is not None and existing.winfo_exists():
            existing.lift()
            existing.focus_force()
            return

        window = tk.Toplevel(self)
        self.signature_window = window
        window.title("Библиотека подписей и отметок")
        window.geometry("480x390")
        window.minsize(440, 340)
        window.transient(self)
        window.configure(bg=BG)
        window.columnconfigure(0, weight=1)
        window.rowconfigure(1, weight=1)
        window.protocol("WM_DELETE_WINDOW", self._close_signature_library)

        heading = tk.Frame(window, bg=BG)
        heading.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 6))
        heading.columnconfigure(0, weight=1)
        tk.Label(heading, text="Подписи и отметки", bg=BG, fg=DEEP_BLUE, font=("Arial", 16, "bold")).grid(row=0, column=0, sticky="w")
        close_button = tk.Button(
            heading,
            text="×",
            command=self._close_signature_library,
            bg=BG,
            fg=MUTED,
            activebackground=SURFACE_ALT,
            activeforeground=TEXT,
            relief="flat",
            borderwidth=0,
            padx=8,
            pady=2,
            cursor="hand2",
            font=("Arial", 16),
        )
        close_button.grid(row=0, column=1, sticky="e")
        self._tip(close_button, "Закрыть библиотеку")

        list_holder = tk.Frame(window, bg=PANEL, highlightthickness=1, highlightbackground=BORDER)
        list_holder.grid(row=1, column=0, sticky="nsew", padx=18)
        list_holder.columnconfigure(0, weight=1)
        list_holder.rowconfigure(0, weight=1)
        self.signature_list = tk.Listbox(
            list_holder,
            height=8,
            exportselection=False,
            bg=PANEL,
            fg=TEXT,
            selectbackground=BLUE,
            selectforeground="white",
            relief="flat",
            borderwidth=0,
            activestyle="none",
            font=("Arial", 10),
        )
        self.signature_list.grid(row=0, column=0, sticky="nsew", padx=8, pady=8)
        scrollbar = ttk.Scrollbar(list_holder, orient="vertical", command=self.signature_list.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.signature_list.configure(yscrollcommand=scrollbar.set)
        self.signature_list.bind("<Double-Button-1>", lambda _event: self._place_signature_from_library())
        self._refresh_signature_list()

        options = tk.Frame(window, bg=BG)
        options.grid(row=2, column=0, sticky="ew", padx=18, pady=(10, 6))
        ttk.Checkbutton(options, text="Удалять фон", variable=self.signature_remove_background).pack(anchor="w")
        ttk.Checkbutton(options, text="Усилить бледные штрихи", variable=self.signature_enhance).pack(anchor="w")

        buttons = tk.Frame(window, bg=BG)
        buttons.grid(row=3, column=0, sticky="ew", padx=18, pady=(4, 16))
        ttk.Button(buttons, text="Добавить файл…", command=self._import_editor_signature).pack(side="left")
        ttk.Button(buttons, text="Удалить", command=self._delete_editor_signature).pack(side="left", padx=5)
        ttk.Button(buttons, text="Разместить", command=self._place_signature_from_library, style="Accent.TButton").pack(side="right")
        self._install_tooltips()

    def _close_signature_library(self) -> None:
        window = getattr(self, "signature_window", None)
        if window is not None and window.winfo_exists():
            window.destroy()
        self.signature_window = None

    def _place_signature_from_library(self) -> None:
        self._start_signature_placement()
        if self.editor_place_mode == "signature":
            self._close_signature_library()

    def _import_editor_signature(self) -> None:
        source = filedialog.askopenfilename(initialdir=self._dialog_initialdir(), filetypes=[("Изображения", "*.png *.jpg *.jpeg *.webp *.bmp *.tif *.tiff")])
        if not source:
            return
        self._remember_dialog_selection(source)
        name = simpledialog.askstring("Название подписи", "Как назвать подпись?", initialvalue=Path(source).stem, parent=self)
        if not name:
            return
        try:
            imported = import_signature(source, name, self.signature_remove_background.get(), self.signature_enhance.get())
        except Exception as exc:
            self._show_error(exc)
            return
        self._refresh_signature_list()
        self.status.set(f"Подпись сохранена: {imported.stem}")

    def _delete_editor_signature(self) -> None:
        selection = self.signature_list.curselection()
        if not selection:
            return
        path = self.editor_signature_paths[selection[0]]
        if not messagebox.askyesno("Удаление подписи", f"Удалить подпись «{path.stem}» из библиотеки?"):
            return
        self.editor_signature_edits = [edit for edit in self.editor_signature_edits if Path(edit.image_path) != path]
        self.editor_selected = None
        self.editor_undo_stack.clear()
        self.editor_redo_stack.clear()
        delete_signature(path)
        self._refresh_signature_list()
        self._render_editor_page()

    def _start_signature_placement(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("Подпись", "Сначала откройте PDF или изображение")
            return
        selection = self.signature_list.curselection()
        if not selection:
            messagebox.showinfo("Подпись", "Выберите подпись в библиотеке")
            return
        source = self.editor_signature_paths[selection[0]]
        destination = app_cache_dir("signature-overlays") / f"signature-{time.time_ns()}.png"
        try:
            self.editor_placing_signature = prepare_signature_for_placement(source, destination)
        except Exception as exc:
            self._show_error(exc)
            return
        self.editor_place_mode = "signature"
        self.editor_canvas.configure(cursor="crosshair")
        self._set_editor_tool_visual("signature", "signature")
        self.status.set("Протяните рамку визуальной подписи на странице")

    def _editor_canvas_press(self, event) -> None:
        if self.editor_word_busy_dialog is not None:
            return
        self._dismiss_editor_context_menu(event)
        self.editor_canvas.focus_set()
        canvas_x = self.editor_canvas.canvasx(event.x)
        canvas_y = self.editor_canvas.canvasy(event.y)
        if self.editor_place_mode == "word_color_pick":
            try:
                self._pick_word_color(canvas_x, canvas_y)
            except Exception as exc:
                self.editor_place_mode = "select"
                self.editor_canvas.configure(cursor="")
                self._show_error(exc)
            return
        if self.editor_reading_mode:
            self.editor_selected = None
            self.editor_object_drag = None
            self.editor_region_selection = None
            if self.editor_text_index:
                self.editor_place_mode = "recognized_text"
            else:
                self.editor_drag_start = None
                return
        if self.editor_place_mode == "detect":
            self._detect_style_at_canvas_point(canvas_x, canvas_y)
            return
        if self.editor_place_mode == "replace_word":
            self._replace_word_at_canvas_point(canvas_x, canvas_y)
            return
        if self.editor_place_mode == "recognized_text":
            page_x0, page_y0 = self.editor_offset
            page_x1 = page_x0 + self.editor_page_size[0] * self.editor_scale
            page_y1 = page_y0 + self.editor_page_size[1] * self.editor_scale
            if page_x0 <= canvas_x <= page_x1 and page_y0 <= canvas_y <= page_y1:
                self.editor_drag_start = (canvas_x, canvas_y)
                self.editor_recognized_text_selection = None
                self.editor_canvas.delete("editor-draft")
            return
        self.editor_recognized_text_selection = None
        if self.editor_place_mode in {None, "select"}:
            pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((canvas_x, canvas_y, canvas_x, canvas_y))
            selected = None
            current = self._selected_editor_object()
            if current is not None and hasattr(current, "rect"):
                current_canvas_rect = self._pdf_rect_to_canvas(current.rect)
                if abs(canvas_x - current_canvas_rect[2]) <= 12 and abs(canvas_y - current_canvas_rect[3]) <= 12:
                    selected = self.editor_selected
            selected = selected or self._find_editor_object_at(pdf_x, pdf_y)
            self.editor_selected = selected
            self.editor_object_drag = None
            if selected:
                kind, index = selected
                edit = self._selected_editor_object()
                canvas_rect = self._pdf_rect_to_canvas(edit.rect)
                replacement_word = isinstance(edit, TextEdit) and bool(edit.replacement_source_rect)
                operation = (
                    "resize"
                    if not replacement_word
                    and abs(canvas_x - canvas_rect[2]) <= 12
                    and abs(canvas_y - canvas_rect[3]) <= 12
                    else "move"
                )
                self.editor_object_drag = (kind, index, operation, tuple(edit.rect))
                self.editor_drag_start = (canvas_x, canvas_y)
                self.editor_object_drag_root = (event.x_root, event.y_root)
                self._load_selected_editor_properties()
                closed_area = isinstance(edit, TextEdit) and edit.opaque_background and not edit.text
                properties = "area" if closed_area else "word" if replacement_word else "text" if kind == "text" else "select"
                self._set_editor_tool_visual("select", properties)
                self.status.set(
                    "Замена выбрана: перетаскивайте её для уточнения положения"
                    if replacement_word
                    else "Объект выбран: перетащите его или синий маркер размера"
                )
            else:
                self._set_editor_tool_visual("select", "select")
                self.status.set("Объект не выбран")
            self._render_editor_page()
            return
        if self.editor_place_mode in {"pencil", "free_erase", "line", "arrow"}:
            pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((canvas_x, canvas_y, canvas_x, canvas_y))
            self.editor_drag_start = (canvas_x, canvas_y)
            self.editor_stroke_points = [(pdf_x, pdf_y)]
            self.editor_canvas.delete("editor-draft")
            return
        if self.editor_place_mode not in {"text", "erase", "signature", "copy_region", "crop"}:
            return
        self.editor_drag_start = (canvas_x, canvas_y)
        self.editor_canvas.delete("editor-draft")

    def _editor_canvas_double_click(self, event) -> None:
        if self.editor_reading_mode:
            self._editor_canvas_press(event)
            return
        self.editor_place_mode = "select"
        self._editor_canvas_press(event)
        self.editor_object_drag = None
        self.editor_drag_start = None
        if self.editor_selected and self.editor_selected[0] == "text":
            self._load_selected_editor_properties()
            edit = self._selected_editor_object()
            entry = self.editor_word_entry if isinstance(edit, TextEdit) and edit.replacement_source_rect else self.editor_text_entry
            entry.focus_set()
            entry.selection_range(0, tk.END)

    def _editor_canvas_drag(self, event) -> None:
        self._editor_eraser_preview(event)
        if not self.editor_drag_start:
            return
        canvas_x = self.editor_canvas.canvasx(event.x)
        canvas_y = self.editor_canvas.canvasy(event.y)
        if self.editor_object_drag:
            canvas_x, canvas_y = self._editor_object_pointer(event)
            self.editor_canvas.delete("editor-draft")
            rect = self._editor_object_drag_rect(canvas_x, canvas_y)
            self.editor_canvas.create_rectangle(*rect, outline=BLUE, width=3, dash=(5, 3), tags="editor-draft")
            return
        if self.editor_place_mode in {"pencil", "free_erase", "line", "arrow"}:
            pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((canvas_x, canvas_y, canvas_x, canvas_y))
            page_width, page_height = self.editor_page_size
            pdf_x = max(0.0, min(page_width, pdf_x))
            pdf_y = max(0.0, min(page_height, pdf_y))
            if self.editor_place_mode in {"line", "arrow"}:
                self.editor_stroke_points = [self.editor_stroke_points[0], (pdf_x, pdf_y)]
            elif not self.editor_stroke_points or abs(pdf_x - self.editor_stroke_points[-1][0]) > 0.5 or abs(pdf_y - self.editor_stroke_points[-1][1]) > 0.5:
                self.editor_stroke_points.append((pdf_x, pdf_y))
            self.editor_canvas.delete("editor-draft")
            points = []
            for point in self.editor_stroke_points:
                cx, cy, _, _ = self._pdf_rect_to_canvas((point[0], point[1], point[0], point[1]))
                points.extend((cx, cy))
            color = self.editor_draw_color.get()
            width = self.editor_eraser_width.get() if self.editor_place_mode == "free_erase" else self.editor_pencil_width.get()
            if len(points) >= 4:
                self.editor_canvas.create_line(
                    *points,
                    fill=color,
                    width=max(1, int(width * self.editor_scale)),
                    capstyle=tk.PROJECTING if self.editor_place_mode == "free_erase" else tk.ROUND,
                    joinstyle=tk.MITER if self.editor_place_mode == "free_erase" else tk.ROUND,
                    smooth=self.editor_place_mode == "pencil",
                    arrow=tk.LAST if self.editor_place_mode == "arrow" else tk.NONE,
                    tags="editor-draft",
                )
            return
        if self.editor_place_mode not in {"text", "erase", "signature", "copy_region", "crop", "recognized_text"}:
            return
        x0, y0 = self.editor_drag_start
        self.editor_canvas.delete("editor-draft")
        outline = "#0079C2" if self.editor_place_mode == "recognized_text" else CYAN
        self.editor_canvas.create_rectangle(x0, y0, canvas_x, canvas_y, outline=outline, width=2, dash=(5, 3), tags="editor-draft")

    def _editor_canvas_release(self, event) -> None:
        if not self.editor_drag_start:
            return
        canvas_x = self.editor_canvas.canvasx(event.x)
        canvas_y = self.editor_canvas.canvasy(event.y)
        if self.editor_object_drag:
            canvas_x, canvas_y = self._editor_object_pointer(event)
            kind, index, _operation, original_rect = self.editor_object_drag
            canvas_rect = self._editor_object_drag_rect(canvas_x, canvas_y)
            pdf_rect = self._canvas_rect_to_pdf(canvas_rect)
            if tuple(round(value, 3) for value in pdf_rect) != tuple(round(value, 3) for value in original_rect):
                self._remember_editor_state()
                collection = self._editor_collection(kind)
                collection[index] = replace(collection[index], rect=pdf_rect)
                moved = collection[index]
                if isinstance(moved, TextEdit) and moved.replacement_source_rect:
                    self.editor_word_offset_x.set(round(moved.rect[0] - moved.replacement_source_rect[0], 1))
                    self.editor_word_offset_y.set(round(moved.rect[1] - moved.replacement_source_rect[1], 1))
                self.status.set("Положение объекта изменено")
            self.editor_object_drag = None
            self.editor_drag_start = None
            self.editor_canvas.delete("editor-draft")
            self._render_editor_page()
            return
        if self.editor_place_mode in {"pencil", "free_erase", "line", "arrow"}:
            if self.editor_stroke_points:
                self._remember_editor_state()
                color_value = self.editor_draw_color.get().lstrip("#")
                color = tuple(int(color_value[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
                width = float(self.editor_eraser_width.get() if self.editor_place_mode == "free_erase" else self.editor_pencil_width.get())
                style = (
                    "eraser"
                    if self.editor_place_mode == "free_erase"
                    else self.editor_place_mode
                    if self.editor_place_mode in {"line", "arrow"}
                    else "free"
                )
                self.editor_stroke_edits.append(
                    StrokeEdit(self.editor_page_index, tuple(self.editor_stroke_points), width, color, style)
                )
                self.status.set("Штрих добавлен")
            self.editor_stroke_points = []
            self.editor_drag_start = None
            self.editor_canvas.delete("editor-draft")
            self._render_editor_page()
            return
        if self.editor_place_mode == "recognized_text":
            x0, y0 = self.editor_drag_start
            source_rect = self._canvas_rect_to_pdf(
                (min(x0, canvas_x), min(y0, canvas_y), max(x0, canvas_x), max(y0, canvas_y))
            )
            page = next(
                (item for item in self.editor_text_index if item.page_index == self.editor_page_index),
                None,
            )
            text = ""
            indexed_rects: tuple[tuple[float, float, float, float], ...] = ()
            if page is not None:
                indexed_selection = self._editor_rect_to_index(page, source_rect)
                text, indexed_rects = select_text_in_rect(page, indexed_selection)
            self.editor_recognized_text_selection = (
                (self.editor_page_index, indexed_rects, text) if text else None
            )
            self.editor_drag_start = None
            self.editor_canvas.delete("editor-draft")
            if text:
                self.status.set(f"Выделено символов: {len(text)}. Нажмите Ctrl+C для копирования")
            else:
                self.status.set("В выбранной области распознанный текст не найден")
            self._render_editor_page()
            return
        if self.editor_place_mode not in {"text", "erase", "signature", "copy_region", "crop"}:
            return
        mode = self.editor_place_mode
        x0, y0 = self.editor_drag_start
        x1, y1 = canvas_x, canvas_y
        if mode == "signature":
            aspect = self._signature_aspect_ratio()
            if abs(x1 - x0) < 12 or abs(y1 - y0) < 12:
                width = 170.0
                height = width / aspect
            else:
                width = abs(x1 - x0)
                height = width / aspect
                available_height = abs(y1 - y0)
                if height > available_height:
                    height = available_height
                    width = height * aspect
            x1, y1 = x0 + width, y0 + height
        if mode == "text" and (abs(x1 - x0) < 12 or abs(y1 - y0) < 12):
            pdf_x, pdf_y, _, _ = self._canvas_rect_to_pdf((x0, y0, x0, y0))
            font_size = float(self.editor_font_size.get())
            lines = self.editor_replace_text.get().splitlines() or [""]
            text_width = max(font_size * 2.0, max(len(line) for line in lines) * font_size * 0.62)
            text_height = max(font_size * 1.25, len(lines) * font_size * 1.3)
            page_width, page_height = self.editor_page_size
            pdf_rect = (
                pdf_x,
                pdf_y,
                min(page_width, pdf_x + text_width),
                min(page_height, pdf_y + text_height),
            )
        else:
            canvas_rect = (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
            pdf_rect = self._canvas_rect_to_pdf(canvas_rect)
        if pdf_rect[2] - pdf_rect[0] < 5 or pdf_rect[3] - pdf_rect[1] < 5:
            self.status.set("Выделенная область слишком мала или находится за пределами страницы")
        elif mode == "crop":
            self.editor_pending_crop = pdf_rect
            self.editor_selected = None
            self.editor_place_mode = "crop"
            self._set_editor_tool_visual("crop", "crop")
            self.status.set("Область выбрана. Нажмите «Применить обрезку»")
        elif mode in {"text", "erase"}:
            self._remember_editor_state()
            color = (self.editor_text_color.get() if mode == "text" else self.editor_draw_color.get()).lstrip("#")
            rgb = tuple(int(color[index:index + 2], 16) / 255.0 for index in (0, 2, 4))
            family = self.editor_font_family.get()
            edit = TextEdit(
                self.editor_page_index,
                pdf_rect,
                self.editor_replace_text.get() if mode == "text" else "",
                float(self.editor_font_size.get()),
                family,
                resolve_font_file(family, self.editor_text_bold.get(), self.editor_text_italic.get()),
                rgb,
                mode == "erase",
                rgb if mode == "erase" else (1.0, 1.0, 1.0),
                self.editor_text_bold.get() if mode == "text" else False,
                self.editor_text_italic.get() if mode == "text" else False,
                self.editor_text_underline.get() if mode == "text" else False,
                scan_blur=self._editor_text_scan_blur(float(self.editor_font_size.get())) if mode == "text" else 0.0,
            )
            self.editor_text_edits.append(edit)
            self.editor_selected = ("text", len(self.editor_text_edits) - 1)
            self.status.set("Надпись добавлена" if mode == "text" else "Область закрыта")
        elif mode == "copy_region":
            self.editor_region_selection = (self.editor_page_index, pdf_rect)
            self.editor_selected = None
            self.status.set("Область выделена. Нажмите Ctrl+C, затем Ctrl+V в этом или другом окне pdf bars")
        else:
            self._remember_editor_state()
            edit = SignatureEdit(self.editor_page_index, pdf_rect, str(self.editor_placing_signature))
            self.editor_signature_edits.append(edit)
            self.editor_selected = None
            self.status.set("Подпись размещена")
        self.editor_placing_signature = None
        if mode != "crop":
            self.editor_place_mode = "select"
        self.editor_drag_start = None
        if mode == "crop":
            self.editor_canvas.configure(cursor="crosshair")
        else:
            self.editor_canvas.configure(cursor="arrow")
            self._set_editor_tool_visual("select", "area" if mode == "erase" else "text" if self.editor_selected and self.editor_selected[0] == "text" else "select")
        self._render_editor_page()

    def _signature_aspect_ratio(self) -> float:
        path = self.editor_placing_signature
        if path is None and self.editor_selected and self.editor_selected[0] == "signature":
            selected = self._selected_editor_object()
            path = Path(selected.image_path) if isinstance(selected, SignatureEdit) else None
        if path is not None:
            try:
                with Image.open(path) as image:
                    return max(0.05, image.width / max(1, image.height))
            except OSError:
                pass
        return 2.25

    def _editor_object_pointer(self, event):
        # Property panels may change the canvas origin after selection. Mouse
        # movement must be measured on screen, not against that moving origin.
        root = getattr(self, "editor_object_drag_root", None)
        if root is not None:
            return (self.editor_drag_start[0] + event.x_root - root[0],
                    self.editor_drag_start[1] + event.y_root - root[1])
        return self.editor_canvas.canvasx(event.x), self.editor_canvas.canvasy(event.y)

    def _editor_object_drag_rect(self, canvas_x: float, canvas_y: float) -> tuple[float, float, float, float]:
        kind, _index, operation, original_pdf_rect = self.editor_object_drag
        x0, y0, x1, y1 = self._pdf_rect_to_canvas(original_pdf_rect)
        start_x, start_y = self.editor_drag_start
        page_x0, page_y0 = self.editor_offset
        page_x1 = page_x0 + self.editor_page_size[0] * self.editor_scale
        page_y1 = page_y0 + self.editor_page_size[1] * self.editor_scale
        if operation == "resize":
            if kind == "signature":
                original_width = max(1.0, x1 - x0)
                original_height = max(1.0, y1 - y0)
                aspect = original_width / original_height
                width = min(page_x1 - x0, max(2.0 * self.editor_scale, original_width + canvas_x - start_x))
                height = width / aspect
                if height > page_y1 - y0:
                    height = page_y1 - y0
                    width = height * aspect
                return x0, y0, x0 + width, y0 + height
            minimum = 2.0 * self.editor_scale
            return x0, y0, min(page_x1, max(x0 + minimum, x1 + canvas_x - start_x)), min(page_y1, max(y0 + minimum, y1 + canvas_y - start_y))
        delta_x, delta_y = canvas_x - start_x, canvas_y - start_y
        selected = self._selected_editor_object()
        delta_x = min(max(delta_x, page_x0 - x0), page_x1 - x1)
        delta_y = min(max(delta_y, page_y0 - y0), page_y1 - y1)
        return x0 + delta_x, y0 + delta_y, x1 + delta_x, y1 + delta_y

    def _editor_eraser_preview(self, event) -> None:
        canvas = self.editor_canvas
        canvas.delete("eraser-preview")
        if self.editor_place_mode != "free_erase" or not self.editor_pdf_path:
            return
        x, y = canvas.canvasx(event.x), canvas.canvasy(event.y)
        left, top = self.editor_offset
        width, height = self.editor_page_size
        if not (left <= x <= left + width * self.editor_scale and top <= y <= top + height * self.editor_scale):
            return
        radius = max(0.5, float(self.editor_eraser_width.get()) * self.editor_scale / 2.0)
        canvas.create_rectangle(x-radius, y-radius, x+radius, y+radius,
                                outline="white", width=3, tags="eraser-preview")
        canvas.create_rectangle(x-radius, y-radius, x+radius, y+radius,
                                outline="#202020", width=1, tags="eraser-preview")

    def _undo_editor_action(self) -> None:
        if not self.editor_undo_stack or self.editor_pages_busy or self.editor_index_busy:
            return
        self.editor_redo_stack.append(self._editor_state_snapshot())
        (
            self.editor_text_edits,
            self.editor_signature_edits,
            self.editor_region_edits,
            self.editor_stroke_edits,
            self.editor_crop_edits,
            self.editor_alignment_edits,
            self.editor_selected,
            self.editor_pdf_path,
            self.editor_source_kind,
            self.editor_page_count,
            self.editor_page_source_names,
            self.editor_page_index,
            self.editor_new_document,
            self.editor_last_save_path,
        ) = self.editor_undo_stack.pop()
        self._reset_editor_search()
        self.editor_alignment_preview = None
        self.editor_thumbnail_generation += 1
        self._refresh_editor_thumbnails()
        self.editor_dirty = True
        self._queue_autosave()
        self.status.set("Действие отменено")
        self._render_editor_page()

    def _redo_editor_action(self) -> None:
        if not self.editor_redo_stack or self.editor_pages_busy or self.editor_index_busy:
            return
        self.editor_undo_stack.append(self._editor_state_snapshot())
        (
            self.editor_text_edits,
            self.editor_signature_edits,
            self.editor_region_edits,
            self.editor_stroke_edits,
            self.editor_crop_edits,
            self.editor_alignment_edits,
            self.editor_selected,
            self.editor_pdf_path,
            self.editor_source_kind,
            self.editor_page_count,
            self.editor_page_source_names,
            self.editor_page_index,
            self.editor_new_document,
            self.editor_last_save_path,
        ) = self.editor_redo_stack.pop()
        self._reset_editor_search()
        self.editor_alignment_preview = None
        self.editor_thumbnail_generation += 1
        self._refresh_editor_thumbnails()
        self.editor_dirty = True
        self._queue_autosave()
        self.status.set("Действие повторено")
        self._render_editor_page()

    def _editor_state_snapshot(
        self,
    ) -> tuple:
        return (
            list(self.editor_text_edits),
            list(self.editor_signature_edits),
            list(self.editor_region_edits),
            list(self.editor_stroke_edits),
            list(self.editor_crop_edits),
            list(self.editor_alignment_edits),
            self.editor_selected,
            self.editor_pdf_path,
            self.editor_source_kind,
            self.editor_page_count,
            list(self.editor_page_source_names),
            self.editor_page_index,
            self.editor_new_document,
            self.editor_last_save_path,
        )

    def _remember_editor_state(self) -> None:
        self.editor_undo_stack.append(self._editor_state_snapshot())
        if len(self.editor_undo_stack) > 100:
            del self.editor_undo_stack[0]
        self.editor_redo_stack.clear()
        self.editor_dirty = True
        self._queue_autosave()

    def _editor_text_scan_blur(self, font_size: float) -> float:
        if not self.editor_text_scan_effect.get():
            return 0.0
        return round(max(0.18, min(0.55, font_size * 0.025)), 2)

    def _apply_selected_text_properties(self) -> None:
        if not self.editor_selected or self.editor_selected[0] != "text":
            messagebox.showinfo("PDF Редактор", "Выберите размещённый текст или закрытую область")
            return
        index = self.editor_selected[1]
        current = self.editor_text_edits[index]
        if current.replacement_source_rect:
            self._apply_selected_word_properties()
            return
        color = self.editor_text_color.get().lstrip("#")
        rgb = tuple(int(color[position:position + 2], 16) / 255.0 for position in (0, 2, 4))
        family = self.editor_font_family.get()
        updated = replace(
            current,
            text=self.editor_replace_text.get(),
            font_size=float(self.editor_font_size.get()),
            font_family=family,
            font_file=resolve_font_file(family, self.editor_text_bold.get(), self.editor_text_italic.get()),
            color=rgb,
            bold=self.editor_text_bold.get(),
            italic=self.editor_text_italic.get(),
            underline=self.editor_text_underline.get(),
            scan_blur=self._editor_text_scan_blur(float(self.editor_font_size.get())),
        )
        if updated != current:
            self._remember_editor_state()
            self.editor_text_edits[index] = updated
            self.status.set("Свойства выбранного текста обновлены")
            self._render_editor_page()

    def _apply_selected_word_properties(self) -> None:
        if not self.editor_selected or self.editor_selected[0] != "text":
            messagebox.showinfo("Замена слова", "Выберите ранее заменённое слово")
            return
        index = self.editor_selected[1]
        current = self.editor_text_edits[index]
        if not current.replacement_source_rect:
            messagebox.showinfo("Замена слова", "Выбранный объект не является заменой слова")
            return
        text = self.editor_replace_text.get().strip()
        if not text:
            messagebox.showinfo("Замена слова", "Введите новое слово")
            return
        color = self.editor_text_color.get().lstrip("#")
        rgb = tuple(int(color[position:position + 2], 16) / 255.0 for position in (0, 2, 4))
        family = self.editor_font_family.get()
        font_size = float(self.editor_font_size.get())
        offset_x = float(self.editor_word_offset_x.get())
        offset_y = float(self.editor_word_offset_y.get())
        width = measure_editor_text(
            text,
            family,
            font_size,
            self.editor_text_bold.get(),
            self.editor_text_italic.get(),
        ) + max(2.0, font_size * 0.18)
        rect = replacement_word_rect(
            current.replacement_source_rect,
            current.rect,
            width,
            self.editor_page_size,
            offset_x,
            offset_y,
        )
        updated = replace(
            current,
            rect=rect,
            text=text,
            font_size=font_size,
            font_family=family,
            font_file=resolve_font_file(family, self.editor_text_bold.get(), self.editor_text_italic.get()),
            color=rgb,
            bold=self.editor_text_bold.get(),
            italic=self.editor_text_italic.get(),
            underline=self.editor_text_underline.get(),
            scan_effect=self.editor_word_scan_effect.get(),
        )
        if updated != current:
            self._remember_editor_state()
            self.editor_text_edits[index] = updated
        self.editor_word_offset_x.set(round(updated.rect[0] - updated.replacement_source_rect[0], 1))
        self.editor_word_offset_y.set(round(updated.rect[1] - updated.replacement_source_rect[1], 1))
        self.status.set("Замена обновлена; фон исходного слова сохраняется на месте")
        self._render_editor_page()

    def _nudge_selected_word(self, delta_x: float, delta_y: float) -> None:
        try:
            self.editor_word_offset_x.set(round(float(self.editor_word_offset_x.get()) + float(delta_x), 1))
            self.editor_word_offset_y.set(round(float(self.editor_word_offset_y.get()) + float(delta_y), 1))
        except (TypeError, ValueError, tk.TclError):
            self.editor_word_offset_x.set(float(delta_x))
            self.editor_word_offset_y.set(float(delta_y))
        self._apply_selected_word_properties()

    def _delete_selected_editor_object(self) -> None:
        if not self.editor_selected:
            return
        self._remember_editor_state()
        kind, index = self.editor_selected
        collection = self._editor_collection(kind)
        del collection[index]
        if kind == "alignment":
            self.editor_alignment_preview = None
            self.editor_thumbnail_generation += 1
            self._refresh_editor_thumbnails()
        self.editor_selected = None
        self.status.set("Выбранный объект удалён")
        self._render_editor_page()

    def _duplicate_selected_editor_object(self) -> None:
        payload = self._editor_clipboard_payload()
        if payload is None:
            messagebox.showinfo("PDF Редактор", "Сначала выберите созданный объект")
            return
        self._write_editor_clipboard(payload)
        self._paste_editor_object()

    def _copy_selected_editor_object(self) -> None:
        payload = self._editor_clipboard_payload()
        if payload is None:
            return
        self._write_editor_clipboard(payload)
        if self.editor_region_selection and not self.editor_selected:
            self.status.set("Область скопирована. Нажмите Ctrl+V в этом или другом окне pdf bars")
        else:
            self.status.set("Объект скопирован. Нажмите Ctrl+V в этом или другом окне pdf bars")

    def _paste_editor_object(self) -> None:
        if not self.editor_pdf_path:
            return
        payload = self._read_editor_clipboard()
        if payload is None:
            self.status.set("В буфере обмена нет объекта pdf bars")
            return
        try:
            kind = payload["kind"]
            data = payload.get("data", {})
            rect = self._offset_clipboard_rect(tuple(float(value) for value in data["rect"]))
            was_dirty = self.editor_dirty
            self._remember_editor_state()
            if kind == "text":
                edit = TextEdit(
                    self.editor_page_index,
                    rect,
                    str(data.get("text", "")),
                    float(data.get("font_size", 11.0)),
                    str(data.get("font_family", "Times New Roman")),
                    data.get("font_file"),
                    tuple(float(value) for value in data.get("color", (0.0, 0.0, 0.0))),
                    bool(data.get("opaque_background", False)),
                    tuple(float(value) for value in data.get("background_color", (1.0, 1.0, 1.0))),
                    bool(data.get("bold", False)),
                    bool(data.get("italic", False)),
                    bool(data.get("underline", False)),
                    scan_blur=float(data.get("scan_blur", 0.0)) if data.get("scan_effect", True) else 0.0,
                    scan_effect=bool(data.get("scan_effect", True)),
                )
                self.editor_text_edits.append(edit)
                self.editor_selected = ("text", len(self.editor_text_edits) - 1)
            elif kind == "image":
                image_path = Path(str(data["image_path"]))
                if not image_path.is_file():
                    raise FileNotFoundError(f"Файл вставляемого изображения не найден: {image_path}")
                self.editor_signature_edits.append(
                    SignatureEdit(
                        self.editor_page_index,
                        rect,
                        str(image_path),
                        bool(data.get("keep_proportion", True)),
                    )
                )
                self.editor_selected = ("signature", len(self.editor_signature_edits) - 1)
            elif kind == "stroke":
                original_rect = tuple(float(value) for value in data["rect"])
                delta_x = rect[0] - original_rect[0]
                delta_y = rect[1] - original_rect[1]
                points = tuple((float(point[0]) + delta_x, float(point[1]) + delta_y) for point in data.get("points", []))
                self.editor_stroke_edits.append(
                    StrokeEdit(
                        self.editor_page_index,
                        points,
                        float(data.get("width", 2.0)),
                        tuple(float(value) for value in data.get("color", (0.0, 0.0, 0.0))),
                        str(data.get("style", "free")),
                    )
                )
                self.editor_selected = ("stroke", len(self.editor_stroke_edits) - 1)
            elif kind == "crop":
                self.editor_crop_edits = [edit for edit in self.editor_crop_edits if edit.page_index != self.editor_page_index]
                self.editor_crop_edits.append(CropEdit(self.editor_page_index, rect))
                self.editor_selected = ("crop", len(self.editor_crop_edits) - 1)
            else:
                raise ValueError("Формат объекта в буфере обмена не поддерживается")
        except Exception as exc:
            if self.editor_undo_stack:
                self.editor_undo_stack.pop()
            self.editor_dirty = was_dirty if "was_dirty" in locals() else self.editor_dirty
            self._show_error(exc)
            return
        self.editor_region_selection = None
        self.status.set("Объект вставлен; его можно переместить или изменить")
        self._render_editor_page()

    def _editor_clipboard_payload(self) -> dict | None:
        if self.editor_region_selection and not self.editor_selected:
            page_index, rect = self.editor_region_selection
            image_path = self._capture_editor_region(page_index, rect)
            return {
                "version": 1,
                "kind": "image",
                "data": {"rect": list(rect), "image_path": str(image_path), "keep_proportion": False},
            }

        edit = self._selected_editor_object()
        if edit is None or self.editor_selected is None:
            return None
        kind = self.editor_selected[0]
        if kind == "text":
            data = {
                "rect": list(edit.rect),
                "text": edit.text,
                "font_size": edit.font_size,
                "font_family": edit.font_family,
                "font_file": edit.font_file,
                "color": list(edit.color),
                "opaque_background": edit.opaque_background,
                "background_color": list(edit.background_color),
                "bold": edit.bold,
                "italic": edit.italic,
                "underline": edit.underline,
                "scan_blur": edit.scan_blur if not edit.replacement_source_rect or edit.scan_effect else 0.0,
                "scan_effect": edit.scan_effect,
            }
            return {"version": 1, "kind": "text", "data": data}
        if kind == "signature":
            return {
                "version": 1,
                "kind": "image",
                "data": {
                    "rect": list(edit.rect),
                    "image_path": edit.image_path,
                    "keep_proportion": edit.keep_proportion,
                },
            }
        if kind == "region":
            image_path = self._capture_editor_region(edit.page_index, edit.source_rect)
            return {
                "version": 1,
                "kind": "image",
                "data": {"rect": list(edit.rect), "image_path": str(image_path), "keep_proportion": False},
            }
        if kind == "stroke":
            rect = self._editor_object_rect(kind, edit)
            return {
                "version": 1,
                "kind": "stroke",
                "data": {
                    "rect": list(rect),
                    "points": [list(point) for point in edit.points],
                    "width": edit.width,
                    "color": list(edit.color),
                    "style": edit.style,
                },
            }
        if kind == "crop":
            return {"version": 1, "kind": "crop", "data": {"rect": list(edit.rect)}}
        return None

    def _write_editor_clipboard(self, payload: dict) -> None:
        self.editor_clipboard = payload
        value = encode_clipboard_payload(payload)
        try:
            self.clipboard_clear()
            self.clipboard_append(value)
            self.update_idletasks()
        except tk.TclError:
            pass

    def _read_editor_clipboard(self) -> dict | None:
        try:
            value = self.clipboard_get()
        except tk.TclError:
            return self.editor_clipboard
        return decode_clipboard_payload(value)

    def _offset_clipboard_rect(self, rect) -> tuple[float, float, float, float]:
        page_width, page_height = self.editor_page_size
        x0, y0, x1, y1 = rect
        width = max(5.0, x1 - x0)
        height = max(5.0, y1 - y0)
        scale = min(1.0, page_width / width if width else 1.0, page_height / height if height else 1.0)
        width *= scale
        height *= scale
        new_x0 = min(max(0.0, x0 + 12.0), max(0.0, page_width - width))
        new_y0 = min(max(0.0, y0 + 12.0), max(0.0, page_height - height))
        return new_x0, new_y0, new_x0 + width, new_y0 + height

    def _capture_editor_region(self, page_index: int, rect) -> Path:
        destination = new_clipboard_image_path()
        image, scale = self._render_editor_composite(page_index)
        try:
            box = tuple(round(value * scale) for value in rect)
            crop = image.crop(box)
            try:
                crop.save(destination, "PNG")
            finally:
                crop.close()
        finally:
            image.close()
        return destination

    def _render_editor_composite(self, page_index: int):
        edits = (self.editor_text_edits, self.editor_signature_edits,
                 self.editor_region_edits, self.editor_stroke_edits)
        if self._editor_is_pdf():
            import fitz
            with fitz.open(str(self.editor_pdf_path)) as document:
                size = document[page_index].rect
                scale = bounded_preview_scale(size.width, size.height, 2.0)
            return render_pdf_page_image(self.editor_pdf_path, page_index, *edits,
                dpi=scale*72, alignment_edits=self.editor_alignment_edits), scale
        return render_image_page_image(self.editor_pdf_path, *edits,
            alignment_edits=self.editor_alignment_edits), 1.0

    def _clear_editor_actions(self) -> None:
        if not self.editor_text_edits and not self.editor_signature_edits and not self.editor_region_edits and not self.editor_stroke_edits and not self.editor_crop_edits and not self.editor_alignment_edits:
            if self.editor_pending_crop is not None:
                self.editor_pending_crop = None
                self._render_editor_page()
                return
            if self.editor_region_selection:
                self.editor_region_selection = None
                self._render_editor_page()
            elif self.editor_recognized_text_selection:
                self.editor_recognized_text_selection = None
                self._render_editor_page()
            return
        self._remember_editor_state()
        self.editor_text_edits.clear()
        self.editor_signature_edits.clear()
        self.editor_region_edits.clear()
        self.editor_stroke_edits.clear()
        self.editor_crop_edits.clear()
        self.editor_alignment_edits.clear()
        self.editor_alignment_preview = None
        self.editor_selected = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self.editor_pending_crop = None
        self._render_editor_page()

    def _clear_editor_document(self) -> None:
        if self.editor_pages_busy or self.editor_index_busy:
            self.status.set("Дождитесь завершения текущей операции")
            return
        if not self.editor_pdf_path:
            return
        if self.editor_dirty and not messagebox.askyesno(
            "PDF Редактор",
            "Очистить все открытые файлы и несохранённые изменения?",
            parent=self,
        ):
            return
        self.editor_pdf_path = None
        self.editor_source_kind = None
        self._editor_preview_cache = None
        self.editor_page_image = None
        self.editor_page_index = 0
        self.editor_page_count = 0
        self.editor_page_source_names.clear()
        self.editor_text_edits.clear()
        self.editor_signature_edits.clear()
        self.editor_region_edits.clear()
        self.editor_stroke_edits.clear()
        self.editor_crop_edits.clear()
        self.editor_alignment_edits.clear()
        self.editor_alignment_preview = None
        self.editor_undo_stack.clear()
        self.editor_redo_stack.clear()
        self.editor_selected = None
        self.editor_region_selection = None
        self.editor_recognized_text_selection = None
        self.editor_pending_crop = None
        self.editor_dirty = False
        self.editor_new_document = False
        self.editor_last_save_path = None
        self.editor_place_mode = None
        self.editor_warning.set("")
        self._reset_editor_search()
        self.editor_thumbnail_generation += 1
        self.editor_thumbnail_images.clear()
        self.editor_thumbnail_widgets.clear()
        self.editor_page_selection.clear()
        for child in self.editor_thumbnail_grid.winfo_children():
            child.destroy()
        self.editor_page_label.set("Страница —")
        self._set_editor_tool_visual("select", "select")
        self._refresh_editor_edits_panel()
        self._draw_editor_empty_state()
        self._layout_editor_thumbnails()
        self.status.set("Рабочая область PDF Редактора очищена")
        self._queue_autosave()

    def _print_editor_document(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("Печать", "Сначала откройте PDF или изображение")
            return
        dialog = PrintDialog(self, self.editor_page_count or 1)
        if not dialog.result:
            return
        source = Path(self.editor_pdf_path)
        source_is_pdf = self._editor_is_pdf()
        options = dialog.result
        text_edits = list(self.editor_text_edits)
        signature_edits = list(self.editor_signature_edits)
        region_edits = list(self.editor_region_edits)
        stroke_edits = list(self.editor_stroke_edits)
        crop_edits = list(self.editor_crop_edits)
        crop_rect = self._current_crop_rect()
        alignment_edits = list(self.editor_alignment_edits)

        def print_job() -> str:
            output = temporary_print_path(source.stem)
            try:
                if source_is_pdf:
                    apply_pdf_edits(
                        source,
                        output,
                        text_edits,
                        signature_edits,
                        region_edits,
                        stroke_edits,
                        crop_edits,
                        alignment_edits,
                    )
                else:
                    apply_image_edits(
                        source,
                        output,
                        text_edits,
                        signature_edits,
                        region_edits,
                        stroke_edits,
                        crop_rect,
                        alignment_edits=alignment_edits,
                    )
                return submit_pdf_print_job(output, options)
            finally:
                if os.name != "nt":
                    remove_temporary_print_file(output)

        self._run_task(print_job, 'Документ отправлен на печать')

    def _save_editor_pdf(self) -> None:
        if not self.editor_pdf_path:
            messagebox.showinfo("PDF Редактор", "Сначала откройте PDF или изображение")
            return
        filetypes = [
            ("PDF", "*.pdf"),
            ("Word DOCX с распознанным текстом", "*.docx"),
            ("PNG", "*.png"),
            ("JPEG", "*.jpg"),
            ("WEBP", "*.webp"),
            ("TIFF", "*.tiff"),
            ("BMP", "*.bmp"),
        ]
        default_suffix = ".pdf" if self._editor_is_pdf() else ".png"
        selected_save_type = tk.StringVar(value="PDF" if self._editor_is_pdf() else "PNG")
        if self.editor_last_save_path:
            initial_dir = self.editor_last_save_path.parent
            default_name = self.editor_last_save_path.name
        elif self.editor_new_document:
            initial_dir = usable_last_folder(self.preferences)
            default_name = f"Новый документ{default_suffix}"
        else:
            initial_dir = self.editor_pdf_path.parent
            default_name = f"{self.editor_pdf_path.stem}_edited{default_suffix}"
        while True:
            output = filedialog.asksaveasfilename(
                initialdir=str(initial_dir),
                initialfile=default_name,
                defaultextension=default_suffix,
                filetypes=filetypes,
                typevariable=selected_save_type,
            )
            if not output:
                return
            output_path = editor_save_path_for_type(Path(output), selected_save_type.get(), default_suffix)
            if not (
                self._editor_is_pdf()
                and output_path.suffix.lower() == ".pdf"
                and editor_output_is_source(Path(self.editor_pdf_path), output_path)
            ):
                break
            suggested = available_output_path(
                Path(self.editor_pdf_path).with_name(f"{Path(self.editor_pdf_path).stem}_edited.pdf")
            )
            messagebox.showwarning(
                "Выберите другое имя",
                "Открытый исходный PDF нельзя заменить во время редактирования. "
                "Сохраните результат как отдельный файл.\n\n"
                f"Предлагаемое имя: {suggested.name}",
            )
            initial_dir = suggested.parent
            default_name = suggested.name
            selected_save_type.set("PDF")
        self._remember_dialog_selection(output_path)
        if self._editor_is_pdf() and output_path.suffix.lower() == ".pdf":
            import fitz
            try:
                with fitz.open(str(self.editor_pdf_path)) as document:
                    standard = declared_pdfa(document)
            except Exception as exc:
                self._show_error(exc)
                return
            if standard and not messagebox.askyesno(
                "Сохранение копии PDF/A",
                f"Исходник заявлен как {standard}. Изменённая копия будет обычным PDF, "
                "без подтверждения соответствия PDF/A. Исходный архивный файл останется неизменным. Продолжить?",
                parent=self,
            ):
                return
        if self._editor_is_pdf() and output_path.suffix.lower() == ".pdf" and pdf_has_signatures(self.editor_pdf_path) and not messagebox.askyesno(
            "Электронная подпись станет недействительной",
            "Документ содержит электронную подпись. После изменения она станет недействительной. Продолжить и сохранить отдельный файл?",
        ):
            return
        text_edits = list(self.editor_text_edits)
        signature_edits = list(self.editor_signature_edits)
        region_edits = list(self.editor_region_edits)
        stroke_edits = list(self.editor_stroke_edits)
        crop_edits = list(self.editor_crop_edits)
        crop_rect = self._current_crop_rect()
        alignment_edits = list(self.editor_alignment_edits)
        ocr_language = self.editor_ocr_language.get()
        if output_path.suffix.lower() == ".docx":
            snapshot = self._editor_pdf_snapshot_job()
            def export_docx():
                with tempfile.TemporaryDirectory(dir=app_cache_dir("editor-pages")) as folder:
                    edited = Path(folder) / "edited.pdf"
                    snapshot(edited)
                    return source_to_layout_ocr_docx(edited, output_path, dpi=350, language=ocr_language)
            self._run_task(export_docx, 'Распознанный текст сохранён в DOCX', source_paths=[self.editor_pdf_path])
        elif self._editor_is_pdf() and output_path.suffix.lower() == ".pdf":
            self._run_task(lambda: apply_pdf_edits(self.editor_pdf_path, output_path, text_edits, signature_edits, region_edits, stroke_edits, crop_edits, alignment_edits), 'Изменённый PDF сохранён', on_success=self._editor_save_completed)
        elif self._editor_is_pdf():
            self._run_task(lambda: render_pdf_page_edits_to_image(self.editor_pdf_path, output_path, self.editor_page_index, text_edits, signature_edits, region_edits, stroke_edits, crop_rect, alignment_edits=alignment_edits), 'Страница сохранена как изображение', on_success=self._editor_save_completed)
        else:
            self._run_task(lambda: apply_image_edits(self.editor_pdf_path, output_path, text_edits, signature_edits, region_edits, stroke_edits, crop_rect, alignment_edits=alignment_edits), 'Изображение сохранено', on_success=self._editor_save_completed)

    def _editor_save_completed(self, result) -> None:
        self.editor_dirty = False
        if result:
            saved_path = Path(result)
            if (self._editor_is_pdf() and saved_path.suffix.lower() == ".pdf") or (
                self._editor_is_image() and saved_path.suffix.lower() in IMAGE_SUFFIXES
            ):
                self.editor_last_save_path = saved_path
                self.editor_new_document = False
        self._queue_autosave()

    def _current_crop_rect(self) -> tuple[float, float, float, float] | None:
        for edit in reversed(self.editor_crop_edits):
            if edit.page_index == self.editor_page_index:
                return edit.rect
        return None

    def _open_pdf(self, variable: tk.StringVar) -> None:
        value = filedialog.askopenfilename(initialdir=self._dialog_initialdir(), filetypes=[("PDF", "*.pdf")])
        if value:
            variable.set(value)
            self._remember_dialog_selection(value)

    def _choose_dir(self, variable: tk.StringVar) -> None:
        value = filedialog.askdirectory(initialdir=self._dialog_initialdir())
        if value:
            variable.set(value)
            self._remember_dialog_selection(value)

    def _export_ocr_diagnostics(self) -> None:
        from .diagnostics import export_diagnostics

        path = filedialog.asksaveasfilename(parent=self, title="Сохранить отладочную информацию",
            initialdir=self._dialog_initialdir(), initialfile=time.strftime("PDF_Bars_diagnostics_%Y%m%d_%H%M%S.zip"),
            defaultextension=".zip", filetypes=[("Отладочная информация", "*.zip")])
        if not path:
            return
        destination = available_output_path(Path(path))
        self._run_task(lambda: export_diagnostics(destination), "Диагностика сохранена",
            on_success=lambda result: messagebox.showinfo("Отладочная информация",
                f"Архив сохранён: {result}\n\nВключены операции текущего запуска. "
                "Для анализа задержки сначала повторите медленное распознавание, затем соберите отладочную информацию. "
                "Документы и распознанный текст в архив не включаются.", parent=self), quiet=True)

    def _open_startup_paths(self, values) -> None:
        paths = [Path(value).expanduser() for value in values if Path(value).expanduser().is_file()]
        supported = [path for path in paths if path.suffix.lower() == ".pdf" or path.suffix.lower() in IMAGE_SUFFIXES]
        if not supported:
            return
        self._remember_dialog_selection(supported)
        if len(supported) == 1:
            source = supported[0]
            self.notebook.select(self.editor_tab)
            self._load_editor_file(source)
            if self.editor_reading_mode:
                self._exit_editor_reading_mode()
            self.after_idle(self._fit_editor_page)
        else:
            self.notebook.select(self.editor_tab)
            self._append_editor_files(supported)

    def _show_whats_new(self, force: bool = False) -> None:
        if not force and self.preferences.get("whats_new_version") == WHATS_NEW_RELEASE:
            return
        if self.grab_current():
            self.after(500, lambda: self._show_whats_new(force=force))
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"Что нового в pdf bars {WHATS_NEW_RELEASE}")
        dialog.configure(bg=PANEL)
        dialog.transient(self)
        dialog.resizable(False, False)
        tk.Label(dialog, text=f"pdf bars {WHATS_NEW_RELEASE}: изменения в выпуске", bg=PANEL,
                 fg=BLUE, font=(FONT_FAMILY, 16, "bold")).pack(anchor="w", padx=24, pady=(24, 12))
        notes = (
            "Открытая версия pdf bars\n"
            "• Удалены корпоративные ресурсы, контакты и сбор статистики.\n"
            "• Все документы и OCR обрабатываются локально.\n\n"
            "Интерфейс и работа со страницами\n"
            "• Добавлен новый облегчённый интерфейс и исправлены ошибки.\n"
            "• Компактная светлая шапка и вкладки со значками освобождают место для документа.\n"
            "• Флажок «Правки» показывает список изменений. Свойства появляются при выборе инструмента или объекта.\n"
            "• Выбранные страницы выделяются синей рамкой и заливкой. В заголовке миниатюр доступны добавление и удаление.\n"
            "• Цвет уже размещённой закрытой области можно изменить через палитру и отменить действие.\n\n"
            "Руководство пользователя\n"
            "• Руководство полностью переработано: инструкции "
            "по всем основным функциям программы.\n"
            "• Откройте его кнопкой «Открыть руководство пользователя» в разделе «О программе».\n\n"
            "Общие улучшения программы\n"
            "• Исправлены преобразование прозрачных изображений, обработка файлов с неверным расширением "
            "и пути с домашним каталогом ~.\n"
            "• Временные исходники редактора сохраняются в его рабочую сессию.\n"
            "• Для недоступных или повреждённых файлов показываются понятные предупреждения "
            "с дальнейшими действиями вместо технических ошибок."
        )
        body = tk.Frame(dialog, bg=PANEL)
        body.pack(fill="both", expand=True, padx=24)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)
        height = min(23, max(10, (self.winfo_screenheight() - 260) // 24))
        text = tk.Text(body, wrap="word", width=76, height=height, bg=PANEL, fg=TEXT,
                       font=(FONT_FAMILY, 11), relief="flat", borderwidth=0, highlightthickness=0,
                       cursor="arrow", padx=0, pady=0)
        text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(body, command=text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(8, 0))
        text.configure(yscrollcommand=scrollbar.set)
        text.insert("1.0", notes)
        text.tag_configure("heading", font=(FONT_FAMILY, 11, "bold"), foreground=DEEP_BLUE)
        for heading in ("Интерфейс и работа со страницами", "Руководство пользователя", "Общие улучшения программы"):
            start = text.search(heading, "1.0", stopindex="end")
            if start:
                text.tag_add("heading", start, f"{start}+{len(heading)}c")
        text.configure(state="disabled")
        def dismiss():
            self.preferences["whats_new_version"] = WHATS_NEW_RELEASE
            try:
                save_preferences(self.preferences)
            except OSError:
                pass
            dialog.destroy()
        ttk.Button(dialog, text="Понятно", command=dismiss, style="Accent.TButton").pack(anchor="e", padx=24, pady=24)
        dialog.protocol("WM_DELETE_WINDOW", dismiss)
        dialog.update_idletasks()
        x = self.winfo_rootx() + max(0, (self.winfo_width() - dialog.winfo_reqwidth()) // 2)
        y = self.winfo_rooty() + max(0, (self.winfo_height() - dialog.winfo_reqheight()) // 2)
        dialog.geometry(f"+{x}+{y}")
        dialog.grab_set()


def main(argv=None) -> int:
    # Do not map a partially constructed Tk window. Until mainloop starts it
    # cannot process input and desktop environments present it as frozen.
    app = PdfCombinerApp(initially_hidden=True)
    startup_paths = list(sys.argv[1:] if argv is None else argv)
    if startup_paths:
        app.after_idle(lambda: app._open_startup_paths(startup_paths))
    else:
        app.after_idle(app._offer_recovery)
    app.deiconify()
    app.after(50, app._activate_custom_chrome)
    app.after(700, app._show_whats_new)
    app.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
