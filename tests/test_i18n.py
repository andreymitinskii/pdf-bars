import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from redos_pdf_combiner import i18n


@pytest.mark.parametrize('system,expected', [('ru_RU.UTF-8','ru'),('ru-KZ','ru'),('en_US','en'),('de_DE','en'),('uk_UA','en'),('C','en'),('', 'en')])
def test_system_language_default(system, expected):
    assert i18n.resolve_language('auto',system)==expected


def test_explicit_language_and_invalid_preferences():
    assert i18n.resolve_language('en','ru_RU')=='en'
    assert i18n.resolve_language('ru','fr_FR')=='ru'
    assert i18n.resolve_language([], 'ru_RU')=='ru'


def test_translations_preserve_dynamic_user_text():
    old=i18n.get_language()
    try:
        i18n.set_language('en')
        assert i18n.tr('Сохранить как…')=='Save as…'
        assert i18n.tr('Результат: C:/Документы/{тест}.pdf')=='Result: C:/Документы/{тест}.pdf'
        assert i18n.tr('Исходное слово: русский текст')=='Original word: русский текст'
        assert i18n.tr('Рабочая область пуста')=='Workspace is empty'
        i18n.set_language('ru')
        assert i18n.tr('Сохранить как…')=='Сохранить как…'
    finally:
        i18n.set_language(old)


def test_catalog_placeholder_integrity():
    import re
    for source,target in i18n._catalog.items():
        assert sorted(re.findall(r'\{\d+\}',source))==sorted(re.findall(r'\{\d+\}',target)),source


def test_nested_format_literals_are_localized_on_all_platforms():
    import ast,re
    root=Path(__file__).resolve().parents[1]/'src/redos_pdf_combiner'
    for file in root.glob('*.py'):
        tree=ast.parse(file.read_text(encoding='utf-8'))
        parents={child:node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
        for formatted in (n for n in ast.walk(tree) if isinstance(n,ast.FormattedValue)):
            for value in ast.walk(formatted.value):
                if isinstance(value,ast.Constant) and isinstance(value.value,str) and re.search('[А-Яа-яЁё]',value.value):
                    parent=parents[value]
                    assert isinstance(parent,ast.Call) and isinstance(parent.func,ast.Name) and parent.func.id=='tr',(file.name,value.lineno)


def test_fresh_english_ui_has_translated_controls_and_retains_document_text(tmp_path):
    code=r'''
import re
from pathlib import Path
from redos_pdf_combiner import gui
from redos_pdf_combiner.i18n import get_language
from redos_pdf_combiner.preferences import DEFAULT_PREFERENCES
gui.load_preferences=lambda:dict(DEFAULT_PREFERENCES)
gui.save_preferences=lambda _:None
gui.load_recovery=lambda:None
gui.PdfCombinerApp._queue_autosave=lambda _:None
app=gui.PdfCombinerApp(initially_hidden=True)
try:
 assert get_language()=='en'
 texts=[]
 def inspect(widget):
  try: texts.append(str(widget.cget('text')))
  except gui.tk.TclError: pass
  for child in widget.winfo_children(): inspect(child)
 inspect(app)
 assert 'About' in texts and 'Save as…' in texts and 'Print' in texts
 assert 'Interface language' in texts
 assert 'andreymitinskii@gmail.com' in texts
 assert not hasattr(gui.PdfCombinerApp,'_show_whats_new')
 foreign=[s for s in texts if re.search('[А-Яа-яЁё]',s) and not s.startswith('Developer: ')]
 assert not foreign, foreign
 assert gui.EDITOR_SAVE_TYPE_SUFFIXES['word docx with recognized text']=='.docx'
 from redos_pdf_combiner.ocr import IndexedWord,PageText
 words=(IndexedWord((0,0,20,10),'Документ',0,0,0),)
 assert words[0].text=='Документ'
finally:
 app.destroy()
'''
    environment=os.environ.copy()
    environment['PDF_BARS_UI_LANGUAGE']='en'
    result=subprocess.run([sys.executable,'-c',code],env=environment,text=True,capture_output=True,timeout=60)
    assert result.returncode==0,result.stdout+'\n'+result.stderr


def test_windows_installer_has_safe_machine_and_user_modes():
    source=(Path(__file__).resolve().parents[1]/'packaging/windows/pdf-bars.iss').read_text(encoding='utf-8')
    assert 'PrivilegesRequired=admin' in source
    assert 'UsePreviousAppDir=no' in source
    assert "ExpandConstant('{autopf}\\pdf bars')" in source
    assert "ExpandConstant('{localappdata}\\Programs\\pdf bars')" in source
    assert 'Root: HKA;' in source
    assert 'ProtectedUserInstall' in source
    assert 'icacls' not in source.lower()
