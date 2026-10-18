"""Questions and messages, shown as libadwaita alert dialogs inside the window
they are about, and the helpers for opening files with other programs."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Iterable, Optional

from gi.repository import Adw, Gdk, Gio, GLib, Gtk

from .. import paths

# The response of a dialog closed without a choice, by Escape or by its
# window closing. `on_answer` gets None for it.
_CLOSED = "nutmeg-closed"


def text_box(text: str, height: int, editable: bool = False) -> tuple[Gtk.TextView, Gtk.Widget]:
    """A monospace text view in a scrolled frame `height` pixels high at
    least. Returns the view and the frame."""
    view = Gtk.TextView(editable=editable, cursor_visible=editable, monospace=True, accepts_tab=False)
    view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR if not editable else Gtk.WrapMode.CHAR)
    for side in ("top", "bottom", "left", "right"):
        getattr(view, f"set_{side}_margin")(8)
    view.get_buffer().set_text(text)
    scroller = Gtk.ScrolledWindow(child=view, vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
    scroller.set_min_content_height(height)
    return view, Gtk.Frame(child=scroller)


def choose(
    parent: Optional[Gtk.Widget],
    title: str,
    message: str,
    choices: list[tuple[str, str]],
    on_answer: Callable[[Optional[str]], None],
    detail: Optional[str] = None,
    extra: Optional[Gtk.Widget] = None,
    focus: Optional[Gtk.Widget] = None,
    destructive: Iterable[str] = (),
) -> Adw.AlertDialog:
    """Show `message` in a dialog over `parent`'s window, with a button per
    (id, label) in `choices`, suggested one first. `on_answer` gets the id
    clicked, or None if the dialog was closed another way. `detail`, if given,
    is shown in a scrollable monospace box; `extra`, if given, is placed above
    the buttons. `focus` is the widget that starts with the keyboard focus,
    the suggested button by default. The choices in `destructive` are marked
    as such, and are never the default. Without a `parent`, the dialog is a
    window of its own."""
    dialog = Adw.AlertDialog(heading=title, body=message, title=title)
    destructive = set(destructive)
    # The suggested choice goes rightmost, as GNOME lays buttons out.
    for index, (choice, label) in reversed(list(enumerate(choices))):
        dialog.add_response(choice, label)
        if choice in destructive:
            dialog.set_response_appearance(choice, Adw.ResponseAppearance.DESTRUCTIVE)
        elif index == 0:
            dialog.set_response_appearance(choice, Adw.ResponseAppearance.SUGGESTED)
    if choices and choices[0][0] not in destructive:
        dialog.set_default_response(choices[0][0])
    dialog.set_close_response(_CLOSED)

    if detail is not None or extra is not None:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        if detail is not None:
            box.append(text_box(detail, 200)[1])
        if extra is not None:
            box.append(extra)
        dialog.set_extra_child(box)
    # Room for long text, from libadwaita 1.6.
    if (detail is not None or extra is not None or len(message) > 200) and hasattr(dialog, "set_prefer_wide_layout"):
        dialog.set_prefer_wide_layout(True)

    chosen: Optional[str] = None
    finished = False

    def respond(_dialog: Adw.AlertDialog, choice: str) -> None:
        nonlocal chosen
        chosen = None if choice == _CLOSED else choice

    # The answer is given once the dialog is gone: a window does not close
    # while a dialog in it is still closing. A dialog whose window is
    # destroyed under it gives no response, and is only unrealized.
    def finish(_dialog: Adw.AlertDialog) -> None:
        nonlocal finished
        if finished:
            return
        finished = True

        def call() -> bool:
            on_answer(chosen)
            return GLib.SOURCE_REMOVE

        # From the main loop afterwards, so that `on_answer` may close windows.
        GLib.idle_add(call)

    dialog.connect("response", respond)
    dialog.connect("closed", finish)
    dialog.connect("unrealize", finish)
    dialog.present(parent)
    if focus is not None:
        dialog.set_focus(focus)
    return dialog


def inform(parent: Optional[Gtk.Widget], title: str, message: str, detail: Optional[str] = None) -> Adw.AlertDialog:
    return choose(parent, title, message, [("close", "Close")], lambda _a: None, detail)


def toast(widget: Gtk.Widget, text: str, timeout: int = 3) -> Optional[Adw.Toast]:
    """Show `text` briefly in the toast overlay `widget` is in, if any.
    `timeout` 0 keeps it until it is dismissed."""
    parent = widget
    while parent is not None and not isinstance(parent, Adw.ToastOverlay):
        parent = parent.get_parent()
    if parent is None:
        return None
    shown = Adw.Toast(title=text, timeout=timeout, use_markup=False)
    parent.add_toast(shown)
    return shown


def launch_context() -> Gio.AppLaunchContext:
    """A context that starts programs in the environment nutmeg was started
    in, without what the AppImage set for itself."""
    display = Gdk.Display.get_default()
    context = display.get_app_launch_context() if display is not None else Gio.AppLaunchContext()
    host = paths.host_env()
    for name in os.environ.keys() - host.keys():
        context.unsetenv(name)
    for name, value in host.items():
        if os.environ.get(name) != value:
            context.setenv(name, value)
    return context


def open_path(path: Path) -> Optional[str]:
    """Open a file or folder with the desktop's default application. Returns
    why it could not be opened, or None."""
    try:
        Gio.AppInfo.launch_default_for_uri(Gio.File.new_for_path(str(path)).get_uri(), launch_context())
    except GLib.Error as e:
        return f"{path}: {e.message}"
    return None


def pick_file(
    parent: Gtk.Widget, title: str, folder: bool, on_picked: Callable[[str], None]
) -> None:
    """Ask for a file, or with `folder` a folder, and pass its path to
    `on_picked` if one is chosen."""
    dialog = Gtk.FileDialog(title=title, modal=True)
    window = parent if isinstance(parent, Gtk.Window) else parent.get_root()

    def finished(source: Gtk.FileDialog, result: Gio.AsyncResult) -> None:
        try:
            picked = source.select_folder_finish(result) if folder else source.open_finish(result)
        except GLib.Error:
            return
        if picked is not None and picked.get_path():
            on_picked(picked.get_path())

    if folder:
        dialog.select_folder(window, None, finished)
    else:
        dialog.open(window, None, finished)
