from pathlib import Path
import json

from redos_pdf_combiner.preferences import (
    DEFAULT_PREFERENCES,
    existing_recent_files,
    load_preferences,
    normalize_word_styles,
    remember_folder,
    remember_recent_files,
    save_preferences,
    usable_last_folder,
)


def test_preferences_round_trip_and_defaults(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    loaded = load_preferences(settings_path)
    assert loaded == DEFAULT_PREFERENCES
    assert loaded["ocr_language"] == "rus"
    assert loaded["document_ocr_language"] == "rus"

    loaded["pdf_image_format"] = "webp"
    loaded["editor_font_family"] = "DejaVu Serif"
    save_preferences(loaded, settings_path)

    restored = load_preferences(settings_path)
    assert restored["pdf_image_format"] == "webp"
    assert restored["editor_font_family"] == "DejaVu Serif"


def test_unchanged_preferences_do_not_rewrite_file(tmp_path: Path):
    settings_path = tmp_path / "settings.json"
    settings = dict(DEFAULT_PREFERENCES)
    save_preferences(settings, settings_path)
    before = settings_path.stat()

    save_preferences(settings, settings_path)

    after = settings_path.stat()
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_remember_folder_uses_parent_for_file(tmp_path: Path):
    file_path = tmp_path / "document.pdf"
    file_path.write_bytes(b"pdf")
    settings = dict(DEFAULT_PREFERENCES)
    remember_folder(settings, file_path)
    assert usable_last_folder(settings) == tmp_path


def test_recent_files_are_unique_and_newest_first(tmp_path: Path):
    first = tmp_path / "first.pdf"
    second = tmp_path / "second.docx"
    first.write_bytes(b"pdf")
    second.write_bytes(b"docx")
    settings = dict(DEFAULT_PREFERENCES)
    remember_recent_files(settings, [first, second, first])
    assert existing_recent_files(settings) == [first.resolve(), second.resolve()]


def test_word_styles_survive_restart_without_document_content(tmp_path):
    settings = load_preferences(tmp_path / "settings.json")
    settings["word_style_presets"] = {"Blue": {"font_family": "Times New Roman", "font_size": 11.4,
        "color": "#1234AB", "bold": True, "italic": False, "underline": True,
        "scan_effect": False, "text": "SECRET", "offset_x": 15, "font_file": "PRIVATE"}}
    save_preferences(settings, tmp_path / "settings.json")
    restored = load_preferences(tmp_path / "settings.json")["word_style_presets"]["Blue"]
    assert restored["color"] == "#1234ab" and restored["font_size"] == 11.4
    assert restored["bold"] and restored["underline"] and not restored["scan_effect"]
    assert "SECRET" not in (tmp_path / "settings.json").read_text()
    assert "PRIVATE" not in json.dumps(restored)


def test_word_styles_reject_invalid_values_and_do_not_share_defaults():
    assert normalize_word_styles({"A": {"font_size": float("nan")}, "B": {"color": "invalid"},
                                  "C": {"font_size": 0}, "D": []}) == {}
    assert len(normalize_word_styles({str(i): {} for i in range(30)})) == 20
