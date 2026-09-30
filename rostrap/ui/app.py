"""The GTK application."""

from __future__ import annotations

import sys
from typing import Optional

from gi.repository import Gio, GLib, Gtk

from .. import paths
from . import dialogs
from .launch_window import LaunchWindow
from .settings_window import SettingsWindow


class Application(Gtk.Application):
    def __init__(self, mode: str, url: Optional[str]):
        # Not unique: a launch from the browser must not be handed to a
        # settings window that happens to be open in another process.
        super().__init__(application_id=paths.APP_ID, flags=Gio.ApplicationFlags.NON_UNIQUE)
        self._mode = mode
        self._url = url

    def _open_settings(self) -> None:
        SettingsWindow(self).present()

    def do_activate(self) -> None:
        if self._mode == "error":
            window = dialogs.choose(
                None, "Roblox could not be started", self._url or "", [("close", "Close")], lambda _c: None
            )
            window.set_application(self)
        elif self._mode == "settings":
            self._open_settings()
        else:
            LaunchWindow(
                self,
                url=self._url,
                start=self._mode == "launch",
                on_settings=self._open_settings,
            ).present()


def run(mode: str, url: Optional[str]) -> int:
    """Show the window for `mode` (`settings`, `launch` or `update`) and run
    until it closes. Mode `error` only shows `url` as an error message."""
    if not Gtk.init_check():
        print("rostrap: cannot open a display; use --no-gui", file=sys.stderr)
        return 1
    GLib.set_application_name("rostrap")
    # GApplication gets no arguments of ours: rostrap parses its own.
    return Application(mode, url).run([sys.argv[0]])
