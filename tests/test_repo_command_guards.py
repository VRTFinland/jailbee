"""Regression tests for repository removal target and agent guards."""

from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jailbee import repo_commands, repo_remove
from jailbee.cli import app
from jailbee.repo_remove import RepoEntry


@pytest.fixture(autouse=True)
def fake_incus(mocker):
    from jailbee.incus import Incus

    mocker.patch.object(repo_commands, "Incus", return_value=mocker.MagicMock(spec=Incus))


def _plan(prefix="demo", containers=None):
    return SimpleNamespace(
        prefix=prefix,
        cfg=None,
        containers=containers or [],
        profiles=[],
        acls=[],
        golden_images=[],
        golden_instances=[],
        golden_kept_reason=None,
        shared_kept_reason=None,
        state_counts={},
        purge_files=[],
        kept_files=[],
        global_refs=[],
    )


def test_missing_repo_config_still_probes_agents(monkeypatch):
    from jailbee.accounts import groups

    monkeypatch.setattr(
        repo_remove, "plan_removal", lambda *a, **kw: _plan(containers=["demo-one"])
    )
    monkeypatch.setattr(
        groups, "agent_running", lambda incus, container, *, command: command == "claude"
    )
    monkeypatch.setattr(repo_remove, "execute_removal", lambda *a, **kw: [])
    result = CliRunner().invoke(app, ["repo", "rm", "demo", "--yes"])
    assert result.exit_code == 2, result.output
    assert "claude: demo-one" in result.output


def test_registered_directory_uses_registry_prefix_without_config(monkeypatch, tmp_path):
    monkeypatch.setattr(
        repo_commands,
        "list_repos",
        lambda *a, **kw: [RepoEntry("demo", tmp_path, "no config", 0, None)],
    )
    planned = []

    def plan(prefix, *args, **kwargs):
        planned.append((prefix, kwargs))
        return _plan(prefix)

    monkeypatch.setattr(repo_remove, "plan_removal", plan)
    result = CliRunner().invoke(app, ["repo", "rm", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert planned == [("demo", {"purge": False})]


def test_unregistered_path_passes_loaded_config(monkeypatch, tmp_path, make_cfg):
    from jailbee import config

    cfg = make_cfg(tmp_path, container_prefix="demo")
    monkeypatch.setattr(repo_commands, "list_repos", lambda *a, **kw: [])
    monkeypatch.setattr(config, "load_repo_config", lambda root: cfg)
    planned = []
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: planned.append((a, kw)) or _plan())
    result = CliRunner().invoke(app, ["repo", "rm", str(tmp_path), "--dry-run"])
    assert result.exit_code == 0, result.output
    assert planned[0][0][0] == "demo"
    assert planned[0][1] == {"purge": False, "cfg": cfg}


def test_unregistered_path_rejects_conflicting_registered_root(monkeypatch, tmp_path, make_cfg):
    from jailbee import config

    cfg = make_cfg(tmp_path, container_prefix="demo")
    monkeypatch.setattr(config, "load_repo_config", lambda root: cfg)
    monkeypatch.setattr(
        repo_commands, "list_repos",
        lambda *a, **kw: [RepoEntry("demo", tmp_path / "other", "ok", 0, None)],
    )
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: pytest.fail("planned"))
    result = CliRunner().invoke(app, ["repo", "rm", str(tmp_path), "--dry-run"])
    assert result.exit_code == 2
    assert "does not match" in result.output


@pytest.mark.parametrize("flags", [["--dry-run"], ["--yes"], ["--yes", "--force"]])
def test_missing_config_warns_git_state_unchecked(monkeypatch, flags):
    from jailbee.accounts import groups

    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: _plan())
    monkeypatch.setattr(repo_remove, "execute_removal", lambda *a, **kw: [])
    monkeypatch.setattr(groups, "agent_running", lambda *a, **kw: False)
    result = CliRunner().invoke(app, ["repo", "rm", "demo", *flags])
    assert result.exit_code == 0, result.output
    assert "container git state was not checked" in result.output


def test_plan_displays_shared_kept_reason(monkeypatch):
    plan = _plan()
    plan.shared_kept_reason = "shared with other"
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    result = CliRunner().invoke(app, ["repo", "rm", "demo", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Shared files kept: shared with other" in result.output


def test_loaded_config_running_agents_block_removal(monkeypatch, mocker, tmp_path, make_cfg):
    import typer

    cfg = make_cfg(tmp_path, container_prefix="demo")
    plan = _plan(containers=["demo-one"])
    plan.cfg = cfg
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    execute = mocker.patch.object(repo_remove, "execute_removal", return_value=[])
    running = mocker.Mock(return_value=["demo-one"])
    with pytest.raises(typer.Exit) as exc:
        repo_commands.remove_command("demo", yes=True, dry_run=False, force=False, purge=False,
                                     running_agents=running, warn_before_destroy=mocker.Mock())
    assert exc.value.exit_code == 2
    running.assert_called_once_with(cfg, mocker.ANY, ["demo-one"])
    execute.assert_not_called()


def test_loaded_config_guards_and_confirmation(monkeypatch, mocker, tmp_path, make_cfg):
    from jailbee import lifecycle, prompting

    cfg = make_cfg(tmp_path, container_prefix="demo")
    plan = _plan(containers=["demo-one"])
    plan.cfg = cfg
    info = SimpleNamespace(name="demo-one")
    other = SimpleNamespace(name="other")
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    execute = mocker.patch.object(repo_remove, "execute_removal", return_value=[])
    monkeypatch.setattr(lifecycle, "list_containers", lambda *a, **kw: [info, other])
    monkeypatch.setattr(prompting, "is_interactive", lambda: True)
    running = mocker.Mock(return_value=[])
    confirm = mocker.Mock(return_value=True)
    repo_commands.remove_command("demo", yes=False, dry_run=False, force=False, purge=False,
                                 running_agents=running, warn_before_destroy=confirm)
    running.assert_called_once_with(cfg, mocker.ANY, ["demo-one"])
    confirm.assert_called_once_with(cfg, [info], question="Remove repository demo?")
    execute.assert_called_once()


def test_yes_reports_loaded_config_git_risks(monkeypatch, mocker, tmp_path, make_cfg):
    from jailbee import destroy_guard, lifecycle, tui

    cfg = make_cfg(tmp_path, container_prefix="demo")
    plan = _plan(containers=["demo-one"])
    plan.cfg = cfg
    info = SimpleNamespace(name="demo-one", display_name="one")
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    execute = mocker.patch.object(repo_remove, "execute_removal", return_value=[])
    monkeypatch.setattr(lifecycle, "list_containers", lambda *a, **kw: [info])
    monkeypatch.setattr(destroy_guard, "status_is_unknown", lambda info: True)
    monkeypatch.setattr(destroy_guard, "assess", lambda cfg, info: SimpleNamespace(line="uncommitted work"))
    warn = mocker.patch.object(tui, "warn")
    running = mocker.Mock(return_value=[])
    confirm = mocker.Mock()
    repo_commands.remove_command("demo", yes=True, dry_run=False, force=False, purge=False,
                                 running_agents=running, warn_before_destroy=confirm)
    assert [call.args[0] for call in warn.call_args_list] == ["Git status unknown: one", "uncommitted work"]
    running.assert_called_once()
    confirm.assert_not_called()
    execute.assert_called_once()
