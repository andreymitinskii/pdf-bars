from __future__ import annotations

from .i18n import tr

import importlib
import os
import queue
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk


BLUE = "#0868D7"
DEEP_BLUE = "#075FCB"
CYAN = "#1688EE"


def _parse_xrandr_monitors(output: str) -> list[tuple[int, int, int, int, bool]]:
    monitors = []
    pattern = re.compile(
        r"^\s*\d+:\s+([+*]*)(?:\S+)\s+(\d+)(?:/\d+)?x(\d+)(?:/\d+)?\+(-?\d+)\+(-?\d+)"
    )
    for line in output.splitlines():
        match = pattern.match(line)
        if not match:
            continue
        flags, width, height, x, y = match.groups()
        monitors.append((int(x), int(y), int(width), int(height), "*" in flags))
    return monitors


def _select_monitor_geometry(
    monitors: list[tuple[int, int, int, int, bool]],
    point_x: int,
    point_y: int,
    *,
    prefer_primary: bool = False,
) -> tuple[int, int, int, int] | None:
    if not monitors:
        return None
    primary = next((monitor for monitor in monitors if monitor[4]), None)
    pointed = next(
        (
            monitor
            for monitor in monitors
            if monitor[0] <= point_x < monitor[0] + monitor[2]
            and monitor[1] <= point_y < monitor[1] + monitor[3]
        ),
        None,
    )
    selected = (primary or pointed or monitors[0]) if prefer_primary else (pointed or primary or monitors[0])
    return selected[0], selected[1], selected[2], selected[3]


def _xrandr_monitor_geometry(
    point_x: int,
    point_y: int,
    *,
    prefer_primary: bool = False,
) -> tuple[int, int, int, int] | None:
    if not sys.platform.startswith("linux"):
        return None

    for option in ("--listactivemonitors", "--listmonitors"):
        try:
            result = subprocess.run(
                ("xrandr", option),
                check=False,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        geometry = _select_monitor_geometry(
            _parse_xrandr_monitors(result.stdout),
            point_x,
            point_y,
            prefer_primary=prefer_primary,
        )
        if geometry is not None:
            return geometry
    return None


def _windows_monitor_geometry(
    window: tk.Tk,
    point_x: int,
    point_y: int,
    *,
    prefer_primary: bool = False,
) -> tuple[int, int, int, int] | None:
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Point(ctypes.Structure):
            _fields_ = (("x", wintypes.LONG), ("y", wintypes.LONG))

        class MonitorInfo(ctypes.Structure):
            _fields_ = (
                ("cbSize", wintypes.DWORD),
                ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT),
                ("dwFlags", wintypes.DWORD),
            )

        user32 = ctypes.windll.user32
        user32.MonitorFromWindow.argtypes = (wintypes.HWND, wintypes.DWORD)
        user32.MonitorFromWindow.restype = ctypes.c_void_p
        user32.MonitorFromPoint.argtypes = (Point, wintypes.DWORD)
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.POINTER(MonitorInfo))
        user32.GetMonitorInfoW.restype = wintypes.BOOL
        if prefer_primary:
            monitor = user32.MonitorFromWindow(None, 1)
        else:
            monitor = user32.MonitorFromPoint(Point(point_x, point_y), 2)
        if not monitor:
            return None
        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        work = info.rcWork
        return work.left, work.top, work.right - work.left, work.bottom - work.top
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def _monitor_geometry_for_point(
    window: tk.Tk,
    point_x: int,
    point_y: int,
    *,
    prefer_primary: bool = False,
) -> tuple[int, int, int, int]:
    geometry = _windows_monitor_geometry(
        window,
        point_x,
        point_y,
        prefer_primary=prefer_primary,
    ) or _xrandr_monitor_geometry(
        point_x,
        point_y,
        prefer_primary=prefer_primary,
    )
    if geometry is not None:
        return geometry
    return 0, 0, window.winfo_screenwidth(), window.winfo_screenheight()


def _active_monitor_geometry(window: tk.Tk) -> tuple[int, int, int, int]:
    try:
        pointer_x, pointer_y = window.winfo_pointerxy()
    except tk.TclError:
        pointer_x = pointer_y = 0

    return _monitor_geometry_for_point(window, pointer_x, pointer_y, prefer_primary=True)


def _center_geometry(window: tk.Tk, width: int, height: int) -> str:
    monitor_x, monitor_y, monitor_width, monitor_height = _active_monitor_geometry(window)
    x = monitor_x + max(0, (monitor_width - width) // 2)
    y = monitor_y + max(0, (monitor_height - height) // 2)
    return f"{width}x{height}{x:+d}{y:+d}"


def _load_gui(result_queue: queue.Queue) -> None:
    try:
        result_queue.put(("ready", importlib.import_module("redos_pdf_combiner.gui")))
    except BaseException as exc:
        result_queue.put(("error", exc))


def main(argv=None) -> int:
    startup_paths = list(sys.argv[1:] if argv is None else argv)
    splash = tk.Tk()
    splash.overrideredirect(True)
    splash.configure(bg=DEEP_BLUE)
    splash.geometry(_center_geometry(splash, 470, 190))
    splash.attributes("-topmost", True)

    icon_path = Path(__file__).with_name("assets") / "pdf-bars-transparent.png"
    if icon_path.exists():
        try:
            icon = tk.PhotoImage(file=str(icon_path)).subsample(12, 12)
            tk.Label(splash, image=icon, bg=DEEP_BLUE).place(x=28, y=28, width=82, height=82)
        except tk.TclError:
            icon = None

    tk.Label(splash, text="pdf bars", bg=DEEP_BLUE, fg="white", font=("Arial", 23, "bold")).place(x=130, y=31)
    status = tk.StringVar(value=tr("Загрузка компонентов..."))
    tk.Label(splash, textvariable=status, bg=DEEP_BLUE, fg="#D8EFFB", font=("Arial", 10)).place(x=132, y=76)
    style = ttk.Style(splash)
    style.configure("Splash.Horizontal.TProgressbar", troughcolor="#D8EAF4", background=CYAN)
    progress = ttk.Progressbar(splash, mode="indeterminate", style="Splash.Horizontal.TProgressbar", length=410)
    progress.place(x=30, y=132, height=12)
    progress.start(12)
    splash.update_idletasks()

    result_queue: queue.Queue = queue.Queue()
    threading.Thread(target=_load_gui, args=(result_queue,), daemon=True).start()
    status_after_id = splash.after(650, lambda: status.set(tr("Подготовка интерфейса...")))

    result = {}

    def poll() -> None:
        try:
            state, value = result_queue.get_nowait()
        except queue.Empty:
            splash.after(40, poll)
            return
        result["state"] = state
        result["value"] = value
        splash.quit()

    splash.after(40, poll)
    splash.mainloop()
    progress.stop()
    try:
        splash.after_cancel(status_after_id)
    except tk.TclError:
        pass

    if result.get("state") != "ready":
        splash.destroy()
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("pdf bars", tr(f"Не удалось запустить приложение:\n{result.get('value')}"))
        root.destroy()
        return 1
    status.set(tr("Подготовка рабочего окна..."))
    splash.update_idletasks()
    splash.destroy()
    return result["value"].main(startup_paths)


if __name__ == "__main__":
    raise SystemExit(main())
