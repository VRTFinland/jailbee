"""Regression tests for repository removal target and agent guards."""

from types import SimpleNamespace

from typer.testing import CliRunner

from jailbee import repo_commands, repo_remove
from jailbee.cli import app
from jailbee.repo_remove import RepoEntry


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
