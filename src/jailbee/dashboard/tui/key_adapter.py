"""Textual key events as the bytes the dashboard's input handlers still read.

Transitional (Textual migration V1-V3): the prompt, the command line and the
menu hotkeys read raw terminal bytes. Each native overlay in V3 removes a
user of this table, and V4 deletes the module.
"""

from __future__ import annotations

_NAMED: dict[str, bytes] = {
    "up": b"\x1b[A",
    "down": b"\x1b[B",
    "right": b"\x1b[C",
    "left": b"\x1b[D",
    "enter": b"\r",
    "escape": b"\x1b",
    "tab": b"\t",
    "backspace": b"\x7f",
    # The old cbreak loop delivered these control bytes with their usual meaning.
    "ctrl+h": b"\x7f",
    "ctrl+j": b"\r",
    "ctrl+m": b"\r",
    "ctrl+c": b"\x03",
    "f2": b"\x1bOQ",
    "space": b" ",
}


def legacy_bytes(key: str, character: str | None) -> bytes | None:
    """What a cbreak-mode terminal would have sent for this key, or None to ignore it."""
    named = _NAMED.get(key)
    if named is not None:
        return named
    if character is not None and len(character) == 1 and character.isprintable():
        return character.encode()
    return None
