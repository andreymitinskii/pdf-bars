from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import List

from .storage import app_cache_dir, cleanup_cache_files


@dataclass(frozen=True)
class PrinterInfo:
    name: str
    is_default: bool = False


@dataclass(frozen=True)
class PrintOptions:
    printer: str = ""
    copies: int = 1
    page_range: str = ""
    duplex: bool = False
    grayscale: bool = False


def available_printers() -> List[PrinterInfo]:
    if os.name == "nt" or not shutil.which("lpstat"):
        return []
    env = os.environ.copy()
    env["LC_ALL"] = "C"
    completed = subprocess.run(
        ("lpstat", "-p", "-d"),
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )
    default = ""
    names: List[str] = []
    for line in completed.stdout.splitlines():
        value = line.strip()
        if value.startswith("printer "):
            name = value[8:].split(None, 1)[0]
            if name and name not in names:
                names.append(name)
        elif value.startswith("system default destination:"):
            default = value.split(":", 1)[1].strip()
    return [PrinterInfo(name, name == default) for name in names]


def temporary_print_path(stem: str = "document") -> Path:
    cache_dir = print_cache_dir()
    cleanup_print_cache(cache_dir)
    handle, filename = tempfile.mkstemp(prefix=f"{_safe_stem(stem)}-", suffix=".pdf", dir=str(cache_dir))
    os.close(handle)
    return Path(filename)


def print_cache_dir() -> Path:
    return app_cache_dir("print")


def cleanup_print_cache(directory: Path | None = None) -> tuple[int, int]:
    return cleanup_cache_files(
        directory or print_cache_dir(),
        ("*.pdf",),
        max_age_seconds=24 * 3600,
        max_total_bytes=256 * 1024 * 1024,
        protect_recent_seconds=10 * 60,
    )


def remove_temporary_print_file(path: str | Path) -> None:
    candidate = Path(path)
    try:
        if candidate.resolve().parent != print_cache_dir().resolve():
            return
        candidate.unlink()
    except (FileNotFoundError, OSError):
        return


def submit_pdf_print_job(source: str | Path, options: PrintOptions) -> str:
    path = Path(source)
    if path.suffix.lower() != ".pdf" or not path.is_file():
        raise ValueError("Для печати требуется существующий PDF-файл")
    if not 1 <= int(options.copies) <= 999:
        raise ValueError("Количество копий должно быть от 1 до 999")
    page_range = options.page_range.replace(" ", "")
    if page_range and not re.fullmatch(r"\d+(?:-\d+)?(?:,\d+(?:-\d+)?)*", page_range):
        raise ValueError("Диапазон страниц задаётся, например, так: 1-3,5,8")

    if os.name == "nt":
        return _submit_windows_print_job(path)

    command = shutil.which("lp")
    if not command:
        raise RuntimeError("Не найдена команда печати lp. Установите системный пакет cups-client.")
    arguments = [command]
    if options.printer:
        arguments.extend(("-d", options.printer))
    if options.copies > 1:
        arguments.extend(("-n", str(options.copies)))
    if page_range:
        arguments.extend(("-P", page_range))
    if options.duplex:
        arguments.extend(("-o", "sides=two-sided-long-edge"))
    if options.grayscale:
        arguments.extend(("-o", "print-color-mode=monochrome", "-o", "ColorModel=Gray"))
    arguments.append(str(path))
    completed = subprocess.run(
        arguments,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        details = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(f"Система печати не приняла документ. {details}")
    return completed.stdout.strip() or "Задание принято системой печати"


def _submit_windows_print_job(path: Path) -> str:
    try:
        import ctypes

        shell_execute = ctypes.windll.shell32.ShellExecuteW
        shell_execute.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        )
        shell_execute.restype = ctypes.c_void_p
        handle = shell_execute(None, "print", str(path), None, str(path.parent), 0)
        result = int(handle or 0)
    except (AttributeError, OSError) as exc:
        raise RuntimeError("Не удалось открыть системную печать Windows") from exc
    if result <= 32:
        raise RuntimeError(f"Windows не смогла передать документ на печать (код {result})")
    return "Документ передан в системное окно печати"


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-zА-Яа-я_-]+", "-", value, flags=re.UNICODE).strip("-")
    return cleaned[:48] or "document"
