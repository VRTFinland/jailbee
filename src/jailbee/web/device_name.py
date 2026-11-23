"""A readable session name derived from the login request's User-Agent.

"Firefox on Linux", "Safari on iPhone", "Chrome on Android"; "Browser" when
the header says nothing usable. Order matters: Edge, Opera and Chrome-based
browsers all say "Chrome", and every iOS browser says "Safari".
"""

from __future__ import annotations

FALLBACK = "Browser"

_BROWSERS: tuple[tuple[str, str], ...] = (
    ("Edg/", "Edge"),
    ("EdgA/", "Edge"),
    ("EdgiOS/", "Edge"),
    ("OPR/", "Opera"),
    ("SamsungBrowser/", "Samsung Internet"),
    ("Firefox/", "Firefox"),
    ("FxiOS/", "Firefox"),
    ("CriOS/", "Chrome"),
    ("Chrome/", "Chrome"),
    ("Chromium/", "Chromium"),
    ("Safari/", "Safari"),
)

_SYSTEMS: tuple[tuple[str, str], ...] = (
    ("iPhone", "iPhone"),
    ("iPad", "iPad"),
    ("Android", "Android"),
    ("CrOS", "ChromeOS"),
    ("Windows", "Windows"),
    ("Mac OS X", "macOS"),
    ("Macintosh", "macOS"),
    ("Linux", "Linux"),
    ("X11", "Linux"),
)


def _first(agent: str, table: tuple[tuple[str, str], ...]) -> str | None:
    for marker, name in table:
        if marker in agent:
            return name
    return None


def derive_device_name(user_agent: str | None) -> str:
    """The session name for ``user_agent``; `FALLBACK` when no browser is recognised."""
    agent = (user_agent or "").replace("HeadlessChrome/", "Chrome/")
    browser = _first(agent, _BROWSERS)
    if browser is None:
        return FALLBACK
    system = _first(agent, _SYSTEMS)
    return f"{browser} on {system}" if system else browser
