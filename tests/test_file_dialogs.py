import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from redos_pdf_combiner import file_dialogs as dialogs
from redos_pdf_combiner.gtk_file_dialog import patterns, save_path


def test_patterns_and_default_extensions():
    assert patterns("*.docx *.doc") == ["*.docx", "*.doc"]
    assert patterns(("*.png", "*.jpg")) == ["*.png", "*.jpg"]
    assert save_path("report", "*.docx", ".pdf") == Path("report.docx")
    assert save_path("report.docx", "*.docx", ".pdf") == Path("report.docx")
    assert save_path("report", "*", ".pdf") == Path("report.pdf")
    assert save_path("report.pdf", "*.docx", ".pdf", force=True) == Path("report.docx")


def test_native_multiple_paths_preserve_spaces_unicode_and_newlines(monkeypatch):
    monkeypatch.setattr(dialogs.sys, "platform", "linux")
    paths = ["/tmp/Акт проверки.pdf", "/tmp/line\nbreak.pdf"]
    monkeypatch.setattr(dialogs, "_native", lambda *args: {"paths": paths})
    assert dialogs.askopenfilenames() == tuple(paths)


def test_native_save_updates_file_type_variable(monkeypatch):
    monkeypatch.setattr(dialogs.sys, "platform", "linux")
    selected = []
    variable = SimpleNamespace(set=selected.append)
    monkeypatch.setattr(dialogs, "_native", lambda *args: {"paths": ["/tmp/doc.docx"], "filter": "DOCX"})
    assert dialogs.asksaveasfilename(typevariable=variable) == "/tmp/doc.docx"
    assert selected == ["DOCX"]


@pytest.mark.parametrize("mode,empty", [("askopenfilename", ""), ("asksaveasfilename", ""),
                                      ("askdirectory", ""), ("askopenfilenames", ())])
def test_native_cancel_does_not_open_second_dialog(monkeypatch, mode, empty):
    monkeypatch.setattr(dialogs.sys, "platform", "linux")
    monkeypatch.setattr(dialogs, "_native", lambda *args: {"paths": []})
    assert getattr(dialogs, mode)() == empty


def test_windows_uses_existing_dialog(monkeypatch):
    monkeypatch.setattr(dialogs.sys, "platform", "win32")
    monkeypatch.setattr(dialogs.tk_dialogs, "askdirectory", lambda **kw: kw["initialdir"])
    assert dialogs.askdirectory(initialdir="C:/Документы") == "C:/Документы"


def test_native_failure_falls_back_without_losing_options(monkeypatch):
    monkeypatch.setattr(dialogs.sys, "platform", "linux")
    def fail(*_args):
        raise RuntimeError("GTK missing")
    monkeypatch.setattr(dialogs, "_native", fail)
    calls = []
    monkeypatch.setattr(dialogs.tk_dialogs, "asksaveasfilename", lambda **kw: calls.append(kw) or "saved.pdf")
    options = {"initialdir": "/tmp", "initialfile": "saved.pdf", "defaultextension": ".pdf"}
    assert dialogs.asksaveasfilename(**options) == "saved.pdf"
    assert calls == [options]


def test_native_launcher_pumps_tk_events_and_releases_modal_guard(monkeypatch):
    import tkinter as tk
    root = tk.Tk()
    root.withdraw()
    original = subprocess.Popen
    events = []
    root.after(0, lambda: events.append("responsive"))
    def launch(args, **kwargs):
        assert args[1].endswith("gtk_file_dialog.py")
        return original([sys.executable, "-c",
            'import json,sys; json.load(sys.stdin); print(json.dumps({"paths": []}))'], **kwargs)
    monkeypatch.setattr(dialogs.subprocess, "Popen", launch)
    try:
        assert dialogs._native("askopenfilename", {"parent": root}) == {"paths": []}
        assert events == ["responsive"]
        assert root.grab_current() is None
        assert not root.winfo_children()
    finally:
        root.destroy()


@pytest.mark.skipif(sys.platform == "win32", reason="Linux GTK file chooser")
def test_real_gtk_open_save_folder_and_cancel_in_isolated_process(tmp_path):
    if not os.environ.get("DISPLAY"):
        pytest.skip("Requires Xvfb or desktop")
    pytest.importorskip("gi")
    source = tmp_path / "Пример с пробелами.PDF"
    source.write_bytes(b"sample")
    code = '''
import sys, gi
from pathlib import Path
gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
from gi.repository import Gtk, GLib
from redos_pdf_combiner.gtk_file_dialog import choose
folder = Path(sys.argv[1])
source = folder / "Пример с пробелами.PDF"
original = Gtk.FileChooserDialog.run
def auto_run(dialog):
    if current[0] == "cancel":
        GLib.timeout_add(100, lambda: dialog.response(Gtk.ResponseType.CANCEL) or False)
    else:
        if current[0] == "open":
            dialog.select_filename(str(source))
        elif current[0] == "save":
            for f in dialog.list_filters():
                if f.get_name() == "DOCX": dialog.set_filter(f)
            dialog.set_current_name("Новый файл")
        GLib.timeout_add(500, lambda: dialog.response(Gtk.ResponseType.ACCEPT) or False)
    return original(dialog)
Gtk.FileChooserDialog.run = auto_run
request = dict(initialdir=str(folder), filetypes=[("PDF", "*.pdf"), ("DOCX", "*.docx")],
    open_label="Open", save_label="Save", cancel_label="Cancel", replace_label="Replace?",
    defaultextension=".pdf", selected_type="PDF")
current = ["open"]
assert choose(dict(request, mode="askopenfilenames"))["paths"] == [str(source)]
current[0] = "save"
result = choose(dict(request, mode="asksaveasfilename"))
assert result["paths"] == [str(folder / "Новый файл.docx")], result
assert result["filter"] == "DOCX"
target = folder / "Новый файл.docx"
target.write_text("keep", encoding="utf-8")
confirmations = []
def refuse(message):
    confirmations.append(True)
    current[0] = "cancel"
    return Gtk.ResponseType.NO
Gtk.MessageDialog.run = refuse
def cancel_chooser():
    for window in Gtk.Window.list_toplevels():
        if isinstance(window, Gtk.FileChooserDialog):
            window.response(Gtk.ResponseType.CANCEL)
    return False
attempts = [0]
def refuse_native_confirmation():
    attempts[0] += 1
    for window in Gtk.Window.list_toplevels():
        if isinstance(window, Gtk.MessageDialog) and window.get_visible():
            refuse(window)
            window.response(Gtk.ResponseType.NO)
            GLib.timeout_add(100, cancel_chooser)
            return False
    if attempts[0] > 50:
        cancel_chooser()
        return False
    return True
watch = GLib.timeout_add(100, refuse_native_confirmation)
assert choose(dict(request, mode="asksaveasfilename")) == {"paths": []}
if GLib.MainContext.default().find_source_by_id(watch):
    GLib.source_remove(watch)
assert confirmations == [True]
assert target.read_text(encoding="utf-8") == "keep"
current[0] = "folder"
assert choose(dict(request, mode="askdirectory"))["paths"] == [str(folder)]
current[0] = "cancel"
assert choose(dict(request, mode="askopenfilename")) == {"paths": []}
'''
    subprocess.run([sys.executable, "-c", code, str(tmp_path)], check=True,
                   capture_output=True, timeout=30)
