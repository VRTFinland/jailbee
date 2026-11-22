"""`agents.default_runner`: the repo's default runner for every container."""

from pathlib import Path

import pytest
import yaml

from jailbee.config import ConfigError, load_config
from jailbee.config.local_layer import local_config_path
from jailbee.orchestration.runner_model import Runner


def _write(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


@pytest.fixture
def layers(tmp_path, mocker, monkeypatch):
    repo_root = tmp_path / "myrepo"
    (repo_root / ".jailbee").mkdir(parents=True)
    (repo_root / ".git").mkdir()
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    return repo_root / ".jailbee" / "config.yaml", home / ".config" / "jailbee" / "global.yaml"


def _runner(model: str, agent: str = "claude") -> dict:
    return {"agent": agent, "model": model}


def _view(repo_path):
    return load_config(repo_path).orchestration_view()


def test_agents_default_runner_in_repo_layer(layers):
    repo_path, _ = layers
    _write(repo_path, {"agents": {"default_runner": _runner("haiku")}})
    cfg = load_config(repo_path)
    view = cfg.orchestration_view()
    assert view.default_runner == Runner(agent="claude", model="haiku")
    assert view.default_runner_source == "repo"
    # The key is not an agent: it never reaches the agents mapping.
    assert "default_runner" not in cfg.agents


def test_agents_default_runner_layers_local_over_repo_over_global(layers):
    repo_path, global_path = layers
    _write(global_path, {"agents": {"default_runner": _runner("sonnet")}})
    _write(repo_path, {"container_prefix": "myrepo"})
    assert _view(repo_path).default_runner_source == "global"
    _write(
        repo_path, {"container_prefix": "myrepo", "agents": {"default_runner": _runner("haiku")}}
    )
    assert _view(repo_path).default_runner == Runner(agent="claude", model="haiku")
    _write(local_config_path("myrepo"), {"agents": {"default_runner": _runner("opus")}})
    view = _view(repo_path)
    assert view.default_runner == Runner(agent="claude", model="opus")
    assert view.default_runner_source == "local"


def test_orchestration_default_runner_is_an_accepted_alias(layers):
    repo_path, _ = layers
    _write(repo_path, {"orchestration": {"default_runner": _runner("haiku")}})
    view = _view(repo_path)
    assert view.default_runner == Runner(agent="claude", model="haiku")
    assert view.default_runner_source == "repo"


def test_agents_key_beats_the_alias_within_a_layer(layers):
    repo_path, _ = layers
    _write(
        repo_path,
        {
            "agents": {"default_runner": _runner("opus")},
            "orchestration": {"default_runner": _runner("haiku")},
        },
    )
    assert _view(repo_path).default_runner == Runner(agent="claude", model="opus")


def test_a_higher_layer_alias_beats_a_lower_layer_agents_key(layers):
    repo_path, _ = layers
    _write(repo_path, {"container_prefix": "myrepo", "agents": {"default_runner": _runner("opus")}})
    _write(local_config_path("myrepo"), {"orchestration": {"default_runner": _runner("haiku")}})
    view = _view(repo_path)
    assert view.default_runner == Runner(agent="claude", model="haiku")
    assert view.default_runner_source == "local"


def test_invalid_agents_default_runner_is_a_config_error(layers):
    repo_path, _ = layers
    _write(repo_path, {"agents": {"default_runner": {"agent": "claude", "model": "gpt5"}}})
    with pytest.raises(ConfigError, match="default_runner"):
        load_config(repo_path)
