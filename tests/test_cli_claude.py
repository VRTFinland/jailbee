"""Tests for the `jailbee claude` command group."""

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from jailbee.accounts import engine
from jailbee.accounts.adapters.claude import CLAUDE
from jailbee.accounts.models import PoolChange, PoolError, Slot
from jailbee.cli import app
from jailbee.global_config import GlobalConfig
from tests.conftest import claude_overview_of as _overview
from tests.conftest import claude_row as _row
from tests.conftest import flat_output as _flat

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, mocker, make_cfg):
    """A loaded repo config with a tmp shared_dir, wired into the CLI."""
    cfg = make_cfg(tmp_path / "app", shared_dir=tmp_path / "shared")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig())
    return cfg


def _built(mocker, overview) -> None:
    mocker.patch("jailbee.accounts.overview.build", return_value=overview)


def test_ls_says_which_account_is_live_in_each_group(repo, mocker):
    """The question `claude ls` could not answer before: one row per holder,
    each naming the login it holds."""
    _built(
        mocker,
        _overview(
            _row("staff@corp.com", group="staff", repos=("app",), containers=("app-x",)),
            _row("demo@corp.com", group="demo", containers=("app-y",)),
        ),
    )

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "staff@corp.com" in result.output
    assert "demo@corp.com" in result.output
    assert "GROUP" in result.output


def test_ls_names_the_containers_of_a_group_no_repo_resolves_to(repo, mocker):
    """A temporary override is the only thing keeping such a group in use, so
    the containers are named rather than counted — nothing else points at it."""
    _built(mocker, _overview(_row("demo@corp.com", group="demo", containers=("app-y",))))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "app-y" in result.output
    assert "no repo" in result.output


def test_ls_counts_the_containers_of_a_group_repos_resolve_to(repo, mocker):
    """With repos named, container names would only add width: the repo list is
    what a holder-wide switch moves."""
    _built(
        mocker,
        _overview(
            _row(
                "staff@corp.com",
                group="staff",
                repos=("app", "other"),
                containers=("app-x", "other-y"),
            )
        ),
    )

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "app, other" in result.output
    assert "2 containers" in result.output
    assert "app-x" not in result.output


def test_ls_shows_a_group_that_holds_no_login(repo, mocker):
    """`jailbee claude group` printed such a group's name and nothing said it
    was empty; a `/login` is what it is waiting for."""
    _built(mocker, _overview(_row(None, group="fresh")))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "fresh" in result.output
    assert "empty" in result.output
    assert "no login" in result.output


def test_ls_says_a_group_is_unused_when_nothing_reads_it(repo, mocker):
    _built(mocker, _overview(_row(None, group="fresh")))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert "unused" in result.output


def test_ls_names_an_ungrouped_holder_by_its_repo(repo, mocker):
    """Without a group the holder is one repo's own config home, so calling it
    a group would name something that does not exist."""
    _built(
        mocker,
        _overview(_row("me@corp.com", prefix="scratch", repos=("scratch",))),
    )

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "(no group)" in result.output
    assert "scratch" in result.output


def test_ls_does_not_claim_the_whole_table_belongs_to_one_group(repo, mocker):
    """Every group on the host is a row now, so the title must stay host-wide:
    naming one group read as a claim over all of it, and a user asked why an
    account had "appeared in" a group they had never touched."""
    _built(mocker, _overview(_row("me@corp.com", group="gisgro", mine=True)))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "Claude logins on this host" in flat
    assert "logins for group" not in flat


def test_ls_says_which_holder_this_repo_uses(repo, mocker):
    from jailbee.paths import display_path

    cfg = repo.model_copy(update={"credential_group": "gisgro"})
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    _built(mocker, _overview(_row("me@corp.com", group="gisgro", mine=True)))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert f"This repo ({cfg.container_prefix}) → group `gisgro`" in flat
    assert display_path(engine.group_dir("claude", "gisgro")) in flat


def test_ls_says_when_this_repo_shares_no_group(repo, mocker):
    _built(mocker, _overview(_row("me@corp.com", prefix=repo.container_prefix, mine=True)))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert f"This repo ({repo.container_prefix}) → no credential group" in flat


def test_ls_does_not_print_the_organization_twice(repo, mocker):
    """`Slot.org_hint` is parsed back out of `Slot.name`, so rendering the name
    in ACCOUNT beside the org in ORG repeated the same eight characters in
    every row."""
    _built(mocker, _overview(_row("me@corp.com#a1b2c3d4", group="staff")))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert result.output.count("a1b2c3d4") == 1
    assert "me@corp.com" in result.output
    assert "me@corp.com#a1b2c3d4" not in result.output


def test_ls_keeps_a_disambiguator_in_the_account_column(repo, mocker):
    """Dropping the org must not drop the `~` half: it is the only thing
    telling two grants of one account apart, and `claude use` needs it."""
    _built(
        mocker,
        _overview(
            _row("me@corp.com#a1b2c3d4~live", group="staff"),
            _row("me@corp.com#a1b2c3d4~20260828-101500", live=False),
        ),
    )

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "me@corp.com~live" in result.output
    assert "me@corp.com~20260828-101500" in result.output


def test_ls_hides_the_org_column_when_no_account_has_one(repo, mocker):
    """A store of personal accounts has no organization anywhere, and a column
    of "-" earns no width."""
    _built(mocker, _overview(_row("me@personal.com", group="staff")))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "ORG" not in result.output
    assert "ACCOUNT" in result.output


def test_ls_reports_the_repos_it_could_not_read(repo, mocker):
    """A repo whose config will not load may hold a holder this table is
    missing, and silence would understate what the host really has."""
    _built(
        mocker,
        _overview(_row("me@corp.com", group="staff"), unreachable=("broken",)),
    )

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "broken" in result.output


def test_ls_says_so_when_the_container_column_could_not_be_filled(repo, mocker):
    """An unreachable Incus daemon costs the container column, not the
    listing — but an empty column must not read as "no containers"."""
    _built(
        mocker,
        _overview(_row("me@corp.com", group="staff", repos=("app",)), containers_known=False),
    )

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "could not be listed" in flat
    assert "no containers" not in flat


def test_ls_filters_to_one_group_and_the_parked_store(repo, mocker):
    """`-g` used to point the whole command at another holder; now it narrows
    the host-wide table, and a parked login stays activatable into it."""
    _built(
        mocker,
        _overview(
            _row("staff@corp.com", group="staff"),
            _row("demo@corp.com", group="demo"),
            _row("old@corp.com", live=False),
        ),
    )

    result = runner.invoke(app, ["claude", "ls", "-g", "demo"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "demo@corp.com" in result.output
    assert "staff@corp.com" not in result.output
    assert "old@corp.com" in result.output


def test_ls_explains_the_parked_scope_only_when_filtered(repo, mocker):
    """Unfiltered, every row names its own group and the mixing is visible. Under
    `-g` the parked rows sit beside one group again, which is what made a user
    ask why an account had appeared in a group they never touched."""
    overview = _overview(_row("staff@corp.com", group="staff"), _row("old@corp.com", live=False))
    _built(mocker, overview)

    plain = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})
    filtered = runner.invoke(app, ["claude", "ls", "-g", "staff"], env={"COLUMNS": "200"})

    assert "host-wide" not in _flat(plain.output)
    assert "host-wide" in _flat(filtered.output)


def test_ls_says_when_the_host_has_no_such_group(repo, mocker):
    _built(mocker, _overview(_row("staff@corp.com", group="staff")))

    result = runner.invoke(app, ["claude", "ls", "-g", "nope"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "nope" in result.output


def test_ls_rejects_a_group_name_outside_the_grammar(repo, mocker):
    """The name reaches `group_dir` as a path component elsewhere, so the CLI
    refuses the same names everywhere rather than only where it writes."""
    _built(mocker, _overview(_row("staff@corp.com", group="staff")))

    result = runner.invoke(app, ["claude", "ls", "-g", "../etc"], env={"COLUMNS": "200"})

    assert result.exit_code == 2


def test_ls_advises_a_login_when_the_host_holds_none(repo, mocker):
    """The caller's own holder is always a row, so an all-empty host still
    renders a table — and the advice has to be about getting a login in."""
    _built(mocker, _overview(_row(None, prefix=repo.container_prefix, mine=True)))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "/login" in result.output


def test_ls_json_carries_the_holder_of_every_row(repo, mocker):
    """A script reading this needs the group: two holders can be logged into one
    account, and the account name alone no longer identifies a row."""
    _built(
        mocker,
        _overview(
            _row(
                "me@corp.com#a1b2c3d4",
                group="staff",
                repos=("app",),
                containers=("app-x",),
                mine=True,
            )
        ),
    )

    result = runner.invoke(app, ["claude", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {
            "account": "me@corp.com#a1b2c3d4",
            "org": "a1b2c3d4",
            "state": "live",
            "group": "staff",
            "repos": ["app"],
            "containers": ["app-x"],
        }
    ]


def test_ls_json_stays_a_clean_payload(repo, mocker):
    """The per-command facts under the table are not rows: printing them in
    JSON mode would make the output unparseable for the caller that asked."""
    _built(
        mocker,
        _overview(_row("me@corp.com", group="staff"), unreachable=("broken",)),
    )

    result = runner.invoke(app, ["claude", "ls", "-o", "json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == [
        {
            "account": "me@corp.com",
            "org": None,
            "state": "live",
            "group": "staff",
            "repos": [],
            "containers": [],
        }
    ]


def test_ls_exits_2_when_the_store_cannot_be_read(repo, mocker):
    """`registered_repos`, the credential reads and the store glob all raise
    `OSError`, and a traceback is not a diagnosis."""
    mocker.patch(
        "jailbee.accounts.overview.build", side_effect=OSError("permission denied: _parked")
    )

    result = runner.invoke(app, ["claude", "ls"])

    assert result.exit_code == 2
    assert "permission denied" in result.output


def test_use_without_an_account_picks_from_a_menu(repo, mocker):
    """The gap the pickers on `jb tmux`/`jb shell` set the expectation for: a
    bare `claude use` must offer the stored logins, not fail on a missing
    argument."""
    mocker.patch(
        "jailbee.accounts.engine.list_slots",
        return_value=[
            Slot("live@corp.com", Path("/h/.credentials.json"), live=True),
            Slot("one@corp.com", Path("/s/one.json"), live=False),
            Slot("two@corp.com", Path("/s/two.json"), live=False),
        ],
    )
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    pick = mocker.patch("jailbee.tui.pick_account", return_value=("claude", "two@corp.com"))
    switch = mocker.patch(
        "jailbee.accounts.engine.switch",
        return_value=PoolChange("live@corp.com", "two@corp.com", [], [], []),
    )
    result = runner.invoke(app, ["claude", "use"])
    assert result.exit_code == 0, result.output
    # The live slot is not a candidate: `switch` would refuse it.
    offered = [s.name for _, s in pick.call_args.args[0]]
    assert offered == ["one@corp.com", "two@corp.com"]
    switch.assert_called_once()
    assert switch.call_args.args[3] == "two@corp.com"


def test_use_without_an_account_auto_selects_a_lone_candidate(repo, mocker):
    """One stored login needs no prompt — a one-row picker is noise, and a
    script with no TTY should still get the switch it asked for."""
    mocker.patch(
        "jailbee.accounts.engine.list_slots",
        return_value=[
            Slot("live@corp.com", Path("/h/.credentials.json"), live=True),
            Slot("only@corp.com", Path("/s/only.json"), live=False),
        ],
    )
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    pick = mocker.patch("jailbee.tui.pick_account")
    switch = mocker.patch(
        "jailbee.accounts.engine.switch",
        return_value=PoolChange("live@corp.com", "only@corp.com", [], [], []),
    )
    result = runner.invoke(app, ["claude", "use"])
    assert result.exit_code == 0, result.output
    pick.assert_not_called()
    assert switch.call_args.args[3] == "only@corp.com"


def test_use_without_an_account_aborts_when_the_menu_is_cancelled(repo, mocker):
    """ESC must not switch anything, and must not print a failure either."""
    mocker.patch(
        "jailbee.accounts.engine.list_slots",
        return_value=[
            Slot("one@corp.com", Path("/s/one.json"), live=False),
            Slot("two@corp.com", Path("/s/two.json"), live=False),
        ],
    )
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.tui.pick_account", return_value=None)
    switch = mocker.patch("jailbee.accounts.engine.switch")
    result = runner.invoke(app, ["claude", "use"])
    assert result.exit_code != 0
    switch.assert_not_called()


def test_cancelling_the_picker_leaves_the_store_untouched(repo, mocker):
    """The selection path only reads: a cancel returns None before any engine
    mutation runs, so nothing on disk can move."""
    mocker.patch(
        "jailbee.accounts.engine.list_slots",
        return_value=[
            Slot("one@corp.com", Path("/s/one.json"), live=False),
            Slot("two@corp.com", Path("/s/two.json"), live=False),
        ],
    )
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    mocker.patch("jailbee.tui.pick_account", return_value=None)
    switch = mocker.patch("jailbee.accounts.engine.switch")
    park = mocker.patch("jailbee.accounts.engine.park")
    remove = mocker.patch("jailbee.accounts.engine.remove_slot")
    result = runner.invoke(app, ["claude", "use"])
    assert result.exit_code != 0
    switch.assert_not_called()
    park.assert_not_called()
    remove.assert_not_called()


def test_use_without_an_account_and_without_a_tty_names_the_candidates(repo, mocker):
    """A script gets the references it should have passed, not a picker."""
    mocker.patch(
        "jailbee.accounts.engine.list_slots",
        return_value=[
            Slot("live@corp.com", Path("/h/.credentials.json"), live=True),
            Slot("one@corp.com", Path("/s/one.json"), live=False),
            Slot("two@corp.com", Path("/s/two.json"), live=False),
        ],
    )
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    switch = mocker.patch("jailbee.accounts.engine.switch")
    result = runner.invoke(app, ["claude", "use"])
    assert result.exit_code == 2
    assert "one@corp.com" in result.output
    assert "two@corp.com" in result.output
    switch.assert_not_called()


def test_use_with_an_account_does_not_read_the_store(repo, mocker):
    """A named account must not pay for a store listing this path never uses —
    `switch` lists it again under the credential locks anyway."""
    slots = mocker.patch("jailbee.accounts.engine.list_slots")
    mocker.patch(
        "jailbee.accounts.engine.switch",
        return_value=PoolChange(None, "new@corp.com", [], [], []),
    )
    result = runner.invoke(app, ["claude", "use", "new@corp.com"])
    assert result.exit_code == 0, result.output
    slots.assert_not_called()


def test_rm_without_an_account_picks_from_a_menu(repo, mocker):
    """`rm` has the same signature as `use`, so it gets the same menu — and its
    own confirmation still stands in front of the deletion."""
    parked = Slot("two@corp.com", Path("/s/two.json"), live=False)
    mocker.patch(
        "jailbee.accounts.engine.list_slots",
        return_value=[
            Slot("live@corp.com", Path("/h/c.json"), live=True),
            Slot("one@corp.com", Path("/s/one.json"), live=False),
            parked,
        ],
    )
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    pick = mocker.patch("jailbee.tui.pick_account", return_value=("claude", "two@corp.com"))
    remove = mocker.patch("jailbee.accounts.engine.remove_slot")
    result = runner.invoke(app, ["claude", "rm"], input="y\n")
    assert result.exit_code == 0, result.output
    assert [s.name for _, s in pick.call_args.args[0]] == ["one@corp.com", "two@corp.com"]
    remove.assert_called_once()
    assert remove.call_args.args[1] is parked


def test_park_warns_when_the_slot_name_says_nothing_about_the_account(repo, mocker):
    """The design requires this warning and it was never implemented, which is
    why an unidentified park was only ever noticed later from `claude ls`. It
    must say the login is intact and how to give it its real name."""
    mocker.patch(
        "jailbee.accounts.engine.park",
        return_value=PoolChange("unknown-20260828-161630", None, ["app"], [], []),
    )
    result = runner.invoke(app, ["claude", "park"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    flat = _flat(result.output)
    assert "does not say which account it holds" in flat
    assert "The login itself is intact" in flat
    assert "jailbee claude park` again" in flat


def test_use_does_not_warn_for_an_identified_park(repo, mocker):
    """The common case must stay quiet — a warning on every switch would train
    the user to ignore the one that matters."""
    mocker.patch(
        "jailbee.accounts.engine.switch",
        return_value=PoolChange("old@corp.com#aaaabbbb", "new@corp.com", ["app"], [], []),
    )
    result = runner.invoke(app, ["claude", "use", "new@corp.com"], env={"COLUMNS": "200"})
    assert result.exit_code == 0, result.output
    assert "does not say which account" not in _flat(result.output)


def test_use_reports_both_sides_of_the_switch(repo, mocker):
    mocker.patch(
        "jailbee.accounts.engine.switch",
        return_value=PoolChange(
            parked_as="old@corp.com",
            activated="new@corp.com",
            updated=["app", "other"],
            not_updated=[],
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
        "jailbee.accounts.engine.switch",
        return_value=PoolChange("old@x.com", "new@x.com", ["app"], [], ["app"]),
    )
    result = runner.invoke(app, ["claude", "use", "new@x.com"])
    assert result.exit_code == 0, result.output
    assert "session" in result.output.lower()


def test_use_warns_about_a_member_it_could_not_refresh(repo, mocker):
    mocker.patch(
        "jailbee.accounts.engine.switch",
        return_value=PoolChange("old@x.com", "new@x.com", ["app"], ["broken"], []),
    )
    result = runner.invoke(app, ["claude", "use", "new@x.com"])
    assert result.exit_code == 0, result.output
    assert "broken" in result.output


def test_use_exits_2_on_a_pool_error(repo, mocker):
    mocker.patch(
        "jailbee.accounts.engine.switch",
        side_effect=PoolError("no stored account matches `nope`."),
    )
    result = runner.invoke(app, ["claude", "use", "nope"])
    assert result.exit_code == 2
    assert "nope" in result.output


def test_use_exits_2_on_a_lock_timeout(repo, mocker):
    from jailbee.claude_locks import ClaudeLockTimeoutError

    mocker.patch(
        "jailbee.accounts.engine.switch",
        side_effect=ClaudeLockTimeoutError("/h/.oauth_refresh.lock is held"),
    )
    result = runner.invoke(app, ["claude", "use", "x@y.com"])
    assert result.exit_code == 2
    assert "oauth_refresh.lock" in result.output


def test_park_tells_the_user_how_a_new_login_gets_in(repo, mocker):
    mocker.patch(
        "jailbee.accounts.engine.park",
        return_value=PoolChange("me@corp.com", None, ["app"], [], []),
    )
    result = runner.invoke(app, ["claude", "park"])
    assert result.exit_code == 0, result.output
    assert "me@corp.com" in result.output
    assert "/login" in result.output


def test_park_of_an_empty_holder_is_not_an_error(repo, mocker):
    mocker.patch("jailbee.accounts.engine.park", return_value=PoolChange(None, None, [], [], []))
    result = runner.invoke(app, ["claude", "park"])
    assert result.exit_code == 0, result.output
    assert "Nothing to park" in result.output


def test_rm_confirms_before_deleting(repo, mocker):
    slot = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    mocker.patch("jailbee.accounts.engine.list_slots", return_value=[slot])
    remove = mocker.patch("jailbee.accounts.engine.remove_slot")

    declined = runner.invoke(app, ["claude", "rm", "old@x.com"], input="n\n")
    assert declined.exit_code == 1
    remove.assert_not_called()

    accepted = runner.invoke(app, ["claude", "rm", "old@x.com"], input="y\n")
    assert accepted.exit_code == 0, accepted.output
    remove.assert_called_once_with(CLAUDE, slot)


def test_rm_warns_that_deletion_is_permanent(repo, mocker):
    slot = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    mocker.patch("jailbee.accounts.engine.list_slots", return_value=[slot])
    mocker.patch("jailbee.accounts.engine.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "old@x.com"], input="n\n")

    assert "/login" in result.output


def test_rm_yes_skips_the_prompt(repo, mocker):
    slot = Slot("old@x.com", Path("/s/old@x.com.json"), live=False)
    mocker.patch("jailbee.accounts.engine.list_slots", return_value=[slot])
    remove = mocker.patch("jailbee.accounts.engine.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "old@x.com", "--yes"])

    assert result.exit_code == 0, result.output
    remove.assert_called_once_with(CLAUDE, slot)


def test_rm_refuses_the_live_account(repo, mocker):
    slot = Slot("me@x.com", Path("/h/.credentials.json"), live=True)
    mocker.patch("jailbee.accounts.engine.list_slots", return_value=[slot])
    remove = mocker.patch("jailbee.accounts.engine.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "me@x.com", "--yes"])

    assert result.exit_code == 2
    assert "park" in result.output
    remove.assert_not_called()


def test_use_exits_2_on_an_os_error(repo, mocker):
    """`_move_file` and `atomic_write` raise `OSError` mid-move: the one
    moment the user most needs a message rather than a stack trace."""
    mocker.patch("jailbee.accounts.engine.switch", side_effect=OSError("no space left on device"))
    result = runner.invoke(app, ["claude", "use", "x@y.com"])
    assert result.exit_code == 2
    assert "no space left" in result.output


def test_park_exits_2_on_an_os_error(repo, mocker):
    mocker.patch("jailbee.accounts.engine.park", side_effect=OSError("read-only file system"))
    result = runner.invoke(app, ["claude", "park"])
    assert result.exit_code == 2
    assert "read-only file system" in result.output


def test_park_warns_that_a_live_session_loses_its_login(repo, mocker):
    """`use` swaps one credential for another; `park` leaves the holder empty.
    The shared "the account in /status may lag" wording is a false reassurance
    in the one command that removes authentication."""
    mocker.patch(
        "jailbee.accounts.engine.park",
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
    mocker.patch("jailbee.accounts.engine.list_slots", return_value=[parked, live])
    remove = mocker.patch("jailbee.accounts.engine.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "old@x.com", "--yes"])

    assert result.exit_code == 0, result.output
    remove.assert_called_once_with(CLAUDE, parked)


def test_rm_speaks_the_shared_live_account_refusal(repo, mocker):
    """One sentence for "that is the live login", not two that can drift."""
    slot = Slot("me@x.com", Path("/h/.credentials.json"), live=True)
    mocker.patch("jailbee.accounts.engine.list_slots", return_value=[slot])
    mocker.patch("jailbee.accounts.engine.remove_slot")

    result = runner.invoke(app, ["claude", "rm", "me@x.com", "--yes"])

    assert result.exit_code == 2
    # Equality with the shared wording, not a substring both happen to
    # contain: a re-inlined literal that merely echoes one phrase from it
    # would still pass a substring check, but not this one.
    assert engine.live_account_refusal(CLAUDE, "me@x.com") in result.output


def test_ls_reads_no_login_even_on_the_row_this_repo_uses(repo, mocker):
    """Bold marks the caller's holder, and an empty one has no account to bold:
    a first smoke test rendered the literal `None` in the ACCOUNT column."""
    _built(mocker, _overview(_row(None, group="fresh", mine=True)))

    result = runner.invoke(app, ["claude", "ls"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "(no login)" in result.output
    assert "None" not in result.output


def test_ls_filters_to_the_ungrouped_holders(repo, mocker):
    """`none` spells "no credential group" everywhere else on the command line
    (`group set none`, `group use none`, `new --claude-group none`), so it has
    to mean the same here rather than being refused as a reserved word."""
    _built(
        mocker,
        _overview(
            _row("staff@corp.com", group="staff"),
            _row("me@corp.com", prefix="scratch", repos=("scratch",)),
            _row("old@corp.com", live=False),
        ),
    )

    result = runner.invoke(app, ["claude", "ls", "-g", "none"], env={"COLUMNS": "200"})

    assert result.exit_code == 0, result.output
    assert "me@corp.com" in result.output
    assert "staff@corp.com" not in result.output
    assert "old@corp.com" in result.output


class _NamedAdapter:
    """A named adapter stub: the selection helpers only read `.name`."""

    def __init__(self, name: str) -> None:
        self.name = name


def _list_slots(mocker, by_agent: dict[str, list[Slot]]):
    """Patch `engine.list_slots` to answer per adapter name."""
    return mocker.patch(
        "jailbee.accounts.engine.list_slots",
        side_effect=lambda adapter, *args, **kwargs: by_agent[adapter.name],
    )


def test_matching_choices_offers_every_parked_slot_of_every_adapter(repo, mocker):
    """No reference means every parked login of every selected adapter is on
    offer, and each choice carries the adapter that holds it."""
    from jailbee.cli import _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [
                Slot("a@x.com", Path("/s/a.json"), live=False),
                Slot("live-a@x.com", Path("/h/a.json"), live=True),
            ],
            "fakeb": [Slot("b@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = _matching_choices([fakea, fakeb], repo, GlobalConfig(), None, removable=False)

    # The live login is not a candidate: `switch` would refuse it.
    assert [(a.name, s.name) for a, s in choices] == [("fakea", "a@x.com"), ("fakeb", "b@x.com")]


def test_matching_choices_resolves_a_ref_in_one_adapter(repo, mocker):
    """A reference that matches one adapter selects it, by `resolve_ref`'s own
    exact-name-then-unique-email rule."""
    from jailbee.cli import _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [Slot("a@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("b@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = _matching_choices([fakea, fakeb], repo, GlobalConfig(), "a@x.com", removable=False)

    assert [(a.name, s.name) for a, s in choices] == [("fakea", "a@x.com")]


def test_matching_choices_keeps_a_ref_that_matches_two_adapters(repo, mocker):
    """One email parked under two agents is two successful matches, not a
    failure: the caller decides between them."""
    from jailbee.cli import _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [Slot("me@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = _matching_choices([fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False)

    assert [(a.name, s.name) for a, s in choices] == [
        ("fakea", "me@x.com"),
        ("fakeb", "me@x.com"),
    ]


def test_matching_choices_does_not_suppress_an_ambiguity_inside_one_adapter(repo, mocker):
    """Two grants of one email in one adapter are not solved by `-a`, so
    `resolve_ref`'s full-slot-name error is kept even when another adapter
    matched: silently picking the other agent could act on the wrong login."""
    from jailbee.cli import _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [
                Slot("me@x.com#aaaa", Path("/s/a1.json"), live=False),
                Slot("me@x.com#bbbb", Path("/s/a2.json"), live=False),
            ],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )

    with pytest.raises(PoolError, match="matches several accounts"):
        _matching_choices([fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False)


def test_matching_choices_raises_when_a_ref_matches_nothing(repo, mocker):
    """A typed reference with no match is an error, not an empty picker: only
    the caller knows whether the reference was typed or picked."""
    from jailbee.cli import _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    _list_slots(mocker, {"fakea": [Slot("a@x.com", Path("/s/a.json"), live=False)]})

    with pytest.raises(PoolError, match="no stored account matches"):
        _matching_choices([fakea], repo, GlobalConfig(), "nope@x.com", removable=False)


def test_account_adapters_without_an_agent_returns_every_pooled_one(repo):
    """No `-a` is normal: the command spans every enabled agent, `claude`
    first."""
    from jailbee.accounts.adapters import base
    from jailbee.cli import _account_adapters
    from tests.conftest import with_agent

    cfg = with_agent(repo, "claude", enabled=True, command="claude")
    cfg = with_agent(cfg, "fakea", enabled=True, command="fakea")
    base.register(_NamedAdapter("fakea"))
    try:
        assert [a.name for a in _account_adapters(cfg, None)] == ["claude", "fakea"]
    finally:
        base.ADAPTERS.pop("fakea", None)


def test_account_adapters_with_an_agent_returns_only_that_one(repo):
    """`-a` narrows before any store is read, so a reference is resolved
    against one adapter's slots."""
    from jailbee.accounts.adapters import base
    from jailbee.cli import _account_adapters
    from tests.conftest import with_agent

    cfg = with_agent(repo, "claude", enabled=True, command="claude")
    cfg = with_agent(cfg, "fakea", enabled=True, command="fakea")
    base.register(_NamedAdapter("fakea"))
    try:
        assert [a.name for a in _account_adapters(cfg, "fakea")] == ["fakea"]
    finally:
        base.ADAPTERS.pop("fakea", None)


def test_account_adapters_refuses_an_unknown_or_disabled_agent(repo):
    """A name this config does not enable has no pool a user can mean; the
    refusal says so instead of silently acting on every agent."""
    from jailbee.cli import _account_adapters
    from tests.conftest import with_agent

    with pytest.raises(PoolError, match="no enabled agent `nope`"):
        _account_adapters(repo, "nope")

    disabled = with_agent(repo, "fakea", enabled=False, command="fakea")
    with pytest.raises(PoolError, match="no enabled agent `fakea`"):
        _account_adapters(disabled, "fakea")


def test_a_ref_across_two_adapters_opens_the_picker_on_a_tty(repo, mocker):
    from jailbee.cli import _choose_account_choice, _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [Slot("me@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )
    choices = _matching_choices([fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False)
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    pick = mocker.patch("jailbee.tui.pick_account", return_value=("fakeb", "me@x.com"))

    choice = _choose_account_choice(choices, ref="me@x.com", nothing="none", message="Switch:")

    assert choice is not None
    assert (choice[0].name, choice[1].name) == ("fakeb", "me@x.com")
    assert pick.call_args.args[0] == choices
    assert pick.call_args.args[1] == "Switch:"


def test_a_ref_across_two_adapters_off_a_tty_names_the_agent_options(repo, mocker):
    """A script cannot answer a picker, so the failure names the `-a` values
    it should have passed."""
    from jailbee.cli import _choose_account_choice, _matching_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [Slot("me@x.com", Path("/s/a.json"), live=False)],
            "fakeb": [Slot("me@x.com", Path("/s/b.json"), live=False)],
        },
    )
    choices = _matching_choices([fakea, fakeb], repo, GlobalConfig(), "me@x.com", removable=False)
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    pick = mocker.patch("jailbee.tui.pick_account")

    with pytest.raises(PoolError) as e:
        _choose_account_choice(choices, ref="me@x.com", nothing="none", message="Switch:")

    assert "-a fakea" in str(e.value)
    assert "-a fakeb" in str(e.value)
    pick.assert_not_called()


def test_live_choices_selects_the_one_adapter_with_a_login(repo, mocker):
    """`park`'s candidates: an adapter with no live credential contributes
    nothing, and a lone login needs no prompt."""
    from jailbee.cli import _choose_account_choice, _live_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [Slot("live-a@x.com", Path("/h/a.json"), live=True)],
            "fakeb": [Slot("b@x.com", Path("/s/b.json"), live=False)],
        },
    )

    choices = _live_choices([fakea, fakeb], repo, GlobalConfig())

    assert [(a.name, s.name) for a, s in choices] == [("fakea", "live-a@x.com")]
    mocker.patch("jailbee.cli._is_tty", return_value=False)
    pick = mocker.patch("jailbee.tui.pick_account")
    choice = _choose_account_choice(choices, ref=None, nothing="nothing", message="Park:")
    assert choice is not None
    assert choice[0].name == "fakea"
    pick.assert_not_called()


def test_live_choices_picks_when_several_adapters_have_one(repo, mocker):
    from jailbee.cli import _choose_account_choice, _live_choices
    from jailbee.global_config import GlobalConfig

    fakea = _NamedAdapter("fakea")
    fakeb = _NamedAdapter("fakeb")
    _list_slots(
        mocker,
        {
            "fakea": [Slot("a@x.com", Path("/h/a.json"), live=True)],
            "fakeb": [Slot("b@x.com", Path("/h/b.json"), live=True)],
        },
    )
    choices = _live_choices([fakea, fakeb], repo, GlobalConfig())
    mocker.patch("jailbee.cli._is_tty", return_value=True)
    pick = mocker.patch("jailbee.tui.pick_account", return_value=("fakeb", "b@x.com"))

    choice = _choose_account_choice(choices, ref=None, nothing="nothing", message="Park:")

    assert choice is not None
    assert (choice[0].name, choice[1].name) == ("fakeb", "b@x.com")
    assert [s.name for _, s in pick.call_args.args[0]] == ["a@x.com", "b@x.com"]
