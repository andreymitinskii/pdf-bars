from pathlib import Path
from types import SimpleNamespace

import fitz
import pytest
from PIL import Image

from redos_pdf_combiner import gui
from redos_pdf_combiner.converters import _image_to_rgb_page, images_to_pdf
from redos_pdf_combiner.document_input import open_pdf_input
from redos_pdf_combiner.editor import apply_pdf_edits, TextEdit
from redos_pdf_combiner.page_tools import combine_editor_sources, arrange_pdf_pages
from redos_pdf_combiner.user_errors import UserNotice, input_path, user_notice


@pytest.mark.parametrize('mode', ['RGBA', 'LA', 'P'])
def test_transparent_images_use_pillow_module_factory(tmp_path, mode):
    image = Image.new(mode, (60, 40), (0, 0, 0, 0) if mode == 'RGBA' else (0, 0) if mode == 'LA' else 0)
    if mode == 'P':
        image.info['transparency'] = 0
    with image, _image_to_rgb_page(image, 'white') as rgb:
        assert rgb.mode == 'RGB'
        assert rgb.getpixel((20, 20)) == (255, 255, 255)
        source = tmp_path/(mode+'.png')
        image.save(source)
    output = images_to_pdf([source], tmp_path/(mode+'.pdf'))
    with fitz.open(output) as document:
        assert document.is_pdf and document.page_count == 1


def test_mislabeled_image_can_be_combined_edited_and_arranged(tmp_path):
    source = tmp_path/'scan.pdf'
    Image.new('RGB', (200, 300), 'white').save(source, format='PNG')
    original = source.read_bytes()
    output = combine_editor_sources([source, source], tmp_path/'combined.pdf')
    arranged = arrange_pdf_pages(source, tmp_path/'arranged.pdf', [0, 0])
    edited = apply_pdf_edits(source, tmp_path/'edited.pdf', [TextEdit(0,(10,10,80,40),'NEW',12)], [])
    for path, count in [(output,2),(arranged,2),(edited,1)]:
        with fitz.open(path) as document:
            assert document.is_pdf and document.page_count == count
    with fitz.open(edited) as document:
        assert 'NEW' in document[0].get_text()
    assert source.read_bytes() == original


def test_home_paths_expand_before_conversion_and_reordering(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('USERPROFILE', str(tmp_path))
    Image.new('RGB', (40,40), 'white').save(tmp_path/'image.jpg')
    pdf = images_to_pdf(['~/image.jpg'], tmp_path/'source.pdf')
    assert input_path('~/source.pdf') == pdf
    arrange_pdf_pages('~/source.pdf', tmp_path/'out.pdf', [0])


def test_missing_input_is_an_actionable_notice_not_any_file_exception(tmp_path):
    with pytest.raises(UserNotice, match='добавьте файл заново'):
        input_path(tmp_path/'missing.jpg')
    assert user_notice(AttributeError("type object 'Image' has no attribute 'new'")) is None
    assert user_notice(RuntimeError('source or target not a PDF')) is None
    assert user_notice(FileNotFoundError('internal font missing')) is None
    path=tmp_path/'gone.pdf'
    assert user_notice(FileNotFoundError(str(path)), [path]).title == 'Файл недоступен'


@pytest.mark.parametrize('data', [b'', b'%PDF-1.4\nbroken'])
def test_invalid_user_file_has_recovery_instructions(tmp_path, data):
    source=tmp_path/'bad.pdf'
    source.write_bytes(data)
    with pytest.raises(UserNotice, match='сохраните новую копию'):
        with open_pdf_input(source):
            pass


def test_password_document_requires_an_authorized_copy(tmp_path):
    path=tmp_path/'protected.pdf'
    with fitz.open() as document:
        document.new_page()
        document.save(path, encryption=fitz.PDF_ENCRYPT_AES_256, owner_pw='owner', user_pw='reader')
    before=path.read_bytes()
    with pytest.raises(UserNotice, match='паролем'):
        with open_pdf_input(path):
            pass
    assert path.read_bytes() == before


def test_notice_dialog_explains_recovery_without_technical_trace(monkeypatch):
    messages=[]
    app=SimpleNamespace(status=SimpleNamespace(set=lambda _:None))
    monkeypatch.setattr(gui.messagebox, 'showwarning', lambda title, message, **kw:messages.append((title,message)))
    error=UserNotice('Файл недоступен','Проверьте папку и добавьте файл заново.')
    gui.PdfCombinerApp._show_error(app,error,'PRIVATE TRACEBACK')
    assert messages == [(error.title,str(error))]


@pytest.mark.parametrize('expected', [True, False])
def test_expected_notices_do_not_pollute_error_statistics(monkeypatch, expected):
    operations, reports, shown, completed = [], [], [], []
    app=SimpleNamespace(_global_progress_active=False, status=SimpleNamespace(set=lambda _:None),
        _start_global_progress=lambda _:None, _finish_global_progress=lambda:completed.append('progress'),
        after=lambda delay, callback:callback(), _background_task_finished=lambda:completed.append('finished'),
        _show_error=lambda error, details:shown.append(error))
    monkeypatch.setattr(gui.threading, 'Thread', lambda target, **kw:SimpleNamespace(start=target))
    error=UserNotice('Файл недоступен','Добавьте файл заново') if expected else AttributeError('program defect')
    def fail():
        raise error
    gui.PdfCombinerApp._run_task(app, fail, 'Done', on_complete=lambda:completed.append('callback'))
    assert shown == [error]
    assert len(operations) == len(reports) == 0
    assert set(completed) == {'progress','finished','callback'}
