import os
from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest

from redos_pdf_combiner import gui
from redos_pdf_combiner.editor import TextEdit
from redos_pdf_combiner.ocr import PageText, IndexedWord
from redos_pdf_combiner.render_budget import MAX_PREVIEW_PIXELS, MAX_PREVIEW_SIDE
from redos_pdf_combiner.preferences import DEFAULT_PREFERENCES, load_preferences, save_preferences


pytestmark = pytest.mark.skipif(os.name != "nt" and not os.environ.get("DISPLAY"), reason="Tk needs a display")


@pytest.fixture(scope="module")
def app(tmp_path_factory):
    tmp_path = tmp_path_factory.mktemp("editor-workspace")
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(gui, "load_preferences", lambda: dict(DEFAULT_PREFERENCES))
    monkeypatch.setattr(gui, "save_preferences", lambda settings: None)
    monkeypatch.setattr(gui.PdfCombinerApp, "_queue_autosave", lambda self: None)
    def cache(name):
        path = tmp_path / name
        path.mkdir(exist_ok=True)
        return path
    monkeypatch.setattr(gui, "app_cache_dir", cache)
    instance = gui.PdfCombinerApp(initially_hidden=True)
    instance.update_idletasks()
    def run(callback, message, *args, on_success=None, on_complete=None, **kwargs):
        try:
            result = callback()
            if on_success:
                on_success(result)
        finally:
            if on_complete:
                on_complete()
    monkeypatch.setattr(instance, "_run_task", run)
    yield instance
    for timer in instance.tk.call("after", "info"):
        instance.after_cancel(timer)
    instance.destroy()
    monkeypatch.undo()


def document(tmp_path):
    path = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        for number in range(4):
            pdf.new_page().insert_text((40, 40), f"PAGE {number}")
        pdf.save(path)
    return path


def test_reader_cache_opens_source_once(app, tmp_path, monkeypatch):
    source = document(tmp_path)
    real, calls = gui.PdfReader, []
    def reader(path):
        calls.append(path)
        return real(path)
    monkeypatch.setattr(gui, 'PdfReader', reader)
    refs = [gui.PageRef(source, index, 4) for index in range(4)]
    app._write_page_refs(refs, tmp_path / 'merged.pdf')
    assert len(calls) == 1


@pytest.mark.parametrize("rotation", [0, 90, 270])
def test_region_copy_captures_current_edits_without_selection(app, tmp_path, monkeypatch, rotation):
    from PIL import Image
    path = tmp_path / "source.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=300)
        page.draw_rect((30, 30, 140, 90), color=(1, 0, 0), fill=(1, 0, 0))
        page.set_rotation(rotation)
        pdf.save(path)
    app._load_editor_pdf(path)
    app.editor_text_edits = [TextEdit(0, (30, 30, 160, 90), "NEW", 24, color=(0, 0, 1),
        opaque_background=True, replacement_source_rect=(30, 30, 160, 90), scan_effect=False)]
    app.editor_selected = ("text", 0)
    monkeypatch.setattr(gui, "new_clipboard_image_path", lambda: tmp_path / "copy.png")
    copied = app._capture_editor_region(0, (30, 30, 160, 90))
    with Image.open(copied) as image:
        assert image.size == (260, 120)
        pixels = list(image.convert("RGB").getdata())
        assert sum(b > 180 and r < 60 and g < 60 for r, g, b in pixels) > 100
        assert not any(r > 180 and g < 60 and b < 60 for r, g, b in pixels)
    before = copied.read_bytes()
    app.editor_text_edits.clear()
    assert copied.read_bytes() == before


def test_word_preset_keeps_word_and_position(app, tmp_path, monkeypatch):
    target = tmp_path / "settings.json"
    monkeypatch.setattr(gui, "save_preferences", lambda settings: save_preferences(settings, target))
    monkeypatch.setattr(gui.simpledialog, "askstring", lambda *args, **kwargs: "Style")
    app.preferences["word_style_presets"] = {}
    app.editor_font_family.set("Times New Roman")
    app.editor_font_size.set(13.5)
    app.editor_text_bold.set(True)
    app.editor_text_underline.set(True)
    app.editor_word_scan_effect.set(False)
    app._set_editor_text_color("#123456")
    app._save_word_style()
    app.preferences = load_preferences(target)
    app.editor_replace_text.set("Another word")
    app.editor_word_offset_x.set(3.2)
    app.editor_word_offset_y.set(-2.1)
    app.editor_font_size.set(8)
    app._load_word_style("Style")
    assert app.editor_font_size.get() == 13.5
    assert app.editor_text_color.get() == "#123456"
    assert app.editor_text_bold.get() and app.editor_text_underline.get()
    assert not app.editor_word_scan_effect.get()
    assert app.editor_replace_text.get() == "Another word"
    assert app.editor_word_offset_x.get() == 3.2 and app.editor_word_offset_y.get() == -2.1
    app._refresh_word_styles_menu()


def test_word_eyedropper_reads_document_and_keeps_selection(app, tmp_path):
    path = tmp_path / "color.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=300)
        page.draw_rect((30, 30, 100, 100), color=(0.1, 0.2, 0.6), fill=(0.1, 0.2, 0.6))
        pdf.save(path)
    app._load_editor_pdf(path)
    app._render_editor_page()
    app.editor_selected = ("text", 0)
    app._start_word_color_pick()
    x, y, _, _ = app._pdf_rect_to_canvas((50, 50, 50, 50))
    app._pick_word_color(x, y)
    channels = [int(app.editor_text_color.get()[i:i+2], 16) for i in (1, 3, 5)]
    assert all(abs(a-b) <= 2 for a, b in zip(channels, (25, 51, 153))), channels
    assert app.editor_selected == ("text", 0)
    assert app.editor_place_mode == "select"


@pytest.mark.parametrize("scan_effect", [False, True])
def test_copy_replacement_word_preserves_scan_setting(app, tmp_path, monkeypatch, scan_effect):
    app._load_editor_pdf(document(tmp_path))
    app.editor_text_edits = [TextEdit(0, (40, 40, 140, 70), "Corrected", 14,
        replacement_source_rect=(40, 40, 140, 70), scan_blur=0.45, scan_effect=scan_effect)]
    app.editor_selected = ("text", 0)
    app._load_selected_editor_properties()
    assert app.editor_text_scan_effect.get() is scan_effect
    payload = app._editor_clipboard_payload()
    assert payload["data"]["scan_effect"] is scan_effect
    assert payload["data"]["scan_blur"] == (0.45 if scan_effect else 0)
    monkeypatch.setattr(app, "_read_editor_clipboard", lambda: payload)
    app._paste_editor_object()
    pasted = app.editor_text_edits[-1]
    assert pasted.scan_effect is scan_effect
    assert pasted.scan_blur == (0.45 if scan_effect else 0)
    assert pasted.replacement_source_rect is None
    assert pasted.replacement_background is None


def test_paste_legacy_word_payload_drops_disabled_blur(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    payload = {"version": 1, "kind": "text", "data": {"rect": [40, 40, 140, 70],
               "text": "Corrected", "scan_effect": False, "scan_blur": 0.45}}
    monkeypatch.setattr(app, "_read_editor_clipboard", lambda: payload)
    app._paste_editor_object()
    assert app.editor_text_edits[-1].scan_blur == 0


@pytest.mark.parametrize("key,code,action", [("c", 67, "copy"), ("C", 67, "copy"),
    ("Cyrillic_es", 67, "copy"), ("Cyrillic_em", 86, "paste"), ("v", 86, "paste")])
def test_clipboard_keys_accept_both_layouts(app, monkeypatch, key, code, action):
    calls = []
    monkeypatch.setattr(app, "_editor_copy_shortcut", lambda event: calls.append("copy") or "break")
    monkeypatch.setattr(app, "_editor_paste_shortcut", lambda event: calls.append("paste") or "break")
    assert app._editor_clipboard_keypress(SimpleNamespace(keysym=key, keycode=code, state=4)) == "break"
    assert calls == [action]
    assert app._editor_clipboard_keypress(SimpleNamespace(keysym=key, keycode=code, state=12)) is None
    assert calls == [action]


@pytest.mark.parametrize("keys", ["virtual", "physical", "physical_shift"])
def test_native_canvas_copy_paste_events_copy_edited_region(app, tmp_path, monkeypatch, keys):
    if keys != "virtual" and os.name != "nt":
        pytest.skip("Windows physical virtual-key codes")
    from PIL import Image
    app._load_editor_pdf(document(tmp_path))
    app.notebook.select(app.editor_tab)
    app.editor_reading_mode = False
    app.deiconify()
    app.update_idletasks()
    app.editor_canvas.focus_force()
    app.update()
    monkeypatch.setattr(gui, "new_clipboard_image_path", lambda: tmp_path / "native-copy.png")
    app.editor_text_edits = [TextEdit(0, (40, 40, 140, 70), "NEW", 14,
        replacement_source_rect=(40, 40, 140, 70), scan_blur=0.45, scan_effect=False)]
    app.editor_region_selection = (0, (30, 30, 160, 90))
    app.editor_selected = None
    def send(copy):
        if keys == "virtual":
            app.editor_canvas.event_generate("<<Copy>>" if copy else "<<Paste>>")
        else:
            app.editor_canvas.event_generate("<KeyPress>", keycode=67 if copy else 86,
                                             state=5 if keys == "physical_shift" else 4)
    try:
        send(True)
        app.update()
        payload = app._read_editor_clipboard()
        assert payload and payload["kind"] == "image"
        send(False)
        app.update()
        assert len(app.editor_signature_edits) == 1
        with Image.open(app.editor_signature_edits[0].image_path) as copied:
            assert copied.size == (260, 120)
    finally:
        app.withdraw()


def test_clipboard_shortcuts_do_not_steal_entry_text(app, monkeypatch):
    app.notebook.select(app.editor_tab)
    monkeypatch.setattr(app, "focus_get", lambda: app.editor_word_entry)
    monkeypatch.setattr(app, "_copy_selected_editor_object", lambda: pytest.fail("Stole entry copy"))
    monkeypatch.setattr(app, "_paste_editor_object", lambda: pytest.fail("Stole entry paste"))
    assert app._editor_copy_shortcut() is None
    assert app._editor_paste_shortcut() is None


def test_editor_reuses_base_raster_on_selection(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app._render_editor_page()
    photo = app.editor_page_image
    def fail(*args):
        raise AssertionError('Base page rendered again')
    monkeypatch.setattr(app, '_render_editor_base_preview', fail)
    app._render_editor_page()
    assert app.editor_page_image is photo


def indexed(page):
    return PageText(page, 595, 842, (IndexedWord((40, 40, 100, 55), 'Text', 0, 0, 0),), True)


def remember_index(app, pages, best=False):
    app._editor_index_ready(app.editor_search_generation, pages, 'page-best:rus' if best else 'ocr:rus')


def test_recognition_survives_undo_and_repeated_commands(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_ocr_language.set('rus')
    app._remember_editor_state()
    remember_index(app, [indexed(i) for i in range(4)])
    app._undo_editor_action()
    assert len(app.editor_text_index) == 4
    def fail(**kwargs):
        raise AssertionError('Repeated OCR')
    monkeypatch.setattr(app, '_build_editor_text_index', fail)
    app._start_editor_ocr()
    app._start_word_replacement()
    assert app.editor_place_mode == 'replace_word'


def test_word_ocr_is_reused_when_recognizing_remaining_pages(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_ocr_language.set('rus')
    remember_index(app, [indexed(2)], best=True)
    calls, callbacks = [], []
    def index(source, **kwargs):
        calls.append(kwargs)
        return [indexed(i) for i in kwargs['page_indexes']]
    class InlineThread:
        def __init__(self, target, **kwargs):
            self.target = target
        def start(self):
            self.target()
    monkeypatch.setattr(gui, 'build_text_index', index)
    monkeypatch.setattr(gui.threading, 'Thread', InlineThread)
    monkeypatch.setattr(app, 'after', lambda delay, callback=None: callbacks.append(callback))
    app._start_editor_ocr()
    assert calls[0]['page_indexes'] == [0, 1, 3]
    callback = callbacks[-1]
    callback()
    assert [page.page_index for page in app.editor_text_index] == [0, 1, 2, 3]
    assert (2, 'rus') in app.editor_best_ocr_pages
    app._start_editor_ocr()
    assert len(calls) == 1


def test_page_reorder_preserves_recognition_and_undo(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_ocr_language.set('rus')
    remember_index(app, [indexed(2)], best=True)
    app._arrange_editor_pages([2, 0, 1, 3], 'Reorder')
    assert [page.page_index for page in app.editor_text_index] == [0]
    app._undo_editor_action()
    assert [page.page_index for page in app.editor_text_index] == [2]


def test_changing_language_requires_a_new_index(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_ocr_language.set('rus')
    remember_index(app, [indexed(0)])
    app.editor_ocr_language.set('eng')
    app._reset_editor_search()
    assert not app.editor_text_index
    app.editor_ocr_language.set('rus')
    app._reset_editor_search()
    assert len(app.editor_text_index) == 1


def test_external_source_change_invalidates_recognition(app, tmp_path, monkeypatch):
    source = document(tmp_path)
    app._load_editor_pdf(source)
    app.editor_ocr_language.set('rus')
    remember_index(app, [indexed(i) for i in range(4)])
    with fitz.open(source) as pdf:
        pdf[0].insert_text((40, 80), 'Changed outside the editor')
        pdf.saveIncr()
    calls = []
    monkeypatch.setattr(app, '_build_editor_text_index', lambda **kwargs: calls.append(kwargs))
    app._start_editor_ocr()
    assert not app.editor_text_index
    assert calls == [dict(force_ocr=True)]


def test_ocr_result_from_replaced_source_is_discarded(app, tmp_path, monkeypatch):
    source = document(tmp_path)
    app._load_editor_pdf(source)
    app._editor_index_request_key = app._editor_ocr_cache_key()
    with fitz.open(source) as pdf:
        pdf[0].insert_text((40, 80), 'Changed during recognition')
        pdf.saveIncr()
    errors = []
    monkeypatch.setattr(app, '_show_error', lambda exc, details: errors.append(str(exc)))
    remember_index(app, [indexed(i) for i in range(4)])
    assert errors
    assert not app.editor_text_index
    assert not app.editor_index_busy


def test_crop_undo_restores_recognition_of_original_page(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_ocr_language.set('rus')
    remember_index(app, [indexed(i) for i in range(4)])
    app.editor_pending_crop = (80, 80, 300, 400)
    app._apply_pending_crop()
    assert not app.editor_text_index
    app._undo_editor_action()
    assert len(app.editor_text_index) == 4


def test_large_page_preview_is_capped_without_changing_pdf(app, tmp_path):
    source = tmp_path / 'long.pdf'
    with fitz.open() as pdf:
        pdf.new_page(width=595, height=20000)
        pdf.save(source)
    before = source.read_bytes()
    app._load_editor_pdf(source)
    app.editor_zoom_percent.set(400)
    app._render_editor_page()
    image = app.editor_page_image
    assert image.width() * image.height() <= MAX_PREVIEW_PIXELS + MAX_PREVIEW_SIDE
    assert max(image.width(), image.height()) <= MAX_PREVIEW_SIDE + 1
    assert app.editor_zoom_percent.get() < 400
    assert source.read_bytes() == before


def test_only_visible_thumbnails_are_rendered(app, tmp_path, monkeypatch):
    source = tmp_path / 'many.pdf'
    with fitz.open() as pdf:
        for _ in range(200):
            pdf.new_page()
        pdf.save(source)
    app._load_editor_pdf(source)
    app.geometry('1400x1000+0+0')
    app.deiconify()
    app._refresh_editor_thumbnails()
    app.update_idletasks()
    rendered = []
    def render(index):
        rendered.append(index)
        return app._editor_thumbnail_placeholder
    monkeypatch.setattr(app, '_render_editor_thumbnail_image', render)
    generation = app.editor_thumbnail_generation
    for _ in range(70):
        app._render_next_editor_thumbnail(generation, 0)
    first = set(rendered)
    assert 0 < len(first) < 64
    app.editor_thumbnail_canvas.yview_moveto(0.95)
    app.update_idletasks()
    for _ in range(70):
        app._render_next_editor_thumbnail(generation, 0)
    assert max(rendered) >= 180
    assert len(app._editor_loaded_thumbnails) <= 64
    assert not first.intersection(app._editor_loaded_thumbnails)
    app.withdraw()


def test_append_keeps_existing_recognized_pages(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_ocr_language.set('rus')
    remember_index(app, [indexed(i) for i in range(4)])
    app._append_editor_files([app.editor_pdf_path])
    assert app.editor_page_count == 8
    assert [page.page_index for page in app.editor_text_index] == [0, 1, 2, 3]


def test_startup_opens_normal_editor_and_fits_page(app, tmp_path):
    app.editor_dirty = False
    app._clear_editor_document()
    source = document(tmp_path)
    app._open_startup_paths([str(source)])
    app.update_idletasks()
    assert app.editor_pdf_path.read_bytes() == source.read_bytes()
    assert not app.editor_reading_mode
    assert not app.editor_grid_mode


def test_crop_can_be_undone_and_redone_without_losing_edits(app, tmp_path):
    source = document(tmp_path)
    app._load_editor_pdf(source)
    app.editor_page_index = 2
    edit = TextEdit(2, (100, 100, 200, 125), "Preserved")
    app.editor_text_edits = [edit]
    app.editor_pending_crop = (80, 80, 300, 400)
    app._apply_pending_crop()
    cropped = app.editor_pdf_path
    assert cropped != source
    assert app.editor_page_index == 2
    with fitz.open(cropped) as pdf:
        assert tuple(pdf[2].rect) == (0, 0, 220, 320)
    app._undo_editor_action()
    assert app.editor_pdf_path == source
    assert app.editor_text_edits == [edit]
    assert app.editor_page_index == 2
    app._redo_editor_action()
    assert app.editor_pdf_path == cropped
    assert app.editor_page_index == 2


def test_stationary_click_after_toolbar_reflow_does_not_move_word(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    edit = TextEdit(0, (80, 120, 180, 150), "Replaced", replacement_source_rect=(80, 122, 170, 150))
    app.editor_text_edits = [edit]
    app.editor_selected = ("text", 0)
    app.editor_object_drag = ("text", 0, "move", edit.rect)
    app.editor_drag_start = (100, 100)
    app.editor_object_drag_root = (500, 500)
    app.editor_offset = (10, 5)
    # Same screen point, but a different canvas y after the properties panel opens.
    event = SimpleNamespace(x=100, y=45, x_root=500, y_root=500)
    app._editor_canvas_release(event)
    assert app.editor_text_edits == [edit]


def test_failed_crop_keeps_history_and_releases_modal(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_pending_crop = (20, 20, 100, 100)
    source = app.editor_pdf_path
    def failure(callback, message, **kwargs):
        kwargs["on_complete"]()
    monkeypatch.setattr(app, "_run_task", failure)
    app._apply_pending_crop()
    assert app.editor_pdf_path == source
    assert app.editor_word_busy_dialog is None


@pytest.mark.parametrize("scale", [0.5, 1.0, 2.0])
def test_resize_uses_mouse_delta_without_initial_jump(app, scale):
    app.editor_scale = scale
    app.editor_offset = (20, 20)
    app.editor_page_size = (600, 800)
    original = (40, 50, 60, 80)
    app.editor_object_drag = ("signature", 0, "resize", original)
    rect = app._pdf_rect_to_canvas(original)
    app.editor_drag_start = (rect[2] - 8, rect[3] - 8)
    assert app._editor_object_drag_rect(*app.editor_drag_start) == pytest.approx(rect)
    resized = app._editor_object_drag_rect(app.editor_drag_start[0] + 1, app.editor_drag_start[1])
    assert resized[2] == pytest.approx(rect[2] + 1)
    app.editor_drag_start = app.editor_object_drag = None


def test_eraser_footprint_follows_zoom_and_hides_on_tool_change(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_place_mode = "free_erase"
    app.editor_scale = 2
    app.editor_offset = (0, 0)
    app.editor_eraser_width.set(14)
    app._editor_eraser_preview(SimpleNamespace(x=70, y=70))
    items = app.editor_canvas.find_withtag("eraser-preview")
    assert len(items) == 2
    rect = app.editor_canvas.coords(items[0])
    assert rect[2] - rect[0] == 28
    app._set_editor_tool_visual("select")
    assert not app.editor_canvas.find_withtag("eraser-preview")


def test_word_busy_dialog_grabs_input_and_releases_it(app):
    app.deiconify()
    app._show_word_busy("OCR")
    dialog = app.editor_word_busy_dialog
    try:
        assert app.grab_current() == dialog
        assert "all" not in dialog.bindtags()
        assert str(app) not in dialog.bindtags()
        app._start_word_replacement()
        assert app.editor_word_busy_dialog == dialog
    finally:
        app._finish_word_busy()
        app.withdraw()
    assert app.grab_current() is None
    assert app.editor_word_busy_dialog is None


def test_single_editor_tab_and_grid_switch(app, tmp_path):
    assert [button.cget("text") for _, button, _ in app.notebook._tabs][0] == "PDF Редактор"
    assert all(button.cget("text") != "PDF" for _, button, _ in app.notebook._tabs)
    app._load_editor_pdf(document(tmp_path))
    app._refresh_editor_thumbnails()
    app._toggle_editor_grid()
    assert len(app.editor_content_panes.panes()) == 1
    app._open_editor_thumbnail(2)
    assert not app.editor_grid_mode
    assert app.editor_page_index == 2
    assert len(app.editor_content_panes.panes()) == (3 if app.editor_edits_visible.get() else 2)


def test_word_replacement_properties_use_one_toolbar_row(app):
    word_panel = app.editor_property_frames["word"]
    row = word_panel.property_row
    assert all(child.pack_info().get('side') in ('left', 'right') for child in row.winfo_children())


def test_word_position_nudge_uses_tenths(app, monkeypatch):
    app.editor_word_offset_x.set(0.0)
    app.editor_word_offset_y.set(0.0)
    monkeypatch.setattr(app, "_apply_selected_word_properties", lambda: None)

    app._nudge_selected_word(0.1, -0.1)

    assert app.editor_word_offset_x.get() == 0.1
    assert app.editor_word_offset_y.get() == -0.1


def test_page_zoom_mode_refits_after_canvas_resize(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    calls = []
    monkeypatch.setattr(app, "_fit_editor_page", lambda: calls.append("fit"))
    app.editor_zoom_mode = "page"

    app._refit_editor_page_after_resize()

    assert calls == ["fit"]
    app.editor_zoom_mode = "manual"
    app._refit_editor_page_after_resize()
    assert calls == ["fit"]


def test_all_pages_button_matches_save_and_print_position(app):
    button = app.editor_view_button
    assert button.cget("style") == "Accent.TButton"
    assert int(button.grid_info()["column"]) == 2
    row = {int(widget.grid_info()["column"]): widget for widget in button.master.grid_slaves(row=0)}
    assert row[1].cget("text") == "Сохранить как…"
    assert row[3].cget("text") == "Печать"


@pytest.mark.parametrize("width", [1366, 1600])
def test_reading_actions_fit_linux_and_windows_font_metrics(app, tmp_path, width):
    app._load_editor_pdf(document(tmp_path))
    app._enter_editor_reading_mode()
    app.geometry(f"{width}x1000+0+0")
    app.deiconify()
    app.update_idletasks()
    toolbar = app.editor_reader_search_entry.master
    buttons = {int(widget.grid_info()["column"]): widget for widget in toolbar.grid_slaves(row=0)}
    try:
        assert buttons[19].winfo_x() + buttons[19].winfo_width() <= toolbar.winfo_width()
        assert abs(buttons[18].winfo_width() - buttons[19].winfo_width()) <= 2
        assert app.editor_reader_search_entry.winfo_width() >= 70
    finally:
        app._exit_editor_reading_mode()
        app.withdraw()


def test_append_files_requests_no_success_report(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    calls = []
    def task(callback, message, *args, **kwargs):
        calls.append(kwargs)
        kwargs["on_complete"]()
    monkeypatch.setattr(app, "_run_task", task)
    app._append_editor_files([app.editor_pdf_path])
    assert calls[0]["quiet"] is True
    assert callable(calls[0]["on_success"])


def test_quiet_success_still_adopts_document_without_report(app, monkeypatch):
    reports, adopted = [], []
    monkeypatch.setattr(app, "_show_success", lambda *args: reports.append(args))
    app._task_succeeded("result", "added", [], on_success=adopted.append, quiet=True)
    assert adopted == ["result"]
    assert not reports


def test_reorder_duplicate_undo_keeps_edits_with_page(app, tmp_path):
    original = document(tmp_path)
    app._load_editor_pdf(original)
    edit = TextEdit(2, (40, 70, 130, 95), "CORRECTED", 12)
    app.editor_text_edits = [edit]
    app._arrange_editor_pages([2, 0, 2, 3], "test")
    assert [item.page_index for item in app.editor_text_edits] == [0, 2]
    app._undo_editor_action()
    assert app.editor_pdf_path == original
    assert app.editor_text_edits == [edit]
    app._redo_editor_action()
    with fitz.open(app.editor_pdf_path) as pdf:
        assert [page.get_text().strip() for page in pdf] == ["PAGE 2", "PAGE 0", "PAGE 2", "PAGE 3"]


def test_reorder_and_undo_keep_thumbnail_source_names(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_page_source_names = ["first.pdf", "second.pdf", "third.pdf", "fourth.pdf"]

    app._arrange_editor_pages([2, 0, 3, 1], "test")
    assert app.editor_page_source_names == ["third.pdf", "first.pdf", "fourth.pdf", "second.pdf"]

    app._undo_editor_action()
    assert app.editor_page_source_names == ["first.pdf", "second.pdf", "third.pdf", "fourth.pdf"]


def test_thumbnail_ctrl_shift_and_blank_selection(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    for index, state in ((0, 0), (2, 4)):
        event = SimpleNamespace(state=state, x_root=50, y_root=50)
        app._press_editor_thumbnail(event, index)
        app._release_editor_thumbnail(event)
    assert app.editor_page_selection == {0, 2}
    event = SimpleNamespace(state=0, x_root=50, y_root=50)
    app._press_editor_thumbnail(event, 1)
    app._release_editor_thumbnail(event)
    assert app.editor_page_selection == {1}
    event.state = 1
    app._press_editor_thumbnail(event, 3)
    assert app.editor_page_selection == {1, 2, 3}
    app._clear_editor_page_selection()
    assert not app.editor_page_selection


def test_editor_thumbnails_show_source_name_and_bright_selection(app, tmp_path):
    source = tmp_path / "договор.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 40), "SOURCE")
        pdf.save(source)
    app._load_editor_pdf(source)
    app._refresh_editor_thumbnails()

    bundle = app.editor_thumbnail_widgets[0]
    assert "договор.pdf" in bundle["label"].cget("text")
    assert "Страница 1" in bundle["label"].cget("text")
    assert bundle["frame"]._panel_border == gui.BLUE
    assert bundle["frame"]._panel_border_width == 3
    assert int(bundle["frame"].cget("highlightthickness")) == 3
    assert bundle["frame"].cget("highlightbackground") == gui.BLUE


def test_editor_thumbnail_drag_reflows_and_clears_drop_ghost(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app._refresh_editor_thumbnails()
    app.update_idletasks()

    app.editor_page_selection = {0}
    app._show_editor_thumbnail_drop_ghost(3)
    app.update_idletasks()

    assert app.editor_thumbnail_drop_ghost is not None
    assert len(app.editor_thumbnail_drop_ghost) == 1
    assert app.editor_thumbnail_drop_ghost[0].winfo_exists()
    assert app.editor_thumbnail_widgets[0]["frame"].grid_info() == {}
    assert int(app.editor_thumbnail_widgets[1]["frame"].grid_info()["row"]) == 0
    assert int(app.editor_thumbnail_widgets[2]["frame"].grid_info()["row"]) == 1
    assert int(app.editor_thumbnail_drop_ghost[0].grid_info()["row"]) == 2

    app._clear_editor_thumbnail_drop_ghost()
    app.update_idletasks()
    assert app.editor_thumbnail_drop_ghost is None
    assert int(app.editor_thumbnail_widgets[0]["frame"].grid_info()["row"]) == 0


def test_page_export_contains_edits(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_text_edits = [TextEdit(2, (40, 70, 180, 100), "CORRECTED", 12)]
    output = tmp_path / "page.pdf"
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", lambda **kwargs: str(output))
    app._export_editor_pages([2], choose_format=True)
    with fitz.open(output) as pdf:
        assert len(pdf) == 1
        assert "CORRECTED" in pdf[0].get_text()
        assert "PAGE 2" in pdf[0].get_text()


def test_replace_word_waits_for_click_before_ocr(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_page_index = 2
    calls = []
    monkeypatch.setattr(app, "_build_editor_text_index", lambda **kwargs: calls.append(kwargs))
    app._start_word_replacement()
    assert calls == []
    assert app.editor_place_mode == "replace_word"


def test_replace_word_click_prioritizes_local_ocr(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app._start_word_replacement()
    calls = []
    def recognize(source, page, point, **kwargs):
        calls.append((page, point))
        return indexed(page)
    monkeypatch.setattr(gui, "recognize_word_region", recognize)
    monkeypatch.setattr(app, "_show_word_busy", lambda *args: None)
    monkeypatch.setattr(app, "_finish_word_busy", lambda: None)
    monkeypatch.setattr(app, "_prepare_editor_replacement_word",
                        lambda page, word: calls.append(word.text))
    x, y, _, _ = app._pdf_rect_to_canvas((60, 46, 60, 46))
    app._replace_word_at_canvas_point(x, y)
    assert calls[0][0] == 0
    assert calls[0][1] == pytest.approx((60, 46))
    assert calls[1] == "Text"


def test_word_dialog_starts_full_page_ocr_in_background(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_word_region_uses_ocr = True
    calls = []
    monkeypatch.setattr(app, "_build_editor_text_index", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(gui, "WordReplacementDialog",
                        lambda *args, **kwargs: SimpleNamespace(result=None))
    app._prompt_word_replacement(SimpleNamespace(font_family="Times New Roman",
                        font_size=12, bold=False, italic=False), "Text")
    assert calls == [dict(force_ocr=True, page_indexes=[0], quality="word", background_only=True)]


def test_native_word_uses_native_page_index_in_background(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_word_region_uses_ocr = False
    calls = []
    monkeypatch.setattr(app, "_build_editor_text_index", lambda **kwargs: calls.append(kwargs))
    monkeypatch.setattr(gui, "WordReplacementDialog",
                        lambda *args, **kwargs: SimpleNamespace(result=None))
    app._prompt_word_replacement(SimpleNamespace(font_family="Times New Roman",
                        font_size=12, bold=False, italic=False), "Text")
    assert calls == [dict(force_ocr=False, page_indexes=[0], quality="fast", background_only=True)]


def test_appended_pdf_drag_releases_over_ghost(app, tmp_path):
    original = document(tmp_path)
    appended = tmp_path / "attached.pdf"
    with fitz.open() as pdf:
        pdf.new_page().insert_text((40, 40), "ATTACHED")
        pdf.save(appended)
    app._load_editor_pdf(original)
    app._append_editor_files([appended])
    if not app.editor_grid_mode:
        app._toggle_editor_grid()
    app.deiconify()
    app.update()
    assert app.editor_page_count == 5

    source = app.editor_thumbnail_widgets[4]["frame"]
    event = SimpleNamespace(state=0, x_root=source.winfo_rootx() + 20,
                            y_root=source.winfo_rooty() + 20)
    app._press_editor_thumbnail(event, 4)
    app.editor_page_drag = (4, event.x_root, event.y_root, True, True)
    app._show_editor_thumbnail_drop_ghost(0)
    app.update()
    ghost = app.editor_thumbnail_drop_ghost[0]
    ghost.event_generate("<ButtonRelease-1>", x=10, y=10,
                         rootx=ghost.winfo_rootx() + 10, rooty=ghost.winfo_rooty() + 10)
    app.update()

    assert app.editor_thumbnail_drop_ghost is None
    assert app.editor_page_drag is None
    with fitz.open(app.editor_pdf_path) as pdf:
        assert "ATTACHED" in pdf[0].get_text()
    if app.editor_grid_mode:
        app._toggle_editor_grid()
    app.withdraw()


def test_background_word_ocr_does_not_close_active_word_dialog(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_page_index = 1
    app.editor_place_mode = "replace_word"
    app.editor_index_busy = True
    app.editor_word_background_ocr = True
    app._editor_index_request_key = app._editor_ocr_cache_key()
    calls = []
    monkeypatch.setattr(app, "_finish_word_busy", lambda: calls.append("closed"))
    app._editor_index_ready(app.editor_search_generation, [indexed(1)], "page-best:rus")
    assert calls == []
    assert app.editor_place_mode == "replace_word"
    assert (1, "rus") in app.editor_best_ocr_pages
    assert not app.editor_index_busy


def test_docx_save_recognizes_edited_document_not_search_cache(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_text_edits = [TextEdit(0, (40, 80, 180, 110), "CORRECTED", 12)]
    app.editor_text_index = [object()]
    output = tmp_path / "edited.docx"
    def choose(**kwargs):
        kwargs["typevariable"].set("Word DOCX с распознанным текстом")
        return str(output)
    calls = []
    def convert(source, destination, **kwargs):
        with fitz.open(source) as pdf:
            assert "CORRECTED" in pdf[0].get_text()
        calls.append(kwargs)
        return destination
    monkeypatch.setattr(gui.filedialog, "asksaveasfilename", choose)
    monkeypatch.setattr(gui, "source_to_layout_ocr_docx", convert)
    app._save_editor_pdf()
    assert calls == [dict(dpi=350, language="rus")]


def test_whats_new_acknowledgement_persists(tmp_path):
    settings = dict(DEFAULT_PREFERENCES, whats_new_version="1.4")
    path = tmp_path / "preferences.json"
    save_preferences(settings, path)
    assert load_preferences(path)["whats_new_version"] == "1.4"


def test_sidebar_thumbnails_resize_without_recreating_tiles(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app.editor_grid_mode = False
    app.update_idletasks()
    app._refresh_editor_thumbnails()
    frames = [b['frame'] for b in app.editor_thumbnail_widgets]
    app.editor_page_selection = {1, 2}
    app._refresh_editor_thumbnail_state()
    monkeypatch.setattr(app.editor_thumbnail_canvas, 'winfo_width', lambda: 630)
    app._resize_editor_thumbnails()
    assert app._editor_thumbnail_size == (600, 776)
    photo = app._render_editor_thumbnail_image(0)
    assert (photo.width(), photo.height()) == (600, 776)
    assert [b['frame'] for b in app.editor_thumbnail_widgets] == frames
    assert app.editor_page_selection == {1, 2}
    assert frames[1]._panel_border_width == 3
    monkeypatch.setattr(app.editor_thumbnail_canvas, 'winfo_width', lambda: 158)
    app._resize_editor_thumbnails()
    assert app._editor_thumbnail_size[0] == 128
    assert [b['frame'] for b in app.editor_thumbnail_widgets] == frames
    app.editor_grid_mode = True
    app._resize_editor_thumbnails()
    assert app._editor_thumbnail_size == (116, 150)
    app.editor_grid_mode = False


def test_thumbnail_resize_is_debounced(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    app._refresh_editor_thumbnails()
    monkeypatch.setattr(app.editor_thumbnail_canvas, 'winfo_width', lambda: 500)
    app._schedule_editor_thumbnail_resize()
    first = app._thumbnail_resize_after
    app._schedule_editor_thumbnail_resize()
    second = app._thumbnail_resize_after
    assert first != second
    assert first not in app.tk.call('after', 'info')
    assert second in app.tk.call('after', 'info')
    app.after_cancel(second)
    app._thumbnail_resize_after = None


def test_plain_text_scan_toggle_survives_selection_and_undo(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_text_edits = [TextEdit(0, (40, 70, 200, 100), 'SCAN', 18)]
    app.editor_selected = ('text', 0)
    app._load_selected_editor_properties()
    assert not app.editor_text_scan_effect.get()
    app.editor_text_scan_effect.set(True)
    app.editor_text_underline.set(True)
    app._apply_selected_text_properties()
    scanned = app.editor_text_edits[0]
    assert scanned.scan_blur == 0.45 and scanned.underline
    app._load_selected_editor_properties()
    assert app.editor_text_scan_effect.get()
    app.editor_text_scan_effect.set(False)
    app._apply_selected_text_properties()
    assert app.editor_text_edits[0].scan_blur == 0
    app._undo_editor_action()
    assert app.editor_text_edits[0] == scanned


def test_context_text_uses_scan_and_underline(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    monkeypatch.setattr(app, '_context_pdf_point', lambda: (40, 70))
    monkeypatch.setattr(gui.simpledialog, 'askstring', lambda *a, **kw: 'NEW')
    app.editor_text_scan_effect.set(True)
    app.editor_text_underline.set(True)
    app._add_context_text()
    assert app.editor_text_edits[-1].scan_blur > 0
    assert app.editor_text_edits[-1].underline


def test_replacement_dialog_passes_underline_to_edit(app, tmp_path, monkeypatch):
    app._load_editor_pdf(document(tmp_path))
    prepared = SimpleNamespace(font_family='Times New Roman', font_size=18,
        bold=False, italic=False, source_rect=(40, 70, 140, 90),
        background_color=(1,1,1), background_path='', background_rect=(38,68,142,92),
        confidence=0.9, scan_blur=0.3)
    monkeypatch.setattr(gui, 'WordReplacementDialog',
                        lambda *a, **kw: SimpleNamespace(result=('NEW', True, True, True, True)))
    app._prompt_word_replacement(prepared, 'OLD')
    edit = app.editor_text_edits[-1]
    assert edit.underline and edit.bold and edit.italic and edit.scan_effect
    assert app.editor_text_underline.get()
    app.editor_text_underline.set(False)
    app._apply_selected_word_properties()
    assert not app.editor_text_edits[-1].underline


def test_text_property_rows_can_scroll_without_clipping_controls(app):
    import time
    app.notebook.select(app.editor_tab)
    app.editor_properties_shell.grid()
    for name in ('text', 'word'):
        app._show_editor_properties(name)
        app.geometry('1080x800+0+0')
        app.deiconify()
        deadline = time.monotonic() + 0.3
        while time.monotonic() < deadline:
            app.update()
            time.sleep(0.01)
        panel = app.editor_property_frames[name]
        assert panel.property_row.winfo_width() >= panel.property_row.winfo_reqwidth()
        assert panel.property_canvas.winfo_width() < panel.property_row.winfo_width()
        panel.property_canvas.xview_moveto(1)
        app.update_idletasks()
        assert panel.property_canvas.xview()[0] > 0
    app.geometry('1400x1000+0+0')
    app.withdraw()


def test_closed_area_recolor_preserves_geometry_and_can_be_undone(app, tmp_path):
    from PIL import Image
    app._load_editor_pdf(document(tmp_path))
    original = TextEdit(0, (40, 50, 140, 110), "", opaque_background=True)
    app.editor_text_edits = [original]
    app.editor_selected = ("text", 0)
    app.editor_place_mode = "select"
    app._load_selected_editor_properties()
    assert app.editor_draw_color.get() == "#ffffff"
    app._set_editor_draw_color("#E53935")
    changed = app.editor_text_edits[0]
    assert changed.rect == original.rect and not changed.text
    assert changed.background_color == pytest.approx((229/255, 57/255, 53/255))
    output = app._capture_editor_region(0, (60, 65, 100, 95))
    with Image.open(output) as image:
        assert image.convert("RGB").getpixel((20, 20)) == (229, 57, 53)
    app._undo_editor_action()
    assert app.editor_text_edits[0] == original


def test_changing_drawing_color_does_not_recolor_selected_text(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    original = TextEdit(0, (40, 50, 140, 110), "Sample", opaque_background=True)
    app.editor_text_edits = [original]
    app.editor_selected = ("text", 0)
    app.editor_place_mode = "select"
    app._set_editor_draw_color("#087FD4")
    assert app.editor_text_edits == [original]


def test_compact_workspace_panels_and_modes(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.editor_edits_visible.set(False)
    app._sync_editor_edits_visibility()
    assert len(app.editor_content_panes.panes()) == 2
    app._show_editor_properties("select")
    assert not app.editor_properties_shell.winfo_manager()
    app.editor_edits_visible.set(True)
    app._sync_editor_edits_visibility()
    assert len(app.editor_content_panes.panes()) == 3
    app._toggle_editor_grid()
    assert len(app.editor_content_panes.panes()) == 1
    app._toggle_editor_grid()
    assert len(app.editor_content_panes.panes()) == 3
    app.editor_edits_visible.set(False)
    app._sync_editor_edits_visibility()
    assert int(app.header_canvas.cget("height")) == 44
    assert not hasattr(app, "header_brand_image")
    assert not hasattr(app, "about_brand_image")


def test_reference_navigation_has_icons_and_clear_active_state(app):
    app.notebook.select(0)
    tabs = app.notebook._tabs
    assert len(tabs) == 6
    for index, (_, button, _) in enumerate(tabs):
        assert button.cget("image")
        assert button.cget("compound") == "left"
        assert button.cget("bg") == (gui.BLUE if index == 0 else gui.NAV_BG)
    app.notebook.select(3)
    assert tabs[3][1].cget("bg") == gui.BLUE
    assert tabs[0][1].cget("bg") == gui.NAV_BG
    app.notebook.select(0)


def test_thumbnail_selection_has_fill_and_border_without_replacing_widgets(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app._refresh_editor_thumbnails()
    original_widgets = [b["frame"] for b in app.editor_thumbnail_widgets]
    app.editor_page_selection.clear()
    app.editor_page_index = 2
    app._refresh_editor_thumbnail_state()
    for index, bundle in enumerate(app.editor_thumbnail_widgets):
        assert bundle["frame"] is original_widgets[index]
        assert bundle["frame"]._panel_fill == ("#E1EDFF" if index == 2 else gui.PANEL)
        assert bundle["frame"]._panel_border_width == (3 if index == 2 else 1)


def test_primary_conversion_actions_have_icons_across_workspace(app):
    def buttons(parent):
        for child in parent.winfo_children():
            if isinstance(child, gui.ttk.Button):
                yield child
            yield from buttons(child)
    actions = [b for b in buttons(app.notebook.content)
               if str(b.cget("text")).startswith("Преобразовать") or str(b.cget("text")) in {"Создать PDF", "Сжать PDF"}]
    assert len(actions) >= 5
    assert all(b.cget("style") == "Primary.TButton" and b.cget("image") for b in actions)


def test_page_fit_uses_real_small_canvas_height(app, tmp_path):
    app._load_editor_pdf(document(tmp_path))
    app.geometry("1200x780+0+0")
    app.deiconify()
    app._show_editor_properties("area")
    app.update_idletasks()
    app._fit_editor_page()
    left, top = app.editor_offset
    page_width, page_height = app.editor_page_size
    assert left + page_width * app.editor_scale <= app.editor_canvas.winfo_width() + 1
    assert top + page_height * app.editor_scale <= app.editor_canvas.winfo_height() + 1
    heading = app.editor_thumbnail_canvas.master.grid_slaves(row=0, column=0)[0]
    label = heading.grid_slaves(row=0, column=0)[0]
    assert label.winfo_width() >= label.winfo_reqwidth()
    app._show_editor_properties("select")
    app.geometry("1460x920+0+0")
    app.withdraw()


def test_editor_retains_temporary_viewer_image_after_source_is_removed(app, tmp_path, monkeypatch):
    from PIL import Image
    from redos_pdf_combiner.editor import apply_image_edits
    app.editor_dirty = False
    app._clear_editor_document()
    monkeypatch.setattr(gui.tempfile, 'gettempdir', lambda: str(tmp_path))
    folder = tmp_path/'atril-1309078'
    folder.mkdir()
    source = folder/'image.9WA0X3.png'
    Image.new('RGB',(200,300),'white').save(source)
    app._load_editor_file(source)
    retained = app.editor_pdf_path
    assert retained != source and retained.is_file()
    assert app.editor_page_source_names == [source.name]
    source.unlink()
    app.update_idletasks()
    app._render_editor_page()
    output=tmp_path/'cropped.png'
    apply_image_edits(retained, output, [], [], crop_rect=(10,20,110,120))
    with Image.open(output) as cropped:
        assert cropped.size == (100,100)


def test_editor_normalizes_pdf_extension_with_image_contents(app, tmp_path):
    from PIL import Image
    app.editor_dirty = False
    app._clear_editor_document()
    source = tmp_path/'scan.pdf'
    Image.new('RGB',(200,300),'white').save(source,format='PNG')
    app._load_editor_file(source)
    assert app.editor_source_kind == 'pdf'
    assert app.editor_page_source_names == [source.name]
    with fitz.open(app.editor_pdf_path) as pdf:
        assert pdf.is_pdf and pdf.page_count == 1
    app.update_idletasks()


def test_regular_source_does_not_get_an_unnecessary_snapshot(app, tmp_path, monkeypatch):
    source = document(tmp_path)
    monkeypatch.setattr(gui.tempfile, 'gettempdir', lambda: str(tmp_path/'other-temp-root'))
    if not any(str(source).startswith(root) for root in ('/tmp/', '/var/tmp/')):
        assert app._retain_editor_source(source) == source


def test_patch_update_notice_is_shown_once_after_application_version(app, monkeypatch):
    monkeypatch.setitem(app.preferences, 'whats_new_version', '1.6')
    app._show_whats_new()
    dialog = next(w for w in app.winfo_children() if isinstance(w, gui.tk.Toplevel) and '1.7' in w.title())
    try:
        body = next(w for w in dialog.winfo_children() if isinstance(w, gui.tk.Frame))
        text = next(w for w in body.winfo_children() if isinstance(w, gui.tk.Text))
        assert 'Добавлен новый облегчённый интерфейс и исправлены ошибки' in text.get('1.0', 'end')
        button = next(w for w in dialog.winfo_children() if isinstance(w, gui.ttk.Button))
        button.invoke()
        assert app.preferences['whats_new_version'] == '1.7'
        app._show_whats_new()
        assert not any(isinstance(w, gui.tk.Toplevel) and '1.7' in w.title() for w in app.winfo_children())
    finally:
        if dialog.winfo_exists():
            dialog.destroy()
