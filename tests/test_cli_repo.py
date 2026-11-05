"""Repository management command tests."""

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.repo_remove import RepoEntry


@pytest.fixture(autouse=True)
def fake_incus(monkeypatch):
    from unittest.mock import MagicMock

    from jailbee import repo_commands
    from jailbee.incus import Incus

    monkeypatch.setattr(repo_commands, "Incus", lambda: MagicMock(spec=Incus))


def test_repo_ls_json(monkeypatch):
    import json

    from jailbee import repo_commands

    monkeypatch.setattr(
        repo_commands,
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


@pytest.fixture
def repo_cli_env(mocker, db_engine, tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from jailbee.global_config import GlobalConfig
    from jailbee.incus import Incus

    from jailbee import repo_remove

    monkeypatch.setenv("HOME", str(tmp_path))
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    mocker.patch("jailbee.repo_commands.list_repos", repo_remove.list_repos)
    incus = MagicMock(spec=Incus)
    incus.profile_list.return_value = []
    incus.network_acl_list.return_value = []
    incus.list_containers.return_value = []
    incus.list_images.return_value = []
    mocker.patch("jailbee.repo_commands.Incus", return_value=incus)
    mocker.patch("jailbee.repo_commands.get_engine", return_value=db_engine)
    mocker.patch("jailbee.repo_remove.load_global_config", return_value=(GlobalConfig(), []))
    return incus


def _register(db_engine, prefix, root, *, synthetic=False):
    from datetime import UTC, datetime

    from sqlmodel import Session

    from jailbee.db.models import RegisteredRepo

    with Session(db_engine) as session:
        session.add(
            RegisteredRepo(
                container_prefix=prefix,
                repo_root=str(root),
                registered_at=datetime.now(UTC),
                synthetic_config=synthetic,
            )
        )
        session.commit()


def test_ls_json_real_inventory(repo_cli_env, db_engine, tmp_path, mocker, make_cfg):
    import json

    root = tmp_path / "registered"
    root.mkdir()
    _register(db_engine, "registered", root, synthetic=True)
    _register(db_engine, "gone", tmp_path / "gone")
    cfg = make_cfg(root, container_prefix="registered", golden={"alias": "custom"})
    mocker.patch("jailbee.repo_remove.load_repo_config", return_value=cfg)
    repo_cli_env.profile_list.return_value = ["registered-base", "orphan-base"]
    repo_cli_env.list_containers.return_value = [
        {"name": "orphan-one", "profiles": ["orphan-base"]}
    ]
    repo_cli_env.list_images.return_value = [{"aliases": [{"name": "custom"}], "size": 2048}]
    result = CliRunner().invoke(app, ["repo", "ls", "--format", "json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout) == [
        {
            "prefix": "gone", "path": str(tmp_path / "gone"), "state": "dir missing",
            "containers": 0, "base": None,
        },
        {"prefix": "orphan", "path": None, "state": "leftover", "containers": 1, "base": None},
        {
            "prefix": "registered", "path": str(root), "state": "ok",
            "containers": 0, "base": "2.0 KB",
        },
    ]


@pytest.mark.parametrize("registered", [True, False], ids=["missing-config", "leftover"])
def test_dry_run_real_plan_never_mutates_incus(repo_cli_env, db_engine, tmp_path, mocker, registered):
    if registered:
        _register(db_engine, "target", tmp_path / "missing")
    repo_cli_env.profile_list.return_value = ["target-base", "target-binds"]
    repo_cli_env.network_acl_list.return_value = ["target-allowlist"]
    repo_cli_env.list_containers.return_value = [
        {"name": "target-one", "profiles": ["target-base"]}
    ]
    repo_cli_env.list_images.return_value = [{"aliases": [{"name": "target-base"}]}]
    from sqlmodel import Session

    from jailbee import repo_remove
    from jailbee.db.models import RegisteredRepo

    mocker.patch("jailbee.config.load_repo_config", side_effect=AssertionError("cwd config loaded"))
    execute = mocker.spy(repo_remove, "execute_removal")
    result = CliRunner().invoke(app, ["repo", "rm", "target", "--dry-run"])
    assert result.exit_code == 0, result.output
    for item in ("target-one", "target-base", "target-binds", "target-allowlist"):
        assert item in result.output
    execute.assert_not_called()
    with Session(db_engine) as session:
        assert (session.get(RegisteredRepo, "target") is not None) is registered
    assert {call[0] for call in repo_cli_env.method_calls} <= {
        "profile_list", "network_acl_list", "list_containers", "list_images"
    }


def test_dry_run_real_plan_preserves_shared_alias(repo_cli_env, db_engine, tmp_path, mocker, make_cfg):
    configs = {}
    for name in ("target", "other"):
        root = tmp_path / name
        root.mkdir()
        _register(db_engine, name, root, synthetic=True)
        configs[root] = make_cfg(root, container_prefix=name, golden={"alias": "shared"})
    mocker.patch("jailbee.repo_remove.load_repo_config", side_effect=lambda root: configs[root])
    repo_cli_env.profile_list.return_value = ["target-base"]
    repo_cli_env.list_containers.return_value = [
        {"name": "shared--golden", "profiles": ["target-base"]}
    ]
    result = CliRunner().invoke(app, ["repo", "rm", "target", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "Golden kept: shared with other" in result.output
    assert "Golden instances: -" in result.output
    assert "Containers: -" in result.output
    assert {call[0] for call in repo_cli_env.method_calls} <= {
        "profile_list", "network_acl_list", "list_containers", "list_images"
    }
