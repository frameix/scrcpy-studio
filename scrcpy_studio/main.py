#!/usr/bin/env python3
"""Main application entry point for scrcpy-studio."""

import sys
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gio, GLib

from scrcpy_studio.ui.app_window import StudioAppWindow


class ScrcpyStudioApp(Adw.Application):
    def __init__(self):
        GLib.set_prgname("io.github.frame.ScrcpyStudio")
        GLib.set_application_name("Scrcpy Studio")
        super().__init__(
            application_id="io.github.frame.ScrcpyStudio",
            flags=Gio.ApplicationFlags.DEFAULT_FLAGS,
        )

    def do_activate(self):
        win = self.props.active_window
        if not win:
            win = StudioAppWindow(self)
        win.present()


def main():
    app = ScrcpyStudioApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
