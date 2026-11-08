"""Destroying a container warns about its staged outbox proposals.

The outbox lives in the host store, so the warning must reach stopped and
mount-mode rows (whose ``git_status`` is None) and must not fall silent when
the store could not be read.
"""

from __future__ import annotations

from dataclasses import replace

from jailbee.dashboard import bulk
from jailbee.dashboard import model as dmodel
from jailbee.destroy_guard import assess
from jailbee.git_status import GitStatus
from tests.dashboard_fixtures import ci

CLEAN = GitStatus(wt="clean", ahead_diff="clean", ahead_count="0", conflict="ok")


def _group(tmp_path, *containers):
    return dmodel.RepoGroup("alpha", str(tmp_path), None, list(containers))


def test_a_stopped_container_with_staged_proposals_warns_in_the_terminal_dashboard(
    tmp_path, mocker
):
    stopped = replace(ci("alpha-a", "alpha", "Stopped"), uuid="u1", outbox_pr=2, outbox_issue=0)
    mocker.patch("jailbee.config.load_repo_config", return_value=mocker.Mock())

    lines = bulk.destroy_risk_lines([_group(tmp_path, stopped)], ["alpha-a"])

    assert any("2 unapplied PR actions" in line for line in lines)
    assert any("git status unknown" in line for line in lines)


def test_a_mount_container_with_staged_proposals_warns_in_the_terminal_dashboard(tmp_path, mocker):
    mount = replace(ci("alpha-a", "alpha", mode="mount"), uuid="u1", outbox_pr=0, outbox_issue=1)
    mocker.patch("jailbee.config.load_repo_config", return_value=mocker.Mock())

    lines = bulk.destroy_risk_lines([_group(tmp_path, mount)], ["alpha-a"])

    # Mount mode: git status is not applicable, so the outbox is the only line.
    assert len(lines) == 1
    assert lines[0].endswith(": 1 unapplied issue action")


def test_an_unreadable_store_is_named_not_passed_as_safe(tmp_path):
    # uuid known, counts None: the store read failed (`lifecycle._issue_count`).
    unread = replace(ci("alpha-a", "alpha", "Stopped"), uuid="u1")

    summary = assess(mocker_cfg(), unread)

    assert summary is not None
    assert any("outbox" in reason and "could not be read" in reason for reason in summary.reasons)


def test_an_unreadable_store_is_named_for_a_probed_clean_container_too():
    probed = replace(ci("alpha-a", "alpha", git_status=CLEAN), uuid="u1")

    summary = assess(mocker_cfg(), probed)

    assert summary is not None
    assert any("could not be read" in reason for reason in summary.reasons)


def test_store_counts_win_over_the_probe_copy():
    probed = replace(
        ci("alpha-a", "alpha", git_status=CLEAN), uuid="u1", outbox_pr=3, outbox_issue=0
    )

    summary = assess(mocker_cfg(), probed)

    assert summary is not None
    assert summary.reasons == ("3 unapplied PR actions",)


def test_a_row_with_known_empty_counts_is_not_warned():
    stopped = replace(ci("alpha-a", "alpha", "Stopped"), uuid="u1", outbox_pr=0, outbox_issue=0)

    assert assess(mocker_cfg(), stopped) is None


def mocker_cfg():  # type: ignore[no-untyped-def]
    # assess only touches cfg for the has_commit check, which a clean or
    # unprobed status never reaches.
    return object()
