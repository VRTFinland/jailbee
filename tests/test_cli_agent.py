"""`jailbee agent use|show|--clear` end to end against real config layers, mocked Incus."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from jailbee import lifecycle as _lifecycle
from jailbee.cli import app
from jailbee.config.local_layer import local_config_path
from jailbee.global_config import GlobalConfig
from jailbee.orchestration.runner_model import Runner

runner = CliRunner()
_REAL_LIST_CONTAINERS = _lifecycle.list_containers  # the rig replaces the module attribute

PAYLOAD = {
    "json": {
        "version": 1,
        "default_profile": "grok",
        "profiles": {
            "grok": {"tiers": {"opus": "x", "sonnet": "y"}},
            "gpt": {"tiers": {"haiku": "z"}},
        },
    },
    "keys": {},
    "unserved": [],
}


@pytest.fixture
def rig(tmp_path, mocker, monkeypatch):
    repo_root = tmp_path / "myrepo"
    (repo_root / ".jailbee").mkdir(parents=True)
    (repo_root / ".git").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("JAILBEE_NO_UPDATE_CHECK", "1")
    monkeypatch.chdir(repo_root)
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    repo_cfg = repo_root / ".jailbee" / "config.yaml"
    repo_cfg.write_text(
        yaml.safe_dump(
            {"container_prefix": "myrepo", "agents": {"claude": {"enabled": True}}},
        )
    )
    gcfg = GlobalConfig.model_validate({"litellm": {"enabled": True}})
    mocker.patch("jailbee.cli._load_global", return_value=gcfg)
    mocker.patch("jailbee.incus.Incus", return_value=MagicMock())
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=PAYLOAD)
    sync = mocker.patch("jailbee.litellm.sync_container")
    running = MagicMock(state="Running")
    running.name = "myrepo-a"
    stopped = MagicMock(state="Stopped")
    stopped.name = "myrepo-b"
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[running, stopped])
    mocker.patch("jailbee.agent_default.read_runner", return_value=None)
    mocker.patch("jailbee.agent_default.runner_problems", side_effect=lambda cfg, i, g, r: [])
    return repo_cfg, sync


def _local():
    return yaml.safe_load(local_config_path("myrepo").read_text())


def test_use_plain_claude_writes_the_local_layer_and_resyncs_running_containers(rig):
    _, sync = rig
    result = runner.invoke(app, ["agent", "use", "claude/opus"])
    assert result.exit_code == 0, result.output
    assert _local() == {"agents": {"default_runner": {"agent": "claude", "model": "opus"}}}
    assert [c.args[1] for c in sync.call_args_list] == ["myrepo-a"]  # stopped one untouched


def test_use_profile_means_claude_jb_and_moves_the_default_profile(rig, mocker):
    payload = mocker.patch("jailbee.litellm.container_sync_payload", return_value=PAYLOAD)
    result = runner.invoke(app, ["agent", "use", "gpt/haiku"])
    assert result.exit_code == 0, result.output
    assert _local()["agents"]["default_runner"] == {
        "agent": "claude-jb",
        "profile": "gpt",
        "model": "haiku",
    }
    assert payload.call_args.kwargs["default_profile"] == "gpt"


def test_use_profile_without_model_is_the_profile_default_tier(rig):
    result = runner.invoke(app, ["agent", "use", "grok"])
    assert result.exit_code == 0, result.output
    assert _local()["agents"]["default_runner"] == {"agent": "claude-jb", "profile": "grok"}


def test_unknown_profile_names_the_candidates_and_writes_nothing(rig):
    _, sync = rig
    result = runner.invoke(app, ["agent", "use", "nope/opus"])
    assert result.exit_code == 1
    assert "claude, grok, gpt" in result.output
    assert not local_config_path("myrepo").exists()
    sync.assert_not_called()


def test_unmapped_tier_names_the_profiles_tiers(rig):
    result = runner.invoke(app, ["agent", "use", "gpt/opus"])
    assert result.exit_code == 1
    assert "haiku" in result.output and "no 'opus' tier" in result.output
    assert not local_config_path("myrepo").exists()


def test_runner_problems_refuse_the_write(rig, mocker):
    mocker.patch("jailbee.agent_default.runner_problems", return_value=["claude is off"])
    result = runner.invoke(app, ["agent", "use", "claude"])
    assert result.exit_code == 1 and "claude is off" in result.output
    assert not local_config_path("myrepo").exists()


def test_missing_argument_off_a_terminal_exits_2_naming_candidates(rig):
    result = runner.invoke(app, ["agent", "use"])
    assert result.exit_code == 2
    assert "claude" in result.output and "grok" in result.output


def test_missing_argument_on_a_terminal_asks_profile_then_tier(rig, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", side_effect=["grok", "sonnet"])
    result = runner.invoke(app, ["agent", "use"])
    assert result.exit_code == 0, result.output
    assert select.call_count == 2
    assert _local()["agents"]["default_runner"] == {
        "agent": "claude-jb",
        "profile": "grok",
        "model": "sonnet",
    }


def test_clear_removes_only_the_key_and_resyncs(rig, mocker):
    _, sync = rig
    runner.invoke(app, ["agent", "use", "gpt/haiku"])
    sync.reset_mock()
    payload = mocker.patch("jailbee.litellm.container_sync_payload", return_value=PAYLOAD)
    result = runner.invoke(app, ["agent", "use", "--clear"])
    assert result.exit_code == 0, result.output
    assert "default_runner" not in (_local() or {}).get("agents", {})
    assert payload.call_args.kwargs["default_profile"] is None
    assert sync.call_count == 1


def test_clear_drops_an_emptied_agents_mapping(rig):
    runner.invoke(app, ["agent", "use", "gpt/haiku"])
    assert runner.invoke(app, ["agent", "use", "--clear"]).exit_code == 0
    assert "agents" not in (_local() or {})


def test_show_prints_each_source(rig, mocker):
    repo_cfg, _ = rig
    out = runner.invoke(app, ["agent", "show"]).output
    assert "source: legacy" in out
    repo_cfg.write_text(
        yaml.safe_dump(
            {
                "container_prefix": "myrepo",
                "agents": {"claude": {"enabled": True}, "default_runner": {"agent": "claude"}},
            }
        )
    )
    assert "claude (source: repo)" in runner.invoke(app, ["agent", "show"]).output
    runner.invoke(app, ["agent", "use", "claude/opus"])
    assert "claude opus (source: local)" in runner.invoke(app, ["agent", "show"]).output


def test_show_lists_containers_with_a_runner_label(rig, mocker):
    mocker.patch(
        "jailbee.agent_default.read_runner",
        side_effect=lambda inc, name: (
            Runner(agent="claude", model="haiku") if name == "myrepo-a" else None
        ),
    )
    out = runner.invoke(app, ["agent", "show"]).output
    assert "container myrepo-a: claude haiku (source: label)" in out
    assert "myrepo-b" not in out


def test_clear_with_a_profile_is_a_usage_error(rig):
    assert runner.invoke(app, ["agent", "use", "--clear", "grok"]).exit_code == 2


def test_real_runner_problems_accept_the_selection(rig, mocker):
    mocker.stopall()
    import jailbee.agent_default as mod
    from jailbee.agent_default import runner_problems as real

    mocker.patch.object(mod, "runner_problems", real)
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=GlobalConfig.model_validate({"litellm": {"enabled": True}}),
    )
    mocker.patch("jailbee.incus.Incus", return_value=MagicMock())
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=PAYLOAD)
    mocker.patch("jailbee.litellm.sync_container")
    mocker.patch("jailbee.lifecycle.list_containers", return_value=[])
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    global_path = Path(os.environ["XDG_CONFIG_HOME"]) / "jailbee" / "global.yaml"
    global_path.parent.mkdir(parents=True, exist_ok=True)
    global_path.write_text(yaml.safe_dump({"litellm": {"enabled": True}}))
    result = runner.invoke(app, ["agent", "use", "grok/sonnet"])
    assert result.exit_code == 0, result.output


def test_trailing_slash_is_a_usage_error(rig):
    result = runner.invoke(app, ["agent", "use", "grok/"])
    assert result.exit_code == 1 and "PROFILE/MODEL" in result.output
    assert not local_config_path("myrepo").exists()


def test_a_served_profile_named_claude_does_not_replace_plain_claude(rig, mocker):
    payload = {"json": {"profiles": {"claude": {"tiers": {"haiku": "z"}}}}}
    mocker.patch("jailbee.litellm.container_sync_payload", return_value=payload)
    assert runner.invoke(app, ["agent", "use", "claude/opus"]).exit_code == 0


# --- per-container override (#146 ticket 10) -------------------------------


@pytest.fixture
def labels(rig, mocker):
    """The one container's runner label, held in a dict behind the mocked Incus."""
    store: dict[str, str] = {}
    incus = MagicMock()
    incus.config_get.side_effect = lambda name, key: store.get(key)
    incus.config_set.side_effect = lambda name, key, value: store.__setitem__(key, value)
    incus.config_unset_checked.side_effect = lambda name, key: store.pop(key, None)
    mocker.patch("jailbee.incus.Incus", return_value=incus)
    mocker.patch("jailbee.lifecycle.resolve_container_name", side_effect=lambda c, i, n: n)
    mocker.patch("jailbee.agent_default.read_runner", side_effect=read_runner_real(store))
    return store, incus


def read_runner_real(store):
    from jailbee.orchestration.runner import parse_runner_label

    return lambda inc, name: parse_runner_label(store.get("user.jailbee.orchestration.runner"))


LABEL = "user.jailbee.orchestration.runner"


def test_use_container_sets_the_label_not_the_repo_layer(labels):
    store, _ = labels
    result = runner.invoke(app, ["agent", "use", "grok/sonnet", "--container", "myrepo-a"])
    assert result.exit_code == 0, result.output
    assert Runner.from_label(store[LABEL]) == Runner(
        agent="claude-jb", profile="grok", model="sonnet"
    )
    assert not local_config_path("myrepo").exists()


def test_use_container_validates_like_the_repo_default(labels):
    store, _ = labels
    result = runner.invoke(app, ["agent", "use", "nope", "--container", "myrepo-a"])
    assert result.exit_code == 1 and "claude, grok, gpt" in result.output
    assert LABEL not in store


def test_clear_container_removes_the_label(labels):
    store, _ = labels
    runner.invoke(app, ["agent", "use", "claude/opus", "--container", "myrepo-a"])
    result = runner.invoke(app, ["agent", "use", "--container", "myrepo-a", "--clear"])
    assert result.exit_code == 0, result.output
    assert LABEL not in store


def test_container_picker_offers_repo_default_which_clears(labels, mocker):
    store, _ = labels
    runner.invoke(app, ["agent", "use", "claude/opus", "--container", "myrepo-a"])
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", side_effect=["(repo default)"])
    result = runner.invoke(app, ["agent", "use", "--container", "myrepo-a"])
    assert result.exit_code == 0, result.output
    assert select.call_count == 1  # no tier question after "repo default"
    assert LABEL not in store


def test_container_picker_asks_profile_then_tier(labels, mocker):
    store, _ = labels
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._select", side_effect=["grok", "sonnet"])
    result = runner.invoke(app, ["agent", "use", "--container", "myrepo-a"])
    assert result.exit_code == 0, result.output
    assert Runner.from_label(store[LABEL]).model == "sonnet"


def test_running_sessions_are_untouched(labels, rig, mocker):
    _, sync = rig
    _, incus = labels
    interrupt = mocker.patch("jailbee.tmux.interrupt_window")
    kill = mocker.patch("jailbee.tmux.kill_window")
    result = runner.invoke(app, ["agent", "use", "claude/opus", "--container", "myrepo-a"])
    assert result.exit_code == 0, result.output
    assert not incus.exec.called  # nothing is sent into the container
    assert not sync.called  # no litellm resync of the running container
    assert not interrupt.called and not kill.called  # existing windows are left alone


def test_show_marks_the_label_source(labels):
    runner.invoke(app, ["agent", "use", "claude/haiku", "--container", "myrepo-a"])
    out = runner.invoke(app, ["agent", "show"]).output
    assert "container myrepo-a: claude haiku (source: label)" in out


def test_the_label_is_what_autostart_launches(labels, mocker):
    """Autostart's own label read: the runner comes from the mocked Incus label."""
    from jailbee.autostart import AutostartTrigger, _on_start_agent_steps
    from jailbee.cli import _load_or_exit

    runner.invoke(app, ["agent", "use", "grok/sonnet", "--container", "myrepo-a"])
    store, incus = labels
    mocker.patch("jailbee.initial_prompt.prompt_pending", return_value=True)  # primary launches
    mocker.patch("jailbee.agent_resume.boot_entries", return_value=[])
    cfg = _load_or_exit(None)
    steps, _ = _on_start_agent_steps(cfg, incus, "myrepo-a", AutostartTrigger.ON_START)
    [claude] = [s for s in steps if s.name == "claude"]
    assert "claude-jb" in claude.run and "-P grok" in claude.run
    # without the label the same container gets the plain command
    store.clear()
    steps, _ = _on_start_agent_steps(cfg, incus, "myrepo-a", AutostartTrigger.ON_START)
    assert all("claude-jb" not in s.run for s in steps)


def test_open_starts_the_effective_agent_in_a_new_window(labels, mocker):
    runner.invoke(app, ["agent", "use", "grok/sonnet", "--container", "myrepo-a"])
    _, incus = labels
    incus.exec.return_value = "claude\nbash\n"  # a `claude` window already runs
    mocker.patch("jailbee.tmux.ensure_session")
    result = runner.invoke(app, ["agent", "open", "myrepo-a"])
    assert result.exit_code == 0, result.output
    assert "window claude-2" in result.output
    sent = " ".join(str(c) for c in incus.exec.call_args_list)
    assert "new-window" in sent and "claude-jb" in sent and "-P grok" in sent
    assert "-n claude-2 " in sent and "-n claude " not in sent  # the taken name is not reused
    assert sent.count("new-window") == 1 and "respawn" not in sent


def test_open_refuses_a_stopped_container(labels):
    result = runner.invoke(app, ["agent", "open", "myrepo-b"])
    assert result.exit_code == 1 and "not running" in result.output


def test_container_agent_names_the_source(labels, mocker):
    from jailbee.agent_default import container_agent
    from jailbee.cli import _load_or_exit

    _, incus = labels

    def now():
        return container_agent(_load_or_exit(None), incus, "myrepo-a")  # config read afresh

    assert now() is None  # legacy path
    runner.invoke(app, ["agent", "use", "claude/opus"])
    assert now() == ("claude/opus", "local")
    runner.invoke(app, ["agent", "use", "grok/sonnet", "--container", "myrepo-a"])
    assert now() == ("grok/sonnet", "container")  # the label outranks the local layer
    runner.invoke(app, ["agent", "use", "--container", "myrepo-a", "--clear"])
    assert now() == ("claude/opus", "local")  # cleared: the repo default shows again
    runner.invoke(app, ["agent", "use", "--clear"])
    assert now() is None


def test_open_follows_autostart_when_the_default_cannot_apply(labels, rig, mocker):
    """A `claude-jb` default with litellm off is skipped by autostart; open skips it too."""
    repo_cfg, _ = rig
    repo_cfg.write_text(
        yaml.safe_dump(
            {
                "container_prefix": "myrepo",
                "primary_agent": "codex",
                "agents": {
                    "claude": {"enabled": True},
                    "codex": {"enabled": True},
                    "default_runner": {"agent": "claude-jb", "profile": "grok", "model": "sonnet"},
                },
            }
        )
    )
    mocker.patch(
        "jailbee.cli._load_global", return_value=GlobalConfig.model_validate({})
    )  # litellm off
    _, incus = labels
    incus.exec.return_value = ""
    mocker.patch("jailbee.tmux.ensure_session")
    result = runner.invoke(app, ["agent", "open", "myrepo-a"])
    assert result.exit_code == 0, result.output
    assert "window codex of" in result.output
    sent = " ".join(str(c) for c in incus.exec.call_args_list)
    assert "claude-jb" not in sent


def test_pilot_override_then_open_then_the_card_shows_the_source(labels, mocker):
    """Set an override, open a new agent window, and the card reads the same label."""
    from jailbee.cli import _load_or_exit
    from tests.test_lifecycle import _container

    store, incus = labels
    assert (
        runner.invoke(app, ["agent", "use", "grok/sonnet", "--container", "myrepo-a"]).exit_code
        == 0
    )
    incus.exec.return_value = ""
    mocker.patch("jailbee.tmux.ensure_session")
    assert runner.invoke(app, ["agent", "open", "myrepo-a"]).exit_code == 0
    mocker.patch("jailbee.orchestration.view.queued_counts", return_value={})
    incus.list_containers.return_value = [
        _container(name="myrepo-a", user_config={LABEL: store[LABEL]})
    ]
    [card] = _REAL_LIST_CONTAINERS(_load_or_exit(None), incus)
    assert (card.agent_runner, card.agent_source) == ("grok/sonnet", "container")
