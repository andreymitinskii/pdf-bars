import os
import time
from pathlib import Path

from redos_pdf_combiner import storage
from redos_pdf_combiner.storage import cleanup_cache_files, filesystem_type_for_path, snapshot_workspace_source


def test_cache_cleanup_removes_expired_files_and_preserves_recent_files(tmp_path: Path):
    expired = tmp_path / "old.pdf"
    recent = tmp_path / "recent.pdf"
    expired.write_bytes(b"old")
    recent.write_bytes(b"new")
    old_time = time.time() - 10 * 86400
    os.utime(expired, (old_time, old_time))

    removed_files, removed_bytes = cleanup_cache_files(
        tmp_path,
        ("*.pdf",),
        max_age_seconds=7 * 86400,
        max_total_bytes=1024,
    )

    assert removed_files == 1
    assert removed_bytes == 3
    assert not expired.exists()
    assert recent.exists()


def test_cache_size_limit_does_not_remove_fresh_files(tmp_path: Path):
    fresh = tmp_path / "fresh.pdf"
    fresh.write_bytes(b"x" * 2048)

    removed_files, _removed_bytes = cleanup_cache_files(
        tmp_path,
        ("*.pdf",),
        max_age_seconds=7 * 86400,
        max_total_bytes=1024,
        protect_recent_seconds=3600,
    )

    assert removed_files == 0
    assert fresh.exists()


def test_network_mount_type_uses_most_specific_mount_point():
    mountinfo = """36 25 8:2 / / rw,relatime - ext4 /dev/sda2 rw
42 36 0:50 / /exports rw,relatime - nfs4 server:/exports rw
"""

    assert filesystem_type_for_path(Path("/exports/archive/document.pdf"), mountinfo) == "nfs4"
    assert filesystem_type_for_path(Path("/home/user/document.pdf"), mountinfo) == "ext4"


def test_network_snapshot_is_written_once_and_reused(tmp_path: Path, monkeypatch):
    source = tmp_path / "network.pdf"
    source.write_bytes(b"network-pdf-content")
    cache = tmp_path / "cache"
    monkeypatch.setattr(storage, "is_network_path", lambda _path: True)
    monkeypatch.setattr(storage, "app_cache_dir", lambda *_parts: cache)

    first = snapshot_workspace_source(source)
    assert first is not None and first.read_bytes() == source.read_bytes()
    first_mtime = first.stat().st_mtime_ns
    second = snapshot_workspace_source(source)

    assert second == first
    assert second.stat().st_mtime_ns == first_mtime
