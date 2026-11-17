import os
from pathlib import Path
from redos_pdf_combiner import __version__
from redos_pdf_combiner.preferences import DEFAULT_PREFERENCES, preferences_path, save_preferences
from redos_pdf_combiner.storage import app_cache_dir
from redos_pdf_combiner.desktop_shortcut import launcher_command, sync_shortcut


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
