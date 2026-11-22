"""`jailbee agent use|show|--clear` end to end against real config layers, mocked Incus."""

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config.local_layer import local_config_path
from jailbee.global_config import GlobalConfig
from jailbee.orchestration.runner_model import Runner

runner = CliRunner()

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
    mocker.patch(
        "jailbee.agent_default.runner_problems", side_effect=lambda cfg, i, g, r: []
    )
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
        side_effect=lambda inc, name: Runner(agent="claude", model="haiku")
        if name == "myrepo-a"
        else None,
    )
    out = runner.invoke(app, ["agent", "show"]).output
    assert "container myrepo-a: claude haiku (source: label)" in out
    assert "myrepo-b" not in out


def test_clear_with_a_profile_is_a_usage_error(rig):
    assert runner.invoke(app, ["agent", "use", "--clear", "grok"]).exit_code == 2


def test_real_runner_problems_accept_the_selection(rig, mocker):
    mocker.stopall()
    from jailbee.agent_default import runner_problems as real

    import jailbee.agent_default as mod

    mocker.patch.object(mod, "runner_problems", real)
    mocker.patch("jailbee.cli._load_global", return_value=GlobalConfig.model_validate({"litellm": {"enabled": True}}))
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
