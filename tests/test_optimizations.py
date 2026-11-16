import sqlite3
import threading
import time
from types import SimpleNamespace

import pytest

from redos_pdf_combiner import gui
from redos_pdf_combiner.render_budget import bounded_preview_scale, MAX_PREVIEW_PIXELS, MAX_PREVIEW_SIDE
from redos_pdf_combiner.session_files import EditorSessionFiles








@pytest.mark.parametrize('size', [(595, 842), (595, 20000), (20000, 595)])
def test_preview_allocations_are_bounded(size):
    scale = bounded_preview_scale(*size, 4)
    assert size[0] * size[1] * scale ** 2 <= MAX_PREVIEW_PIXELS + 1
    assert max(size) * scale <= MAX_PREVIEW_SIDE


def test_session_cleanup_preserves_live_history_recovery_and_other_instances(tmp_path):
    first, second = EditorSessionFiles(tmp_path), EditorSessionFiles(tmp_path)
    active, undo, recovery, orphan = [first.path(name + '.pdf') for name in ('active', 'undo', 'recovery', 'orphan')]
    external = second.path('other.pdf')
    for path in (active, undo, recovery, orphan, external):
        path.write_bytes(b'test')
    assert first.prune((active, [undo], {'source': str(recovery)})) == 1
    assert all(path.exists() for path in (active, undo, recovery, external))
    assert not orphan.exists()
    with pytest.raises(ValueError):
        first.path('../outside.pdf')
