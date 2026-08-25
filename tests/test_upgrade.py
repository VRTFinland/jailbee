"""Upgrade-advice logic: version parsing, the manifest, and the comparison."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlmodel import Session


def test_parse_version_accepts_release_triples() -> None:
    from jailbee.upgrade import parse_version

    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("  10.0.11  ") == (10, 0, 11)


@pytest.mark.parametrize(
    "raw",
    [
        "0.0.0+unknown",  # the __init__.py fallback
        # A PEP 440 dev version. Nothing in this project produces one — the
        # static `pyproject.toml` version means even an editable install
        # reports a plain X.Y.Z — but rejecting it is still correct.
        "1.2.3.dev4+gdeadbee",
        "1.2",
        "1.2.3rc1",
        "v1.2.3",
        "",
    ],
)
def test_parse_version_rejects_non_releases(raw: str) -> None:
    """A version that is not release-shaped must not be compared against
    release-numbered notes: returning None is how the mechanism stays silent
    for it."""
    from jailbee.upgrade import parse_version

    assert parse_version(raw) is None


def _manifest_shape_errors(notes: tuple) -> list[str]:
    """Every way a manifest can be malformed, as messages. Returns [] when clean.

    A helper rather than inline assertions so the same predicates guard the
    real manifest and are themselves exercised by the synthetic cases below —
    `UPGRADE_NOTES` ships empty, so asserting against it alone can never fail.
    """
    from jailbee.upgrade import ACTIONS

    errors: list[str] = []
    versions = [n.version for n in notes]

    if versions != sorted(versions):
        errors.append("versions not in ascending order")

    if len(versions) != len(set(versions)):
        errors.append("one entry per version")

    for note in notes:
        if not note.actions:
            errors.append(f"{note.version}: an entry with no actions says nothing")
        if note.actions and not note.actions <= set(ACTIONS):
            errors.append(f"{note.version}: unknown action")
        if not note.reason.strip():
            errors.append(f"{note.version}: reason is what the user reads")

    return errors


def test_manifest_is_ascending_and_well_formed() -> None:
    """Guards the hand-maintained manifest's shape. Passes trivially while it
    is empty; the moment an entry is added it must be ordered and complete.

    The shape-checking predicates themselves are exercised by synthetic cases
    below, since the real manifest is empty."""
    from jailbee.upgrade import UPGRADE_NOTES

    assert _manifest_shape_errors(UPGRADE_NOTES) == []


def test_manifest_shape_rejects_descending_versions() -> None:
    """Versions must be in ascending order."""
    notes = (
        _note(2, 0, 0, "base_build", reason="later"),
        _note(1, 0, 0, "base_build", reason="earlier"),
    )
    errors = _manifest_shape_errors(notes)
    assert errors, "should reject descending versions"


def test_manifest_shape_rejects_duplicate_versions() -> None:
    """Each version must appear at most once."""
    notes = (
        _note(1, 0, 0, "base_build", reason="first"),
        _note(1, 0, 0, "apply", reason="duplicate"),
    )
    errors = _manifest_shape_errors(notes)
    assert errors, "should reject duplicate versions"


def test_manifest_shape_rejects_empty_actions() -> None:
    """Every note must declare at least one action."""
    from jailbee.upgrade import UpgradeNote

    notes = (UpgradeNote((1, 0, 0), frozenset(), "no actions"),)
    errors = _manifest_shape_errors(notes)
    assert errors, "should reject empty actions"


def test_manifest_shape_rejects_unknown_action() -> None:
    """All declared actions must be in ACTIONS."""
    from jailbee.upgrade import UpgradeNote

    notes = (UpgradeNote((1, 0, 0), frozenset({"unknown"}), "bad"),)  # type: ignore[arg-type]
    errors = _manifest_shape_errors(notes)
    assert errors, "should reject unknown action"


def test_manifest_shape_rejects_blank_reason() -> None:
    """Reason must be non-empty when stripped."""
    from jailbee.upgrade import UpgradeNote

    notes = (UpgradeNote((1, 0, 0), frozenset({"base_build"}), "   "),)
    errors = _manifest_shape_errors(notes)
    assert errors, "should reject blank reason"


def test_manifest_shape_accepts_well_formed() -> None:
    """A multi-entry manifest with all predicates satisfied."""
    notes = (
        _note(1, 0, 0, "base_build", reason="first release"),
        _note(1, 1, 0, "apply", reason="second release"),
        _note(2, 0, 0, "base_build", "apply", reason="third release"),
    )
    errors = _manifest_shape_errors(notes)
    assert errors == [], f"well-formed manifest should be clean: {errors}"


def _note(major: int, minor: int, patch: int, *actions: str, reason: str = "r"):
    from jailbee.upgrade import UpgradeNote

    return UpgradeNote(version=(major, minor, patch), actions=frozenset(actions), reason=reason)


def test_pending_is_empty_when_current_version_is_unparseable() -> None:
    from jailbee.upgrade import Watermark, pending

    notes = (_note(1, 0, 0, "base_build"),)
    got = pending(
        "0.0.0+unknown",
        {"base_build": Watermark((0, 9, 0), observed=True)},
        notes=notes,
    )
    assert not got
    assert got.actions == ()


def test_observed_watermark_excludes_its_own_version() -> None:
    """A run observed at 1.0.0 satisfied 1.0.0's notes: exclusive lower bound."""
    from jailbee.upgrade import Watermark, pending

    notes = (_note(1, 0, 0, "base_build", reason="old"),)
    got = pending("1.0.0", {"base_build": Watermark((1, 0, 0), observed=True)}, notes=notes)
    assert got.actions == ()


def test_assumed_watermark_includes_its_own_version() -> None:
    """The backfill's assumption: at first sight the upgrade has just happened,
    so an image built by the current version cannot exist — the current
    version's own notes must still fire. This boundary is the whole design."""
    from jailbee.upgrade import Watermark, pending

    notes = (_note(1, 0, 0, "base_build", reason="install.sh installs fd"),)
    got = pending("1.0.0", {"base_build": Watermark((1, 0, 0), observed=False)}, notes=notes)
    assert [a.action for a in got.actions] == ["base_build"]
    assert got.actions[0].reasons == ("install.sh installs fd",)
    assert got.actions[0].watermark.observed is False


def test_pending_collects_every_note_in_the_interval_per_action() -> None:
    from jailbee.upgrade import Watermark, pending

    notes = (
        _note(1, 0, 0, "base_build", reason="too old"),
        _note(1, 1, 0, "base_build", reason="rg and fd"),
        _note(1, 2, 0, "apply", reason="new ACL rule"),
        _note(1, 3, 0, "base_build", "apply", reason="node 24"),
        _note(2, 0, 0, "base_build", reason="not released yet"),
    )
    got = pending(
        "1.3.0",
        {
            "base_build": Watermark((1, 0, 0), observed=True),
            "apply": Watermark((1, 2, 0), observed=True),
        },
        notes=notes,
    )
    by_action = {a.action: a.reasons for a in got.actions}
    assert by_action["base_build"] == ("rg and fd", "node 24")
    assert by_action["apply"] == ("node 24",)


def test_notes_above_the_current_version_are_invisible() -> None:
    """An entry added during development for an unreleased version must not
    fire until that version ships."""
    from jailbee.upgrade import Watermark, pending

    notes = (_note(9, 9, 9, "base_build"),)
    got = pending("1.0.0", {"base_build": Watermark((1, 0, 0), observed=True)}, notes=notes)
    assert got.actions == ()


def test_watermark_newer_than_current_yields_nothing() -> None:
    """A downgrade: the recorded run is from the future, nothing is owed."""
    from jailbee.upgrade import Watermark, pending

    notes = (_note(1, 0, 0, "base_build"),)
    got = pending("1.0.0", {"base_build": Watermark((2, 0, 0), observed=True)}, notes=notes)
    assert got.actions == ()


def test_pending_orders_actions_predictably() -> None:
    from jailbee.upgrade import ACTIONS, Watermark, pending

    notes = (_note(1, 0, 0, "base_build", "apply", reason="both"),)
    got = pending(
        "1.0.0",
        {
            "apply": Watermark((1, 0, 0), observed=False),
            "base_build": Watermark((1, 0, 0), observed=False),
        },
        notes=notes,
    )
    assert [a.action for a in got.actions] == list(ACTIONS)


def test_missing_watermark_for_an_action_is_skipped() -> None:
    from jailbee.upgrade import Watermark, pending

    notes = (_note(1, 0, 0, "base_build", "apply", reason="both"),)
    got = pending("1.0.0", {"apply": Watermark((1, 0, 0), observed=False)}, notes=notes)
    assert [a.action for a in got.actions] == ["apply"]


def test_format_advice_is_empty_for_nothing_owed() -> None:
    from jailbee.upgrade import Pending, format_advice

    assert format_advice(Pending()) == []


def test_format_advice_names_the_observed_watermark() -> None:
    """With a real run on record the message can say so honestly."""
    from jailbee.upgrade import Pending, PendingAction, Watermark, format_advice

    owed = Pending(
        (
            PendingAction(
                action="base_build",
                watermark=Watermark((1, 0, 3), observed=True),
                reasons=("install.sh installs fd",),
            ),
        )
    )
    lines = format_advice(owed)
    assert lines[0] == "Since this repo last ran `jb base build` (jailbee 1.0.3):"
    assert lines[1] == "    - install.sh installs fd"
    assert lines[2] == "    Run `jb base build` in this repo to pick these up."


def test_format_advice_does_not_claim_a_run_it_never_saw() -> None:
    """An assumed watermark means jailbee never observed a run — the message
    must not say "since this repo last ran"."""
    from jailbee.upgrade import Pending, PendingAction, Watermark, format_advice

    owed = Pending(
        (
            PendingAction(
                action="apply",
                watermark=Watermark((1, 1, 0), observed=False),
                reasons=("the ACL gained a rule",),
            ),
        )
    )
    lines = format_advice(owed)
    assert lines[0] == "jailbee 1.1.0 changed what `jb apply` writes:"
    assert "last ran" not in lines[0]


def test_format_advice_truncates_a_long_reason_list() -> None:
    from jailbee.upgrade import Pending, PendingAction, Watermark, format_advice

    owed = Pending(
        (
            PendingAction(
                action="base_build",
                watermark=Watermark((1, 0, 0), observed=True),
                reasons=("a", "b", "c", "d", "e"),
            ),
        )
    )
    lines = format_advice(owed, max_reasons=3)
    assert lines[1:5] == [
        "    - a",
        "    - b",
        "    - c",
        "    - ... and 2 more (see the CHANGELOG)",
    ]


def test_format_advice_renders_both_actions() -> None:
    from jailbee.upgrade import Pending, PendingAction, Watermark, format_advice

    owed = Pending(
        (
            PendingAction("base_build", Watermark((1, 0, 0), observed=True), ("x",)),
            PendingAction("apply", Watermark((1, 0, 0), observed=True), ("y",)),
        )
    )
    lines = format_advice(owed)
    assert sum(1 for line in lines if line.startswith("Since this repo")) == 2
    assert any("`jb base build`" in line for line in lines)
    assert any("`jb apply`" in line for line in lines)


_NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def test_load_or_backfill_creates_an_assumed_row_on_first_sight(db_engine) -> None:
    """The bootstrap: at first sight the upgrade has just happened, so the row
    records the current version as *assumed*, never observed."""
    from jailbee.db.models import RepoUpgradeState
    from jailbee.upgrade import load_or_backfill

    with Session(db_engine) as session:
        marks = load_or_backfill(session, "sampleapp", "1.4.0", now=_NOW)
        row = session.get(RepoUpgradeState, "sampleapp")

    assert row is not None
    assert row.base_build_version == "1.4.0"
    assert row.base_build_observed is False
    assert row.apply_version == "1.4.0"
    assert row.apply_observed is False
    assert marks["base_build"].version == (1, 4, 0)
    assert marks["base_build"].observed is False


def test_load_or_backfill_does_not_rewrite_an_existing_row(db_engine) -> None:
    """Called on every relevant command, including the dashboard's refresh
    loop: after the first insert it must only read."""
    from jailbee.upgrade import load_or_backfill

    with Session(db_engine) as session:
        load_or_backfill(session, "sampleapp", "1.4.0", now=_NOW)
    with Session(db_engine) as session:
        marks = load_or_backfill(session, "sampleapp", "1.9.0", now=_NOW)

    assert marks["apply"].version == (1, 4, 0), "the stored watermark wins"


def test_load_or_backfill_skips_an_unparseable_stored_version(db_engine) -> None:
    """A row written by a dev build carries a version no comparison can use.
    Dropping that action is silence, which beats a wrong hint."""
    from jailbee.db.models import RepoUpgradeState
    from jailbee.upgrade import load_or_backfill

    with Session(db_engine) as session:
        session.add(
            RepoUpgradeState(
                container_prefix="sampleapp",
                base_build_version="0.0.0+unknown",
                base_build_observed=True,
                apply_version="1.4.0",
                apply_observed=True,
                updated_at=_NOW,
            )
        )
        session.commit()
        marks = load_or_backfill(session, "sampleapp", "1.9.0", now=_NOW)

    assert "base_build" not in marks
    assert marks["apply"].version == (1, 4, 0)


def test_record_marks_one_action_observed(db_engine) -> None:
    from jailbee.db.models import RepoUpgradeState
    from jailbee.upgrade import load_or_backfill, record

    with Session(db_engine) as session:
        load_or_backfill(session, "sampleapp", "1.4.0", now=_NOW)
        record(session, "sampleapp", "base_build", "1.9.0", now=_NOW)
        row = session.get(RepoUpgradeState, "sampleapp")

    assert row is not None
    assert row.base_build_version == "1.9.0"
    assert row.base_build_observed is True
    assert row.apply_version == "1.4.0", "the other action is untouched"
    assert row.apply_observed is False


def test_record_creates_the_row_when_there_is_none(db_engine) -> None:
    """A first-ever `jb base build` may run before anything backfilled. The
    action that ran is observed; the other one gets the same assumed
    semantics the backfill would have given it."""
    from jailbee.db.models import RepoUpgradeState
    from jailbee.upgrade import record

    with Session(db_engine) as session:
        record(session, "sampleapp", "base_build", "1.9.0", now=_NOW)
        row = session.get(RepoUpgradeState, "sampleapp")

    assert row is not None
    assert row.base_build_observed is True
    assert row.apply_version == "1.9.0"
    assert row.apply_observed is False


def test_advice_lines_is_silent_after_the_action_ran(db_engine) -> None:
    from jailbee.upgrade import advice_lines, record

    notes = (_note(1, 4, 0, "base_build", reason="install.sh installs fd"),)
    with Session(db_engine) as session:
        assert advice_lines(session, "sampleapp", "1.4.0", now=_NOW, notes=notes)
        record(session, "sampleapp", "base_build", "1.4.0", now=_NOW)
        assert advice_lines(session, "sampleapp", "1.4.0", now=_NOW, notes=notes) == []


def test_advice_lines_reports_the_current_release_on_a_fresh_repo(db_engine) -> None:
    """End to end over the boundary this design turns on."""
    from jailbee.upgrade import advice_lines

    notes = (_note(1, 4, 0, "base_build", reason="install.sh installs fd"),)
    with Session(db_engine) as session:
        lines = advice_lines(session, "sampleapp", "1.4.0", now=_NOW, notes=notes)

    assert lines[0] == "jailbee 1.4.0 changed what `jb base build` produces:"
    assert "install.sh installs fd" in lines[1]
