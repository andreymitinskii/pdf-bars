from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any, Dict

from .storage import app_cache_dir, cleanup_cache_files


CLIPBOARD_PREFIX = "PDFBARS_EDITOR_CLIPBOARD_V1:"


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
