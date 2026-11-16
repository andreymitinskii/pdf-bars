from pathlib import Path

from pypdf import PdfReader

from redos_pdf_combiner.blank_pages import PAGE_SIZES, create_blank_pdf
from redos_pdf_combiner.recovery import clear_recovery, has_recoverable_content, load_recovery, save_recovery


def test_recovery_round_trip_and_clear(tmp_path: Path):
    path = tmp_path / "autosave.json"
    payload = {
        "workspace": [{"source": "Документ.pdf", "page_index": 0}],
        "editor": {"source": "Скан.png", "text_edits": [{"text": "Текст"}]},
    }
    save_recovery(payload, path)
    restored = load_recovery(path)
    assert restored["workspace"][0]["source"] == "Документ.pdf"
    assert has_recoverable_content(restored)
    clear_recovery(path)
    assert not path.exists()


def test_unchanged_recovery_does_not_rewrite_file(tmp_path: Path):
    path = tmp_path / "autosave.json"
    payload = {"workspace": [{"source": "document.pdf", "page_index": 0}], "editor": None}
    save_recovery(payload, path)
    before = path.stat()
    content = path.read_bytes()

    save_recovery(payload, path)

    after = path.stat()
    assert path.read_bytes() == content
    assert after.st_mtime_ns == before.st_mtime_ns
    assert after.st_ino == before.st_ino


def test_blank_pdf_page_size_and_orientation(tmp_path: Path):
    portrait = create_blank_pdf(tmp_path, "A4", "portrait")
    landscape = create_blank_pdf(tmp_path, "A4", "landscape")
    portrait_page = PdfReader(str(portrait)).pages[0]
    landscape_page = PdfReader(str(landscape)).pages[0]
    width, height = PAGE_SIZES["A4"]
    assert abs(float(portrait_page.mediabox.width) - width) < 0.1
    assert abs(float(portrait_page.mediabox.height) - height) < 0.1
    assert abs(float(landscape_page.mediabox.width) - height) < 0.1
    assert abs(float(landscape_page.mediabox.height) - width) < 0.1
