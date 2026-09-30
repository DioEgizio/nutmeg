"""A modal question window, built from a plain Gtk.Window so that it works the
same on every GTK 4 release (Gtk.MessageDialog is deprecated since 4.10, and
Gtk.AlertDialog only exists from 4.10)."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from gi.repository import Gdk, Gio, GLib, Gtk


def choose(
    parent: Optional[Gtk.Window],
    title: str,
    message: str,
    choices: list[tuple[str, str]],
    on_answer: Callable[[Optional[str]], None],
    detail: Optional[str] = None,
) -> Gtk.Window:
    """Show `message` with a button per (id, label) in `choices`, suggested one
    first. `on_answer` gets the id clicked, or None if the window was closed
    another way. `detail`, if given, is shown in a scrollable monospace box."""
    window = Gtk.Window(title=title, modal=True, resizable=detail is not None)
    window.set_default_size(520 if detail else 440, -1)
    finished = False
    parent_handler = 0

    # With PyGObject, gtk_window_destroy() does not reliably lead to the
    # "destroy" signal (it waits for the last reference, which Python holds),
    # so the end of the question is taken from the click, from close-request,
    # and from "unrealize", which a destroyed window always emits. For the same
    # reason the parent's going away is watched here instead of relying on
    # destroy-with-parent.
    def finish(choice: Optional[str]) -> None:
        nonlocal finished, parent_handler
        if finished:
            return
        finished = True
        if parent_handler:
            parent.disconnect(parent_handler)
            parent_handler = 0

        def call() -> bool:
            on_answer(choice)
            return GLib.SOURCE_REMOVE

        # From the main loop afterwards, so that `on_answer` may close windows.
        GLib.idle_add(call)

    if parent is not None:
        window.set_transient_for(parent)

        def parent_gone(_parent: Gtk.Window) -> None:
            finish(None)
            window.destroy()

        parent_handler = parent.connect("unrealize", parent_gone)

    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
    for side in ("top", "bottom", "start", "end"):
        getattr(box, f"set_margin_{side}")(20)
    heading = Gtk.Label(xalign=0, wrap=True)
    heading.set_markup(f"<b><big>{GLib.markup_escape_text(title)}</big></b>")
    box.append(heading)
    body = Gtk.Label(label=message, xalign=0, wrap=True, selectable=True)
    body.set_max_width_chars(64)
    box.append(body)
    if detail is not None:
        view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(detail)
        scroller = Gtk.ScrolledWindow(child=view, vexpand=True)
        scroller.set_min_content_height(200)
        box.append(scroller)
    buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8, halign=Gtk.Align.END)
    buttons.set_margin_top(8)
    first = None
    # The suggested choice goes rightmost, as GNOME lays buttons out.
    for index, (choice, label) in reversed(list(enumerate(choices))):
        button = Gtk.Button(label=label)
        if index == 0:
            button.add_css_class("suggested-action")
            first = button

        def clicked(_button: Gtk.Button, choice: str = choice) -> None:
            finish(choice)
            window.destroy()

        button.connect("clicked", clicked)
        buttons.append(button)
    box.append(buttons)
    window.set_child(box)

    keys = Gtk.EventControllerKey()

    def key_pressed(_controller, keyval: int, _keycode: int, _state) -> bool:
        if keyval == Gdk.KEY_Escape:
            window.close()
            return True
        return False

    keys.connect("key-pressed", key_pressed)
    window.add_controller(keys)
    window.connect("close-request", lambda _w: finish(None) or False)
    window.connect("unrealize", lambda _w: finish(None))
    if first is not None:
        window.set_default_widget(first)
        window.set_focus(first)
    window.present()
    return window


def inform(parent: Optional[Gtk.Window], title: str, message: str, detail: Optional[str] = None) -> None:
    choose(parent, title, message, [("close", "Close")], lambda _a: None, detail)


def open_path(path: Path) -> Optional[str]:
    """Open a file or folder with the desktop's default application. Returns
    why it could not be opened, or None."""
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), None)
    except GLib.Error as e:
        return f"{path}: {e.message}"
    return None
