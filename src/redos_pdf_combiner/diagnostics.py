"""Bounded, RAM-only OCR diagnostics. Document content is never collected."""
from __future__ import annotations

from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
import hashlib
import json
import os
import platform
import threading
import time
import uuid
import zipfile

_EVENTS = deque(maxlen=1000)
_LOCK = threading.Lock()
_LOCAL = threading.local()
_START = time.monotonic()


def process_snapshot():
    result = {"cpu_seconds": time.process_time()}
    try:
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes

            class Memory(ctypes.Structure):
                _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
                    (name, ctypes.c_size_t) for name in
                    ("peak", "rss", "qp", "q", "qnp", "qn", "page", "peakpage")]

            class IO(ctypes.Structure):
                _fields_ = [(name, ctypes.c_ulonglong) for name in
                            ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]

            handle = ctypes.windll.kernel32.GetCurrentProcess
            handle.restype = wintypes.HANDLE
            memory = Memory()
            memory.cb = ctypes.sizeof(memory)
            process = wintypes.HANDLE(handle())
            if ctypes.windll.psapi.GetProcessMemoryInfo(process, ctypes.byref(memory), memory.cb):
                result.update(rss_bytes=memory.rss, peak_rss_bytes=memory.peak, page_faults=memory.faults)
            io = IO()
            if ctypes.windll.kernel32.GetProcessIoCounters(process, ctypes.byref(io)):
                result.update(read_bytes=io.read_bytes, write_bytes=io.write_bytes)
        else:
            with open("/proc/self/status", encoding="ascii") as stream:
                for line in stream:
                    key, _, value = line.partition(":")
                    name = {"VmRSS": "rss_bytes", "VmHWM": "peak_rss_bytes", "VmSwap": "swap_bytes"}.get(key)
                    if name:
                        result[name] = int(value.split()[0]) * 1024
            with open("/proc/self/io", encoding="ascii") as stream:
                for line in stream:
                    key, _, value = line.partition(":")
                    if key in {"read_bytes", "write_bytes"}:
                        result[key] = int(value)
    except (OSError, ValueError, AttributeError, OverflowError):
        pass
    return result


def record(event):
    with _LOCK:
        _EVENTS.append(dict(event, utc=datetime.now(timezone.utc).isoformat()))


@contextmanager
def measurement():
    previous = getattr(_LOCAL, "metrics", None)
    metrics = {"stages_seconds": {}, "process_before": process_snapshot()}
    started = time.monotonic()
    _LOCAL.metrics = metrics
    try:
        yield metrics
    finally:
        metrics["elapsed_seconds"] = round(time.monotonic() - started, 4)
        metrics["process_after"] = process_snapshot()
        _LOCAL.metrics = previous


@contextmanager
def stage(name):
    started = time.monotonic()
    try:
        yield
    except Exception:
        metrics = getattr(_LOCAL, "metrics", None)
        if metrics is not None:
            metrics.setdefault("failed_stage", name)
        raise
    finally:
        metrics = getattr(_LOCAL, "metrics", None)
        if metrics is not None:
            stages = metrics["stages_seconds"]
            stages[name] = round(stages.get(name, 0) + time.monotonic() - started, 4)
            calls = metrics.setdefault("stage_calls", {})
            calls[name] = calls.get(name, 0) + 1


def note(**values):
    metrics = getattr(_LOCAL, "metrics", None)
    if metrics is not None:
        metrics.update(values)


def timed(name):
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            with stage(name):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def operation(function):
    @wraps(function)
    def wrapped(source, *args, **kwargs):
        run_id = uuid.uuid4().hex
        previous = getattr(_LOCAL, "run_id", None)
        _LOCAL.run_id = run_id
        settings = {key: kwargs[key] for key in
                    ("force_ocr", "dpi", "language", "enhance", "workers", "quality", "page_indexes", "preserve_graphics", "detect_tables") if key in kwargs}
        try:
            file_bytes = Path(source).stat().st_size
        except OSError:
            file_bytes = None
        kind = "ocr" if function.__name__ == "build_text_index" else "conversion"
        record(dict(kind=kind + "_start", run_id=run_id, parent_run_id=previous,
                    operation=function.__name__, settings=settings, source_bytes=file_bytes,
                    source_format=Path(source).suffix.lower()))
        metrics = {}
        try:
            with measurement() as metrics:
                try:
                    result = function(source, *args, **kwargs)
                except Exception as exc:
                    metrics.update(status="error", error_type=type(exc).__name__)
                    raise
                else:
                    metrics.update(status="success")
                    if isinstance(result, (list, tuple)):
                        metrics["result_pages"] = len(result)
                    return result
        finally:
            _LOCAL.run_id = previous
            record(dict(metrics, kind=kind + "_finish", run_id=run_id))
    return wrapped


def current_run():
    return getattr(_LOCAL, "run_id", None)


def export_diagnostics(destination):
    from importlib import metadata
    from . import __version__
    from .ocr import _available_memory_bytes, tessdata_path

    with _LOCK:
        events = list(_EVENTS)
    versions = {}
    for name in ("PyMuPDF", "Pillow", "pypdf", "python-docx"):
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = "unknown"
    models = []
    for path in sorted(tessdata_path().glob("*.traineddata")):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        models.append(dict(language=path.stem, bytes=path.stat().st_size, sha256=digest.hexdigest()))
    report = dict(schema=1, app_version=__version__, collected_utc=datetime.now(timezone.utc).isoformat(),
                  system=dict(os=platform.system(), release=platform.release(), machine=platform.machine(),
                              python=platform.python_version(), cpu_threads=os.cpu_count(),
                              cpu=platform.processor(), available_memory_bytes=_available_memory_bytes()),
                  uptime_seconds=round(time.monotonic()-_START, 2), process=process_snapshot(),
                  libraries=versions, models=models, events=events, buffer_limit=1000,
                  privacy="No document contents, names, paths, IP addresses or credentials are collected.")
    destination = Path(destination)
    # The chooser can offer an existing name; never silently overwrite it.
    with destination.open("xb") as stream:
        with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("diagnostics.json", json.dumps(report, ensure_ascii=False, indent=2))
            archive.writestr("README.txt", "pdf bars OCR diagnostics\n"
                "Only the current application session is included (up to 1000 events).\n"
                "Reproduce slow OCR, then export again before closing pdf bars.\n"
                "stages_seconds: accumulated wall time per stage; stage_calls: pass count.\n"
                "received_seconds includes process startup/queue time. Per-page elapsed excludes it.\n"
                "process_before/after: CPU time, RSS, peak RSS, I/O and swap where available.\n"
                "I/O counters and peak RSS are process totals; subtract before from after for I/O.\n"
                "Stage times may overlap and must not be summed. No document contents are included.\n")
    return destination
