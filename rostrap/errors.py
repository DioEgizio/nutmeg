"""Exceptions whose message is written for the person using rostrap."""

from __future__ import annotations


class RostrapError(Exception):
    """Something went wrong that the user has to know about.

    `settings` says whether changing a setting is the likely fix, so that the
    window reporting it can offer to open the settings.
    """

    def __init__(self, message: str, *, settings: bool = False):
        super().__init__(message)
        self.settings = settings


class Cancelled(Exception):
    """The user cancelled."""
