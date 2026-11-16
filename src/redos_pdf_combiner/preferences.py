from __future__ import annotations

import json
import math
import os
import re
from pathlib import Path
from typing import Any, Dict

from .storage import atomic_write_text_if_changed


DEFAULT_PREFERENCES: Dict[str, Any] = {
    "last_folder": "",
    "pdf_image_format": "jpg",
    "editor_font_family": "Times New Roman",
    "compress_mode": "preset",
    "compress_preset": "medium",
    "compress_color_mode": "color",
    "ocr_language": "rus",
    "pdf_docx_pre_ocr": False,
    "document_ocr_language": "rus",
    "document_ocr_preserve_graphics": False,
    "recent_files": [],
    "whats_new_version": "",
    "word_style_presets": {},
}


def normalize_word_styles(value) -> Dict[str, Any]:
    result = {}
    if not isinstance(value, dict):
        return result
    for name, style in list(value.items())[:20]:
        if not isinstance(name, str) or not name.strip() or not isinstance(style, dict):
            continue
        try:
            size = float(style.get("font_size", 11))
        except (TypeError, ValueError):
            continue
        color = style.get("color", "#000000")
        family = style.get("font_family", "Times New Roman")
        if not math.isfinite(size) or not 6 <= size <= 72:
            continue
        if not isinstance(color, str) or not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
            continue
        if not isinstance(family, str) or not family.strip():
            continue
        result[name.strip()[:60]] = {
            "font_family": family.strip()[:120], "font_size": size, "color": color.lower(),
            **{key: style.get(key, key == "scan_effect") is True
               for key in ("bold", "italic", "underline", "scan_effect")},
        }
    return result


def preferences_path() -> Path:
    if os.name == "nt":
        root = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "PDFBars"
    else:
        root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "pdf-bars"
    return root / "settings.json"


def load_preferences(path: os.PathLike[str] | str | None = None) -> Dict[str, Any]:
    settings = dict(DEFAULT_PREFERENCES, word_style_presets={})
    source = Path(path) if path is not None else preferences_path()
    try:
        loaded = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return settings
    if isinstance(loaded, dict):
        for key, default in DEFAULT_PREFERENCES.items():
            value = loaded.get(key)
            if key == "word_style_presets":
                settings[key] = normalize_word_styles(value)
            elif isinstance(default, str) and isinstance(value, str):
                settings[key] = value
            elif isinstance(default, bool) and isinstance(value, bool):
                settings[key] = value
            elif isinstance(default, list) and isinstance(value, list):
                settings[key] = [item for item in value if isinstance(item, str)]
    return settings


def save_preferences(settings: Dict[str, Any], path: os.PathLike[str] | str | None = None) -> Path:
    destination = Path(path) if path is not None else preferences_path()
    destination.parent.mkdir(parents=True, exist_ok=True)
    payload = {key: settings.get(key, value) for key, value in DEFAULT_PREFERENCES.items()}
    payload["word_style_presets"] = normalize_word_styles(payload["word_style_presets"])
    atomic_write_text_if_changed(destination, json.dumps(payload, ensure_ascii=False, indent=2))
    return destination


def usable_last_folder(settings: Dict[str, Any], fallback: os.PathLike[str] | str | None = None) -> Path:
    configured = Path(str(settings.get("last_folder", ""))).expanduser()
    if configured.is_dir():
        return configured
    if fallback is not None:
        candidate = Path(fallback).expanduser()
        if candidate.is_dir():
            return candidate
    return Path.home()


def remember_folder(settings: Dict[str, Any], selected: os.PathLike[str] | str) -> None:
    path = Path(selected).expanduser()
    folder = path if path.is_dir() else path.parent
    if folder.is_dir():
        settings["last_folder"] = str(folder)


def remember_recent_files(
    settings: Dict[str, Any],
    selected,
    limit: int = 10,
) -> None:
    values = selected if isinstance(selected, (tuple, list)) else (selected,)
    recent = [str(Path(item).expanduser()) for item in settings.get("recent_files", []) if isinstance(item, str)]
    for item in values:
        path = Path(item).expanduser()
        if not path.is_file():
            continue
        normalized = str(path.resolve())
        recent = [existing for existing in recent if os.path.normcase(existing) != os.path.normcase(normalized)]
        recent.insert(0, normalized)
    settings["recent_files"] = recent[: max(1, int(limit))]


def existing_recent_files(settings: Dict[str, Any]) -> list[Path]:
    result = []
    for item in settings.get("recent_files", []):
        if not isinstance(item, str):
            continue
        path = Path(item).expanduser()
        if path.is_file():
            result.append(path)
    return result
