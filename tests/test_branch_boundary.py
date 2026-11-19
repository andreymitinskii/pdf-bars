import os
from pathlib import Path
from redos_pdf_combiner import __version__
from redos_pdf_combiner.preferences import DEFAULT_PREFERENCES, preferences_path, save_preferences
from redos_pdf_combiner.storage import app_cache_dir
from redos_pdf_combiner.editor import list_signatures, delete_signature, signature_directory
from redos_pdf_combiner.desktop_shortcut import launcher_command, sync_shortcut
import pytest


def test_public_settings_do_not_replace_corporate_settings(tmp_path, monkeypatch):
    for key in ['APPDATA','LOCALAPPDATA','XDG_CONFIG_HOME','XDG_CACHE_HOME']:
        monkeypatch.setenv(key,str(tmp_path))
    old=tmp_path/('PDFBars' if os.name=='nt' else 'pdf-bars')/'settings.json'
    old.parent.mkdir()
    old.write_text('{"last_folder":"keep"}',encoding='utf-8')
    save_preferences(dict(DEFAULT_PREFERENCES))
    assert preferences_path().parent.name==('PDFBarsPublic' if os.name=='nt' else 'pdf-bars-public')
    assert old.read_text(encoding='utf-8')=='{"last_folder":"keep"}'
    assert app_cache_dir().name==('PDFBarsPublic' if os.name=='nt' else 'pdf-bars-public')
    assert __version__.startswith('1.7')


def test_public_shortcut_is_idempotent_and_handles_directory_alias(tmp_path):
    entry=tmp_path/'source.desktop'
    entry.write_text('[Desktop Entry]\nName=pdf bars\nExec=pdf-bars-gui %F\n',encoding='utf-8')
    desktop=tmp_path/'Desktop'
    target=sync_shortcut(entry,desktop)
    stamp=target.stat().st_mtime_ns
    assert sync_shortcut(entry,desktop,[desktop/'..'/'Desktop'])==target
    assert target.exists() and target.stat().st_mtime_ns==stamp
    assert launcher_command(target)=='pdf-bars-gui'


def test_public_signatures_never_use_corporate_directory(tmp_path, monkeypatch):
    for key in ('APPDATA', 'XDG_CONFIG_HOME'):
        monkeypatch.setenv(key, str(tmp_path))
    corporate_dir = tmp_path / ('PDFBars' if os.name == 'nt' else 'pdf-bars') / 'signatures'
    corporate_dir.mkdir(parents=True)
    corporate_file = corporate_dir / 'signature.png'
    corporate_file.write_bytes(b'corporate')
    assert signature_directory() == preferences_path().parent / 'signatures'
    assert list_signatures() == []
    with pytest.raises(ValueError):
        delete_signature(corporate_file)
    assert corporate_file.read_bytes() == b'corporate'


def test_public_shortcut_deduplicates_only_its_own_entries(tmp_path):
    public = '[Desktop Entry]\nName=pdf bars\nExec=pdf-bars-gui %F\n'
    entry = tmp_path / 'source.desktop'
    entry.write_text(public, encoding='utf-8')
    desktop = tmp_path / 'Desktop'
    desktop.mkdir()
    collision = desktop / 'pdf bars.desktop'
    legacy = desktop / 'Corporate copy.desktop'
    for path, command in ((collision, 'redos-pdf-gui'), (legacy, 'redos-pdf-combiner-gui')):
        path.write_text(public.replace('pdf-bars-gui', command), encoding='utf-8')
    originals = {path: path.read_bytes() for path in (collision, legacy)}
    target = sync_shortcut(entry, desktop)
    duplicate = desktop / 'Public copy.desktop'
    duplicate.write_text(public, encoding='utf-8')
    assert sync_shortcut(entry, desktop) == target
    assert target != collision and launcher_command(target) == 'pdf-bars-gui'
    assert not duplicate.exists()
    assert {path: path.read_bytes() for path in originals} == originals
