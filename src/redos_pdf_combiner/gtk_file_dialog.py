"""GTK 3 file picker in a separate process, isolated from Tk and GTK 4."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def patterns(value):
    return list(value) if isinstance(value, (list, tuple)) else str(value).split()


def suffix_for_filter(value, default=""):
    for pattern in patterns(value):
        if re.fullmatch(r"\*\.[A-Za-z0-9]+", pattern):
            return pattern[1:]
    return default


def save_path(filename, selected_patterns, default="", force=False):
    path = Path(filename)
    suffix = suffix_for_filter(selected_patterns, default)
    if suffix and (force or not path.suffix):
        path = path.with_suffix(suffix if suffix.startswith(".") else "." + suffix)
    return path


def choose(request):
    import gi
    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gtk

    initialized, _ = Gtk.init_check([])
    if not initialized:
        raise RuntimeError("No desktop available")
    mode = request["mode"]
    save = mode == "asksaveasfilename"
    directory = mode == "askdirectory"
    action = Gtk.FileChooserAction.SAVE if save else (
        Gtk.FileChooserAction.SELECT_FOLDER if directory else Gtk.FileChooserAction.OPEN)
    dialog = Gtk.FileChooserDialog(title=request.get("title") or (
        request["save_label"] if save else request["open_label"]), action=action)
    dialog.add_buttons(request["cancel_label"], Gtk.ResponseType.CANCEL,
                       request["save_label"] if save else request["open_label"], Gtk.ResponseType.ACCEPT)
    dialog.set_default_response(Gtk.ResponseType.ACCEPT)
    dialog.set_default_size(900, 600)
    dialog.set_modal(True)
    dialog.set_local_only(True)
    dialog.set_select_multiple(mode == "askopenfilenames")
    if save:
        dialog.set_do_overwrite_confirmation(True)
    folder = Path(request.get("initialdir") or str(Path.home())).expanduser()
    if folder.is_dir():
        dialog.set_current_folder(str(folder))
    if request.get("initialfile"):
        if save:
            dialog.set_current_name(Path(request["initialfile"]).name)
        else:
            dialog.set_filename(str(folder / request["initialfile"]))
    filters = {}
    known_suffixes = set()
    for name, values in request.get("filetypes", []):
        file_filter = Gtk.FileFilter()
        file_filter.set_name(name)
        for pattern in patterns(values):
            file_filter.add_pattern(pattern)
            # Also show upper-case extensions frequently used by scanned PDFs.
            file_filter.add_pattern("".join("[" + c.lower() + c.upper() + "]" if c.isalpha() else c for c in pattern))
        filters[file_filter] = values
        known_suffixes.add(suffix_for_filter(values).lower())
        dialog.add_filter(file_filter)
        if name == request.get("selected_type"):
            dialog.set_filter(file_filter)

    def filter_changed(*_args):
        suffix = suffix_for_filter(filters.get(dialog.get_filter(), []))
        name = dialog.get_current_name()
        if suffix and name and Path(name).suffix.lower() in known_suffixes:
            dialog.set_current_name(str(Path(name).with_suffix(suffix)))

    if save:
        dialog.connect("notify::filter", filter_changed)
    # Mark the native window transient for its Tk owner on MATE/X11.
    if request.get("parent_id"):
        try:
            gi.require_version("GdkX11", "3.0")
            from gi.repository import Gdk, GdkX11
            dialog.realize()
            foreign = GdkX11.X11Window.foreign_new_for_display(
                Gdk.Display.get_default(), int(request["parent_id"]))
            if foreign is not None:
                dialog.get_window().set_transient_for(foreign)
        except (ValueError, TypeError, AttributeError, ImportError):
            pass
    try:
        while dialog.run() == Gtk.ResponseType.ACCEPT:
            paths = dialog.get_filenames()
            selected = dialog.get_filter()
            if not paths:
                continue
            if save:
                path = save_path(paths[0], filters.get(selected, []), request.get("defaultextension", ""),
                                 force=bool(request.get("selected_type")))
                if str(path) != paths[0] and path.exists():
                    confirmation = Gtk.MessageDialog(transient_for=dialog, modal=True,
                        message_type=Gtk.MessageType.QUESTION, buttons=Gtk.ButtonsType.YES_NO,
                        text=request["replace_label"])
                    confirmation.format_secondary_text(str(path))
                    try:
                        replace = confirmation.run() == Gtk.ResponseType.YES
                    finally:
                        confirmation.destroy()
                    if not replace:
                        continue
                paths = [str(path)]
            return {"paths": paths, "filter": selected.get_name() if selected else ""}
        return {"paths": []}
    finally:
        dialog.destroy()


if __name__ == "__main__":
    print(json.dumps(choose(json.load(sys.stdin)), ensure_ascii=True))
