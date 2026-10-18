"""The GTK 4 and libadwaita windows. Importing this package selects the GTK 4
and libadwaita 1 bindings."""

import gi

gi.require_version("Gdk", "4.0")
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
