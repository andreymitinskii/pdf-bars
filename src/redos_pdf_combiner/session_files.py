"""Own only this editor instance's intermediates, never shared/user files."""
from dataclasses import fields, is_dataclass
from pathlib import Path
import tempfile


class EditorSessionFiles:
    def __init__(self, parent):
        self.root = Path(tempfile.mkdtemp(prefix="editor-session-", dir=parent)).resolve()

    def path(self, name):
        destination = self.root / name
        if destination.parent.resolve() != self.root:
            raise ValueError("Intermediate file must stay in its session directory")
        return destination

    def references(self, value):
        result = set()
        if isinstance(value, (str, Path)):
            try:
                path = Path(value)
                if path.is_absolute() and path.parent.resolve() == self.root:
                    result.add(path)
            except (OSError, ValueError):
                pass
        elif is_dataclass(value):
            for field in fields(value):
                result.update(self.references(getattr(value, field.name)))
        elif isinstance(value, dict):
            for item in value.values():
                result.update(self.references(item))
        elif isinstance(value, (list, tuple, set)):
            for item in value:
                result.update(self.references(item))
        return result

    def prune(self, live):
        protected = self.references(live)
        removed = 0
        for path in self.root.iterdir():
            if path in protected or not path.is_file() or path.is_symlink():
                continue
            # All candidates are direct children of a directory created by us.
            if path.parent.resolve() != self.root:
                continue
            try:
                path.unlink()
                removed += 1
            except OSError:
                pass
        return removed
