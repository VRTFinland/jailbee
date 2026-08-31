"""Upgrade advice: which releases need `jb base build` / `jb apply` re-run.

Some releases change what `jailbee base build` produces (`provision/install.sh`,
`provision/install.d.available/`, the provisioning env) or what `jailbee apply` writes
(profiles, ACL). Neither is re-run automatically, so a user who upgrades the
tool keeps a stale golden image or stale profiles with nothing to tell them.

A release *declares* its requirements in `UPGRADE_NOTES`. Per repo, jailbee
records the version at which each action last ran (`RepoUpgradeState`); the
difference between that watermark and the running version is the advice.

Only release-shaped versions take part: `parse_version` rejects anything but
`X.Y.Z`, and an unparseable version produces no advice at all. In practice
that is `0.0.0+unknown`, the `__init__.py` fallback for an install with no
package metadata. An editable install is *not* excluded — `pyproject.toml`
carries a static version, so `uv tool install -e .` reports the last released
number and participates like any other install.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from datetime import datetime

    from sqlmodel import Session

    from jailbee.db.models import RepoUpgradeState

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

    Strict on purpose: only a release-shaped version can be compared against
    release-numbered notes. The one version this project actually produces
    that fails here is `0.0.0+unknown` — the `__init__.py` fallback when the
    package metadata cannot be read — and `None` is how the mechanism stays
    silent for it. This does *not* exclude editable installs: `pyproject.toml`
    pins a static `X.Y.Z`, so those parse and take part like any other.
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


UPGRADE_NOTES: tuple[UpgradeNote, ...] = (
    UpgradeNote(
        version=(1, 2, 0),
        actions=frozenset({"base_build"}),
        reason=(
            "install.sh masks Ubuntu's apt-daily timers and heals a pruned "
            "`claude` launcher at login"
        ),
    ),
    UpgradeNote(
        version=(1, 2, 0),
        actions=frozenset({"base_build", "apply"}),
        reason="Claude Code's global config moves into the shared `~/.claude` mount",
    ),
    UpgradeNote(
        version=(1, 2, 0),
        actions=frozenset({"base_build"}),
        reason=(
            "install.sh masks the user gnupg/pulse socket units that hijacked "
            "the host's agent sockets on every container boot"
        ),
    ),
    UpgradeNote(
        version=(1, 2, 0),
        actions=frozenset({"apply"}),
        reason=(
            "the Gradle and Maven caches move into per-container pool slots, "
            "so two containers no longer share one set of lock files"
        ),
    ),
)
"""What each release requires, ascending by version. Maintained by hand.

Add an entry whenever a change alters what `jailbee base build` produces or
what `jailbee apply` writes — see the rule in `CLAUDE.md`. Use the *upcoming*
release's version number; an entry above the running version is invisible
until that version ships, so adding it early is safe, but if the release
number changes before publication the entry is wrong.

One release may carry **several** entries, one per thing that changed, rather
than one entry listing every action it touches. `reason` is rendered under the
action it belongs to, so a single entry declaring both actions would tell the
user running `jailbee apply` about golden-image changes that have nothing to
do with `apply`. Split by reason, and let each entry name only the actions
that reason actually requires.

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
    """One action that is owed, with the reasons and the watermark it beat.

    `releases` are the versions of the notes behind `reasons`, ascending and
    deduplicated. An assumed watermark survives upgrades — a repo first seen
    under 1.1.0 keeps that watermark while 1.2.0 runs — so the watermark alone
    cannot say which release changed anything, and rendering it as if it could
    advertises the new release's changes under the old release's number.
    """

    action: Action
    watermark: Watermark
    reasons: tuple[str, ...]
    releases: tuple[tuple[int, int, int], ...]


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
        firing = tuple(
            note
            for note in notes
            if action in note.actions
            and note.version <= now
            and (note.version > mark.version if mark.observed else note.version >= mark.version)
        )
        if firing:
            owed.append(
                PendingAction(
                    action=action,
                    watermark=mark,
                    reasons=tuple(note.reason for note in firing),
                    releases=tuple(sorted({note.version for note in firing})),
                )
            )
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
            # would be a fabrication. Name the releases the reasons come from
            # instead — never the watermark, which is only where the repo was
            # first seen and is older than those releases after any upgrade.
            verb = "produces" if item.action == "base_build" else "writes"
            span = _dotted(item.releases[0])
            if item.releases[-1] != item.releases[0]:
                span = f"{span}-{_dotted(item.releases[-1])}"
            lines.append(f"jailbee {span} changed what `{command}` {verb}:")
        shown = item.reasons[:max_reasons]
        lines.extend(f"    - {reason}" for reason in shown)
        hidden = len(item.reasons) - len(shown)
        if hidden:
            lines.append(f"    - ... and {hidden} more (see the CHANGELOG)")
        lines.append(f"    Run `{command}` in this repo to pick these up.")
    return lines


def _bootstrap_row(prefix: str, version: str, now: datetime) -> RepoUpgradeState:
    """Construct the assumed-bootstrap row: both watermarks at `version`,
    neither observed. This is the single place the bootstrap shape is defined,
    so all callers that need to create a fresh row share the same structure.

    The `RepoUpgradeState` import is local on purpose: it keeps `db.models`
    (and the SQLModel metadata it registers) off this module's import graph,
    so the pure comparison helpers above stay importable without a DB.
    """
    from jailbee.db.models import RepoUpgradeState

    return RepoUpgradeState(
        container_prefix=prefix,
        base_build_version=version,
        base_build_observed=False,
        apply_version=version,
        apply_observed=False,
        updated_at=now,
    )


def _set_watermark(row: RepoUpgradeState, action: Action, version: str, *, observed: bool) -> None:
    """Write one action's watermark onto `row`. The only place the
    action-to-column mapping lives, shared by the repair path and `record`."""
    if action == "base_build":
        row.base_build_version = version
        row.base_build_observed = observed
    else:
        row.apply_version = version
        row.apply_observed = observed


def load_or_backfill(
    session: Session,
    prefix: str,
    current: str,
    *,
    now: datetime,
) -> dict[Action, Watermark]:
    """Return this repo's watermarks, writing the assumed row on first sight.

    First sight means an upgrade has just happened, so an image built by
    `current` cannot exist — the row is stored with `observed=False`, which
    keeps the comparison's lower bound inclusive so `current`'s own notes
    still fire. Anything older is deliberately water under the bridge: a repo
    that predates this bookkeeping gets no retroactive backlog.

    The row is written once, at first sight, and thereafter only read — the
    stored watermark is the record of history and a later run must never
    overwrite it. The single exception is the repair below.

    An action whose stored version does not parse cannot be compared against
    release-numbered notes, and since the row is otherwise never rewritten,
    simply dropping it would silence that action's advice for the life of the
    repo. It is repaired instead: rewritten as an assumed watermark at
    `current`, exactly the state a first sight under this version would have
    produced. (Such a version can only come from an install with no package
    metadata — `0.0.0+unknown`. Under one of those `pending` returns nothing
    anyway, so the repair is invisible until a real release runs.)
    """
    from jailbee.db.models import RepoUpgradeState

    row = session.get(RepoUpgradeState, prefix)
    if row is None:
        row = _bootstrap_row(prefix, current, now)
        session.add(row)
        session.commit()

    # Reading the columns into a plain dict here is load-bearing, not
    # stylistic: `commit()` above expires the instance, so these attribute
    # accesses are what re-SELECT them while the session is still open.
    # Deferring them past the caller's `with Session(...)` would raise
    # DetachedInstanceError.
    stored: dict[Action, tuple[str, bool]] = {
        "base_build": (row.base_build_version, row.base_build_observed),
        "apply": (row.apply_version, row.apply_observed),
    }
    marks: dict[Action, Watermark] = {}
    repaired = False
    for action, (raw, observed) in stored.items():
        version = parse_version(raw)
        if version is None:
            version = parse_version(current)
            if version is None:
                continue
            observed = False
            _set_watermark(row, action, current, observed=False)
            repaired = True
        marks[action] = Watermark(version=version, observed=observed)
    if repaired:
        row.updated_at = now
        session.add(row)
        session.commit()
    return marks


def record(
    session: Session,
    prefix: str,
    action: Action,
    version: str,
    *,
    now: datetime,
) -> None:
    """Mark `action` as observed at `version` for this repo.

    Called only after the action actually succeeded — a half-finished run must
    not silence the advice.

    When no row exists yet (a first-ever `base build` on a fresh install), one
    is created: the action that ran is observed, and the other gets the same
    assumed semantics `load_or_backfill` would have given it.
    """
    from jailbee.db.models import RepoUpgradeState

    row = session.get(RepoUpgradeState, prefix)
    if row is None:
        row = _bootstrap_row(prefix, version, now)
    # Assignment is no-op on freshly-bootstrapped path (where version was just
    # set), but load-bearing on existing-row path (where version may differ).
    _set_watermark(row, action, version, observed=True)
    row.updated_at = now
    session.add(row)
    session.commit()


def advice_lines(
    session: Session,
    prefix: str,
    current: str,
    *,
    now: datetime,
    notes: tuple[UpgradeNote, ...] | None = None,
) -> list[str]:
    """The whole read path: backfill if needed, compare, format."""
    marks = load_or_backfill(session, prefix, current, now=now)
    return format_advice(pending(current, marks, notes=notes))
