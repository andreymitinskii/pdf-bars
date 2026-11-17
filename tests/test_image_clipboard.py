from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

import redos_pdf_combiner.clipboard as clipboard
import redos_pdf_combiner.gui as gui


def test_x11_publishes_png_not_editor_json(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda _: "/usr/bin/xclip")
    calls = []
    monkeypatch.setattr(clipboard.subprocess, "run", lambda args, **kw: calls.append((args, kw)))
    path = tmp_path / "region.png"
    Image.new("RGB", (13, 7), "red").save(path)
    clipboard.copy_image_to_system_clipboard(path)
    assert calls[0][0] == ["/usr/bin/xclip", "-selection", "clipboard", "-target",
                            "image/png", "-in"]
    assert calls[0][1]["input"].startswith(b"\x89PNG\r\n\x1a\n")
    assert calls[0][1]["check"] is True
    assert calls[0][1]["timeout"] == 5


def test_wayland_publishes_png(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "wayland-0")
    monkeypatch.setattr(clipboard.shutil, "which", lambda _: "/usr/bin/wl-copy")
    path = tmp_path / "region.png"
    Image.new("RGB", (13, 7), "red").save(path)
    calls = []
    monkeypatch.setattr(clipboard.subprocess, "run", lambda args, **kw: calls.append((args, kw["input"])))
    clipboard.copy_image_to_system_clipboard(path)
    assert calls[0][0] == ["wl-copy", "--type", "image/png"]
    assert calls[0][1].startswith(b"\x89PNG\r\n\x1a\n")


def test_missing_linux_clipboard_tool_uses_gtk(monkeypatch, tmp_path):
    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr(clipboard.shutil, "which", lambda _: None)
    path = tmp_path / "region.png"
    Image.new("RGB", (13, 7), "red").save(path)
    calls = []
    monkeypatch.setattr(clipboard.subprocess, "run", lambda args, **kw: calls.append(args))
    clipboard.copy_image_to_system_clipboard(path)
    assert calls[0][0] == clipboard.sys.executable
    assert calls[0][1].endswith("image_clipboard.py")
    assert calls[0][2] == str(path)


def test_image_copy_failure_keeps_internal_paste_without_json(monkeypatch):
    def unavailable(*args):
        raise OSError("missing xclip")
    monkeypatch.setattr(gui, "copy_image_to_system_clipboard", unavailable)
    notices = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *args, **kwargs: notices.append(args))
    app = SimpleNamespace(winfo_id=lambda: 123)
    payload = {"version": 1, "kind": "image", "data": {"image_path": "region.png"}}
    gui.PdfCombinerApp._write_editor_clipboard(app, payload)
    assert gui.PdfCombinerApp._read_editor_clipboard(app) == payload
    assert len(notices) == 1


def test_editor_image_copy_keeps_private_data_out_of_text_clipboard(monkeypatch):
    calls = []
    monkeypatch.setattr(gui, "copy_image_to_system_clipboard", lambda *args: calls.append(args))
    app = SimpleNamespace(winfo_id=lambda: 123)
    payload = {"version": 1, "kind": "image", "data": {"image_path": "region.png"}}
    gui.PdfCombinerApp._write_editor_clipboard(app, payload)
    assert app.editor_clipboard == payload
    assert calls == [(Path("region.png"), 123)]


def test_linux_editor_requests_text_target_not_binary_png(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    calls = []
    payload = {"version": 1, "kind": "image", "data": {"image_path": "region.png"}}
    def get(**kwargs):
        calls.append(kwargs)
        raise gui.tk.TclError("No text target")
    app = SimpleNamespace(editor_clipboard=payload, clipboard_get=get,
        selection_get=lambda **kw: "TARGETS UTF8_STRING",
        tk=SimpleNamespace(splitlist=lambda text: text.split()))
    assert gui.PdfCombinerApp._read_editor_clipboard(app) == payload
    assert calls == [{"type": "UTF8_STRING"}]


def test_linux_editor_uses_image_payload_when_clipboard_advertises_png(monkeypatch):
    monkeypatch.setattr(gui.sys, "platform", "linux")
    payload = {"version": 1, "kind": "image", "data": {"image_path": "region.png"}}
    app = SimpleNamespace(editor_clipboard=payload,
        selection_get=lambda **kw: "TARGETS image/png",
        tk=SimpleNamespace(splitlist=lambda text: text.split()))
    assert gui.PdfCombinerApp._read_editor_clipboard(app) == payload


@pytest.mark.skipif(clipboard.sys.platform != "win32", reason="Windows native clipboard")
def test_windows_clipboard_contains_image_at_original_size(tmp_path):
    from PIL import ImageGrab
    path = tmp_path / "region.png"
    Image.new("RGB", (137, 83), (12, 34, 56)).save(path)
    clipboard.copy_image_to_system_clipboard(path)
    result = ImageGrab.grabclipboard()
    assert isinstance(result, Image.Image)
    assert result.size == (137, 83)
    assert result.convert("RGB").getpixel((20, 30)) == (12, 34, 56)


@pytest.mark.skipif(clipboard.sys.platform == "win32", reason="Linux GTK clipboard")
def test_linux_gtk_clipboard_contains_real_image(monkeypatch, tmp_path):
    import os
    import subprocess
    if not os.environ.get("DISPLAY"):
        pytest.skip("Requires a desktop or Xvfb")
    pytest.importorskip("gi")
    path = tmp_path / "region.png"
    Image.new("RGB", (137, 83), (12, 34, 56)).save(path)
    # Other tests may load GTK 4; the clipboard provider is a GTK 3 process.
    code = '''
import sys, gi
from pathlib import Path
from unittest.mock import patch
from redos_pdf_combiner.clipboard import copy_image_to_system_clipboard
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gdk, Gtk
with patch("redos_pdf_combiner.clipboard.shutil.which", return_value=None):
    copy_image_to_system_clipboard(Path(sys.argv[1]))
initialized, _ = Gtk.init_check([])
assert initialized
selection = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
result = selection.wait_for_image()
assert result is not None
assert (result.get_width(), result.get_height()) == (137, 83)
assert bytes(result.get_pixels()[:3]) == bytes((12, 34, 56))
selection.set_text("test complete", -1)
'''
    subprocess.run([clipboard.sys.executable, "-c", code, str(path)], check=True,
                   capture_output=True, timeout=20)
