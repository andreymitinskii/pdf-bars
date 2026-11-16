"""Local interface translations. Document content is never translated."""
from __future__ import annotations

import ctypes
import json
import locale
import os
import re
import sys
from functools import lru_cache
from pathlib import Path


def system_language() -> str:
    if sys.platform == 'win32':
        try:
            language_id = ctypes.windll.kernel32.GetUserDefaultUILanguage()
            return 'ru' if language_id & 0x3ff == 0x19 else 'en'
        except (AttributeError, OSError):
            pass
    for variable in ('LC_ALL', 'LC_MESSAGES', 'LANGUAGE', 'LANG'):
        if os.environ.get(variable):
            return os.environ[variable].split(':', 1)[0]
    try:
        return locale.getlocale()[0] or 'en'
    except (ValueError, TypeError):
        return 'en'


def resolve_language(preference='auto', system=None) -> str:
    if isinstance(preference, str) and preference in {'ru', 'en'}:
        return preference
    language = system_language() if system is None else system
    return 'ru' if str(language).lower().replace('-', '_').split('_')[0] == 'ru' else 'en'


def _initial_language() -> str:
    override = os.environ.get('PDF_BARS_UI_LANGUAGE')
    if override in {'ru', 'en'}:
        return override
    if os.name == 'nt':
        root = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'PDFBars'
    else:
        root = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'pdf-bars'
    try:
        settings = json.loads((root / 'settings.json').read_text(encoding='utf-8'))
        preference = settings.get('ui_language', 'auto') if isinstance(settings, dict) else 'auto'
    except (OSError, ValueError, TypeError):
        preference = 'auto'
    return resolve_language(preference)


_language = _initial_language()
_catalog = json.loads((Path(__file__).with_name('locales') / 'en.json').read_text(encoding='utf-8'))
_patterns = []
for source, target in _catalog.items():
    slots = list(re.finditer(r'\{(\d+)\}', source))
    if not slots:
        continue
    parts = []; start = 0
    for slot in slots:
        parts += [re.escape(source[start:slot.start()]), '(.*?)']
        start = slot.end()
    parts.append(re.escape(source[start:]))
    _patterns.append((source[:slots[0].start()], re.compile(''.join(parts), re.DOTALL), target))
_patterns.sort(key=lambda item: len(item[0]), reverse=True)


def get_language() -> str:
    return _language


def set_language(preference='auto', system=None) -> str:
    """Set the language before constructing UI; persisted switches apply on restart."""
    global _language
    _language = resolve_language(preference, system)
    _translated.cache_clear()
    return _language


@lru_cache(maxsize=2048)
def _translated(text: str) -> str:
    if text in _catalog:
        return _catalog[text]
    for prefix, pattern, target in _patterns:
        if not text.startswith(prefix):
            continue
        match = pattern.fullmatch(text)
        if match:
            return target.format(*match.groups())
    return text


def tr(text: str) -> str:
    return _translated(text) if _language == 'en' else text
