"""Repository management command tests."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.repo_remove import RepoEntry


def test_repo_ls_json(monkeypatch):
    import json

    from jailbee import repo_remove

    monkeypatch.setattr(
        repo_remove,
        "list_repos",
        lambda *args, **kwargs: [RepoEntry("demo", None, "leftover", 2, None)],
    )
    result = CliRunner().invoke(app, ["repo", "ls", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {"prefix": "demo", "path": None, "state": "leftover", "containers": 2, "base": None}
    ]


def test_repo_rm_dry_run_skips_guards(monkeypatch):
    from types import SimpleNamespace

    from jailbee import repo_remove

    plan = SimpleNamespace(
        prefix="demo",
        cfg=None,
        containers=["demo-one"],
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
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan, raising=False)
    result = CliRunner().invoke(app, ["repo", "rm", "demo", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "demo-one" in result.stdout


def test_force_does_not_skip_confirmation(monkeypatch):
    from types import SimpleNamespace

    from jailbee import repo_remove

    plan = SimpleNamespace(
        prefix="demo",
        cfg=None,
        containers=[],
        profiles=[],
        acls=[],
        golden_images=[],
        golden_instances=[],
        golden_kept_reason="shared",
        state_counts={},
        purge_files=[],
        kept_files=[],
        global_refs=[],
    )
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    result = CliRunner().invoke(app, ["repo", "rm", "demo", "--force"])
    assert result.exit_code == 2
    assert "--yes" in result.output
    assert "shared" in result.output


def test_execution_failure_returns_one(monkeypatch):
    from types import SimpleNamespace

    from jailbee import repo_remove

    plan = SimpleNamespace(
        prefix="demo",
        cfg=None,
        containers=[],
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
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    monkeypatch.setattr(
        repo_remove,
        "execute_removal",
        lambda *a, **kw: [SimpleNamespace(label="profile", ok=False, detail="in use")],
        raising=False,
    )
    result = CliRunner().invoke(app, ["repo", "rm", "demo", "--force", "--yes"])
    assert result.exit_code == 1, result.output
    assert "in use" in result.output


def test_omitted_target_requires_destructive_picker_even_with_yes(monkeypatch):
    from jailbee import repo_commands

    monkeypatch.setattr(
        repo_commands, "list_repos", lambda *a, **kw: [RepoEntry("demo", None, "leftover", 0, None)]
    )
    result = CliRunner().invoke(app, ["repo", "rm", "--yes"])
    assert result.exit_code == 2
    assert "demo" in result.output


def test_prefix_target_never_loads_cwd_config(monkeypatch):
    from types import SimpleNamespace

    from jailbee import config, repo_remove

    plan = SimpleNamespace(
        prefix="demo",
        cfg=None,
        containers=[],
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
    monkeypatch.setattr(config, "load_repo_config", lambda *a: pytest.fail("cwd config loaded"))
    monkeypatch.setattr(repo_remove, "plan_removal", lambda *a, **kw: plan)
    result = CliRunner().invoke(app, ["repo", "rm", "demo", "--dry-run"])
    assert result.exit_code == 0, result.output
