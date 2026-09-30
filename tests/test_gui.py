"""The windows, driven through GTK. Skipped without a display; run under
xvfb-run on a machine without one. With ROSTRAP_SCREENSHOTS set to a folder,
each window is also saved there as a PNG."""

from __future__ import annotations

import os
import time
import unittest
from pathlib import Path
from unittest import mock

from rostrap import launcher as launcher_module
from rostrap import config as config_module
from rostrap import paths, roblox, rosetta
from rostrap.config import Config

from helpers import Sandbox, Server, roblox_zip

try:
    import gi

    gi.require_version("Gtk", "4.0")
    gi.require_version("Graphene", "1.0")
    from gi.repository import GLib, Graphene, Gtk

    HAVE_DISPLAY = Gtk.init_check()
except (ImportError, ValueError):
    HAVE_DISPLAY = False

if HAVE_DISPLAY:
    from rostrap.ui.app import Application
    from rostrap.ui.launch_window import LaunchWindow
    from rostrap.ui.settings_window import SettingsWindow


def pump(until, timeout: float = 20.0) -> bool:
    """Run the main loop until `until()` is true."""
    context = GLib.MainContext.default()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if until():
            return True
        time.sleep(0.02)
    return False


def toplevels() -> list:
    model = Gtk.Window.get_toplevels()
    return [model.get_item(i) for i in range(model.get_n_items())]


def dialog_titled(title: str):
    for window in toplevels():
        if window.get_title() == title and window.get_visible():
            return window
    return None


def buttons(widget) -> list:
    found = []
    child = widget.get_first_child()
    while child is not None:
        if isinstance(child, Gtk.Button) and not isinstance(child, (Gtk.ToggleButton,)):
            found.append(child)
        found.extend(buttons(child))
        child = child.get_next_sibling()
    return found


def click(window, label: str) -> None:
    for button in buttons(window):
        if button.get_label() == label:
            button.emit("clicked")
            return
    raise AssertionError(f"no {label!r} button in {window.get_title()!r}")


def screenshot(window, name: str) -> None:
    folder = os.environ.get("ROSTRAP_SCREENSHOTS")
    if not folder:
        return
    pump(lambda: window.get_width() > 0, 5)
    pump(lambda: False, 0.3)
    width, height = window.get_width(), window.get_height()
    paintable = Gtk.WidgetPaintable.new(window)
    snapshot = Gtk.Snapshot.new()
    paintable.snapshot(snapshot, width, height)
    node = snapshot.to_node()
    if node is None:
        return
    renderer = window.get_native().get_renderer()
    rect = Graphene.Rect().init(0, 0, width, height)
    renderer.render_texture(node, rect).save_to_png(str(Path(folder) / f"{name}.png"))


@unittest.skipUnless(HAVE_DISPLAY, "needs GTK 4 and a display")
class WindowTests(Sandbox):
    def setUp(self) -> None:
        super().setUp()
        self.app = Application("settings", None)
        self.app.register(None)
        self.addCleanup(self.close_all)

    def close_all(self) -> None:
        for window in toplevels():
            window.destroy()
        pump(lambda: not toplevels(), 5)

    def test_settings_pages_save_and_ask_before_discarding(self):
        window = SettingsWindow(self.app)
        window.present()
        pump(lambda: window.get_mapped())
        stack = window._stack
        for name in ("roblox", "rosetta3", "flags", "integration"):
            stack.set_visible_child_name(name)
            screenshot(window, f"settings-{name}")
        window._swap.set("0")
        window._flags_view.get_buffer().set_text('{"FIntTargetRefreshRate": 144}')
        window._env_view.get_buffer().set_text("# comment\nRUST_BACKTRACE=1\n")
        click(window, "Save")
        saved = config_module.load()
        self.assertEqual(saved.swap_interval, "0")
        self.assertEqual(saved.fast_flags, {"FIntTargetRefreshRate": 144})
        self.assertEqual(saved.extra_env, {"RUST_BACKTRACE": "1"})

        # A setting the extra variables may not hold is refused with a message.
        window._env_view.get_buffer().set_text("ROSETTA3_PREFIX=/x\n")
        click(window, "Save")
        self.assertIn("Prefix setting", window._message.get_text())
        self.assertEqual(config_module.load(), saved)

        window.close()
        self.assertTrue(pump(lambda: dialog_titled("Save changes?") is not None))
        screenshot(dialog_titled("Save changes?"), "settings-unsaved")
        click(dialog_titled("Save changes?"), "Discard")
        self.assertTrue(pump(lambda: not window.get_visible()))
        self.assertEqual(config_module.load(), saved)

    def test_an_unreadable_settings_file_is_replaced_on_save(self):
        paths.config_file().parent.mkdir(parents=True)
        paths.config_file().write_text("{broken")
        window = SettingsWindow(self.app)
        window.present()
        screenshot(window, "settings-unreadable")
        click(window, "Save")
        self.assertEqual(config_module.load(), Config())

    def test_bad_fast_flags_are_reported(self):
        window = SettingsWindow(self.app)
        window.present()
        window._stack.set_visible_child_name("flags")
        window._flags_view.get_buffer().set_text("[1, 2]")
        self.assertIn("must be a JSON object", window._flags_state.get_text())
        click(window, "Save")
        self.assertIn("must be a JSON object", window._message.get_text())
        screenshot(window, "settings-flags-error")
        self.assertFalse(paths.config_file().exists())

    def test_launch_window_runs_a_launch(self):
        server = Server()
        self.addCleanup(server.close)
        guid = "version-cccccccccccccccc"
        server.publish(guid, "0.702.0.1", {"x86_64": roblox_zip()}, ["a"])
        patches = [
            mock.patch.object(roblox, "CLIENT_VERSION_URLS", (f"{server.url}/client-version",)),
            mock.patch.object(roblox, "CDN_HOSTS", (f"{server.url}/a",)),
            mock.patch.object(rosetta, "host_arch", return_value="x86_64"),
            mock.patch.object(launcher_module, "STARTUP_GRACE_SECONDS", 0.5),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        tool = self.fake_rosetta3()
        config_module.save(Config(rosetta3_path=str(tool)))
        url = "roblox-player:1+launchmode:play"

        window = LaunchWindow(self.app, url=url, start=True)
        window.present()
        self.assertTrue(pump(lambda: dialog_titled("Set up the rosetta3 prefix") is not None))
        screenshot(dialog_titled("Set up the rosetta3 prefix"), "launch-prefix-question")
        click(dialog_titled("Set up the rosetta3 prefix"), "Download the base")
        self.assertTrue(pump(lambda: not window.get_visible(), 30))
        record = self.record()
        self.assertEqual(record["argv"][-2:], ["-protocolString", url])
        self.assertEqual(roblox.load_installed().guid, guid)
        self._stop_fake_roblox()

    def test_launch_failure_offers_the_log(self):
        config_module.save(Config(rosetta3_path=str(self.root / "missing")))
        window = LaunchWindow(self.app, url=None, start=True, on_settings=lambda: None)
        window.present()
        title = "Roblox could not be started"
        self.assertTrue(pump(lambda: any(w is not window and w.get_title() == title for w in toplevels())))
        dialog = next(w for w in toplevels() if w is not window and w.get_title() == title)
        screenshot(window, "launch-failed-window")
        screenshot(dialog, "launch-failed-dialog")
        # The suggested choice is rightmost.
        self.assertEqual([b.get_label() for b in buttons(dialog)], ["Open Log", "Close", "Open Settings"])
        click(dialog, "Close")
        self.assertTrue(pump(lambda: not window.get_visible()))

    def test_update_window_stays_open_when_done(self):
        server = Server()
        self.addCleanup(server.close)
        server.publish("version-dddddddddddddddd", "0.703.0.1", {"x86_64": roblox_zip()}, ["a"])
        for patch in (
            mock.patch.object(roblox, "CLIENT_VERSION_URLS", (f"{server.url}/client-version",)),
            mock.patch.object(roblox, "CDN_HOSTS", (f"{server.url}/a",)),
            mock.patch.object(rosetta, "host_arch", return_value="x86_64"),
        ):
            patch.start()
            self.addCleanup(patch.stop)
        config_module.save(Config(rosetta3_path=str(self.fake_rosetta3())))
        window = LaunchWindow(self.app, url=None, start=False)
        window.present()
        self.assertTrue(pump(lambda: dialog_titled("Set up the rosetta3 prefix") is not None))
        click(dialog_titled("Set up the rosetta3 prefix"), "Continue without it")
        self.assertTrue(pump(lambda: window._button.get_label() == "Close", 30))
        self.assertIn("is installed", window._status.get_text())
        screenshot(window, "update-done")
        click(window, "Close")
        self.assertTrue(pump(lambda: not window.get_visible()))

    def _stop_fake_roblox(self) -> None:
        from rostrap import processes

        processes.terminate(sorted(processes.running()))


if __name__ == "__main__":
    unittest.main()
