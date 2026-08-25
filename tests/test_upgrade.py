"""Upgrade-advice logic: version parsing, the manifest, and the comparison."""

from __future__ import annotations

import pytest


def test_parse_version_accepts_release_triples() -> None:
    from jailbee.upgrade import parse_version

    assert parse_version("1.2.3") == (1, 2, 3)
    assert parse_version("  10.0.11  ") == (10, 0, 11)


@pytest.mark.parametrize(
    "raw",
    [
        "0.0.0+unknown",  # the __init__.py fallback
        "1.2.3.dev4+gdeadbee",  # an editable install
        "1.2",
        "1.2.3rc1",
        "v1.2.3",
        "",
    ],
)
def test_parse_version_rejects_non_releases(raw: str) -> None:
    """A dev/editable version must not be compared against release-numbered
    notes: returning None is how the whole mechanism stays silent for it."""
    from jailbee.upgrade import parse_version

    assert parse_version(raw) is None


def test_manifest_is_ascending_and_well_formed() -> None:
    """Guards the hand-maintained manifest's shape. Passes trivially while it
    is empty; the moment an entry is added it must be ordered and complete."""
    from jailbee.upgrade import ACTIONS, UPGRADE_NOTES

    versions = [n.version for n in UPGRADE_NOTES]
    assert versions == sorted(versions)
    assert len(versions) == len(set(versions)), "one entry per version"
    for note in UPGRADE_NOTES:
        assert note.actions, f"{note.version}: an entry with no actions says nothing"
        assert note.actions <= set(ACTIONS), f"{note.version}: unknown action"
        assert note.reason.strip(), f"{note.version}: reason is what the user reads"


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
