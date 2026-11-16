from pathlib import Path
import pytest
from redos_pdf_combiner import converters as c

def prepare(tmp_path, monkeypatch, platform='linux'):
    source=tmp_path/'document.docx'; source.write_bytes(b'docx')
    monkeypatch.setattr(c.sys,'platform',platform)
    monkeypatch.setattr(c,'find_microsoft_word',lambda:None)
    monkeypatch.setattr(c,'find_r7_converter',lambda:None)
    monkeypatch.setattr(c,'find_libreoffice_converter',lambda:None)
    return source

def output(items, folder, *args):
    path=Path(folder)/(items[0].stem+'.pdf'); path.write_bytes(b'%PDF-test'); return [path]

def test_windows_can_use_libreoffice_without_word(tmp_path, monkeypatch):
    source=prepare(tmp_path,monkeypatch,'win32')
    monkeypatch.setattr(c,'find_libreoffice_converter',lambda:'soffice.exe')
    monkeypatch.setattr(c,'_convert_with_libreoffice',output)
    assert c.docx_to_pdf([source],tmp_path/'out')[0].read_bytes()==b'%PDF-test'

def test_failure_falls_back_without_exposing_partial_files(tmp_path, monkeypatch):
    source=prepare(tmp_path,monkeypatch)
    monkeypatch.setattr(c,'find_r7_converter',lambda:'x2t')
    monkeypatch.setattr(c,'find_libreoffice_converter',lambda:'soffice')
    def fail(items, folder, *args):
        (Path(folder)/'document.pdf').write_bytes(b'partial')
        raise RuntimeError('fonts unavailable')
    monkeypatch.setattr(c,'_convert_with_r7',fail)
    monkeypatch.setattr(c,'_convert_with_libreoffice',output)
    assert c.docx_to_pdf([source],tmp_path/'out')==[tmp_path/'out/document.pdf']
    assert list((tmp_path/'out').iterdir())==[tmp_path/'out/document.pdf']

def test_explicit_soffice_is_preferred(tmp_path, monkeypatch):
    source=prepare(tmp_path,monkeypatch,'win32')
    monkeypatch.setattr(c,'find_microsoft_word',lambda:'WINWORD.EXE')
    monkeypatch.setattr(c,'_convert_with_microsoft_word',lambda *a:pytest.fail('unexpected Word'))
    monkeypatch.setattr(c,'_convert_with_libreoffice',output)
    assert c.docx_to_pdf([source],tmp_path/'out',soffice_path='custom-office')

def test_missing_office_has_actionable_notice(tmp_path, monkeypatch):
    source=prepare(tmp_path,monkeypatch)
    with pytest.raises(RuntimeError,match='LibreOffice'):
        c.docx_to_pdf([source],tmp_path/'out')

def test_soffice_environment_override(tmp_path, monkeypatch):
    binary=tmp_path/'soffice'; binary.touch()
    monkeypatch.setenv('PDF_BARS_OFFICE_CONVERTER',str(binary))
    assert c.find_libreoffice_converter()==str(binary)

def test_batch_progress_and_no_overwrite(tmp_path, monkeypatch):
    source=prepare(tmp_path,monkeypatch)
    second=tmp_path/'second.docx'; second.write_bytes(b'docx')
    folder=tmp_path/'out'; folder.mkdir(); (folder/'document.pdf').write_bytes(b'original')
    monkeypatch.setattr(c,'find_libreoffice_converter',lambda:'soffice')
    monkeypatch.setattr(c,'_convert_with_libreoffice',output)
    reports=[]
    outputs=c.docx_to_pdf([source,second],folder,progress=lambda *a:reports.append(a))
    assert len(outputs)==2 and (folder/'document.pdf').read_bytes()==b'original'
    assert reports[-1][:2]==(2,2)
