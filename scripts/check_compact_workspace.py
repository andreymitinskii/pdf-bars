"""Capture and check the native Windows workspace without changing preferences."""
import ctypes
from pathlib import Path
import tempfile
import time

import fitz
from PIL import Image

from redos_pdf_combiner import gui
from redos_pdf_combiner.editor import TextEdit
from redos_pdf_combiner.preferences import DEFAULT_PREFERENCES


def capture_window(hwnd):
    from ctypes import wintypes
    user32, gdi32 = ctypes.windll.user32, ctypes.windll.gdi32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowDC.argtypes = [wintypes.HWND]
    user32.GetWindowDC.restype = wintypes.HDC
    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                              ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]
    rect = wintypes.RECT()
    assert user32.GetWindowRect(hwnd, ctypes.byref(rect))
    width, height = rect.right - rect.left, rect.bottom - rect.top
    screen = user32.GetWindowDC(hwnd)
    memory = gdi32.CreateCompatibleDC(screen)
    bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
    previous = gdi32.SelectObject(memory, bitmap)
    try:
        assert user32.PrintWindow(hwnd, memory, 2), 'Window-only capture failed'
        gdi32.SelectObject(memory, previous)
        info = ctypes.create_string_buffer(40)
        import struct
        info.raw = struct.pack('<IiiHHIIiiII', 40, width, -height, 1, 32, 0, width * height * 4, 0, 0, 0, 0)
        pixels = ctypes.create_string_buffer(width * height * 4)
        assert gdi32.GetDIBits(memory, bitmap, 0, height, pixels, info, 0) == height
        return Image.frombytes('RGB', (width, height), pixels.raw, 'raw', 'BGRX')
    finally:
        gdi32.SelectObject(memory, previous)
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(memory)
        user32.ReleaseDC(hwnd, screen)


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
            from ctypes import wintypes
            ctypes.windll.user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            ctypes.windll.user32.GetAncestor.restype = wintypes.HWND
            hwnd = ctypes.windll.user32.GetAncestor(app.winfo_id(), 2)
            with capture_window(hwnd) as image:
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
