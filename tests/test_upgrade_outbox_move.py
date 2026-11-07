"""The outbox-through-the-broker release owes the user no `base build` / `apply`.

The broker restarts itself on a version change (`UPGRADED_EXIT`), re-stages the
in-container app and skills, and `serve_plain` attaches the socket device to
running containers; `jb new` attaches it to new ones. The golden image and the
profiles are unchanged, so an UPGRADE_NOTES entry saying otherwise is wrong. What
the user must do *before* upgrading lives at the top of the CHANGELOG entry.
"""

from __future__ import annotations

import re
from pathlib import Path

from jailbee.upgrade import UPGRADE_NOTES

REPO_ROOT = Path(__file__).resolve().parent.parent
UPCOMING = (1, 8, 5)


def test_no_upcoming_note_blames_apply_for_the_broker_socket_or_outbox_move() -> None:
    wrong = [
        n
        for n in UPGRADE_NOTES
        if n.version >= UPCOMING
        and re.search(r"socket|outbox|Sent history", n.reason, re.IGNORECASE)
    ]
    assert wrong == []


def test_the_broker_unit_restarts_itself_on_upgrade() -> None:
    from importlib.resources import files

    from jailbee.orchestration import broker

    unit = (files("jailbee.templates.systemd") / "jailbee-broker.service").read_text()
    assert f"RestartForceExitStatus={broker.UPGRADED_EXIT}" in unit


def _outbox_entry() -> str:
    text = (REPO_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    unreleased = text.split("\n## Unreleased\n", 1)[1].split("\n## ", 1)[0]
    entries = re.split(r"\n(?=- )", unreleased)
    (entry,) = [e for e in entries if "The outbox moved to the host" in e]
    return entry


def test_changelog_outbox_entry_opens_with_the_before_upgrading_block() -> None:
    entry = _outbox_entry()
    block = entry.split("After upgrading", 1)[0]
    assert "**Before upgrading**" in block
    assert block.index("**Before upgrading**") < entry.index("Every container now stages")
    assert "jb outbox" in block
    assert "Sent history" in block and "state.sqlite" in block
    assert "roll back" in block and "broker.db" in block
    assert "~/.jailbee/{issue,pr}-outbox" in block
