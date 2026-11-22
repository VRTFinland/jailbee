"""The repo's agent picker: shared choice list and label, the repo row and the repo menu."""

from __future__ import annotations

import pytest

from jailbee.config.models_remote import RemoteSSHConfig
from jailbee.dashboard import agent_choice as ac
from jailbee.dashboard import model as dmodel
from jailbee.dashboard.tui import fleet
from jailbee.dashboard.tui import menu_state as tmenu
from jailbee.dashboard.tui import session as tsession
from jailbee.orchestration.runner_model import Runner
from tests.dashboard_fixtures import cfg_group, repo_menu_verbs
from tests.dashboard_pilot import bare_session, drive, repo_menu_keys

OFFERED = {
    "claude": ("fable", "opus", "sonnet", "haiku"),
    "grok": ("opus", "sonnet"),
    "codex": (),
}


@pytest.mark.parametrize(
    ("runner", "label"),
    [
        (Runner(agent="claude", model="opus"), "claude/opus"),
        (Runner(agent="claude"), "claude"),
        (Runner(agent="claude-jb", profile="codex", model="sonnet"), "codex/sonnet"),
        (Runner(agent="claude-jb", profile="codex"), "codex"),
    ],
)
def test_the_label_is_profile_slash_model(runner, label):
    assert ac.runner_label(runner) == label


def test_profile_choices_come_from_the_offered_catalog_with_the_current_marked():
    options = ac.profile_options(OFFERED, "grok")
    assert [o.value for o in options] == ["claude", "grok", "codex"]
    assert [o.title for o in options] == ["claude", "grok (current)", "codex"]


def test_unserved_profiles_are_absent_because_the_catalog_omits_them():
    assert "gpt" not in [o.value for o in ac.profile_options(OFFERED, None)]


def test_tier_choices_are_default_plus_only_the_mapped_tiers():
    options = ac.tier_options(OFFERED["grok"], "sonnet")
    assert [o.value for o in options] == ["", "opus", "sonnet"]
    assert [o.title for o in options] == ["profile default", "opus", "sonnet (current)"]


def test_the_default_tier_is_marked_when_no_tier_is_set():
    options = ac.tier_options(OFFERED["grok"], None)
    assert options[0].title == "profile default (current)"


def test_split_current_reads_profile_and_tier():
    assert ac.split_current(Runner(agent="claude", model="opus")) == ("claude", "opus")
    assert ac.split_current(Runner(agent="claude-jb", profile="grok")) == ("grok", None)
    assert ac.split_current(None) == (None, None)


def test_the_repo_row_shows_the_effective_selection():
    group = dmodel.RepoGroup("alpha", "/r", None, [], agent_label="codex/sonnet")
    assert "codex/sonnet" in fleet.repo_heading(group, None, frozenset()).plain
    plain = dmodel.RepoGroup("alpha", "/r", None, [])
    assert "codex" not in fleet.repo_heading(plain, None, frozenset()).plain


def test_the_repo_menu_offers_agent_and_it_runs_agent_use(mocker, tmp_path):
    group = cfg_group(tmp_path)
    mocker.patch.object(tsession.subprocess, "run").return_value.returncode = 0
    session, terminal = bare_session(mocker, [group])
    session.overlay = tmenu.open_repo_menu([group], "alpha", frozenset())
    assert ("Agent…", "agent") in session.overlay.actions
    session.menu_chosen("agent", None, 0)
    (ran,) = terminal.ran  # the action runner: the picker's questions are prompt-channel modals
    assert ran.argv[:2] == ["agent", "use"]


def test_the_agent_entry_shows_over_ssh_under_the_default_policy(tmp_path):
    # The real policy, not a mock: a default restricted session may run `agent use`.
    menu = tmenu.open_repo_menu(
        [cfg_group(tmp_path)],
        "alpha",
        frozenset(),
        ssh_policy=RemoteSSHConfig(),
        transport="ssh",
    )
    assert "agent" in repo_menu_verbs(menu)


def test_pilot_repo_menu_agent_runs_agent_use_and_the_row_label_follows(mocker, tmp_path):
    group = cfg_group(tmp_path)
    child = mocker.patch.object(tsession.subprocess, "run")
    child.return_value.returncode = 0

    run = drive(mocker, repo_menu_keys(group, "agent"), [group])
    assert run.rc == 0

    child.assert_called_once_with(
        ["jailbee", "agent", "use", "--config", str(group.config_path)], check=False, cwd=tmp_path
    )
    # The next gather carries the new choice; the heading shows it.
    after = cfg_group(tmp_path)
    after.agent_label = "grok/sonnet"
    assert "grok/sonnet" in fleet.repo_heading(after, None, frozenset()).plain


def test_choose_marks_the_current_choice_and_starts_the_cursor_on_it(mocker):
    from unittest.mock import MagicMock

    from jailbee import agent_default

    cfg = MagicMock()
    cfg.orchestration_view.return_value.default_runner = Runner(
        agent="claude-jb", profile="grok", model="sonnet"
    )
    mocker.patch.object(agent_default, "catalog", return_value=OFFERED)
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", side_effect=["grok", "opus"])
    chosen = agent_default.choose(cfg, MagicMock(), MagicMock(), None)
    assert chosen == Runner(agent="claude-jb", profile="grok", model="opus")
    profile_call, tier_call = select.call_args_list
    assert [o.title for o in profile_call.args[1]] == ["claude", "grok (current)", "codex"]
    assert profile_call.kwargs["default"] == "grok"
    assert [o.title for o in tier_call.args[1]] == [
        "profile default",
        "opus",
        "sonnet (current)",
    ]
    assert tier_call.kwargs["default"] == "sonnet"


def test_agent_label_follows_the_runner_then_the_legacy_launch(mocker):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    cfg = MagicMock()
    runner = Runner(agent="claude-jb", profile="codex", model="sonnet")
    assert dmodel._agent_label(cfg, runner) == "codex/sonnet"

    mocker.patch("jailbee.autostart.litellm_autostart_on", return_value=True)
    cfg.litellm_view.return_value.config.default_profile = "grok"
    assert dmodel._agent_label(cfg, None) == "(grok)"

    mocker.patch("jailbee.autostart.litellm_autostart_on", return_value=False)
    mocker.patch(
        "jailbee.agents.primary_agent", return_value=SimpleNamespace(name="claude", problem=None)
    )
    assert dmodel._agent_label(cfg, None) == "(claude)"


def test_gather_labels_the_repo_from_the_local_layer_runner(tmp_path, mocker, monkeypatch):
    """`agent use` writes the local layer; the next gather's row carries the new label."""
    import yaml

    from jailbee import agent_default
    from jailbee.config.loader import load_repo_config

    repo = tmp_path / "myrepo"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("JAILBEE_NO_UPDATE_CHECK", "1")
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    (repo / ".jailbee" / "config.yaml").write_text(
        yaml.safe_dump({"container_prefix": "myrepo", "agents": {"claude": {"enabled": True}}})
    )
    mocker.patch.object(dmodel, "list_containers", return_value=[])

    def gather() -> str | None:
        groups = dmodel.gather_rows(mocker.MagicMock(), [repo], with_git=False)
        return groups[0].agent_label

    agent_default.write(
        load_repo_config(repo), Runner(agent="claude-jb", profile="grok", model="sonnet")
    )
    assert gather() == "grok/sonnet"
    agent_default.write(load_repo_config(repo), None)
    assert gather() != "grok/sonnet"


def _running(tmp_path, **fields):
    import dataclasses

    from tests.dashboard_fixtures import ci

    return cfg_group(tmp_path, (dataclasses.replace(ci("alpha-x", "alpha"), **fields),))


def test_the_container_menu_offers_agent_and_a_new_window_when_running(mocker, tmp_path):
    group = _running(tmp_path)
    mocker.patch.object(tsession.subprocess, "run").return_value.returncode = 0
    session, terminal = bare_session(mocker, [group])
    session.open_container_entry("alpha-x", "container-agent")
    session.open_container_entry("alpha-x", "agent-window")
    assert [r.argv[:4] for r in terminal.ran] == [
        ["agent", "use", "--container", "alpha-x"],
        ["agent", "open", "alpha-x", "--config"],
    ]


@pytest.mark.parametrize(
    ("state", "verbs"),
    [
        ("Running", {"container-agent", "agent-window"}),
        ("Stopped", {"container-agent"}),
        ("—", set()),
    ],
)
def test_container_agent_entries_follow_the_state(tmp_path, state, verbs):
    menu = tmenu.open_menu([_running(tmp_path, state=state)], "alpha-x")
    found = {v for _, v in menu.actions} if menu else set()
    assert found & {"container-agent", "agent-window"} == verbs


def test_container_agent_entries_are_allowed_over_ssh_by_default(tmp_path):
    menu = tmenu.open_menu(
        [_running(tmp_path)], "alpha-x", ssh_policy=RemoteSSHConfig(), transport="ssh"
    )
    assert "container-agent" in {v for _, v in menu.actions}


def test_the_container_picker_has_a_repo_default_row_current_when_no_label():
    options = ac.profile_options(OFFERED, None, repo_default=True)
    assert options[0].value == ac.REPO_DEFAULT
    assert options[0].title == "repo default (current)"
    marked = ac.profile_options(OFFERED, "grok", repo_default=True)
    assert marked[0].title == "repo default"
    assert ac.profile_options(OFFERED, None)[0].value == "claude"
