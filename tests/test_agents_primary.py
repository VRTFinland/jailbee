from jailbee.agents import primary_agent
from tests.conftest import make_cfg

ON = {"enabled": True}
AUTO = {"enabled": True, "autostart": True}


def test_auto_prefers_autostart_pool(tmp_path):
    assert primary_agent(make_cfg(tmp_path, agents={"claude": ON, "codex": AUTO})).name == "codex"


def test_auto_prefers_claude_within_pool(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": AUTO, "codex": AUTO})
    assert primary_agent(cfg).name == "claude"


def test_auto_orders_others_by_name(tmp_path):
    assert primary_agent(make_cfg(tmp_path, agents={"opencode": ON, "codex": ON})).name == "codex"


def test_auto_nothing_enabled_is_none_without_problem(tmp_path):
    p = primary_agent(make_cfg(tmp_path))
    assert p.name is None and p.problem is None


def test_explicit_enabled_agent_wins_over_autostart(tmp_path):
    cfg = make_cfg(tmp_path, agents={"claude": AUTO, "codex": ON}, primary_agent="codex")
    assert primary_agent(cfg).name == "codex"


def test_explicit_disabled_agent_is_a_problem(tmp_path):
    p = primary_agent(make_cfg(tmp_path, agents={"claude": AUTO}, primary_agent="codex"))
    assert p.name is None
    assert "codex" in p.problem and "enabled" in p.problem


def test_explicit_unknown_agent_is_a_problem(tmp_path):
    p = primary_agent(make_cfg(tmp_path, primary_agent="nosuch"))
    assert p.name is None and "nosuch" in p.problem


def test_primary_agent_loads_from_repo_yaml(tmp_path, mocker):
    from jailbee.config import load_config

    mocker.patch("jailbee.config.loader.detect_default_branch", return_value="main")
    repo = tmp_path / "r"
    (repo / ".jailbee").mkdir(parents=True)
    (repo / ".git").mkdir()
    cfg_path = repo / ".jailbee" / "config.yaml"
    cfg_path.write_text("primary_agent: codex\nagents:\n  codex:\n    enabled: true\n")

    cfg = load_config(cfg_path)

    assert cfg.primary_agent == "codex"
    assert primary_agent(cfg).name == "codex"
