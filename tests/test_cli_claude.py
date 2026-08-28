"""Tests for the `jailbee claude` command group."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee import claude_pool
from jailbee.claude_pool import PoolChange, Slot
from jailbee.cli import app
from jailbee.global_config import GlobalConfig

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, mocker, make_cfg):
    """A loaded repo config with a tmp shared_dir, wired into the CLI."""
    cfg = make_cfg(tmp_path / "app", shared_dir=tmp_path / "shared")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    return cfg


def test_ls_renders_the_live_account_first(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.list_slots",
        return_value=[
            Slot("me@corp.com#a1b2c3d4", Path("/h/.credentials.json"), live=True),
            Slot("other@x.com", Path("/s/other@x.com.json"), live=False),
        ],
    )
    result = runner.invoke(app, ["claude", "ls"])
    assert result.exit_code == 0, result.output
    assert result.output.index("me@corp.com") < result.output.index("other@x.com")
    assert "live" in result.output


def test_ls_json_carries_the_fields(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.list_slots",
        return_value=[Slot("me@corp.com#a1b2c3d4", Path("/h/c.json"), live=True)],
    )
    result = runner.invoke(app, ["claude", "ls", "-o", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {"account": "me@corp.com#a1b2c3d4", "org": "a1b2c3d4", "state": "live"}
    ]


def test_ls_says_so_when_the_pool_is_empty(repo, mocker):
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[])
    result = runner.invoke(app, ["claude", "ls"])
    assert result.exit_code == 0, result.output
    assert "park" in result.output


def test_use_reports_both_sides_of_the_switch(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.switch",
        return_value=PoolChange(
            parked_as="old@corp.com",
            activated="new@corp.com",
            cleared=["app", "other"],
            not_cleared=[],
            live_sessions=[],
        ),
    )
    result = runner.invoke(app, ["claude", "use", "new@corp.com"])
    assert result.exit_code == 0, result.output
    assert "new@corp.com" in result.output
    assert "old@corp.com" in result.output
    assert "other" in result.output


def test_use_warns_about_a_live_session_without_failing(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.switch",
        return_value=PoolChange("old@x.com", "new@x.com", ["app"], [], ["app"]),
    )
    result = runner.invoke(app, ["claude", "use", "new@x.com"])
    assert result.exit_code == 0, result.output
    assert "session" in result.output.lower()


def test_use_warns_about_a_member_it_could_not_refresh(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.switch",
        return_value=PoolChange("old@x.com", "new@x.com", ["app"], ["broken"], []),
    )
    result = runner.invoke(app, ["claude", "use", "new@x.com"])
    assert result.exit_code == 0, result.output
    assert "broken" in result.output


def test_use_exits_2_on_a_pool_error(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.switch",
        side_effect=claude_pool.PoolError("no stored account matches `nope`."),
    )
    result = runner.invoke(app, ["claude", "use", "nope"])
    assert result.exit_code == 2
    assert "nope" in result.output


def test_use_exits_2_on_a_lock_timeout(repo, mocker):
    from jailbee.claude_locks import ClaudeLockTimeoutError

    mocker.patch(
        "jailbee.claude_pool.switch",
        side_effect=ClaudeLockTimeoutError("/h/.oauth_refresh.lock is held"),
    )
    result = runner.invoke(app, ["claude", "use", "x@y.com"])
    assert result.exit_code == 2
    assert "oauth_refresh.lock" in result.output


def test_park_tells_the_user_how_a_new_login_gets_in(repo, mocker):
    mocker.patch(
        "jailbee.claude_pool.park",
        return_value=PoolChange("me@corp.com", None, ["app"], [], []),
    )
    result = runner.invoke(app, ["claude", "park"])
    assert result.exit_code == 0, result.output
    assert "me@corp.com" in result.output
    assert "/login" in result.output


def test_park_of_an_empty_holder_is_not_an_error(repo, mocker):
    mocker.patch("jailbee.claude_pool.park", return_value=PoolChange(None, None, [], [], []))
    result = runner.invoke(app, ["claude", "park"])
    assert result.exit_code == 0, result.output
    assert "Nothing to park" in result.output


def test_rm_confirms_before_deleting(repo, mocker):
    slot = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[slot])
    remove = mocker.patch("jailbee.claude_pool.remove_slot")

    declined = runner.invoke(app, ["claude", "rm", "old@x.com"], input="n\n")
    assert declined.exit_code == 1
    remove.assert_not_called()

    accepted = runner.invoke(app, ["claude", "rm", "old@x.com"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    remove.assert_called_once_with(slot)


def test_rm_warns_that_deletion_is_permanent(repo, mocker):
    slot = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[slot])
    mocker.patch("jailbee.claude_pool.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "old@x.com"], input="n\n")

    assert "/login" in result.output


def test_rm_yes_skips_the_prompt(repo, mocker):
    slot = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[slot])
    remove = mocker.patch("jailbee.claude_pool.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "old@x.com", "--yes"])

    assert result.exit_code == 0, result.output
    remove.assert_called_once_with(slot)


def test_rm_refuses_the_live_account(repo, mocker):
    slot = Slot("me@x.com", Path("/h/.credentials.json"), live=True)
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[slot])
    remove = mocker.patch("jailbee.claude_pool.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "me@x.com", "--yes"])

    assert result.exit_code == 2
    assert "park" in result.output
    remove.assert_not_called()


def test_ls_names_the_repos_that_share_the_holder(repo, mocker):
    """A switch is holder-wide, so who else moves with it is part of reading
    the table — and the in-container skill file promises `ls` says so."""
    mocker.patch(
        "jailbee.claude_pool.list_slots",
        return_value=[Slot("me@x.com", Path("/h/.credentials.json"), live=True)],
    )
    mocker.patch(
        "jailbee.claude_pool.members",
        return_value=(
            [
                claude_pool.Member("app", Path("/repos/app/.shared/claude")),
                claude_pool.Member("other", Path("/repos/other/.shared/claude")),
            ],
            ["broken"],
        ),
    )

    result = runner.invoke(app, ["claude", "ls"])

    assert result.exit_code == 0, result.output
    assert "app, other" in result.output
    # A member whose config would not load shares the holder too — silently
    # dropping it would understate who the next switch moves.
    assert "broken" in result.output


def test_ls_json_stays_a_clean_payload(repo, mocker):
    """The member list is a per-command fact, not a row: printing it in JSON
    mode would make the output unparseable for the caller that asked for it."""
    mocker.patch(
        "jailbee.claude_pool.list_slots",
        return_value=[Slot("me@x.com", Path("/h/c.json"), live=True)],
    )
    mocker.patch(
        "jailbee.claude_pool.members",
        return_value=([claude_pool.Member("app", Path("/repos/app"))], []),
    )

    result = runner.invoke(app, ["claude", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [{"account": "me@x.com", "org": None, "state": "live"}]


def test_ls_exits_2_when_the_store_cannot_be_read(repo, mocker):
    """`_registered_repos`, `holder.mkdir` and every credential read raise
    `OSError`, and a traceback is not a diagnosis."""
    mocker.patch(
        "jailbee.claude_pool.list_slots", side_effect=OSError("permission denied: _parked")
    )
    result = runner.invoke(app, ["claude", "ls"])
    assert result.exit_code == 2
    assert "permission denied" in result.output


def test_use_exits_2_on_an_os_error(repo, mocker):
    """`_move_file` and `_atomic_write` raise `OSError` mid-move: the one
    moment the user most needs a message rather than a stack trace."""
    mocker.patch("jailbee.claude_pool.switch", side_effect=OSError("no space left on device"))
    result = runner.invoke(app, ["claude", "use", "x@y.com"])
    assert result.exit_code == 2
    assert "no space left" in result.output


def test_park_exits_2_on_an_os_error(repo, mocker):
    mocker.patch("jailbee.claude_pool.park", side_effect=OSError("read-only file system"))
    result = runner.invoke(app, ["claude", "park"])
    assert result.exit_code == 2
    assert "read-only file system" in result.output


def test_park_warns_that_a_live_session_loses_its_login(repo, mocker):
    """`use` swaps one credential for another; `park` leaves the holder empty.
    The shared "the account in /status may lag" wording is a false reassurance
    in the one command that removes authentication."""
    mocker.patch(
        "jailbee.claude_pool.park",
        return_value=PoolChange("me@x.com", None, ["app"], [], ["app"]),
    )

    result = runner.invoke(app, ["claude", "park"])

    assert result.exit_code == 0, result.output
    assert "no login" in result.output
    assert "lag" not in result.output


def test_rm_deletes_the_parked_half_of_a_duplicated_name(repo, mocker):
    """A store corrupt enough for two slots to share a name still has to be
    fixable with `rm`: the error that reports it must not also block the only
    command that clears it. `rm` never deletes a live login, so the parked file
    is the unambiguous target."""
    parked = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    live = Slot("old@x.com", Path("/h/.credentials.json"), live=True)
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[parked, live])
    remove = mocker.patch("jailbee.claude_pool.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "old@x.com", "--yes"])

    assert result.exit_code == 0, result.output
    remove.assert_called_once_with(parked)


def test_rm_speaks_the_shared_live_account_refusal(repo, mocker):
    """One sentence for "that is the live login", not two that can drift."""
    slot = Slot("me@x.com", Path("/h/.credentials.json"), live=True)
    mocker.patch("jailbee.claude_pool.list_slots", return_value=[slot])
    mocker.patch("jailbee.claude_pool.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "me@x.com", "--yes"])

    assert result.exit_code == 2
    # Equality with the shared wording, not a substring both happen to
    # contain: a re-inlined literal that merely echoes one phrase from it
    # would still pass a substring check, but not this one.
    assert claude_pool.live_account_refusal("me@x.com") in result.output
