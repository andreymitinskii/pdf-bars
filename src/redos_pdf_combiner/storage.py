from __future__ import annotations

import hashlib
import os
import posixpath
import re
import shutil
import threading
import time
from pathlib import Path
from typing import Iterable


NETWORK_FILESYSTEMS = {
    "9p",
    "afs",
    "autofs",
    "ceph",
    "cifs",
    "davfs",
    "fuse.rclone",
    "fuse.sshfs",
    "glusterfs",
    "lustre",
    "nfs",
    "nfs4",
    "smb3",
}
MAX_WORKSPACE_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_WORKSPACE_CACHE_BYTES = 2 * 1024 * 1024 * 1024


def app_cache_dir(*parts: str) -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "PDFBars"
    else:
        root = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache")) / "pdf-bars"
    destination = root.joinpath(*parts)
    destination.mkdir(parents=True, exist_ok=True)
    return destination


def filesystem_type_for_path(path: Path, mountinfo_text: str | None = None) -> str:
    """Return the Linux mount type containing path, or an empty string."""
    if os.name == "nt" and mountinfo_text is None:
        return ""
    try:
        text = mountinfo_text if mountinfo_text is not None else Path("/proc/self/mountinfo").read_text(encoding="utf-8")
    except OSError:
        return ""
    target = posixpath.abspath(str(path).replace("\\", "/"))
    best_mount = ""
    best_type = ""
    for line in text.splitlines():
        try:
            left, right = line.split(" - ", 1)
            mount_point = _decode_mountinfo_path(left.split()[4])
            filesystem = right.split()[0].casefold()
            if posixpath.commonpath((target, mount_point)) != posixpath.abspath(mount_point):
                continue
        except (IndexError, ValueError):
            continue
        if len(mount_point) > len(best_mount):
            best_mount = mount_point
            best_type = filesystem
    return best_type


def is_network_path(path: Path, mountinfo_text: str | None = None) -> bool:
    filesystem = filesystem_type_for_path(path, mountinfo_text)
    return filesystem in NETWORK_FILESYSTEMS or filesystem.startswith("fuse.smb")


def snapshot_workspace_source(source: Path) -> Path | None:
    """Cache one accessible network source without repeatedly rewriting it."""
    source = Path(source)
    try:
        if not source.is_file() or not is_network_path(source):
            return None
        stat = source.stat()
    except OSError:
        return None
    if stat.st_size <= 0 or stat.st_size > MAX_WORKSPACE_SNAPSHOT_BYTES:
        return None

    directory = app_cache_dir("workspace-sources")
    cleanup_cache_files(
        directory,
        ("workspace-source-*",),
        max_age_seconds=14 * 86400,
        max_total_bytes=MAX_WORKSPACE_CACHE_BYTES,
        protect_recent_seconds=86400,
    )
    identity = f"{source.absolute()}|{stat.st_dev}|{stat.st_ino}|{stat.st_size}|{stat.st_mtime_ns}"
    digest = hashlib.sha256(identity.encode("utf-8", "surrogatepass")).hexdigest()[:20]
    safe_stem = re.sub(r"[^A-Za-z0-9А-Яа-я._-]+", "_", source.stem)[:48] or "document"
    destination = directory / f"workspace-source-{safe_stem}-{digest}{source.suffix.lower()}"
    try:
        if destination.is_file() and destination.stat().st_size == stat.st_size:
            return destination
    except OSError:
        pass

    temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        shutil.copyfile(source, temporary)
        if temporary.stat().st_size != stat.st_size:
            raise OSError("Размер резервной копии не совпадает с исходным файлом")
        temporary.replace(destination)
        return destination
    except OSError:
        try:
            temporary.unlink()
        except OSError:
            pass
        return None


def available_workspace_source(original: Path, cached: Path | None = None) -> Path | None:
    for candidate in (Path(original), Path(cached) if cached else None):
        if candidate is None:
            continue
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return None


def _decode_mountinfo_path(value: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda match: chr(int(match.group(1), 8)), value)


def atomic_write_text_if_changed(destination: Path, content: str) -> bool:
    data = content.encode("utf-8")
    try:
        if destination.read_bytes() == data:
            return False
    except FileNotFoundError:
        pass
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_bytes(data)
        temporary.replace(destination)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
    return True


def cleanup_cache_files(
    directory: Path,
    patterns: Iterable[str],
    *,
    max_age_seconds: int,
    max_total_bytes: int,
    protect_recent_seconds: int = 3600,
) -> tuple[int, int]:
    """Remove expired cache files and cap old cache data by total size."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError:
        return 0, 0
    now = time.time()
    files: dict[Path, os.stat_result] = {}
    for pattern in patterns:
        for path in directory.glob(pattern):
            try:
                if path.is_file():
                    files[path] = path.stat()
            except OSError:
                continue

    removed_files = 0
    removed_bytes = 0
    for path, stat in list(files.items()):
        if now - stat.st_mtime <= max(1, int(max_age_seconds)):
            continue
        try:
            path.unlink()
            removed_files += 1
            removed_bytes += stat.st_size
            files.pop(path, None)
        except OSError:
            continue

    total = sum(stat.st_size for stat in files.values())
    limit = max(0, int(max_total_bytes))
    if total <= limit:
        return removed_files, removed_bytes
    for path, stat in sorted(files.items(), key=lambda item: item[1].st_mtime):
        if now - stat.st_mtime < max(0, int(protect_recent_seconds)):
            continue
        try:
            path.unlink()
            total -= stat.st_size
            removed_files += 1
            removed_bytes += stat.st_size
        except OSError:
            continue
        if total <= limit:
            break
    return removed_files, removed_bytes
