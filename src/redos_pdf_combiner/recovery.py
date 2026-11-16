from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from .preferences import preferences_path
from .storage import atomic_write_text_if_changed


RECOVERY_FORMAT_VERSION = 1


def recovery_path() -> Path:
    return preferences_path().with_name("autosave.json")


def save_recovery(payload: Dict[str, Any], path: os.PathLike[str] | str | None = None) -> Path:
    destination = Path(path) if path is not None else recovery_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    document = dict(payload)
    document["format_version"] = RECOVERY_FORMAT_VERSION
    try:
        existing = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        existing = None
    if isinstance(existing, dict):
        comparable = dict(existing)
        comparable.pop("saved_at", None)
        if comparable == document:
            return destination
    document["saved_at"] = datetime.now(timezone.utc).isoformat()
    atomic_write_text_if_changed(destination, json.dumps(document, ensure_ascii=False, indent=2))
    return destination


def load_recovery(path: os.PathLike[str] | str | None = None) -> Dict[str, Any] | None:
    source = Path(path) if path is not None else recovery_path()
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict) or payload.get("format_version") != RECOVERY_FORMAT_VERSION:
        return None
    return payload


def clear_recovery(path: os.PathLike[str] | str | None = None) -> None:
    source = Path(path) if path is not None else recovery_path()
    try:
        source.unlink()
    except FileNotFoundError:
        pass


def has_recoverable_content(payload: Dict[str, Any] | None) -> bool:
    if not payload:
        return False
    workspace = payload.get("workspace")
    editor = payload.get("editor")
    return bool(isinstance(workspace, list) and workspace) or bool(isinstance(editor, dict) and editor.get("source"))
