"""The small window shown while Roblox is installed, updated and started."""

from __future__ import annotations

import threading
import traceback
from pathlib import Path
from typing import Callable, Optional

from gi.repository import GLib, Gtk

from .. import config as config_module
from ..errors import Cancelled, NutmegError
from ..launcher import Launcher
from . import dialogs


class GtkFrontend:
    """The launcher's view of the window. Called from the worker thread; it
    hands everything to the main loop, and coalesces the frequent progress
    updates so that the main loop sees at most one pending update."""

    def __init__(self, window: "LaunchWindow"):
        self._window = window
        self._lock = threading.Lock()
        self._pending: dict = {}
        self._scheduled = False

    def _post(self, **changes) -> None:
        with self._lock:
            self._pending.update(changes)
            if self._scheduled:
                return
            self._scheduled = True
        GLib.idle_add(self._flush)

    def _flush(self) -> bool:
        with self._lock:
            pending, self._pending = self._pending, {}
            self._scheduled = False
        if "status" in pending:
            self._window.show_status(*pending["status"])
        if "progress" in pending:
            self._window.show_progress(pending["progress"])
        return GLib.SOURCE_REMOVE

    def status(self, text: str, detail: str = "") -> None:
        self._post(status=(text, detail))

    def progress(self, fraction: Optional[float]) -> None:
        self._post(progress=fraction)

    def ask(self, title: str, message: str, choices: list[tuple[str, str]]) -> Optional[str]:
        answered = threading.Event()
        result: list[Optional[str]] = [None]

        def done(choice: Optional[str]) -> None:
            result[0] = choice
            answered.set()

        def show() -> bool:
            if self._window.cancel.is_set():
                done(None)
            else:
                dialogs.choose(self._window, title, message, choices, done)
            return GLib.SOURCE_REMOVE

        GLib.idle_add(show)
        while not answered.wait(0.1):
            if self._window.cancel.is_set():
                return None
        return result[0]


class LaunchWindow(Gtk.ApplicationWindow):
    """Runs a `Launcher` on a worker thread and shows how it is going.

    With `start`, Roblox is started and the window closes once it is running;
    without, Roblox is only installed or updated. `on_settings`, if given, is
    offered when an error is one a setting could fix. `relaunch` is passed to
    the `Launcher`.
    """

    def __init__(
        self,
        application: Gtk.Application,
        url: Optional[str],
        start: bool,
        parent: Optional[Gtk.Window] = None,
        on_settings: Optional[Callable[[], None]] = None,
        relaunch: Optional[list[str]] = None,
    ):
        super().__init__(application=application, resizable=False)
        self.set_title("Roblox" if start else "Update Roblox")
        self.set_default_size(440, -1)
        if parent is not None:
            self.set_transient_for(parent)
            self.set_modal(True)
        self.cancel = threading.Event()
        self._url = url
        self._start = start
        self._on_settings = on_settings
        self._relaunch = relaunch
        self._working = True

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for side in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{side}")(24)
        self._status = Gtk.Label(xalign=0, wrap=True)
        self._detail = Gtk.Label(xalign=0, wrap=True, selectable=False)
        self._detail.add_css_class("dim-label")
        self._bar = Gtk.ProgressBar()
        self._button = Gtk.Button(label="Cancel", halign=Gtk.Align.END)
        self._button.connect("clicked", lambda _b: self.close())
        for widget in (self._status, self._detail, self._bar, self._button):
            box.append(widget)
        self._button.set_margin_top(6)
        self.set_child(box)
        self.show_status("Starting…" if start else "Checking for Roblox updates…", "")
        self._pulse = GLib.timeout_add(120, self._tick)
        self._indeterminate = True
        self.connect("close-request", self._close_requested)

        self._frontend = GtkFrontend(self)
        threading.Thread(target=self._work, daemon=True).start()

    # Main-thread display.

    def show_status(self, text: str, detail: str) -> None:
        self._status.set_markup(f"<b>{GLib.markup_escape_text(text)}</b>")
        self._detail.set_text(detail)
        self._detail.set_visible(bool(detail))

    def show_progress(self, fraction: Optional[float]) -> None:
        self._indeterminate = fraction is None
        if fraction is not None:
            self._bar.set_fraction(max(0.0, min(1.0, fraction)))

    def _tick(self) -> bool:
        if self._indeterminate and self._working:
            self._bar.pulse()
        return GLib.SOURCE_CONTINUE

    def _close_requested(self, _window) -> bool:
        if not self._working:
            return False
        # The worker notices, stops what it is doing, and closes the window.
        self.cancel.set()
        self._button.set_sensitive(False)
        self.show_status("Cancelling…", "")
        return True

    def _finish(self) -> None:
        self._working = False
        if self._pulse:
            GLib.source_remove(self._pulse)
            self._pulse = 0

    # Worker thread.

    def _work(self) -> None:
        log: Optional[Path] = None
        tail = ""
        try:
            config = config_module.load()
            launcher = Launcher(config, self._frontend, self.cancel, self._relaunch)
            log = launcher.log.path
            try:
                launched = launcher.run(self._url, self._start)
                if launched is not None:
                    launcher.settle(launched)
            except (NutmegError, Cancelled):
                raise
            except Exception:
                launcher.log.write(traceback.format_exc().rstrip())
                raise
            finally:
                tail = launcher.log.tail()
                launcher.log.close()
        except Cancelled:
            GLib.idle_add(self._done_cancelled)
            return
        except config_module.ConfigError as e:
            GLib.idle_add(self._done_failed, f"The settings could not be read.\n\n{e}", True, None, "")
            return
        except NutmegError as e:
            GLib.idle_add(self._done_failed, str(e), e.settings, log, tail)
            return
        except OSError as e:
            where = f" ({e.filename})" if e.filename else ""
            GLib.idle_add(self._done_failed, f"{e.strerror or e}{where}", False, log, tail)
            return
        except Exception as e:
            message = f"Something unexpected went wrong: {type(e).__name__}: {e}"
            GLib.idle_add(self._done_failed, message, False, log, tail or traceback.format_exc())
            return
        GLib.idle_add(self._done_ok)

    # Back on the main thread.

    def _done_ok(self) -> bool:
        self._finish()
        if self._start:
            self.destroy()
        else:
            self._bar.set_fraction(1.0)
            self._button.set_label("Close")
            self._button.set_sensitive(True)
        return GLib.SOURCE_REMOVE

    def _done_cancelled(self) -> bool:
        self._finish()
        self.destroy()
        return GLib.SOURCE_REMOVE

    def _done_failed(self, message: str, settings: bool, log: Optional[Path], tail: str) -> bool:
        self._finish()
        self.show_status("Roblox could not be started" if self._start else "Roblox could not be updated", "")
        self._bar.set_fraction(0.0)
        choices = [("close", "Close")]
        if log is not None:
            choices.append(("log", "Open Log"))
        if settings and self._on_settings is not None:
            choices.insert(0, ("settings", "Open Settings"))

        def answered(choice: Optional[str]) -> None:
            if choice == "log" and log is not None:
                problem = dialogs.open_path(log)
                if problem is not None:
                    dialogs.choose(
                        self, "Could not open the log", problem, [("close", "Close")],
                        lambda _choice: self.destroy(),
                    )
                    return
            elif choice == "settings" and self._on_settings is not None:
                self._on_settings()
            self.destroy()

        dialogs.choose(
            self,
            "Roblox could not be started" if self._start else "Roblox could not be updated",
            message,
            choices,
            answered,
            detail=tail or None,
        )
        return GLib.SOURCE_REMOVE
