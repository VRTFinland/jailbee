"""Tests for the `jailbee orchestrate` command group."""

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.orchestration.commands import GrantFlags, GrantRow
from jailbee.orchestration.grant import Grant, Grantable, GrantError

runner = CliRunner()


@pytest.fixture
def patched(tmp_path, mocker, make_cfg):
    """A loaded config, a stubbed container resolver and stubbed command logic."""
    cfg = make_cfg(tmp_path / "app")
    mocker.patch("jailbee.cli._load_or_exit", return_value=cfg)
    incus = mocker.MagicMock()
    resolve = mocker.patch(
        "jailbee.cli._resolve_existing", return_value=(incus, f"{cfg.container_prefix}-a")
    )
    return SimpleNamespace(
        cfg=cfg,
        incus=incus,
        resolve=resolve,
        grant=mocker.patch("jailbee.orchestration.commands.grant", return_value=Grant()),
        revoke=mocker.patch("jailbee.orchestration.commands.revoke", return_value=True),
        show=mocker.patch(
            "jailbee.orchestration.commands.show",
            return_value=GrantRow("a", "myrepo-a", None, None, 0, None),
        ),
        list_grants=mocker.patch("jailbee.orchestration.commands.list_grants", return_value=[]),
    )


def test_grant_passes_flags(patched):
    res = runner.invoke(
        app,
        [
            "orchestrate",
            "grant",
            "a",
            "--children",
            "3",
            "--allow",
            "destroy",
            "--mount",
            "data",
            "--no-loose",
        ],
    )
    assert res.exit_code == 0, res.output
    flags = patched.grant.call_args.args[3]
    assert flags == GrantFlags(children=3, allow=("destroy",), mounts=("data",), loose=False)


def test_grant_clear_flags(patched):
    runner.invoke(app, ["orchestrate", "grant", "a", "--clear-mounts", "--clear-egress"])
    f = patched.grant.call_args.args[3]
    assert (f.clear_mounts, f.clear_egress, f.mounts, f.egress) == (True, True, None, None)


def test_grant_without_mount_keeps_mounts(patched):
    runner.invoke(app, ["orchestrate", "grant", "a"])
    f = patched.grant.call_args.args[3]
    assert f.mounts is None and f.egress is None and f.loose is None


def test_grant_error_exits_1(patched):
    patched.grant.side_effect = GrantError("nope")
    res = runner.invoke(app, ["orchestrate", "grant", "a"])
    assert res.exit_code == 1 and "nope" in res.output


def test_grant_prints_summary(patched):
    patched.grant.return_value = Grant(
        max_running_children=2,
        grantable=Grantable(mounts=("data",), loose=True, egress=("example.com",)),
    )
    res = runner.invoke(app, ["orchestrate", "grant", "a"])
    assert res.exit_code == 0, res.output
    out = " ".join(res.output.split())
    assert "Granted 'a': 2 running children" in out
    assert "mounts data; loose yes; egress example.com" in out


def test_revoke_uses_destructive_picker(patched):
    runner.invoke(app, ["orchestrate", "revoke"])
    assert patched.resolve.call_args.kwargs.get("always_prompt") is True


def test_revoke_reports_both_outcomes(patched):
    res = runner.invoke(app, ["orchestrate", "revoke", "a"])
    assert res.exit_code == 0 and "Revoked 'a'." in res.output
    patched.revoke.return_value = False
    res = runner.invoke(app, ["orchestrate", "revoke", "a"])
    assert res.exit_code == 0 and "'a' had no grant." in res.output


def test_show_without_grant(patched):
    res = runner.invoke(app, ["orchestrate", "show", "a"])
    assert res.exit_code == 0 and "No grant on 'a'." in res.output


def test_show_malformed_exits_1(patched):
    patched.show.return_value = GrantRow("a", "myrepo-a", None, "invalid grant label: x", 0, None)
    res = runner.invoke(app, ["orchestrate", "show", "a"])
    assert res.exit_code == 1 and "invalid grant label" in res.output


def test_show_repo_forbids_orchestration(patched):
    patched.show.return_value = GrantRow("a", "myrepo-a", Grant(), None, 0, None)
    res = runner.invoke(app, ["orchestrate", "show", "a"])
    assert res.exit_code == 0, res.output
    assert "orchestration is not allowed in this repo" in res.output
    assert "No grant" not in res.output


def test_show_prints_effective_when_capped(patched):
    stored = Grant(max_running_children=5)
    patched.show.return_value = GrantRow(
        "a", "myrepo-a", stored, None, 1, Grant(max_running_children=2)
    )
    res = runner.invoke(app, ["orchestrate", "show", "a"])
    assert res.exit_code == 0, res.output
    assert "5 running children" in res.output
    assert "2 running children" in res.output
    assert "Running children: 1" in res.output


def test_ls_never_resolves_a_container(patched):
    res = runner.invoke(app, ["orchestrate", "ls"])
    assert res.exit_code == 0 and "No orchestration grants" in res.output
    patched.resolve.assert_not_called()


def test_ls_table(patched):
    patched.list_grants.return_value = [
        GrantRow("orch", "myrepo-orch", Grant(), None, 1, Grant()),
        GrantRow("bad", "myrepo-bad", None, "invalid grant label: x", 0, None),
        GrantRow("off", "myrepo-off", Grant(), None, 0, None),
    ]
    res = runner.invoke(app, ["orchestrate", "ls"])
    assert res.exit_code == 0, res.output
    for header in ("NAME", "LIMIT", "RUNNING", "OPERATIONS", "MOUNTS", "LOOSE", "EGRESS"):
        assert header in res.output
    assert "orch" in res.output
    assert "invalid" in res.output
    assert "not allowed" in res.output


def test_ls_json(patched):
    patched.list_grants.return_value = [GrantRow("orch", "myrepo-orch", Grant(), None, 1, Grant())]
    out = json.loads(runner.invoke(app, ["orchestrate", "ls", "--json"]).output)
    assert out == [
        {
            "name": "orch",
            "grant": json.loads(Grant().to_label()),
            "problem": None,
            "running_children": 1,
        }
    ]


@pytest.mark.parametrize("remote_session", [True, False])
def test_grant_passes_the_remote_policy_in_a_remote_session(patched, mocker, remote_session):
    from jailbee.config.models_remote import RemoteSSHConfig

    policy = RemoteSSHConfig(orchestration="ssh")
    from jailbee.remote_ssh.repo_scope import RemoteRepoScope

    mocker.patch("jailbee.remote_ssh.session.is_remote_session", return_value=remote_session)
    mocker.patch(
        "jailbee.remote_ssh.repo_scope.scope_for_session",
        return_value=RemoteRepoScope(frozenset()),
    )
    mocker.patch(
        "jailbee.cli._load_global",
        return_value=SimpleNamespace(remote=SimpleNamespace(ssh=policy)),
    )
    res = runner.invoke(app, ["orchestrate", "grant", "a"])
    assert res.exit_code == 0, res.output
    assert patched.grant.call_args.kwargs["remote"] is (policy if remote_session else None)
