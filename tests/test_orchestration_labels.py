from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from jailbee.orchestration import labels


def test_child_env_empty_for_ordinary(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.config_get.return_value = None
    assert labels.child_env(cfg, incus, f"{cfg.container_prefix}-feat") == {}
    incus.config_get.assert_called_once_with(f"{cfg.container_prefix}-feat", labels.SPAWNED_BY)


def test_child_env_for_child(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = MagicMock()
    incus.config_get.return_value = f"{cfg.container_prefix}-orch"
    assert labels.child_env(cfg, incus, f"{cfg.container_prefix}-kid") == {
        "JAILBEE_ROLE": "child",
        "JAILBEE_ORCHESTRATOR": "orch",
    }


def _grant_incus(grant):
    incus = MagicMock()
    incus.config_get.side_effect = lambda _n, key: grant if key == labels.GRANT else None
    return incus


@pytest.mark.parametrize(
    "primary,litellm,expected",
    [
        ("claude", False, "claude"),
        ("claude", True, "claude-jb"),
        ("codex", False, "codex"),
    ],
)
def test_orchestrator_env_names_default_agent(make_cfg, tmp_path, primary, litellm, expected):
    from jailbee.config.models_litellm import LiteLLMConfig, LiteLLMRepoView

    cfg = make_cfg(
        tmp_path,
        primary_agent=primary,
        agents={"claude": {"enabled": True}, "codex": {"enabled": True}},
    )
    cfg._litellm_view = LiteLLMRepoView(config=LiteLLMConfig(enabled=litellm, autostart=litellm))
    env = labels.orchestrator_env(cfg, _grant_incus("g1"), f"{cfg.container_prefix}-orch")
    assert env == {"JAILBEE_DEFAULT_AGENT": expected}


def test_orchestrator_env_none_without_primary_agent(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path, primary_agent="auto", agents={})
    env = labels.orchestrator_env(cfg, _grant_incus("g1"), f"{cfg.container_prefix}-orch")
    assert env == {"JAILBEE_DEFAULT_AGENT": "none"}


def test_orchestrator_env_empty_without_grant(make_cfg, tmp_path):
    cfg = make_cfg(tmp_path)
    incus = _grant_incus(None)
    assert labels.orchestrator_env(cfg, incus, f"{cfg.container_prefix}-feat") == {}
    incus.config_get.assert_called_once_with(f"{cfg.container_prefix}-feat", labels.GRANT)
