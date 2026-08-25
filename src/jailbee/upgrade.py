"""Upgrade advice: which releases need `jb base build` / `jb apply` re-run.

Some releases change what `jailbee base build` produces (`provision/install.sh`,
`provision/install.d/`, the provisioning env) or what `jailbee apply` writes
(profiles, ACL). Neither is re-run automatically, so a user who upgrades the
tool keeps a stale golden image or stale profiles with nothing to tell them.

A release *declares* its requirements in `UPGRADE_NOTES`. Per repo, jailbee
records the version at which each action last ran (`RepoUpgradeState`); the
difference between that watermark and the running version is the advice.

Only released versions take part: `parse_version` deliberately rejects
anything but `X.Y.Z`, so an editable install from a git tree — whose
`__version__` never moves — produces no advice at all.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Action = Literal["base_build", "apply"]

ACTIONS: tuple[Action, ...] = ("base_build", "apply")
"""Iteration order, and the order actions appear in a hint."""

ACTION_COMMANDS: dict[Action, str] = {
    "base_build": "jb base build",
    "apply": "jb apply",
}

_VERSION_RE = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def parse_version(raw: str) -> tuple[int, int, int] | None:
    """`"1.2.3"` -> `(1, 2, 3)`; anything else -> `None`.

    Strict on purpose. `0.0.0+unknown` (the `__init__.py` fallback when the
    package metadata is missing) and editable-install versions must not be
    compared against release-numbered notes — `None` is how the mechanism
    stays silent for them, which is the intended scope.
    """
    m = _VERSION_RE.match(raw.strip())
    if m is None:
        return None
    return (int(m[1]), int(m[2]), int(m[3]))


@dataclass(frozen=True)
class UpgradeNote:
    """One release's declaration that an action must be re-run.

    `reason` is shown to the user and is load-bearing, not decoration: "run
    base build" alone does not let anyone judge whether it matters to them,
    while "install.sh now installs ripgrep and fd" does. One line, no
    trailing period, phrased as what changed.
    """

    version: tuple[int, int, int]
    actions: frozenset[Action]
    reason: str


UPGRADE_NOTES: tuple[UpgradeNote, ...] = ()
"""What each release requires, ascending by version. Maintained by hand.

Add an entry whenever a change alters what `jailbee base build` produces or
what `jailbee apply` writes — see the rule in `CLAUDE.md`. Use the *upcoming*
release's version number; an entry above the running version is invisible
until that version ships, so adding it early is safe, but if the release
number changes before publication the entry is wrong.

    UPGRADE_NOTES = (
        UpgradeNote(
            version=(1, 2, 0),
            actions=frozenset({"base_build"}),
            reason="install.sh now installs ripgrep and fd",
        ),
    )
"""


@dataclass(frozen=True)
class Watermark:
    """The version at which an action last ran in one repo.

    `observed` separates a run jailbee actually saw from the assumption
    written when a repo is first seen (see `load_or_backfill`). It sets the
    lower bound of the comparison: **exclusive** when observed — that
    version's own notes are satisfied — and **inclusive** when assumed,
    because at first sight the upgrade has just happened and an image built
    by that version cannot exist yet.
    """

    version: tuple[int, int, int]
    observed: bool


@dataclass(frozen=True)
class PendingAction:
    """One action that is owed, with the reasons and the watermark it beat."""

    action: Action
    watermark: Watermark
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class Pending:
    """Everything owed in one repo. Falsy when nothing is."""

    actions: tuple[PendingAction, ...] = ()

    def __bool__(self) -> bool:
        return bool(self.actions)


def pending(
    current: str,
    watermarks: dict[Action, Watermark],
    *,
    notes: tuple[UpgradeNote, ...] | None = None,
) -> Pending:
    """Return the actions owed in a repo with these watermarks.

    `notes` defaults to `UPGRADE_NOTES` and is resolved here rather than in
    the signature, so a test can pass its own manifest without the real one's
    contents — which change every release — leaking into the assertions.
    """
    if notes is None:
        notes = UPGRADE_NOTES
    now = parse_version(current)
    if now is None:
        return Pending()

    owed: list[PendingAction] = []
    for action in ACTIONS:
        mark = watermarks.get(action)
        if mark is None:
            continue
        reasons = tuple(
            note.reason
            for note in notes
            if action in note.actions
            and note.version <= now
            and (note.version > mark.version if mark.observed else note.version >= mark.version)
        )
        if reasons:
            owed.append(PendingAction(action=action, watermark=mark, reasons=reasons))
    return Pending(tuple(owed))


MAX_REASONS = 3
"""Reasons shown per action before collapsing into "... and N more".

A user who skipped several releases should get a readable hint, not a wall.
"""


def _dotted(version: tuple[int, int, int]) -> str:
    return ".".join(str(part) for part in version)


def format_advice(owed: Pending, *, max_reasons: int = MAX_REASONS) -> list[str]:
    """Render `owed` as plain lines for `tui.hint`.

    Returns lines rather than printing, so the wording is testable without
    capturing output and the caller owns the output stream.
    """
    lines: list[str] = []
    for item in owed.actions:
        command = ACTION_COMMANDS[item.action]
        version = _dotted(item.watermark.version)
        if item.watermark.observed:
            lines.append(f"Since this repo last ran `{command}` (jailbee {version}):")
        else:
            # No run was ever observed, so claiming "since this repo last ran"
            # would be a fabrication. Name the release instead.
            verb = "produces" if item.action == "base_build" else "writes"
            lines.append(f"jailbee {version} changed what `{command}` {verb}:")
        shown = item.reasons[:max_reasons]
        lines.extend(f"    - {reason}" for reason in shown)
        hidden = len(item.reasons) - len(shown)
        if hidden:
            lines.append(f"    - ... and {hidden} more (see the CHANGELOG)")
        lines.append(f"    Run `{command}` in this repo to pick these up.")
    return lines
