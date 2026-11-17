"""Reuse one application-owned desktop launcher without touching user files."""
from __future__ import annotations
import configparser
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile

COMMANDS = {"redos-pdf-gui", "pdf-bars-gui", "redos-pdf-combiner-gui"}


def launcher_command(path: Path) -> str:
    try:
        config = configparser.ConfigParser(interpolation=None, strict=False)
        config.read_string(path.read_text(encoding="utf-8-sig"))
        args = shlex.split(config.get("Desktop Entry", "Exec", fallback=""))
        if args and Path(args[0]).name == "env":
            args.pop(0)
            while args and ("=" in args[0] or args[0] == "--"):
                args.pop(0)
        command = Path(args[0]).name if args else ""
        return command if command in COMMANDS else ""
    except (OSError, UnicodeError, ValueError, configparser.Error):
        return ""


def sync_shortcut(entry: Path, desktop: Path, other_desktops=()) -> Path:
    desktop.mkdir(parents=True, exist_ok=True)
    owned = sorted((p for p in desktop.glob("*.desktop") if launcher_command(p)),
        key=lambda p: (p.name != "pdf bars.desktop", launcher_command(p).startswith("redos-"), p.name))
    target = owned[0] if owned else desktop / "pdf bars.desktop"
    index = 1
    while (target.exists() or target.is_symlink()) and not launcher_command(target):
        target = desktop / ("pdf bars (%d).desktop" % index)
        index += 1
    data = entry.read_bytes()
    if not target.exists() or target.read_bytes() != data:
        fd, temporary = tempfile.mkstemp(prefix=".pdf-bars-shortcut-", dir=str(desktop))
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
            os.chmod(temporary, 0o755)
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    if target.stat().st_mode & 0o777 != 0o755:
        target.chmod(0o755)
    for folder in {desktop, *(Path(p) for p in other_desktops)}:
        for path in folder.glob("*.desktop"):
            same_entry = path.parent.resolve() == target.parent.resolve() and path.name == target.name
            if not same_entry and launcher_command(path):
                path.unlink()
    return target


def desktop_directory(home: Path) -> Path:
    if shutil.which("xdg-user-dir"):
        try:
            result = subprocess.run(["xdg-user-dir", "DESKTOP"], check=True,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, timeout=3)
            value = result.stdout.strip()
            if value and Path(value).is_absolute() and Path(value) != home:
                return Path(value)
        except (OSError, subprocess.SubprocessError):
            pass
    for name in ("Рабочий стол", "Desktop"):
        if (home / name).is_dir():
            return home / name
    return home / ("Рабочий стол" if os.environ.get("LANG", "").startswith("ru") else "Desktop")


def main() -> int:
    entry = Path(os.environ.get("PDF_BARS_APP_ENTRY", "/usr/share/applications/pdf-bars.desktop"))
    if not entry.is_file():
        return 0
    home = Path.home()
    target = sync_shortcut(entry, desktop_directory(home), (home / "Desktop", home / "Рабочий стол"))
    if shutil.which("gio"):
        try:
            subprocess.run(["gio", "set", str(target), "metadata::trusted", "true"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False)
        except (OSError, subprocess.SubprocessError):
            pass
    if os.environ.get("PDF_BARS_CREATE_MARKER", "1") != "0":
        marker = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config"))) / "pdf-bars-public" / "desktop-shortcut"
        marker.parent.mkdir(parents=True, exist_ok=True)
        if not marker.exists():
            marker.touch()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
