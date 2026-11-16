import json
from pathlib import Path
from zipfile import ZipFile

import fitz
import pytest

from redos_pdf_combiner import diagnostics, ocr


@pytest.fixture(autouse=True)
def reset_events():
    with diagnostics._LOCK:
        diagnostics._EVENTS.clear()
    yield
    with diagnostics._LOCK:
        diagnostics._EVENTS.clear()


def test_diagnostics_exports_timings_without_document_contents(tmp_path):
    source = tmp_path / "private-customer-secret.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((20, 40), "CONFIDENTIAL CONTENT that must never enter diagnostics")
        pdf.save(source)
    ocr.build_text_index(source)
    destination = tmp_path / "diagnostics.zip"
    diagnostics.export_diagnostics(destination)
    with ZipFile(destination) as archive:
        raw = archive.read("diagnostics.json").decode("utf-8")
        data = json.loads(raw)
    assert "CONFIDENTIAL" not in raw
    assert "private-customer" not in raw
    assert str(tmp_path) not in raw
    assert data["events"][0]["kind"] == "ocr_start"
    assert data["events"][-1]["status"] == "success"
    assert data["events"][-1]["elapsed_seconds"] >= 0
    assert data["models"]
    assert data["process"]["cpu_seconds"] >= 0
    with pytest.raises(FileExistsError):
        diagnostics.export_diagnostics(destination)


def test_diagnostics_records_failed_operations_without_exception_paths(tmp_path):
    with pytest.raises(Exception):
        ocr.build_text_index(tmp_path / "SECRET_MISSING.pdf")
    events = list(diagnostics._EVENTS)
    assert events[-1]["status"] == "error"
    assert events[-1]["error_type"]
    assert "SECRET_MISSING" not in json.dumps(events)


def test_diagnostics_ring_is_bounded_and_stages_accumulate():
    for number in range(1100):
        diagnostics.record({"number": number})
    assert len(diagnostics._EVENTS) == 1000
    assert diagnostics._EVENTS[0]["number"] == 100
    with diagnostics.measurement() as metrics:
        with diagnostics.stage("test"):
            pass
        with diagnostics.stage("test"):
            pass
    assert metrics["stage_calls"]["test"] == 2
    assert metrics["elapsed_seconds"] >= metrics["stages_seconds"]["test"]


def test_real_worker_diagnostics_arrive_in_parent(tmp_path):
    source = tmp_path / "scan.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=240, height=100)
        page.insert_text((20, 40), "Example document for OCR", fontsize=14)
        pdf.save(source)
    ocr.build_text_index(source, force_ocr=True, language="eng", dpi=150, enhance=False)
    page = next(event for event in diagnostics._EVENTS if event["kind"] == "ocr_page")
    assert page["words"] >= 3
    assert page["stages_seconds"]["ocr_engine"] > 0
    assert page["process_after"]["cpu_seconds"] >= page["process_before"]["cpu_seconds"]
    assert page["received_seconds"] >= page["elapsed_seconds"]


def test_conversion_diagnostics_identify_failed_stage_without_paths(tmp_path):
    @diagnostics.operation
    def convert(source, output):
        with diagnostics.stage("colored_ink_filter"):
            raise ValueError("private document path")

    with pytest.raises(ValueError):
        convert(tmp_path / "private.pdf", tmp_path / "private.docx")
    finish = list(diagnostics._EVENTS)[-1]
    assert finish["kind"] == "conversion_finish"
    assert finish["failed_stage"] == "colored_ink_filter"
    assert finish["error_type"] == "ValueError"
    assert "private" not in json.dumps(list(diagnostics._EVENTS))
