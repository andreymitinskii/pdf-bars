"""Desktop-native Linux file pickers with the standard Tk dialogs as fallback."""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import tkinter as tk
from pathlib import Path
from tkinter import filedialog as tk_dialogs

from .i18n import tr


def _request(mode, options):
    variable = options.get("typevariable")
    return {
        "mode": mode,
        "title": options.get("title", ""),
        "initialdir": str(options.get("initialdir", "")),
        "initialfile": str(options.get("initialfile", "")),
        "defaultextension": options.get("defaultextension", ""),
        "filetypes": options.get("filetypes", []),
        "selected_type": variable.get() if variable is not None else "",
        "open_label": tr("Открыть"), "save_label": tr("Сохранить"),
        "cancel_label": tr("Отмена"),
        "replace_label": tr("Файл уже существует. Заменить его?"),
    }


def _native(mode, options):
    owner = options.get("parent") or tk._default_root
    if owner is None:
        raise RuntimeError("No Tk owner")
    request = _request(mode, options)
    request["parent_id"] = owner.winfo_toplevel().winfo_id()
    guard = tk.Toplevel(owner)
    guard.withdraw()
    guard.overrideredirect(True)
    guard.attributes("-alpha", 0.0)
    guard.geometry("1x1+0+0")
    previous_grab = owner.grab_current()
    process = None
    # File paths and filter selections are passed as JSON, not shell arguments.
    with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
        try:
            guard.deiconify()
            guard.wait_visibility()
            guard.grab_set()
            process = subprocess.Popen(
                [sys.executable, str(Path(__file__).with_name("gtk_file_dialog.py"))],
                stdin=subprocess.PIPE, stdout=output, stderr=errors,
            )
            process.stdin.write(json.dumps(request, ensure_ascii=True).encode("utf-8"))
            process.stdin.close()

            def poll():
                if process.poll() is None:
                    guard.after(50, poll)
                else:
                    guard.destroy()

            guard.after(50, poll)
            owner.wait_window(guard)
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)
                return {"paths": []}
            if process.returncode:
                raise RuntimeError("GTK file dialog unavailable")
            output.seek(0)
            result = json.load(output)
            if not isinstance(result, dict) or not isinstance(result.get("paths"), list):
                raise ValueError("Invalid file dialog result")
            if not all(isinstance(p, str) for p in result["paths"]):
                raise ValueError("Invalid file path")
            return result
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=3)
            if guard.winfo_exists():
                guard.destroy()
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()


def _choose(mode, **options):
    fallback = getattr(tk_dialogs, mode)
    if not sys.platform.startswith("linux"):
        return fallback(**options)
    try:
        result = _native(mode, options)
    except (OSError, ValueError, RuntimeError, tk.TclError, subprocess.SubprocessError):
        return fallback(**options)
    variable = options.get("typevariable")
    if result["paths"] and variable is not None and result.get("filter"):
        variable.set(result["filter"])
    if mode == "askopenfilenames":
        return tuple(result["paths"])
    return result["paths"][0] if result["paths"] else ""


def askopenfilename(**options):
    return _choose("askopenfilename", **options)


def askopenfilenames(**options):
    return _choose("askopenfilenames", **options)


def asksaveasfilename(**options):
    return _choose("asksaveasfilename", **options)


def askdirectory(**options):
    return _choose("askdirectory", **options)
