"""Tests for the `jailbee orchestrate` command group."""

import json
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from jailbee.cli import app
from jailbee.config.models_orchestration import OrchestrationProfile
from jailbee.orchestration.commands import GrantFlags, GrantRow, SavedProfile
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
    mocker.patch("jailbee.cli._global_config_path_for_write", return_value=tmp_path / "global.yaml")
    return SimpleNamespace(
        save_profile=mocker.patch(
            "jailbee.orchestration.commands.save_profile",
            return_value=SavedProfile("reviewer", OrchestrationProfile(), False),
        ),
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


def test_profile_save_passes_names_and_force(patched, tmp_path):
    res = runner.invoke(app, ["orchestrate", "profile", "save", "a", "reviewer", "--force"])
    assert res.exit_code == 0, res.output
    args, kwargs = patched.save_profile.call_args
    assert args == (patched.cfg, patched.incus, f"{patched.cfg.container_prefix}-a", "reviewer")
    assert kwargs == {"force": True, "global_path": tmp_path / "global.yaml"}
    assert "Saved profile 'reviewer' from 'a':" in res.output


def test_profile_save_replaced_and_double_dash(patched):
    patched.save_profile.return_value = SavedProfile("reviewer", OrchestrationProfile(), True)
    res = runner.invoke(app, ["orchestrate", "profile", "save", "--force", "--", "a", "reviewer"])
    assert res.exit_code == 0, res.output
    assert "Saved profile 'reviewer' (replaced) from 'a':" in res.output


def test_profile_save_refusal_exits_1(patched):
    patched.save_profile.side_effect = GrantError("profile 'reviewer' already exists")
    res = runner.invoke(app, ["orchestrate", "profile", "save", "a", "reviewer"])
    assert res.exit_code == 1 and "already exists" in res.output


def test_profile_save_prompts_for_missing_name(patched, mocker):
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    ask = mocker.patch("jailbee.prompting._ask", side_effect=["Invalid name", "reviewer"])
    res = runner.invoke(app, ["orchestrate", "profile", "save", "a"])
    assert res.exit_code == 0, res.output
    assert ask.call_args_list == [mocker.call("Profile name", None, None)] * 2
    assert "Invalid profile name" in res.output
    assert patched.save_profile.call_args.args[3] == "reviewer"


def test_profile_save_missing_name_noninteractive_exits_2(patched, monkeypatch):
    monkeypatch.setenv("JAILBEE_NONINTERACTIVE", "1")
    res = runner.invoke(app, ["orchestrate", "profile", "save", "a"])
    assert res.exit_code == 2
    assert "missing Profile name" in res.output
    patched.save_profile.assert_not_called()


@pytest.mark.parametrize("enabled", [False, True])
def test_show_prints_publish_rights(patched, enabled):
    from jailbee.orchestration.grant import PublishRights

    grant = Grant(publish=PublishRights(issues=("comment",)) if enabled else PublishRights())
    patched.show.return_value = GrantRow("a", "myrepo-a", grant, None, 0, grant)
    res = runner.invoke(app, ["orchestrate", "show", "a"])
    assert res.exit_code == 0, res.output
    out = " ".join(res.output.split())
    assert out.count("publish: ") == 1
    assert ("publish: issues: comment" if enabled else "publish: none") in out


def test_repeatable_project_grant_flags_reach_commands(patched):
    res = runner.invoke(
        app,
        [
            "orchestrate",
            "grant",
            "a",
            "--publish-project",
            "Org/3=set,remove",
            "--publish-project",
            "org/7=none",
        ],
    )
    assert res.exit_code == 0, res.output
    assert patched.grant.call_args.args[3].publish_project == ("Org/3=set,remove", "org/7=none")


def test_publish_flags_reach_commands(patched):
    res = runner.invoke(
        app,
        [
            "orchestrate",
            "grant",
            "a",
            "--publish-issues",
            "comment,state",
            "--publish-prs",
            "--publish-rate",
            "12",
            "--publish-foreign-issues",
            "org/lib:comment",
        ],
    )
    assert res.exit_code == 0, res.output
    flags = patched.grant.call_args.args[3]
    assert flags.publish_issues == ("comment", "state")
    assert flags.publish_foreign_issues == ("org/lib:comment",)
    assert flags.publish_prs is True
    assert flags.publish_rate == 12


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


def test_grant_runner_flags_reach_flags(patched):
    from jailbee.orchestration.runner_model import Runner

    res = runner.invoke(
        app,
        [
            "orchestrate",
            "grant",
            "a",
            "--runner-agent",
            "claude-jb",
            "--runner-profile",
            "grok",
            "--runner-model",
            "sonnet",
        ],
    )
    assert res.exit_code == 0, res.output
    assert patched.grant.call_args.args[3].runner == Runner(
        agent="claude-jb", profile="grok", model="sonnet"
    )


def test_grant_runner_profile_without_agent_exits_1(patched):
    res = runner.invoke(app, ["orchestrate", "grant", "a", "--runner-profile", "grok"])
    assert res.exit_code == 1
    assert "--runner-agent" in res.output
    patched.grant.assert_not_called()


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--runner-model", "haiku"], "--runner-profile and --runner-model require --runner-agent"),
        (
            ["--clear-runner", "--runner-profile", "grok"],
            "--runner-agent and --clear-runner are mutually exclusive; give only one",
        ),
        (
            ["--runner-agent", "claude", "--runner-profile", "grok"],
            "profile is only valid with agent 'claude-jb'",
        ),
        (["--runner-agent", "bogus"], "Input should be 'claude' or 'claude-jb'"),
    ],
)
def test_grant_rejects_invalid_runner(patched, args, message):
    res = runner.invoke(app, ["orchestrate", "grant", "a", *args])
    assert res.exit_code == 1
    assert message in " ".join(res.output.split())
    patched.grant.assert_not_called()


def test_grant_profile_reports_cleared_runner(patched, mocker):
    from jailbee.orchestration.runner_model import Runner

    def grant_with_previous(*args, on_previous, **kwargs):
        on_previous(Grant(default_runner=Runner(agent="claude")))
        return Grant()

    patched.grant.side_effect = grant_with_previous
    read = mocker.patch("jailbee.orchestration.commands.read_grant")
    res = runner.invoke(app, ["orchestrate", "grant", "a", "--profile", "bugfixer"])
    assert res.exit_code == 0, res.output
    assert "container default runner cleared" in " ".join(res.output.split()).lower()
    read.assert_not_called()


def test_grant_profile_without_previous_runner_does_not_report_clearing(patched):
    def grant_with_previous(*args, on_previous, **kwargs):
        on_previous(Grant())
        return Grant()

    patched.grant.side_effect = grant_with_previous
    res = runner.invoke(app, ["orchestrate", "grant", "a", "--profile", "bugfixer"])
    assert res.exit_code == 0, res.output
    assert "cleared" not in res.output.lower()


def test_grant_clear_runner_passes_flag(patched):
    res = runner.invoke(app, ["orchestrate", "grant", "a", "--clear-runner"])
    assert res.exit_code == 0, res.output
    assert patched.grant.call_args.args[3].clear_runner is True


def test_grant_profile_with_explicit_runner_does_not_report_clearing(patched):
    from jailbee.orchestration.runner_model import Runner

    selected = Runner(agent="claude-jb", profile="grok", model="sonnet")

    def grant_with_previous(*args, on_previous, **kwargs):
        on_previous(Grant(default_runner=Runner(agent="claude")))
        return Grant(default_runner=selected)

    patched.grant.side_effect = grant_with_previous
    res = runner.invoke(
        app,
        [
            "orchestrate",
            "grant",
            "a",
            "--profile",
            "bugfixer",
            "--runner-agent",
            "claude-jb",
            "--runner-profile",
            "grok",
            "--runner-model",
            "sonnet",
        ],
    )
    assert res.exit_code == 0, res.output
    assert patched.grant.call_args.args[3].runner == selected
    assert "cleared" not in res.output.lower()


def test_grant_warns_on_unservable_default(patched, mocker):
    from jailbee.orchestration.runner_model import Runner

    patched.grant.return_value = Grant(default_runner=Runner(agent="claude"))
    mocker.patch("jailbee.orchestration.runner.runner_problems", return_value=["not available"])
    mocker.patch("jailbee.cli._load_global")
    res = runner.invoke(app, ["orchestrate", "grant", "a"])
    assert res.exit_code == 0, res.output
    assert "not available" in res.output


def test_show_prints_default_runner_source(patched):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    patched.cfg._orchestration_view = OrchestrationView(
        default_runner=Runner(agent="claude", model="haiku"), default_runner_source="repo"
    )
    patched.show.return_value = GrantRow("a", "myrepo-a", Grant(), None, 0, Grant())
    res = runner.invoke(app, ["orchestrate", "show", "a"])
    assert res.exit_code == 0, res.output
    assert "Default runner: claude haiku (from repo)" in res.output


def test_ls_json_has_default_runner(patched):
    from jailbee.orchestration.runner_model import Runner

    grant = Grant(default_runner=Runner(agent="claude", model="haiku"))
    patched.list_grants.return_value = [GrantRow("a", "myrepo-a", grant, None, 0, grant)]
    res = runner.invoke(app, ["orchestrate", "ls", "--json"])
    assert res.exit_code == 0, res.output
    row = json.loads(res.output)[0]
    assert row["default_runner"] == {"agent": "claude", "model": "haiku"}
    assert row["default_runner_source"] == "container"


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
    operation_cells = [
        line.split("│")[3] for line in res.output.splitlines() if line.count("│") == 8
    ]
    assert "orchestration is not allowed in this repo" in " ".join(
        " ".join(operation_cells).split()
    )
    assert "RUNNER" in res.output


def test_ls_malformed_grant_has_unknown_runner(patched):
    from jailbee.config.models_orchestration import OrchestrationView
    from jailbee.orchestration.runner_model import Runner

    patched.cfg._orchestration_view = OrchestrationView(
        default_runner=Runner(agent="claude", model="haiku"), default_runner_source="repo"
    )
    patched.list_grants.return_value = [GrantRow("bad", "myrepo-bad", None, "invalid", 0, None)]
    result = runner.invoke(app, ["orchestrate", "ls", "--json"])
    assert result.exit_code == 0, result.output
    row = json.loads(result.output)[0]
    assert row["default_runner"] is None
    assert row["default_runner_source"] == "unknown"
    result = runner.invoke(app, ["orchestrate", "ls"])
    assert result.exit_code == 0, result.output
    assert "unknown" in result.output
    assert "haiku" not in result.output


def test_ls_json(patched):
    patched.list_grants.return_value = [GrantRow("orch", "myrepo-orch", Grant(), None, 1, Grant())]
    out = json.loads(runner.invoke(app, ["orchestrate", "ls", "--json"]).output)
    assert out == [
        {
            "name": "orch",
            "grant": json.loads(Grant().to_label()),
            "problem": None,
            "running_children": 1,
            "default_runner": None,
            "default_runner_source": None,
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


def test_adopt_and_release_cli(patched, mocker):
    from jailbee.orchestration.adoption import AdoptResult, ReleaseResult

    prefix = patched.cfg.container_prefix
    patched.resolve.side_effect = lambda cfg, name: (patched.incus, f"{prefix}-{name}")
    adopt = mocker.patch(
        "jailbee.orchestration.adoption.adopt",
        return_value=AdoptResult((f"{prefix}-a", f"{prefix}-b"), (), ()),
    )
    release = mocker.patch(
        "jailbee.orchestration.adoption.release",
        return_value=ReleaseResult(f"{prefix}-o", ("mounts data",), ()),
    )
    result = runner.invoke(app, ["orchestrate", "adopt", "o", "a", "b"])
    assert result.exit_code == 0, result.output
    adopt.assert_called_once_with(
        patched.cfg, patched.incus, f"{prefix}-o", [f"{prefix}-a", f"{prefix}-b"]
    )
    assert "Adopted 'a'" in result.output
    assert "Adopted 'b'" in result.output
    result = runner.invoke(app, ["orchestrate", "release", "a"])
    assert result.exit_code == 0, result.output
    release.assert_called_once_with(patched.cfg, patched.incus, f"{prefix}-a")
    assert "Released 'a' from 'o'" in result.output
    assert "revoked: mounts data" in result.output


def test_adopt_pickers_and_already_warning(patched, mocker):
    from jailbee.orchestration.adoption import AdoptResult

    prefix = patched.cfg.container_prefix
    mocker.patch("jailbee.incus.Incus", return_value=patched.incus)
    patched.list_grants.return_value = [
        GrantRow("o", f"{prefix}-o", Grant(), None, 0, None),
        GrantRow("p", f"{prefix}-p", Grant(), None, 0, None),
    ]
    mocker.patch(
        "jailbee.orchestration.adoption.candidates", return_value=[f"{prefix}-a", f"{prefix}-b"]
    )
    adopt = mocker.patch(
        "jailbee.orchestration.adoption.adopt",
        return_value=AdoptResult((), (f"{prefix}-a",), ("notify failed",)),
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._select", side_effect=[f"{prefix}-o", f"{prefix}-a"])
    result = runner.invoke(app, ["orchestrate", "adopt"])
    assert result.exit_code == 0, result.output
    adopt.assert_called_once_with(patched.cfg, patched.incus, f"{prefix}-o", [f"{prefix}-a"])
    assert "'a' is already a child of 'o'" in result.output
    assert "notify failed" in result.output


def test_adopt_no_children_noninteractive(patched, mocker):
    prefix = patched.cfg.container_prefix
    mocker.patch(
        "jailbee.orchestration.adoption.candidates", return_value=[f"{prefix}-a", f"{prefix}-b"]
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["orchestrate", "adopt", "o"])
    assert result.exit_code == 2
    assert "a" in result.output and "b" in result.output


def test_release_picker(patched, mocker):
    from jailbee.orchestration.adoption import ReleaseResult

    prefix = patched.cfg.container_prefix
    mocker.patch("jailbee.incus.Incus", return_value=patched.incus)
    mocker.patch(
        "jailbee.orchestration.adoption.children_of_repo",
        return_value=[f"{prefix}-a", f"{prefix}-b"],
    )
    release = mocker.patch(
        "jailbee.orchestration.adoption.release",
        return_value=ReleaseResult(f"{prefix}-o", (), ("warning",)),
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    mocker.patch("jailbee.prompting._select", return_value=f"{prefix}-a")
    result = runner.invoke(app, ["orchestrate", "release"])
    assert result.exit_code == 0, result.output
    release.assert_called_once_with(patched.cfg, patched.incus, f"{prefix}-a")
    assert "warning" in result.output


def test_release_single_candidate_noninteractive_refuses(patched, mocker):
    prefix = patched.cfg.container_prefix
    mocker.patch("jailbee.incus.Incus", return_value=patched.incus)
    mocker.patch("jailbee.orchestration.adoption.children_of_repo", return_value=[f"{prefix}-a"])
    release = mocker.patch("jailbee.orchestration.adoption.release")
    mocker.patch("jailbee.prompting.is_interactive", return_value=False)
    result = runner.invoke(app, ["orchestrate", "release"])
    assert result.exit_code == 2, result.output
    assert "a" in result.output
    release.assert_not_called()


def test_release_single_candidate_interactive_uses_picker(patched, mocker):
    from jailbee.orchestration.adoption import ReleaseResult

    prefix = patched.cfg.container_prefix
    mocker.patch("jailbee.incus.Incus", return_value=patched.incus)
    mocker.patch("jailbee.orchestration.adoption.children_of_repo", return_value=[f"{prefix}-a"])
    release = mocker.patch(
        "jailbee.orchestration.adoption.release",
        return_value=ReleaseResult(f"{prefix}-o", (), ()),
    )
    mocker.patch("jailbee.prompting.is_interactive", return_value=True)
    select = mocker.patch("jailbee.prompting._select", return_value=f"{prefix}-a")
    result = runner.invoke(app, ["orchestrate", "release"])
    assert result.exit_code == 0, result.output
    select.assert_called_once()
    release.assert_called_once_with(patched.cfg, patched.incus, f"{prefix}-a")
    assert "Released 'a' from 'o'" in result.output


@pytest.mark.parametrize("command,args", [("adopt", ["o", "a"]), ("release", ["a"])])
@pytest.mark.parametrize("exception", [ValueError("autostart is running"), OSError("io failure")])
def test_adoption_errors(patched, mocker, command, args, exception):
    mocker.patch(f"jailbee.orchestration.adoption.{command}", side_effect=exception)
    result = runner.invoke(app, ["orchestrate", command, *args])
    assert result.exit_code == 1
    assert str(exception) in result.output


def _store(tmp_path, mocker, prefix="myrepo"):
    from jailbee.orchestration.store import BrokerStore, store_path

    mocker.patch("jailbee.orchestration.state.state_root", return_value=tmp_path)
    st = BrokerStore(store_path(tmp_path))
    m = {"type": "message", "kind": "note", "ref": "r", "from": "bob"}
    st.insert_item("u1", f"{prefix}-a", {**m, "id": "m1"}, now=100.0)
    st.insert_item("u1", f"{prefix}-a", {**m, "id": "m2"}, now=110.0)
    st.ack("u1", "m2", now=200.0)


def test_messages_table_and_never_prompts(patched, tmp_path, mocker):
    _store(tmp_path, mocker)
    res = runner.invoke(app, ["orchestrate", "messages"])
    assert res.exit_code == 0, res.output
    assert "CONTAINER" in res.output and "m1" in res.output and "m2" not in res.output
    patched.resolve.assert_not_called()


def test_messages_all_json_filters_container(patched, tmp_path, mocker):
    _store(tmp_path, mocker, patched.cfg.container_prefix)
    mocker.patch(
        "jailbee.lifecycle.resolve_container_name",
        return_value=f"{patched.cfg.container_prefix}-a",
    )
    mocker.patch("jailbee.incus.Incus")
    res = runner.invoke(app, ["orchestrate", "messages", "a", "--all", "--json"])
    assert res.exit_code == 0, res.output
    rows = json.loads(res.stdout)
    assert [r["id"] for r in rows] == ["m1", "m2"]
    assert rows[0]["container"] == "a" and rows[1]["acked_at"] is not None


def test_messages_missing_store_notes_on_stderr(patched, tmp_path, mocker):
    mocker.patch("jailbee.orchestration.state.state_root", return_value=tmp_path)
    res = runner.invoke(app, ["orchestrate", "messages", "--json"])
    assert res.exit_code == 0
    assert "no broker message store yet" in res.output
    assert not (tmp_path / "broker.db").exists()
