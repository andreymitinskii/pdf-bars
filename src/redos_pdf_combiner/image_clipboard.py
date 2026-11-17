"""Detached GTK clipboard owner for Linux desktops without xclip."""
from __future__ import annotations

import os
import select
import signal
import sys


def main() -> int:
    read_fd, write_fd = os.pipe()
    child = os.fork()
    if child:
        os.close(write_fd)
        try:
            ready, _, _ = select.select([read_fd], [], [], 8)
            if ready and os.read(read_fd, 32) == b"READY":
                return 0
            try:
                os.kill(child, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(child, 0)
            return 1
        finally:
            os.close(read_fd)
    os.close(read_fd)
    os.setsid()
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        gi.require_version("Gdk", "3.0")
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import Gdk, GdkPixbuf, GLib, Gtk

        initialized, _ = Gtk.init_check([])
        if not initialized:
            raise RuntimeError("No desktop clipboard available")
        clipboard = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
        armed = [False]

        def owner_changed(*_args):
            if armed[0]:
                Gtk.main_quit()

        def arm():
            armed[0] = True
            return False

        clipboard.connect("owner-change", owner_changed)
        clipboard.set_image(GdkPixbuf.Pixbuf.new_from_file(sys.argv[1]))
        clipboard.set_can_store(None)
        Gdk.Display.get_default().sync()
        GLib.idle_add(arm)
        os.write(write_fd, b"READY")
        os.close(write_fd)
        Gtk.main()
    except Exception:
        os._exit(1)
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
