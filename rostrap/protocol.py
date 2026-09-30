"""The URLs a browser hands to Roblox.

The macOS client registers two schemes in its Info.plist: `roblox-player`,
which the website's Play button opens (`roblox-player:1+launchmode:play+...`),
and `roblox`, for deep links such as `roblox://experiences/start?placeId=1`.
"""

from __future__ import annotations

import re

SCHEMES = ("roblox-player", "roblox")


class InvalidURL(ValueError):
    pass


def scheme_of(url: str) -> str:
    scheme, sep, _ = url.partition(":")
    return scheme.lower() if sep else ""


def check(url: str) -> str:
    """Return `url` unchanged if it is one Roblox handles, else raise."""
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in url):
        raise InvalidURL("the link contains control characters")
    scheme = scheme_of(url)
    if scheme not in SCHEMES:
        raise InvalidURL(f"not a Roblox link: {url[:200]!r}")
    if not url[len(scheme) + 1 :]:
        raise InvalidURL(f"the {scheme}: link is empty")
    return url


_TICKET = re.compile(r"(gameinfo:)[^+]*", re.IGNORECASE)


def redact(text: str) -> str:
    """`text` with the authentication ticket of any `roblox-player:` link in it
    hidden, for writing to a log that may be shared."""
    return _TICKET.sub(r"\1<hidden>", text)
