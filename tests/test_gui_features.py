from source_text import source_text
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from PIL import Image

import redos_pdf_combiner.gui as gui
from redos_pdf_combiner.gui import (
    EDITOR_BASIC_COLORS,
    PageRef,
    PdfCombinerApp,
    duplicate_selected_page_refs,
    editor_preview_font_spec,
    editor_output_is_source,
    editor_save_path_for_type,
    page_refs_for_editor,
    replacement_word_rect,
    select_page_indexes,
)
from redos_pdf_combiner.page_tools import combine_editor_sources


ROOT = Path(__file__).resolve().parents[1]


def test_duplicate_selected_pages_inserts_copies_after_originals():
    pages = [PageRef(Path("a.pdf"), index, 3) for index in range(3)]
    duplicated, selected = duplicate_selected_page_refs(pages, {0, 2})

    assert [page.page_index for page in duplicated] == [0, 0, 1, 2, 2]
    assert selected == {1, 4}


def test_editor_has_basic_color_palette_and_custom_color_choice():
    colors = {color for color, _name in EDITOR_BASIC_COLORS}
    assert {"#000000", "#FFFFFF", "#E53935", "#F6C344", "#2E7D32", "#087FD4"} <= colors


def test_replacement_word_can_move_horizontally_and_vertically_with_page_clamping():
    source = (40.0, 50.0, 100.0, 72.0)
    current = (40.0, 50.0, 110.0, 78.0)

    assert replacement_word_rect(source, current, 80.0, (300.0, 400.0), 12.0, -7.0) == (
        52.0,
        43.0,
        132.0,
        71.0,
    )
    assert replacement_word_rect(source, current, 80.0, (100.0, 70.0), 500.0, 500.0) == (
        20.0,
        42.0,
        100.0,
        70.0,
    )


def test_copied_page_region_requests_exact_size_paste(tmp_path):
    image_path = tmp_path / "region.png"
    image_path.write_bytes(b"png")
    rect = (10.0, 20.0, 70.0, 55.0)
    app = SimpleNamespace(
        editor_region_selection=(0, rect),
        editor_selected=None,
        _capture_editor_region=lambda _page, _rect: image_path,
    )

    payload = PdfCombinerApp._editor_clipboard_payload(app)

    assert payload["kind"] == "image"
    assert payload["data"]["rect"] == list(rect)
    assert payload["data"]["keep_proportion"] is False


def test_plain_page_click_replaces_selection_and_shift_selects_range():
    assert select_page_indexes(4, anchor=1, extend=False) == {4}
    assert select_page_indexes(4, anchor=1, extend=True) == {1, 2, 3, 4}


def test_control_click_toggles_pages_and_control_shift_adds_range():
    assert select_page_indexes(4, anchor=1, toggle=True, selected={1, 2}) == {1, 2, 4}
    assert select_page_indexes(2, anchor=1, toggle=True, selected={1, 2}) == {1}
    assert select_page_indexes(5, anchor=3, extend=True, toggle=True, selected={0}) == {0, 3, 4, 5}


def test_visual_editor_combines_pdf_and_image_pages(tmp_path):
    pdf = tmp_path / "source.pdf"
    with fitz.open() as document:
        document.new_page(width=200, height=300)
        document.save(pdf)
    image = tmp_path / "image.png"
    Image.new("RGB", (120, 80), "white").save(image)
    output = tmp_path / "combined.pdf"

    combine_editor_sources([pdf, image], output)

    with fitz.open(output) as document:
        assert document.page_count == 2


def test_pdf_pages_sent_to_editor_follow_selection_or_use_whole_workspace():
    pages = [PageRef(Path("a.pdf"), index, 3) for index in range(3)]

    assert [page.page_index for page in page_refs_for_editor(pages, {0, 2})] == [0, 2]
    assert [page.page_index for page in page_refs_for_editor(pages, set())] == [0, 1, 2]
    assert [page.page_index for page in page_refs_for_editor(pages, {1}, selected_only=False)] == [0, 1, 2]


def test_pdf_workspace_saves_from_cached_copy_when_original_disappears(tmp_path):
    original = tmp_path / "network-source.pdf"
    cached = tmp_path / "cached-source.pdf"
    with fitz.open() as document:
        document.new_page(width=200, height=300)
        document.save(original)
    cached.write_bytes(original.read_bytes())
    original.unlink()
    output = tmp_path / "result.pdf"
    ref = PageRef(original, 0, 1, cached_source=cached)
    app = SimpleNamespace(
        _is_image_ref=lambda _ref: False,
        _image_ref_to_pdf_stream=lambda *_args: None,
    )

    PdfCombinerApp._write_page_refs(app, [ref], output)

    with fitz.open(output) as document:
        assert document.page_count == 1


def test_pdf_workspace_reports_all_missing_sources_before_writing(tmp_path):
    missing = tmp_path / "missing-network-source.pdf"
    output = tmp_path / "result.pdf"
    ref = PageRef(missing, 0, 1)
    app = SimpleNamespace(
        _is_image_ref=lambda _ref: False,
        _image_ref_to_pdf_stream=lambda *_args: None,
    )

    with pytest.raises(FileNotFoundError, match="сетевой папки") as error:
        PdfCombinerApp._write_page_refs(app, [ref], output)

    assert str(missing) in str(error.value)
    assert not output.exists()


def test_editor_save_format_replaces_stale_pdf_extension():
    assert editor_save_path_for_type(Path("result.pdf"), "JPEG", ".pdf") == Path("result.jpg")
    assert editor_save_path_for_type(Path("result.pdf"), "PNG", ".pdf") == Path("result.png")
    assert editor_save_path_for_type(Path("result.pdf"), "Word DOCX с распознанным текстом", ".pdf") == Path(
        "result.docx"
    )


def test_editor_source_path_conflict_handles_normalized_paths(tmp_path):
    source = tmp_path / "document.pdf"
    source.write_bytes(b"%PDF-1.4\n%%EOF")

    assert editor_output_is_source(source, tmp_path / "." / "document.pdf")
    assert not editor_output_is_source(source, tmp_path / "document_edited.pdf")


def test_visual_editor_reprompts_when_pdf_output_is_the_open_source(tmp_path, monkeypatch):
    source = tmp_path / "document.pdf"
    import fitz
    with fitz.open() as document:
        document.new_page()
        document.save(source)
    output = tmp_path / "document_edited.pdf"
    dialog_calls = []
    warnings = []
    tasks = []

    class FakeVariable:
        def __init__(self, value=""):
            self.value = value

        def get(self):
            return self.value

        def set(self, value):
            self.value = value

    responses = iter((str(source), str(output)))

    def fake_save_dialog(**kwargs):
        dialog_calls.append(kwargs)
        return next(responses)

    monkeypatch.setattr(gui.tk, "StringVar", FakeVariable)
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", fake_save_dialog)
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda title, text: warnings.append((title, text)))
    monkeypatch.setattr(gui, "pdf_has_signatures", lambda _path: False)

    app = SimpleNamespace(
        editor_pdf_path=source,
        editor_last_save_path=None,
        editor_new_document=False,
        preferences={},
        editor_text_edits=[],
        editor_signature_edits=[],
        editor_region_edits=[],
        editor_stroke_edits=[],
        editor_crop_edits=[],
        editor_alignment_edits=[],
        editor_ocr_language=FakeVariable("rus"),
        editor_text_index=[],
        editor_page_count=1,
        _editor_is_pdf=lambda: True,
        _editor_is_image=lambda: False,
        _remember_dialog_selection=lambda _path: None,
        _current_crop_rect=lambda: None,
        _run_task=lambda callback, message, **kwargs: tasks.append((callback, message, kwargs)),
        _editor_save_completed=lambda _result: None,
    )

    PdfCombinerApp._save_editor_pdf(app)

    assert len(dialog_calls) == 2
    assert dialog_calls[1]["initialfile"] == "document_edited.pdf"
    assert warnings and "исходный PDF нельзя заменить" in warnings[0][1]
    assert len(tasks) == 1


def test_visual_editor_exposes_distraction_free_reading_mode():
    source = source_text(ROOT / "src" / "redos_pdf_combiner" / "gui.py")

    assert 'text="Чтение"' in source
    assert 'reader_exit_button = ttk.Button' in source
    assert 'reader_print_button = ttk.Button' in source
    assert 'command=self._print_editor_document' in source
    assert 'columnconfigure(18, minsize=112, uniform="reader_actions")' in source
    assert 'columnconfigure(19, minsize=112, uniform="reader_actions")' in source
    assert 'reader_print_button.grid(row=0, column=18, sticky="nsew"' in source
    assert 'reader_exit_button.grid(row=0, column=19, sticky="nsew"' in source
    assert "def _enter_editor_reading_mode" in source
    assert "def _exit_editor_reading_mode" in source
    assert "self.notebook.navigation_shell.grid_remove()" in source
    assert "self.editor_content_panes.forget(self.editor_edits_shell)" in source
    editor_tab_source = source.split("def _editor_tab", 1)[1].split("def _", 1)[0]
    pdf_tab_source = source.split("def _pdf_tab", 1)[1].split("def _editor_tab", 1)[0]
    assert "self.editor_toolbar_shell = toolbar_shell" in editor_tab_source
    assert "self.editor_toolbar_shell = toolbar_shell" not in pdf_tab_source
    assert 'if source.suffix.lower() == ".pdf":' in source
    assert "lambda path=source: self._enter_editor_reading_mode()" not in source
    assert "self.editor_reading_restore_window = not self._window_maximized" in source
    assert "self.after(260, self._finish_entering_editor_reading_mode)" in source
    assert 'ttk.Menubutton(toolbar, text="Объект")' not in source


def test_visual_editor_toolbar_uses_icons_and_style_detection_is_context_only():
    source = source_text(ROOT / "src" / "redos_pdf_combiner" / "gui.py")
    editor_tab_source = source.split("def _editor_tab", 1)[1].split("def _images_tab", 1)[0]

    assert "create_editor_tool_icon" in editor_tab_source
    assert 'image=self.editor_tool_icons[mode]' in editor_tab_source
    assert '("detect", "Стиль"' not in editor_tab_source
    assert 'text="Определить стиль"' not in editor_tab_source
    assert 'label="Определить стиль в этой точке"' in source


def test_editor_preview_font_uses_pdf_scaled_pixels_and_resolved_family():
    edit = gui.TextEdit(
        0,
        (10, 20, 200, 80),
        "Текст",
        12,
        "Times New Roman",
        "/usr/share/fonts/liberation/LiberationSerif-BoldItalic.ttf",
        bold=True,
        italic=True,
        underline=True,
    )

    assert editor_preview_font_spec(edit, 1.5) == (
        "Liberation Serif",
        -18,
        "bold italic underline",
    )


def test_reading_mode_arrow_key_changes_page():
    page_changes = []
    app = SimpleNamespace(
        editor_reading_mode=True,
        _change_editor_page=lambda delta: page_changes.append(delta),
    )

    result = PdfCombinerApp._editor_reading_page_key(
        app,
        SimpleNamespace(widget=SimpleNamespace(), keysym="Right"),
    )

    assert result == "break"
    assert page_changes == [1]


def test_pdf_context_menu_is_unposted_and_forgotten():
    menu = SimpleNamespace(unpost_calls=0)
    menu.unpost = lambda: setattr(menu, "unpost_calls", menu.unpost_calls + 1)
    app = SimpleNamespace(pdf_context_menu=menu)

    PdfCombinerApp._dismiss_pdf_context_menu(app)

    assert menu.unpost_calls == 1
    assert app.pdf_context_menu is None


def test_background_click_closes_pdf_menu_even_without_selection():
    app = SimpleNamespace(
        selected=set(),
        dismiss_calls=0,
        _dismiss_pdf_context_menu=lambda: setattr(app, "dismiss_calls", app.dismiss_calls + 1),
    )

    PdfCombinerApp._clear_page_selection_from_background(app)

    assert app.dismiss_calls == 1


def test_canvas_wheel_scrolls_on_windows_and_x11_events():
    calls = []
    canvas = SimpleNamespace(
        yview_scroll=lambda amount, unit: calls.append(("y", amount, unit)),
        xview_scroll=lambda amount, unit: calls.append(("x", amount, unit)),
    )
    app = SimpleNamespace(_wheel_direction=PdfCombinerApp._wheel_direction)

    assert PdfCombinerApp._scroll_canvas_with_wheel(app, canvas, SimpleNamespace(num=0, delta=-120, state=0)) == "break"
    assert PdfCombinerApp._scroll_canvas_with_wheel(app, canvas, SimpleNamespace(num=4, delta=0, state=0)) == "break"
    assert PdfCombinerApp._scroll_canvas_with_wheel(app, canvas, SimpleNamespace(num=5, delta=0, state=1)) == "break"
    assert calls == [("y", 3, "units"), ("y", -3, "units"), ("x", 3, "units")]


def test_control_wheel_is_reserved_for_editor_zoom():
    calls = []
    canvas = SimpleNamespace(
        yview_scroll=lambda *args: calls.append(args),
        xview_scroll=lambda *args: calls.append(args),
    )
    app = SimpleNamespace(_wheel_direction=PdfCombinerApp._wheel_direction)

    assert PdfCombinerApp._scroll_canvas_with_wheel(app, canvas, SimpleNamespace(num=4, delta=0, state=4)) is None
    assert calls == []


def test_blank_sheet_opens_as_unsaved_editor_document(tmp_path, monkeypatch):
    source = tmp_path / "blank.pdf"
    source.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(gui, "BlankPageDialog", lambda _parent: SimpleNamespace(result=("A4", "portrait")))
    monkeypatch.setattr(gui, "create_blank_pdf", lambda *_args: source)
    monkeypatch.setattr(gui, "recovery_path", lambda: tmp_path / "recovery.json")

    warning = SimpleNamespace(value=None)
    warning.set = lambda value: setattr(warning, "value", value)
    status = SimpleNamespace(value=None)
    status.set = lambda value: setattr(status, "value", value)
    app = SimpleNamespace(
        editor_pdf_path=None,
        editor_new_document=False,
        editor_last_save_path=Path("old.pdf"),
        editor_dirty=False,
        editor_warning=warning,
        status=status,
        _show_error=lambda _error: None,
        _load_editor_pdf=lambda path: setattr(app, "editor_pdf_path", path),
        _queue_autosave=lambda: setattr(app, "autosave_queued", True),
        autosave_queued=False,
    )

    PdfCombinerApp._create_blank_editor_document(app)

    assert app.editor_pdf_path == source
    assert app.editor_new_document is True
    assert app.editor_last_save_path is None
    assert app.editor_dirty is True
    assert app.autosave_queued is True
