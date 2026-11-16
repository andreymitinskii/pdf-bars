"""Capture and check the native Windows workspace without changing preferences."""
import ctypes
from pathlib import Path
import tempfile
import time

import fitz
from PIL import ImageGrab

from redos_pdf_combiner import gui
from redos_pdf_combiner.editor import TextEdit
from redos_pdf_combiner.preferences import DEFAULT_PREFERENCES


def main():
    ctypes.windll.user32.SetProcessDPIAware()
    output = Path(__file__).resolve().parents[1] / "tmp" / "ui-redesign"
    output.mkdir(parents=True, exist_ok=True)
    gui.load_preferences = lambda: dict(DEFAULT_PREFERENCES)
    gui.save_preferences = lambda _: None
    gui.PdfCombinerApp._queue_autosave = lambda _: None
    errors = []
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "Workspace.pdf"
        with fitz.open() as pdf:
            for number in range(4):
                page = pdf.new_page()
                page.insert_text((55, 80), "pdf bars / document " + str(number + 1), fontsize=22)
                page.insert_textbox((55, 120, 540, 700), ("Document text and page content.\n" * 22), fontsize=12)
            pdf.save(source)
        app = gui.PdfCombinerApp(initially_hidden=True)
        app.report_callback_exception = lambda *args: errors.append(str(args))

        def settle():
            end = time.monotonic() + 0.5
            while time.monotonic() < end:
                app.update()
                time.sleep(0.01)

        def capture(name):
            settle()
            hwnd = ctypes.windll.user32.GetAncestor(app.winfo_id(), 2)
            with ImageGrab.grab(window=hwnd) as image:
                assert image.convert("L").getextrema()[0] != image.convert("L").getextrema()[1]
                image.save(output / (name + ".png"))

        try:
            app.geometry("1460x950+20+20")
            app.deiconify()
            app._load_editor_pdf(source)
            settle()
            app._fit_editor_page()
            capture("editor-wide")
            assert app.editor_canvas.winfo_height() > 600
            for index in range(1, 6):
                app.notebook.select(index)
                capture("tab-" + str(index))
            app.notebook.select(0)
            app.geometry("1200x780+20+20")
            app.editor_text_edits = [TextEdit(0, (55, 180, 300, 230), "", opaque_background=True)]
            app.editor_selected = ("text", 0)
            app.editor_place_mode = "select"
            app._load_selected_editor_properties()
            app._set_editor_draw_color("#F6C344")
            app._fit_editor_page()
            capture("area-compact")
            assert app.editor_canvas.winfo_height() > 250
            assert app.editor_document_commands.winfo_width() >= app.editor_document_commands.winfo_reqwidth()
            app._enter_editor_reading_mode()
            settle()
            app._exit_editor_reading_mode()
            app.editor_selected = None
            app._show_editor_properties("select")
            app._toggle_editor_grid()
            capture("all-pages")
            app._toggle_editor_grid()
            assert len(app.editor_content_panes.panes()) == 2
            assert not errors, errors
            print("COMPACT_WORKSPACE_OK", output)
        finally:
            app.destroy()


if __name__ == "__main__":
    main()
