"""The FastFlags editor: one row per flag, with its name, type and value."""

from __future__ import annotations

import json
from typing import Callable, Optional

from gi.repository import Adw, Gdk, GObject, Gtk

from .. import fastflags
from ..config import ConfigError
from . import dialogs

# The largest flags file that is read in, which is far beyond any real one.
_IMPORT_LIMIT = 4 * 1024 * 1024


class FlagRow:
    """One flag. A new row takes its type from the name's prefix until the
    type is chosen by hand."""

    def __init__(
        self,
        name: str,
        value: object,
        on_changed: Callable[[], None],
        on_remove: Callable[["FlagRow"], None],
        kinds: Gtk.SizeGroup,
        new: bool = False,
    ):
        self._on_changed = on_changed
        self._loading = False
        self._auto_kind = new
        # The row is the name's entry, with the type, value and Remove after it.
        self.name = Adw.EntryRow(title="Flag name", use_markup=False)
        self.kind = Gtk.DropDown.new_from_strings([fastflags.KIND_LABELS[k] for k in fastflags.KINDS])
        self.kind.set_valign(Gtk.Align.CENTER)
        # Every row's type column is as wide as the widest, so the columns line up.
        kinds.add_widget(self.kind)
        self.switch = Gtk.Switch(halign=Gtk.Align.START, valign=Gtk.Align.CENTER)
        self.entry = Gtk.Entry(placeholder_text="Value", width_chars=10, valign=Gtk.Align.CENTER)
        self._value = Gtk.Stack()
        self._value.add_named(self.switch, "bool")
        self._value.add_named(self.entry, "text")
        self.remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text="Remove", valign=Gtk.Align.CENTER)
        self.remove.add_css_class("flat")
        self.remove.connect("clicked", lambda _b: on_remove(self))
        for widget in (self.kind, self._value, self.remove):
            self.name.add_suffix(widget)
        self.widget = self.name

        self._shown = ""
        self.set_value(value)
        self.name.set_text(name)
        self.name.connect("changed", self._name_changed)
        self.entry.connect("changed", lambda _e: self._on_changed())
        self.switch.connect("notify::active", lambda *_a: self._on_changed())
        self.kind.connect("notify::selected", self._kind_changed)

    def kind_name(self) -> str:
        return fastflags.KINDS[self.kind.get_selected()]

    def _show(self, kind: str) -> None:
        self._value.set_visible_child_name("bool" if kind == "bool" else "text")
        self._shown = kind

    def set_value(self, value: object) -> None:
        """Show `value` under the kind it has."""
        kind = fastflags.kind_of(value)
        self._loading = True
        self.kind.set_selected(fastflags.KINDS.index(kind))
        if kind == "bool":
            self.switch.set_active(bool(value))
        else:
            self.entry.set_text(fastflags.text_of(value))
        self._show(kind)
        self._loading = False

    def _set_kind(self, kind: str) -> None:
        """Change the type as the name's prefix says, keeping what was typed."""
        self._loading = True
        self.kind.set_selected(fastflags.KINDS.index(kind))
        self._show(kind)
        self._loading = False

    def _name_changed(self, _entry: Gtk.Editable) -> None:
        if self._auto_kind:
            kind = fastflags.kind_for_name(self.name.get_text())
            if kind is not None and kind != self.kind_name():
                self._set_kind(kind)
        self._on_changed()

    def _kind_changed(self, *_args) -> None:
        if self._loading:
            return
        # Chosen by hand: the name no longer decides it.
        self._auto_kind = False
        kind = self.kind_name()
        if kind == "bool" and self._shown != "bool":
            self.switch.set_active(self.entry.get_text().strip().lower() in ("true", "1"))
        elif kind != "bool" and self._shown == "bool":
            self.entry.set_text("true" if self.switch.get_active() else "false")
        self._show(kind)
        self._on_changed()

    def data(self) -> tuple[str, str, str, bool]:
        return (self.name.get_text(), self.kind_name(), self.entry.get_text(), self.switch.get_active())


class FlagEditor(Gtk.Box):
    """The list of flags, with search, add, import and copy.

    `on_changed` is called after every edit.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.on_changed: Callable[[], None] = lambda: None
        self._rows: list[FlagRow] = []
        self._by_widget: dict = {}
        self._kinds = Gtk.SizeGroup(mode=Gtk.SizeGroupMode.HORIZONTAL)

        self._search = Gtk.SearchEntry(hexpand=True, placeholder_text="Search flags")
        self._search.connect("search-changed", lambda _e: self._refilter())
        add = Gtk.Button(label="Add Flag")
        add.add_css_class("suggested-action")
        add.connect("clicked", lambda _b: self.add_flag())
        import_button = Gtk.Button(icon_name="document-open-symbolic", tooltip_text="Import JSON…")
        import_button.connect("clicked", lambda _b: self.import_json())
        copy = Gtk.Button(icon_name="edit-copy-symbolic", tooltip_text="Copy as JSON")
        copy.connect("clicked", lambda _b: self.copy_json())
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        for widget in (self._search, add, import_button, copy):
            toolbar.append(widget)

        self._list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self._list.add_css_class("boxed-list")
        self._placeholder = Gtk.Label(wrap=True, justify=Gtk.Justification.CENTER)
        self._placeholder.add_css_class("dim-label")
        for side in ("top", "bottom", "start", "end"):
            getattr(self._placeholder, f"set_margin_{side}")(24)
        self._list.set_placeholder(self._placeholder)
        self._list.set_filter_func(self._matches)

        self._state = Gtk.Label(xalign=0, wrap=True)
        self._state.add_css_class("dim-label")
        self.append(toolbar)
        self.append(self._list)
        self.append(self._state)
        self._update_state()

    # The flags.

    def set_flags(self, flags: dict) -> None:
        for row in list(self._rows):
            self._kinds.remove_widget(row.kind)
            self._list.remove(row.widget)
        self._rows.clear()
        self._by_widget.clear()
        for name, value in flags.items():
            self._append(FlagRow(name, value, self._changed, self._remove, self._kinds))
        self._changed()

    def get_flags(self) -> dict:
        """The flags as saved. Raises ConfigError for a row that cannot be."""
        return fastflags.collect(row.data() for row in self._rows)

    def rows(self) -> list[FlagRow]:
        return list(self._rows)

    def _append(self, row: FlagRow) -> None:
        self._rows.append(row)
        self._by_widget[row.widget] = row
        self._list.append(row.widget)

    def add_flag(self) -> FlagRow:
        """Add an empty row at the end and put the cursor in its name."""
        if self._search.get_text():
            self._search.set_text("")
            self._refilter()
        row = FlagRow("", False, self._changed, self._remove, self._kinds, new=True)
        self._append(row)
        row.name.grab_focus()
        self._changed()
        return row

    def _remove(self, row: FlagRow) -> None:
        self._kinds.remove_widget(row.kind)
        self._list.remove(row.widget)
        self._rows.remove(row)
        del self._by_widget[row.widget]
        self._changed()

    def merge(self, flags: dict) -> None:
        """Set each flag in `flags`, replacing the value of one already listed."""
        by_name = {row.name.get_text().strip(): row for row in self._rows}
        for name, value in flags.items():
            if name in by_name:
                by_name[name].set_value(value)
            else:
                self._append(FlagRow(name, value, self._changed, self._remove, self._kinds))
        self._changed()

    # Display.

    def _matches(self, widget: Gtk.ListBoxRow) -> bool:
        wanted = self._search.get_text().strip().lower()
        row = self._by_widget.get(widget)
        return not wanted or row is None or wanted in row.name.get_text().lower()

    def _refilter(self) -> None:
        self._list.invalidate_filter()
        self._update_state()

    def _changed(self) -> None:
        self._update_state()
        self.on_changed()

    def _update_state(self) -> None:
        if not self._rows:
            self._placeholder.set_text("No FastFlags. Add a flag, or import a JSON object of them.")
        else:
            self._placeholder.set_text("No flag matches the search.")
        try:
            flags = self.get_flags()
        except ConfigError as e:
            self._say(str(e), error=True)
            return
        count = len(flags)
        self._say("No FastFlags." if count == 0 else f"{count} flag{'s' if count != 1 else ''}.")

    def _say(self, text: str, error: bool = False) -> None:
        self._state.set_text(text)
        if error:
            self._state.remove_css_class("dim-label")
            self._state.add_css_class("error")
        else:
            self._state.remove_css_class("error")
            self._state.add_css_class("dim-label")

    # Import and copy.

    def import_json(self, text: str = "", problem: Optional[str] = None) -> Adw.AlertDialog:
        """Ask for a JSON object of flags, pasted or loaded from a file, and
        add it to the list or replace the list with it."""
        view, frame = dialogs.text_box(text, 220, editable=True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        if problem:
            error = Gtk.Label(label=problem, xalign=0, wrap=True)
            error.add_css_class("error")
            box.append(error)
        box.append(frame)
        load = Gtk.Button(label="Load File…", halign=Gtk.Align.START)
        box.append(load)

        def answered(choice: Optional[str]) -> None:
            if choice not in ("add", "replace"):
                return
            buffer = view.get_buffer()
            pasted = buffer.get_text(buffer.get_start_iter(), buffer.get_end_iter(), False)
            try:
                flags = fastflags.parse_object(pasted)
            except ConfigError as e:
                self.import_json(pasted, str(e))
                return
            if choice == "replace":
                self.set_flags(flags)
            else:
                self.merge(flags)

        dialog = dialogs.choose(
            self,
            "Import FastFlags",
            "Paste a JSON object of flags, or load one from a file.",
            [("add", "Add to List"), ("replace", "Replace List"), ("cancel", "Cancel")],
            answered,
            extra=box,
            focus=view,
            destructive=("replace",),
        )
        load.connect("clicked", lambda _b: self._load_file(dialog, view))
        return dialog

    def _load_file(self, parent: Adw.Dialog, view: Gtk.TextView) -> None:
        def loaded(path: str) -> None:
            try:
                with open(path, "rb") as f:
                    raw = f.read(_IMPORT_LIMIT + 1)
                if len(raw) > _IMPORT_LIMIT:
                    raise ValueError("the file is larger than 4 MB")
                view.get_buffer().set_text(raw.decode("utf-8-sig"))
            except (OSError, ValueError) as e:
                dialogs.inform(parent, "Could not load the file", f"{path}: {e}")

        dialogs.pick_file(parent, "Load FastFlags", False, loaded)

    def copy_json(self) -> Optional[str]:
        """Put the flags on the clipboard as a JSON object, and return it."""
        try:
            flags = self.get_flags()
        except ConfigError as e:
            self._say(str(e), error=True)
            return None
        text = json.dumps(flags, indent=2)
        value = GObject.Value(GObject.TYPE_STRING, text)
        self.get_clipboard().set_content(Gdk.ContentProvider.new_for_value(value))
        count = len(flags)
        self._say(f"Copied {count} flag{'s' if count != 1 else ''} to the clipboard as JSON.")
        return text
