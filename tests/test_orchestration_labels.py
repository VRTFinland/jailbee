from __future__ import annotations

from unittest.mock import MagicMock

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
