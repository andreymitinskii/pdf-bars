from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from io import BytesIO
from pathlib import Path
from typing import Any, Dict

from .storage import app_cache_dir, cleanup_cache_files


CLIPBOARD_PREFIX = "PDFBARS_EDITOR_CLIPBOARD_V1:"


def copy_image_to_system_clipboard(path: Path, window_id: int = 0) -> None:
    """Publish a real image, never the editor's private JSON as plain text."""
    if sys.platform == "win32":
        _copy_windows_image(path, window_id)
        return
    from PIL import Image
    stream = BytesIO()
    with Image.open(path) as image:
        image.save(stream, "PNG")
    png = stream.getvalue()
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        subprocess.run(["wl-copy", "--type", "image/png"], input=png,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       check=True, timeout=5)
        return
    executable = shutil.which("xclip")
    if not executable:
        subprocess.run([sys.executable, str(Path(__file__).with_name("image_clipboard.py")), str(path)],
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, check=True, timeout=10)
        return
    subprocess.run([executable, "-selection", "clipboard", "-target", "image/png",
                    "-in"], input=png,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   check=True, timeout=5)


def _copy_windows_image(path: Path, window_id: int) -> None:
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.EmptyClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
    user32.RegisterClipboardFormatW.restype = wintypes.UINT
    user32.CloseClipboard.restype = wintypes.BOOL
    user32.GetDesktopWindow.restype = wintypes.HWND
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.restype = wintypes.HGLOBAL
    with Image.open(path) as source:
        # DIB is supported by Office; PNG preserves alpha for compatible clients.
        rgba = source.convert("RGBA")
        background = Image.new("RGBA", rgba.size, "white")
        background.alpha_composite(rgba)
        stream = BytesIO()
        background.convert("RGB").save(stream, "BMP")
        png_stream = BytesIO()
        rgba.save(png_stream, "PNG")
    formats = [(8, stream.getvalue()[14:]),
               (user32.RegisterClipboardFormatW("PNG"), png_stream.getvalue())]
    if not user32.OpenClipboard(window_id or user32.GetDesktopWindow()):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        if not user32.EmptyClipboard():
            raise ctypes.WinError(ctypes.get_last_error())
        for format_id, data in formats:
            if not format_id:
                raise ctypes.WinError(ctypes.get_last_error())
            handle = kernel32.GlobalAlloc(0x0002, len(data))
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            try:
                pointer = kernel32.GlobalLock(handle)
                if not pointer:
                    raise ctypes.WinError(ctypes.get_last_error())
                ctypes.memmove(pointer, data, len(data))
                kernel32.GlobalUnlock(handle)
                if not user32.SetClipboardData(format_id, handle):
                    raise ctypes.WinError(ctypes.get_last_error())
                handle = None  # The operating system now owns the allocation.
            finally:
                if handle:
                    kernel32.GlobalFree(handle)
    finally:
        user32.CloseClipboard()


def encode_clipboard_payload(payload: Dict[str, Any]) -> str:
    return CLIPBOARD_PREFIX + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def decode_clipboard_payload(value: str) -> Dict[str, Any] | None:
    if not isinstance(value, str) or not value.startswith(CLIPBOARD_PREFIX):
        return None
    try:
        payload = json.loads(value[len(CLIPBOARD_PREFIX):])
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("kind"), str):
        return None
    return payload


def clipboard_cache_dir() -> Path:
    return app_cache_dir("clipboard")


def new_clipboard_image_path() -> Path:
    cleanup_clipboard_cache()
    return clipboard_cache_dir() / f"region-{uuid.uuid4().hex}.png"


def cleanup_clipboard_cache(max_age_days: int = 7) -> None:
    cleanup_cache_files(
        clipboard_cache_dir(),
        ("region-*.png",),
        max_age_seconds=max(1, max_age_days) * 86400,
        max_total_bytes=128 * 1024 * 1024,
        protect_recent_seconds=3600,
    )
