"""Model guidance through config loading and the container command boundary."""

import json

import pytest
from typer.testing import CliRunner

# ruff: noqa: F811 - fixtures imported from the sibling test module
from jailbee.config import ConfigError, load_config_from_layers
from jailbee.container_cli import container_app
from tests.test_inbox_socket_cli import FakeBroker, broker, sockdir  # noqa: F401 - shared fixtures


def test_model_map_layers_override_one_field_without_losing_other_models(tmp_path):
    cfg = load_config_from_layers(
        {
            "agents": {
                "models": {"haiku": {"label": "cheap", "notes": "local alias"}, "opus": {}},
                "model_policy": "Prefer cheap models.",
            }
        },
        {
            "agents": {
                "models": {
                    "haiku": {"label": "coding", "use_for": "small fixes", "avoid_for": "reviews"}
                }
            }
        },
        tmp_path / "repo" / ".jailbee" / "config.yaml",
        origin="repo",
        local_raw={},
        skip_git=True,
    )
    assert cfg.agent_models().model_dump() == {
        "models": {
            "haiku": {
                "label": "coding",
                "use_for": "small fixes",
                "avoid_for": "reviews",
                "notes": "local alias",
            },
            "opus": {"label": "", "use_for": "", "avoid_for": "", "notes": ""},
        },
        "model_policy": "Prefer cheap models.",
    }
    assert "models" not in cfg.agents
    assert "model_policy" not in cfg.agents


@pytest.mark.parametrize(
    "agents", [{"models": {"haiku": {"unknown": "no"}}}, {"models": []}, {"model_policy": 12}]
)
def test_invalid_model_guidance_is_rejected(tmp_path, agents):
    with pytest.raises(ConfigError):
        load_config_from_layers(
            {},
            {"agents": agents},
            tmp_path / "repo" / ".jailbee" / "config.yaml",
            origin="repo",
            local_raw={},
            skip_git=True,
        )


@pytest.mark.parametrize("as_json", [False, True])
@pytest.mark.parametrize("empty", [False, True])
def test_container_model_guidance_output(broker: FakeBroker, sockdir, monkeypatch, as_json, empty):
    monkeypatch.setenv("JAILBEE_ORCHESTRATION_SOCKET", str(sockdir / "broker.sock"))
    guidance = {
        "models": {}
        if empty
        else {
            "haiku": {
                "label": "cheap",
                "use_for": "small fixes",
                "avoid_for": "reviews",
                "notes": "local alias",
            }
        },
        "model_policy": "Prefer cheap models.",
    }
    broker.on("agent.models", guidance)
    result = CliRunner().invoke(
        container_app, ["agent", "models", *(["--json"] if as_json else [])]
    )
    assert result.exit_code == 0, result.output
    if as_json:
        assert json.loads(result.output) == guidance
    else:
        assert "Prefer cheap models." in result.output
        if empty:
            assert "~/.config/jailbee/global.yaml" in result.output
            assert "agents.models" in result.output
        else:
            for text in ("haiku", "cheap", "small fixes", "reviews", "local alias"):
                assert text in result.output
    assert [(r.op, r.args) for r in broker.requests] == [("agent.models", {})]


def test_model_guidance_defaults_are_opinion_free(tmp_path):
    cfg = load_config_from_layers(
        {},
        {},
        tmp_path / "repo" / ".jailbee" / "config.yaml",
        origin="repo",
        local_raw={},
        skip_git=True,
    )
    assert cfg.agent_models().model_dump() == {"models": {}, "model_policy": ""}
