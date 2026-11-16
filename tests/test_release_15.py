import errno
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from redos_pdf_combiner import core, gui
from redos_pdf_combiner.user_errors import is_permission_denied


@pytest.mark.parametrize('error', [PermissionError(errno.EACCES, 'Permission denied'),
    OSError(errno.EPERM, 'Operation not permitted'), RuntimeError('cannot open: Permission denied'),
    RuntimeError('[Errno 13] Permission denied'),
    subprocess.CalledProcessError(1, ['gs'], stderr='Permission denied')])
def test_access_denials_are_recognized(error):
    assert is_permission_denied(error)


def test_nested_access_denial_and_exception_cycle():
    outer = RuntimeError('Save failed')
    inner = PermissionError(13, 'denied')
    outer.__cause__ = inner
    inner.__context__ = outer
    assert is_permission_denied(outer)
    assert not is_permission_denied(FileNotFoundError(2, 'missing'))
    assert not is_permission_denied(ValueError('OCR failed'))


def test_permission_dialog_does_not_expose_traceback(monkeypatch):
    messages = []
    app = SimpleNamespace(status=SimpleNamespace(set=lambda value: None))
    monkeypatch.setattr(gui.messagebox, 'showwarning', lambda title, text, **kw: messages.append((title, text)))
    gui.PdfCombinerApp._show_error(app, PermissionError(13, 'Permission denied', '//server/share/out.pdf'), 'TRACEBACK')
    assert len(messages) == 1
    assert 'сетев' in messages[0][1] and 'другую папку' in messages[0][1]
    assert all(word not in messages[0][1] for word in ('Errno', 'TRACEBACK', 'Permission denied'))


@pytest.mark.parametrize('candidate', [b'longer candidate data', b'original'])
def test_compression_keeps_original_if_not_smaller(tmp_path, monkeypatch, candidate):
    source, output = tmp_path/'source.pdf', tmp_path/'out.pdf'
    source.write_bytes(b'original')
    def compress(_source, path, *_args):
        path.write_bytes(candidate)
        return path
    monkeypatch.setattr(core, '_compress_pdf_preset_candidate', compress)
    core.compress_pdf_preset(source, output)
    assert output.read_bytes() == source.read_bytes() == b'original'
    assert not list(tmp_path.glob('.pdf-bars-compress-*'))


def test_compression_keeps_smaller_candidate_and_refuses_source(tmp_path, monkeypatch):
    source, output = tmp_path/'source.pdf', tmp_path/'out.pdf'
    source.write_bytes(b'original')
    monkeypatch.setattr(core, '_compress_pdf_preset_candidate', lambda _s, path, *_a: path.write_bytes(b'small'))
    core.compress_pdf_preset(source, output)
    assert output.read_bytes() == b'small'
    with pytest.raises(ValueError, match='новым именем'):
        core.compress_pdf_preset(source, source)
    assert source.read_bytes() == b'original'


def test_compression_failure_preserves_existing_output(tmp_path, monkeypatch):
    source, output = tmp_path/'source.pdf', tmp_path/'out.pdf'
    source.write_bytes(b'original')
    output.write_bytes(b'existing result')
    def fail(*_args):
        raise PermissionError(13, 'denied')
    monkeypatch.setattr(core, '_compress_pdf_preset_candidate', fail)
    with pytest.raises(PermissionError):
        core.compress_pdf_preset(source, output)
    assert output.read_bytes() == b'existing result'
    assert not list(tmp_path.glob('.pdf-bars-compress-*'))


def test_compression_summary_never_predicts_size(tmp_path):
    source = tmp_path/'source.pdf'
    source.write_bytes(b'original')
    messages = []
    app = SimpleNamespace(compress_files=[str(source)], compress_estimate=SimpleNamespace(set=messages.append),
                          _format_file_size=gui.PdfCombinerApp._format_file_size)
    gui.PdfCombinerApp._update_compression_estimate(app)
    assert messages == ['Исходный объём: 0.0 КБ']


def test_unchanged_compression_has_explicit_warning(tmp_path):
    source, output = tmp_path/'source.pdf', tmp_path/'out.pdf'
    source.write_bytes(b'original')
    output.write_bytes(b'original')
    reports = []
    app = SimpleNamespace(status=SimpleNamespace(set=lambda value: None),
                          _show_batch_report=lambda *args: reports.append(args))
    gui.PdfCombinerApp._show_compression_result(app, [source], [output])
    assert 'предупрежден' in reports[0][2]
    assert 'не уменьшил' in reports[0][3] and 'без ухудшения качества' in reports[0][3]


def test_compression_view_has_only_two_presets():
    text = Path(gui.__file__).read_text(encoding='utf-8')
    section = text.split('    def _compress_tab(', 1)[1].split('    def _update_compress_mode_controls', 1)[0]
    assert '("medium", "Оптимальное")' in section
    assert '("maximum", "Максимальное")' in section
    assert all(label not in section for label in ('До размера', 'Среднее', 'Минимальное', 'Цвет результата'))


def test_illustrated_guide_has_complete_pages_and_local_images():
    root = Path(__file__).resolve().parents[1]
    from pypdf import PdfReader
    guide = PdfReader(root/'docs/PDF_Bars_User_Guide.pdf')
    text = ''.join(page.extract_text() for page in guide.pages)
    assert len(guide.pages) >= 2
    assert all(title in text for title in ['Страницы документа','Правки и замена слова','Сохранение'])
    assert 'pdf bars 1.7' not in text


def test_update_notes_and_diagnostics_use_current_user_labels():
    source = Path(gui.__file__).read_text(encoding='utf-8')
    about = source.split('    def _about_tab(',1)[1].split('    def _enable_drop',1)[0]
    notes = source.split('    def _show_whats_new(',1)[1].split('def main(',1)[0]
    assert 'Собрать отладочную информацию' in about
    assert 'Собрать статистику' not in about
    assert 'Руководство пользователя' in notes
    assert 'Общие улучшения программы' in notes
    assert 'Раздел «PDF» объединён' not in notes
